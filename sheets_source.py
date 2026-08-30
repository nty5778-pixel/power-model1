"""구글 시트에서 학습 데이터를 읽어 기존 CSV 와 같은 모양으로 떨어뜨린다.

왜 필요한가
  `data/` 의 CSV 는 사람이 갱신해야 해서 2026-06-24 에서 멈춰 있었다(실측 67일 방치).
  모델이 '최근 시장' 이라고 믿는 값이 두 달 전 것이었고, 그 결과 M2 가 8월 DA 를
  $84 로 예측했다(과거 8월 실적 평균 $37.5). 시트는 매일 갱신되므로 여기서 읽으면
  이 문제가 구조적으로 사라진다.

설계
  * 기존 로더를 건드리지 않는다. 시트를 읽어 **CSV 와 똑같은 컬럼명**으로 임시 파일에
    떨어뜨리고, 그 경로를 파일 목록에 끼워 넣기만 한다. NEEDLES 매칭·중복제거·
    winsorize 등 검증된 경로를 그대로 탄다.
  * 시트가 2026-01-01 부터라 2024~2025 는 계속 CSV 가 담당한다. `load_history` 가
    타임스탬프 기준으로 합치고 `keep='last'` 하므로, 겹치는 구간은 시트가 이긴다.
  * 시트를 못 읽으면 예외를 삼키고 None 을 돌려준다 — CSV 만으로도 서버는 떠야 한다.
    대신 무엇이 실패했는지 로그로 크게 남긴다(조용한 실패 금지).

실측 대조 (2026-01-01~01-07 겹치는 154시간)
  Historical data 탭의 10개 컬럼은 CSV 와 **최대차 0.000** 로 완전 일치.
  demand 탭의 Demand 는 CSV 부하예보와 상관 0.99955 · 절대오차 81MW
  (예보 순부하의 0.25%, 순부하예보오차의 3.9% — 모델 결과를 바꿀 수준이 아니다).
"""
import io
import json
import os
import sys

import pandas as pd

# ---------------------------------------------------------------- 설정
DB_SHEET_ID = os.environ.get("SHEET_DB_ID", "1g-yuKuUhSd3nU7eDiLWFgxOcbuFkBWmWH0wZvGg6B9I")
WX_SHEET_ID = os.environ.get("SHEET_WEATHER_ID", "1_K__Dyw5PJwXRweY38rvumWjyLx_2zyJ5kRWFh9pYXs")
GID_HIST = int(os.environ.get("SHEET_GID_HIST", 2119869267))   # Historical data
GID_DEMAND = int(os.environ.get("SHEET_GID_DEMAND", 652364015))  # demand (부하·풍력·태양광 예보)
GID_GAS = int(os.environ.get("SHEET_GID_GAS", 969659245))     # GD Katy

# 시트 컬럼 → CSV 의 긴 컬럼명. NEEDLES 가 부분매칭으로 찾는 이름이라 그대로 맞춘다.
HIST_MAP = {
    "DAM":     "ERCOT - Price - Load Zone: LZ_HOUSTON - ISO: Actual - DA LMP - Latest (Now)",
    "RT_LZ":   "ERCOT - Price - Load Zone: LZ_HOUSTON - ISO: Actual - RT SPP (15 min) - Latest (Now)",
    "RT_HB":   "ERCOT - Price - Hub: HB_BUSAVG - ISO: Actual - RT SPP (15 min) - Latest (Now)",
    "Load":    "ERCOT - Load - System Wide - ISO: Actual - Load - Latest (Now)",
    "NetLoad": "ERCOT - Load - System Wide - ENV: Actual - Net Load - Latest (Now)",
    "Temp":    "ERCOT - Load - System Wide - ENV: Actual - Temperature - Latest (Now)",
    "Solar":   "ERCOT - Generation - Solar - System Wide - ISO: Actual - Generation - Latest (Now)",
    "Solar_f": "ERCOT - Generation - Solar - System Wide - ISO: Forecast - Generation (STPF) - Prior Day (Rolling)",
    "Wind":    "ERCOT - Generation - Wind - System Wide - ISO: Actual - Generation (5 min) - Latest (Now)",
    "Wind_f":  "ERCOT - Generation - Wind - System Wide - ISO: Forecast - Generation (STPF) - Prior Day (Rolling)",
    "PRC":     "ERCOT - Grid Conditions - System Wide - ISO: Actual - PRC - Latest (Now)",
}
# demand 탭에서 가져오는 건 부하 예보 하나뿐. 풍력·태양광 예보는 Historical 탭에 이미 있고
# 그쪽이 CSV 와 완전히 일치하므로 굳이 두 곳에서 가져오지 않는다.
FC_LOAD_COL = "ERCOT - Load - System Wide - ISO: Forecast - Load - Prior Day (Rolling)"


def _log(msg):
    print(f"[sheets] {msg}", file=sys.stderr, flush=True)


# ---------------------------------------------------------------- 접속
def _client():
    """서비스 계정으로 시트에 붙는다. 키가 없으면 None (= 시트 기능 끔)."""
    raw = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip()
    if not raw:
        return None
    try:
        import gspread
        from google.oauth2.service_account import Credentials
    except ImportError as e:
        _log(f"라이브러리 없음({e}) — requirements.txt 에 gspread/google-auth 필요")
        return None
    try:
        info = json.loads(raw)
    except json.JSONDecodeError:
        # 파일 경로를 넣은 경우도 받아준다
        if os.path.exists(raw):
            info = json.load(io.open(raw, encoding="utf-8"))
        else:
            _log("GOOGLE_SERVICE_ACCOUNT_JSON 이 JSON 도 파일경로도 아니다")
            return None
    creds = Credentials.from_service_account_info(
        info, scopes=["https://www.googleapis.com/auth/spreadsheets.readonly"])
    return gspread.authorize(creds)


def _grid(ws):
    """워크시트 → DataFrame. 첫 행을 헤더로 쓰고 빈 컬럼은 버린다."""
    vals = ws.get_all_values()
    if len(vals) < 2:
        return pd.DataFrame()
    hdr = [h.strip() for h in vals[0]]
    keep = [i for i, h in enumerate(hdr) if h]
    df = pd.DataFrame([[r[i] if i < len(r) else "" for i in keep] for r in vals[1:]],
                      columns=[hdr[i] for i in keep])
    return df.replace("", pd.NA)


def _ts(s):
    """타임스탬프 정규화. CSV 와 같은 규칙으로 tz 오프셋을 떼고 wall-clock 으로 읽는다
    (DST 때문에 파일마다 -06:00/-05:00 이 섞여 있어 UTC 변환하면 시간대별 분석이 어긋난다)."""
    return pd.to_datetime(s.astype(str).str.replace(r"[+-]\d{2}:\d{2}$", "", regex=True),
                          errors="coerce")


# ---------------------------------------------------------------- ERCOT
def fetch_ercot(gc):
    """Historical data 탭(실적 10종) + demand 탭(부하 예보) → CSV 모양 DataFrame."""
    sh = gc.open_by_key(DB_SHEET_ID)
    hist = _grid(sh.get_worksheet_by_id(GID_HIST))
    if hist.empty or "Timestamp" not in hist.columns:
        _log("Historical data 탭이 비었거나 Timestamp 컬럼이 없다")
        return None

    out = pd.DataFrame({"Timestamp": hist["Timestamp"]})
    out["_ts"] = _ts(hist["Timestamp"])
    missing = []
    for src, dst in HIST_MAP.items():
        if src in hist.columns:
            out[dst] = pd.to_numeric(hist[src], errors="coerce")
        else:
            missing.append(src)
    if missing:
        _log(f"Historical 탭에 없는 컬럼: {missing}")

    # 부하 예보: demand 탭. 시각으로 붙인다.
    dem = _grid(sh.get_worksheet_by_id(GID_DEMAND))
    dcol = next((c for c in ("DateTime", "Timestamp", "Date") if c in dem.columns), None)
    vcol = next((c for c in ("Demand", "Load", "Load_f") if c in dem.columns), None)
    if dem.empty or not dcol or not vcol:
        _log(f"demand 탭에서 시각/부하 컬럼을 못 찾음 (있는 컬럼: {list(dem.columns)[:8]})")
    else:
        d = pd.DataFrame({"_ts": _ts(dem[dcol]),
                          FC_LOAD_COL: pd.to_numeric(dem[vcol], errors="coerce")})
        d = d.dropna(subset=["_ts"]).drop_duplicates("_ts", keep="last")
        out = out.merge(d, on="_ts", how="left")
        n = out[FC_LOAD_COL].notna().sum()
        _log(f"부하 예보 {n:,}/{len(out):,} 시간 결합 ({vcol} @ demand 탭)")

    out = out.dropna(subset=["_ts"]).sort_values("_ts").drop(columns=["_ts"])
    return out


# ---------------------------------------------------------------- 가스
def fetch_gas(gc):
    """GD Katy 탭 → (date, gas) 2열. 못 읽으면 None — 가스는 M2 만 쓰고
    현재 배분 규칙(m1_only)에는 관여하지 않으므로 없어도 서버는 돈다."""
    try:
        ws = gc.open_by_key(DB_SHEET_ID).get_worksheet_by_id(GID_GAS)
        df = _grid(ws)
    except Exception as e:
        _log(f"GD Katy 탭 읽기 실패: {e}")
        return None
    if df.empty:
        return None
    dcol = next((c for c in df.columns if "date" in c.lower()), None)
    # KATY 가 붙은 가격 컬럼 우선, 없으면 Close
    vcol = (next((c for c in df.columns if "katy" in c.lower()), None)
            or next((c for c in df.columns if c.lower() in ("close", "price")), None))
    if not dcol or not vcol:
        _log(f"GD Katy 탭에서 날짜/가격 컬럼을 못 찾음 (있는 컬럼: {list(df.columns)[:8]})")
        return None
    g = pd.DataFrame({"date": pd.to_datetime(df[dcol], errors="coerce"),
                      "gas": pd.to_numeric(df[vcol], errors="coerce")})
    g = g.dropna(subset=["date"]).drop_duplicates("date", keep="last").sort_values("date")
    _log(f"가스 {len(g):,}일 ({dcol} / {vcol})")
    return g


# ---------------------------------------------------------------- 날씨
def fetch_weather(gc):
    """기온 탭 + 30년 평년값 탭 → date/temp/normal 컬럼.

    평년값이 붙으면 모델이 자체 산출(2.5년치) 대신 이걸 쓴다. 시트 실측에서
    8월 기온편차가 +2.7~+8.8F 로 한쪽에 쏠려 있었는데(과거 8월 실적 -4.6~+4.1F),
    30년 평년으로 바꾸면 그 계통 오차가 해소된다.
    """
    try:
        sh = gc.open_by_key(WX_SHEET_ID)
        tabs = {ws.title: _grid(ws) for ws in sh.worksheets()}
    except Exception as e:
        _log(f"날씨 시트 읽기 실패: {e}")
        return None

    obs = next((d for t, d in tabs.items()
                if not d.empty and "temp_mean_f" in d.columns and "date" in d.columns), None)
    if obs is None:
        _log(f"기온 탭을 못 찾음 (탭: {list(tabs)})")
        return None
    w = pd.DataFrame({"date": pd.to_datetime(obs["date"], errors="coerce")})
    for c in ("temp_mean_f", "temp_max_f"):
        if c in obs.columns:
            w[c] = pd.to_numeric(obs[c], errors="coerce")
    w = w.dropna(subset=["date"]).drop_duplicates("date", keep="last")
    # 기존 CSV 와 같은 region 값을 달아준다. 이게 없으면 (date, region) 중복제거에서
    # 시트 행과 CSV 행이 서로 다른 것으로 취급돼 같은 날이 두 번 남는다.
    w["region"] = "Texas 4-city average"

    nrm = next((d for t, d in tabs.items()
                if not d.empty and "normal_temp_mean_f" in d.columns), None)
    if nrm is None:
        _log("30년 평년값 탭 없음 — 자체 산출 평년을 계속 쓴다")
    else:
        dcol = next((c for c in nrm.columns if "date" in c.lower()), None)
        n = pd.DataFrame({"_d": pd.to_datetime(nrm[dcol], errors="coerce")})
        for c in ("normal_temp_mean_f", "normal_temp_max_f"):
            if c in nrm.columns:
                n[c] = pd.to_numeric(nrm[c], errors="coerce")
        n = n.dropna(subset=["_d"])
        n["_md"] = n._d.dt.strftime("%m-%d")
        n = n.drop(columns=["_d"]).drop_duplicates("_md", keep="last")
        w["_md"] = w.date.dt.strftime("%m-%d")
        w = w.merge(n, on="_md", how="left").drop(columns=["_md"])
        cov = w["normal_temp_mean_f"].notna().sum() if "normal_temp_mean_f" in w.columns else 0
        doys = w.loc[w.get("normal_temp_mean_f", pd.Series(dtype=float)).notna(), "date"]                 .dt.strftime("%m-%d").nunique() if cov else 0
        _log(f"30년 평년값 적용: {cov:,}일 / 달력일 {doys}종")
        if doys < 360:
            _log(f"  주의: 달력일 {doys}종만 평년이 붙었다. 나머지 날짜는 자체산출 평년을 쓴다"
                 f" — 기준이 섞이므로 기온 시트의 관측 기간을 1년 이상으로 늘리는 게 좋다")
    _log(f"기온 {len(w):,}일 ({w.date.min().date()} ~ {w.date.max().date()})")
    return w.sort_values("date")


# ---------------------------------------------------------------- 진입점
def materialize(outdir):
    """시트를 읽어 CSV 로 떨어뜨리고 (ercot경로, 가스경로, 날씨경로) 를 돌려준다.
    실패한 항목은 None. 전부 실패하면 (None, None, None) 이고 호출측은 CSV 만 쓴다."""
    gc = _client()
    if gc is None:
        return None, None, None
    os.makedirs(outdir, exist_ok=True)
    ep = gp = wp = None
    try:
        e = fetch_ercot(gc)
        if e is not None and len(e):
            ep = os.path.join(outdir, "sheet_ercot.csv")
            e.to_csv(ep, index=False)
            ts = _ts(e["Timestamp"])
            _log(f"ERCOT {len(e):,}행 ({ts.min().date()} ~ {ts.max().date()}) → {ep}")
    except Exception as ex:
        _log(f"ERCOT 시트 실패: {ex}")
    try:
        g = fetch_gas(gc)
        if g is not None and len(g):
            gp = os.path.join(outdir, "sheet_gas_katy.csv")
            # 가스 로더가 skiprows=1 을 전제하므로 헤더 한 줄을 얹는다
            with io.open(gp, "w", encoding="utf-8", newline="\n") as f:
                f.write("*,Platts Katy FDt Com\n")
                g.to_csv(f, index=False, header=["Date", "Close"])
    except Exception as ex:
        _log(f"가스 시트 실패: {ex}")
    try:
        w = fetch_weather(gc)
        if w is not None and len(w):
            wp = os.path.join(outdir, "sheet_weather.csv")
            w.to_csv(wp, index=False)
    except Exception as ex:
        _log(f"날씨 시트 실패: {ex}")
    return ep, gp, wp
