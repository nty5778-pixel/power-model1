"""구글 시트에서 학습 데이터를 읽어 기존 CSV 와 같은 모양으로 떨어뜨린다.

왜 필요한가
  `data/` 의 CSV 는 사람이 갱신해야 해서 2026-06-24 에서 멈춰 있었다(실측 67일 방치).
  모델이 '최근 시장' 이라고 믿는 값이 두 달 전 것이었고, 그 결과 M2 가 8월 DA 를
  $84 로 예측했다(과거 8월 실적 평균 $37.5). 시트는 매일 갱신되므로 여기서 읽으면
  이 문제가 구조적으로 사라진다.

읽는 방법 두 가지 — 환경변수로 결정된다
  1) **웹 게시 CSV** (권장, 구글 클라우드 콘솔 불필요)
     시트에서 파일 > 공유 > 웹에 게시 > 탭 선택 > CSV. 나오는 주소를 아래 환경변수에 넣는다.
       SHEET_CSV_HIST    Historical data 탭
       SHEET_CSV_DEMAND  demand 탭 (부하 예보)
       SHEET_CSV_GAS     GD Katy 탭 (선택)
       SHEET_CSV_WX      기온 탭
       SHEET_CSV_WXNORM  30년 평년값 탭 (선택)
     탭 단위로만 게시되므로 같은 문서의 다른 탭(예: 유료 리서치)은 계속 비공개다.
     주소는 추측 불가능한 토큰이고 언제든 게시를 중단할 수 있다.
     ※ 게시본은 구글이 몇 분 캐시한다. 하루 한 번 도는 작업엔 문제없다.
  2) **서비스 계정** — GOOGLE_SERVICE_ACCOUNT_JSON 에 키 JSON 을 넣으면 이쪽을 쓴다.
     시트를 공개하지 않아도 되지만 구글 클라우드 콘솔에서 계정을 만들어야 한다.
  둘 다 없으면 시트 기능이 꺼지고 `data/` CSV 만 쓴다.

설계
  * 기존 로더를 건드리지 않는다. 시트를 읽어 **CSV 와 똑같은 컬럼명**으로 임시 파일에
    떨어뜨리고, 그 경로를 파일 목록에 끼워 넣기만 한다. NEEDLES 매칭·중복제거·
    winsorize 등 검증된 경로를 그대로 탄다.
  * 시트가 2026-01-01 부터라 2024~2025 는 계속 CSV 가 담당한다. `load_history` 가
    타임스탬프로 합치면서 컬럼별 마지막 유효값을 취하므로 겹치는 구간은 시트가 이긴다.
  * 시트를 못 읽으면 None 을 돌려준다 — CSV 만으로도 서버는 떠야 한다.
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

# 서비스 계정 모드에서 쓰는 탭 위치 (문서ID, gid)
API_TABS = {
    "hist":   (DB_SHEET_ID, int(os.environ.get("SHEET_GID_HIST", 2119869267))),
    "demand": (DB_SHEET_ID, int(os.environ.get("SHEET_GID_DEMAND", 652364015))),
    "gas":    (DB_SHEET_ID, int(os.environ.get("SHEET_GID_GAS", 969659245))),
}
# 웹 게시 모드에서 쓰는 환경변수 이름
URL_ENV = {"hist": "SHEET_CSV_HIST", "demand": "SHEET_CSV_DEMAND", "gas": "SHEET_CSV_GAS",
           "wx": "SHEET_CSV_WX", "wxnorm": "SHEET_CSV_WXNORM"}

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
    # n8n_3 이 만드는 탭에는 부하 예보도 같이 들어온다. 그러면 demand 탭 없이도 자급된다.
    "Load_f":  "ERCOT - Load - System Wide - ISO: Forecast - Load - Prior Day (Rolling)",
}
# demand 탭에서 가져오는 건 부하 예보 하나뿐. 풍력·태양광 예보는 Historical 탭에 이미 있고
# 그쪽이 CSV 와 완전히 일치하므로 굳이 두 곳에서 가져오지 않는다.
FC_LOAD_COL = "ERCOT - Load - System Wide - ISO: Forecast - Load - Prior Day (Rolling)"


def _log(msg):
    print(f"[sheets] {msg}", file=sys.stderr, flush=True)


# ---------------------------------------------------------------- 읽기 방식
def _csv_url(u):
    """웹 게시 주소를 CSV 형식으로 맞춘다.

    '웹에 게시' 화면에서 형식 드롭다운을 '웹페이지' 인 채로 두기 쉽다. 그러면 주소가
    .../pubhtml?gid=..&single=true 가 되고, 받아보면 표가 아니라 HTML 페이지가 온다.
    실측으로 다섯 탭 전부 이 상태였고(Content-Type: text/html), 조용히 빈 표가 됐다.
    사람에게 다시 게시하라고 하는 대신 여기서 형식만 바꿔 준다.
    """
    u = (u or "").strip()
    if not u or "output=csv" in u:
        return u
    u = u.replace("/pubhtml", "/pub")
    return u + ("&" if "?" in u else "?") + "output=csv"


def _read_url(url):
    """웹 게시된 탭을 CSV 로 받는다. 인증 없음."""
    if not url:
        return None
    url = _csv_url(url)
    df = pd.read_csv(url, dtype=str, keep_default_na=False)
    df.columns = [str(c).strip() for c in df.columns]
    return df.replace("", pd.NA)


def _sa_client():
    """서비스 계정 클라이언트. 키가 없거나 라이브러리가 없으면 None."""
    raw = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip()
    if not raw:
        return None
    try:
        import gspread
        from google.oauth2.service_account import Credentials
    except ImportError as e:
        _log(f"서비스 계정 라이브러리 없음({e}) — requirements.txt 의 gspread/google-auth 확인")
        return None
    try:
        info = json.loads(raw)
    except json.JSONDecodeError:
        if os.path.exists(raw):                       # 파일 경로를 넣은 경우도 받아준다
            info = json.load(io.open(raw, encoding="utf-8"))
        else:
            _log("GOOGLE_SERVICE_ACCOUNT_JSON 이 JSON 도 파일경로도 아니다")
            return None
    creds = Credentials.from_service_account_info(
        info, scopes=["https://www.googleapis.com/auth/spreadsheets.readonly"])
    return gspread.authorize(creds)


def _grid(ws):
    """워크시트 → DataFrame. 첫 행을 헤더로 쓰고 이름 없는 컬럼은 버린다."""
    vals = ws.get_all_values()
    if len(vals) < 2:
        return pd.DataFrame()
    hdr = [h.strip() for h in vals[0]]
    keep = [i for i, h in enumerate(hdr) if h]
    df = pd.DataFrame([[r[i] if i < len(r) else "" for i in keep] for r in vals[1:]],
                      columns=[hdr[i] for i in keep])
    return df.replace("", pd.NA)


def make_reader():
    """(reader, 모드이름) 반환. reader(key) → DataFrame 또는 None."""
    urls = {k: os.environ.get(v, "").strip() for k, v in URL_ENV.items()}
    if any(urls.values()):
        def r(key):
            try:
                return _read_url(urls.get(key))
            except Exception as e:
                _log(f"{key} 웹게시 CSV 읽기 실패: {e}")
                return None
        have = [k for k, v in urls.items() if v]
        return r, f"웹 게시 CSV ({', '.join(have)})"

    gc = _sa_client()
    if gc is None:
        return None, None

    wx_cache = {}

    def r(key):
        try:
            if key in API_TABS:
                sid, gid = API_TABS[key]
                return _grid(gc.open_by_key(sid).get_worksheet_by_id(gid))
            # 날씨 시트는 gid 를 모르므로 컬럼으로 탭을 찾는다
            if not wx_cache:
                sh = gc.open_by_key(WX_SHEET_ID)
                wx_cache.update({ws.title: _grid(ws) for ws in sh.worksheets()})
            need = "temp_mean_f" if key == "wx" else "normal_temp_mean_f"
            return next((d for d in wx_cache.values()
                         if not d.empty and need in d.columns), None)
        except Exception as e:
            _log(f"{key} 시트 읽기 실패: {e}")
            return None
    return r, "서비스 계정"


def _ts(s):
    """타임스탬프 정규화. CSV 와 같은 규칙으로 tz 오프셋을 떼고 wall-clock 으로 읽는다
    (DST 때문에 파일마다 -06:00/-05:00 이 섞여 있어 UTC 변환하면 시간대별 분석이 어긋난다)."""
    return pd.to_datetime(s.astype(str).str.replace(r"[+-]\d{2}:\d{2}$", "", regex=True),
                          errors="coerce")


# ---------------------------------------------------------------- ERCOT
def fetch_ercot(read):
    """Historical data 탭(실적 10종) + demand 탭(부하 예보) → CSV 모양 DataFrame."""
    hist = read("hist")
    if hist is None or hist.empty or "Timestamp" not in hist.columns:
        _log("Historical data 를 못 읽었거나 Timestamp 컬럼이 없다")
        return None

    out = pd.DataFrame({"Timestamp": hist["Timestamp"]})
    out["_ts"] = _ts(hist["Timestamp"])
    missing = [s for s in HIST_MAP if s not in hist.columns]
    for src, dst in HIST_MAP.items():
        if src in hist.columns:
            out[dst] = pd.to_numeric(hist[src], errors="coerce")
    if missing:
        _log(f"Historical 탭에 없는 컬럼: {missing}")

    dem = read("demand")
    dcol = vcol = None
    if dem is not None and not dem.empty:
        dcol = next((c for c in ("DateTime", "Timestamp", "Date") if c in dem.columns), None)
        vcol = next((c for c in ("Demand", "Load", "Load_f") if c in dem.columns), None)
    if not dcol or not vcol:
        cols = list(dem.columns)[:8] if dem is not None else "(못 읽음)"
        _log(f"demand 탭에서 시각/부하 컬럼을 못 찾음 (있는 컬럼: {cols})")
    else:
        d = pd.DataFrame({"_ts": _ts(dem[dcol]),
                          "_fc": pd.to_numeric(dem[vcol], errors="coerce")})
        d = d.dropna(subset=["_ts"]).drop_duplicates("_ts", keep="last")
        out = out.merge(d, on="_ts", how="left")
        # hist 탭이 이미 부하 예보를 갖고 있으면 그쪽이 우선이다. demand 탭은 빈 곳만 메운다.
        # (hist 쪽은 '전일 발표분' 이라는 뜻이 분명한데, demand 탭은 어느 시점 예보인지
        #  보장되지 않는다. 덮어쓰면 학습 때와 의미가 달라진다.)
        if FC_LOAD_COL in out.columns:
            filled = int(out[FC_LOAD_COL].isna().sum())
            out[FC_LOAD_COL] = out[FC_LOAD_COL].combine_first(out["_fc"])
            _log(f"부하 예보: hist 탭 우선, demand 탭이 빈 {filled:,}칸 중 "
                 f"{int(out[FC_LOAD_COL].notna().sum()) - (len(out) - filled):,}칸을 메움")
        else:
            out[FC_LOAD_COL] = out["_fc"]
            _log(f"부하 예보 {out[FC_LOAD_COL].notna().sum():,}/{len(out):,} 시간 결합"
                 f" ({vcol} @ demand 탭)")
        out = out.drop(columns=["_fc"])

    out = out.dropna(subset=["_ts"]).sort_values("_ts").drop(columns=["_ts"])
    return out


# ---------------------------------------------------------------- 가스
def fetch_gas(read):
    """GD Katy 탭 → (date, gas). 없어도 된다 — 가스는 M2 만 쓰고 현재 배분 규칙
    (m1_only)에는 관여하지 않으므로 서버는 그대로 돈다."""
    df = read("gas")
    if df is None or df.empty:
        return None
    dcol = next((c for c in df.columns if "date" in c.lower()), None)
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
def fetch_weather(read):
    """기온 탭 + 30년 평년값 탭 → date/temp/normal.

    평년값이 붙으면 모델이 자체 산출(2.5년치) 대신 그것을 쓴다. 시트 실측에서
    8월 기온편차가 +2.7~+8.8F 로 한쪽에 쏠려 있었는데(과거 8월 실적 -4.6~+4.1F),
    30년 평년으로 바꾸면 그 계통 오차가 해소된다.
    """
    obs = read("wx")
    if obs is None or obs.empty or "temp_mean_f" not in obs.columns or "date" not in obs.columns:
        _log("기온 탭을 못 읽었거나 date/temp_mean_f 컬럼이 없다")
        return None
    w = pd.DataFrame({"date": pd.to_datetime(obs["date"], errors="coerce")})
    for c in ("temp_mean_f", "temp_max_f"):
        if c in obs.columns:
            w[c] = pd.to_numeric(obs[c], errors="coerce")
    w = w.dropna(subset=["date"]).drop_duplicates("date", keep="last")
    # 기존 CSV 와 같은 region 값을 달아준다. 없으면 (date, region) 중복제거에서
    # 시트 행과 CSV 행이 서로 다른 것으로 취급돼 같은 날이 두 번 남는다.
    w["region"] = "Texas 4-city average"

    nrm = read("wxnorm")
    if nrm is None or nrm.empty or "normal_temp_mean_f" not in nrm.columns:
        _log("30년 평년값 탭 없음 — 자체 산출 평년(2.5년치)을 계속 쓴다")
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
        cov = int(w["normal_temp_mean_f"].notna().sum()) if "normal_temp_mean_f" in w else 0
        doys = int(w.loc[w["normal_temp_mean_f"].notna(), "date"].dt.strftime("%m-%d").nunique()) if cov else 0
        _log(f"30년 평년값 적용: {cov:,}일 / 달력일 {doys}종")
        if doys < 360:
            _log(f"  주의: 달력일 {doys}종만 평년이 붙었다. 나머지 날짜는 자체산출 평년을 쓴다"
                 f" — 기준이 섞이므로 기온 탭의 관측 기간을 1년 이상으로 늘리는 게 좋다")
    _log(f"기온 {len(w):,}일 ({w.date.min().date()} ~ {w.date.max().date()})")
    return w.sort_values("date")


# ---------------------------------------------------------------- 진입점
def materialize(outdir):
    """시트를 읽어 CSV 로 떨어뜨리고 (ercot, 가스, 날씨) 경로를 돌려준다.
    실패한 항목은 None. 아무것도 못 읽으면 (None, None, None) → 호출측은 CSV 만 쓴다."""
    read, mode = make_reader()
    if read is None:
        return None, None, None
    _log(f"읽기 방식: {mode}")
    os.makedirs(outdir, exist_ok=True)
    ep = gp = wp = None
    try:
        e = fetch_ercot(read)
        if e is not None and len(e):
            ep = os.path.join(outdir, "sheet_ercot.csv")
            e.to_csv(ep, index=False)
            ts = _ts(e["Timestamp"]).dropna()
            _log(f"ERCOT {len(e):,}행 ({ts.min().date()} ~ {ts.max().date()}) → {ep}")
    except Exception as ex:
        _log(f"ERCOT 처리 실패: {ex}")
    try:
        g = fetch_gas(read)
        if g is not None and len(g):
            gp = os.path.join(outdir, "sheet_gas_katy.csv")
            with io.open(gp, "w", encoding="utf-8", newline="\n") as f:
                f.write("*,Platts Katy FDt Com\n")   # 가스 로더가 skiprows=1 을 전제한다
                g.to_csv(f, index=False, header=["Date", "Close"])
    except Exception as ex:
        _log(f"가스 처리 실패: {ex}")
    try:
        w = fetch_weather(read)
        if w is not None and len(w):
            wp = os.path.join(outdir, "sheet_weather.csv")
            w.to_csv(wp, index=False)
    except Exception as ex:
        _log(f"날씨 처리 실패: {ex}")
    return ep, gp, wp
