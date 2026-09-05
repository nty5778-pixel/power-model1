"""
Render 배포용 FastAPI 래퍼 — run_models_d1_d4_v4_weather.py 를 HTTP 로 노출.

엔드포인트
  GET  /health          헬스체크 (Render keep-alive 용)
  POST /predict         D+1~D+4 배분 산출 → JSON (n8n 이 호출)
  POST /score           예측 + 실적을 받아 look-back 지표 계산

설계 노트
  * 모델 학습(XGBoost, ~900행)은 요청당 10~30초. Render 무료 티어는 15분 idle 후
    spin-down 되어 콜드스타트가 추가된다 → n8n 타임아웃을 180초 이상으로.
  * 과거 CSV 는 레포에 함께 커밋(data/)하거나 DATA_URL 로 외부에서 받는다.
    Render 디스크는 ephemeral 이므로 런타임 생성 파일은 보존되지 않는다.
  * 학습 결과 캐시: 같은 날 두 번째 호출은 메모리 캐시 사용(콜드스타트 시 무효).
"""
import os, sys, json, math, re, tempfile, datetime as dt
import urllib.request
from typing import Optional, List, Dict, Any

import pandas as pd
from fastapi import FastAPI, HTTPException, Header
from pydantic import BaseModel

MODEL_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.environ.get("DATA_DIR", os.path.join(MODEL_DIR, "data"))
API_KEY = os.environ.get("API_KEY")  # n8n 과 공유하는 단순 인증키

sys.path.insert(0, MODEL_DIR)
import run_models_d1_d4_v4_weather as M  # noqa: E402
import sheets_source  # noqa: E402

# 시트에서 학습 데이터를 받아 임시 CSV 로 떨어뜨릴 위치. Render 는 /tmp 쓰기 가능.
SHEET_CACHE_DIR = os.environ.get("SHEET_CACHE_DIR", "/tmp/sheet_cache")

# 배분 규칙 기본값 — Render 대시보드에서 ALLOC_MODE 로 덮어쓸 수 있다.
# 기본 m1_only = 평소 RT, 모델1 점수가 문턱을 넘는 날만 DA. 근거는 M.combine_votes() 주석.
ALLOC_MODE = os.environ.get("ALLOC_MODE", M.ALLOC_DEFAULT)
M1_THRESHOLD = float(os.environ.get("M1_DA_THRESHOLD", M.M1_DA_THRESHOLD))

app = FastAPI(title="ERCOT DA/RT Allocation", version="4.0")
_cache: Dict[str, Any] = {"key": None, "models": None, "panel": None}


class ForecastRow(BaseModel):
    timestamp: str
    fc_load_mw: float
    fc_wind_mw: float
    fc_solar_mw: float


class WeatherRow(BaseModel):
    """예보일의 기온. 서버가 평년값과 비교해 t_anom 을 만든다.

    이게 없으면 과거 날씨 CSV 에 있는 날짜만 t_anom 이 채워진다. 예보 대상은 항상 '미래'라
    CSV 범위를 벗어나므로, 날씨 피처(M1)와 오버레이가 통째로 비활성화된다.
    """
    date: str                       # YYYY-MM-DD
    temp_mean_f: float
    temp_max_f: Optional[float] = None


class PredictRequest(BaseModel):
    forecast: List[ForecastRow]
    weather: Optional[List[WeatherRow]] = None
    volume_mw: float = 100.0
    use_gate: bool = False
    run_id: Optional[str] = None
    # 배분 규칙. 미지정이면 서버 기본값(환경변수 ALLOC_MODE, 없으면 모델 모듈 기본).
    # n8n 이 규칙을 바꿔가며 A/B 하고 싶을 때 요청 단위로 덮어쓸 수 있게 열어둔다.
    alloc: Optional[str] = None
    threshold: Optional[float] = None


def _auth(key: Optional[str]):
    if API_KEY and key != API_KEY:
        raise HTTPException(401, "invalid api key")


def _build_panel():
    """과거 CSV 로드 → 일별 패널 + 학습. 하루 단위 캐시."""
    today = dt.date.today().isoformat()
    if _cache["key"] == today and _cache["models"] is not None:
        return _cache["panel"], _cache["models"]

    csvs = M.list_csvs(DATA_DIR)
    if not csvs:
        raise HTTPException(500, f"no CSV found in {DATA_DIR}")
    # 'DA LMP 보유' 가 아니라 '필요 컬럼 전부 보유' 로 판별 — Congesiton_*.csv 오인식 방지
    # (오인식되면 concat 시 정상 행을 덮어써서 fc_load/ENV Net Load/PRC 가 조용히 NaN 이 된다)
    ercot_files = [f for f in csvs if M.is_ercot_history(f)[0]]
    gas = M.pick_csv(csvs, ["katy", "gas", "gd_"], exclude=set(ercot_files))
    wx_files = [c for c in csvs if c not in ercot_files]

    # 구글 시트에서 최신분을 받아 뒤에 덧붙인다. 시트는 2026-01-01 부터라
    # 2024~2025 는 계속 CSV 가 담당하고, 겹치는 구간은 뒤에 오는 시트가 이긴다
    # (load_history / _weather_frame 이 drop_duplicates(keep="last") 를 쓴다).
    # 시트를 못 읽어도 CSV 만으로 계속 돌아야 하므로 실패는 로그만 남기고 넘어간다.
    s_ercot, s_gas, s_wx = sheets_source.materialize(SHEET_CACHE_DIR)
    if s_ercot:
        ok, miss = M.is_ercot_history(s_ercot)
        if ok:
            ercot_files.append(s_ercot)
        else:
            print(f"[sheets] 시트 ERCOT 데이터에 필요 컬럼 없음 {miss} — 무시하고 CSV 만 사용",
                  file=sys.stderr, flush=True)
    if s_gas:
        gas = s_gas
    if s_wx:
        wx_files.append(s_wx)

    if not ercot_files:
        raise HTTPException(500, "no ERCOT history (CSV/시트 어디에도 필요 컬럼이 없다)")
    wx = M.load_weather(wx_files)

    mn, gas_df = M.load_history(ercot_files, gas)
    panel = M.daily_panel(mn, gas_df, wx)
    models = M.train_models(panel)
    models["_wx"] = wx
    models["_wxnorm"] = M.weather_normals(wx_files)
    _cache.update(key=today, models=models, panel=panel)
    return panel, models


@app.get("/health")
def health():
    return {"ok": True, "ts": dt.datetime.utcnow().isoformat(), "data_dir": DATA_DIR}


@app.post("/predict")
def predict(req: PredictRequest, x_api_key: Optional[str] = Header(None)):
    _auth(x_api_key)
    panel, models = _build_panel()

    with tempfile.TemporaryDirectory() as td:
        fp = os.path.join(td, "forecast_input.csv")
        pd.DataFrame([r.model_dump() for r in req.forecast]).to_csv(fp, index=False)
        fc = M.forecast_daily(pd.read_csv(fp))

    for c in ("t_anom", "t_anom_abs", "t_anom_max"):
        fc[c] = float("nan")
    if models.get("_wx") is not None:
        fc = fc.drop(columns=["t_anom", "t_anom_abs", "t_anom_max"]).merge(
            models["_wx"], on="date", how="left")

    # 요청에 기온 예보가 오면 그것으로 t_anom 을 채운다(과거 CSV 에 없는 미래 날짜용).
    # 예보 대상은 항상 미래라 이 경로가 없으면 날씨 피처가 사실상 늘 비어 있게 된다.
    norm = models.get("_wxnorm")
    if req.weather and norm is not None:
        w = pd.DataFrame([r.model_dump() for r in req.weather])
        w["date"] = pd.to_datetime(w["date"], errors="coerce").dt.normalize()
        w = w.dropna(subset=["date"]).drop_duplicates(subset=["date"], keep="last")
        doy = w.date.dt.dayofyear
        w["_anom"] = w.temp_mean_f - doy.map(norm["mean"])
        w["_anom_max"] = (w.temp_max_f - doy.map(norm["max"])
                          if norm.get("max") is not None and "temp_max_f" in w.columns
                          else float("nan"))
        # 과거 CSV 에 실측이 있으면 그쪽 우선. 요청의 기온은 '예보' 이므로 빈 곳만 채운다.
        fc = fc.merge(w[["date", "_anom", "_anom_max"]], on="date", how="left")
        fc["t_anom"] = fc["t_anom"].combine_first(fc["_anom"])
        fc["t_anom_max"] = fc["t_anom_max"].combine_first(fc["_anom_max"])
        fc["t_anom_abs"] = fc["t_anom"].abs()
        fc = fc.drop(columns=["_anom", "_anom_max"])

    n_missing_wx = int(fc["t_anom"].isna().sum())

    reg, d0_ts = M.regime_from_last(panel)      # CLI(run) 와 동일한 구성 — 두 경로가 갈리지 않게

    alloc = req.alloc or ALLOC_MODE
    if alloc not in M.ALLOC_MODES:
        raise HTTPException(400, f"alloc 은 {list(M.ALLOC_MODES)} 중 하나여야 합니다: {alloc}")
    threshold = req.threshold if req.threshold is not None else M1_THRESHOLD

    rows = M.forecast_rows(fc, models, reg, req.volume_mw, use_gate=req.use_gate,
                           alloc=alloc, threshold=threshold)
    d0 = pd.Timestamp(d0_ts).date().isoformat()
    # 학습 데이터가 얼마나 낡았는지. 이 값이 크면 모델이 '최근 상황'이라고 믿는 게
    # 사실은 몇 주 전 값이다(regime 피처: 최근 DA/basis/예비력/순부하오차).
    # 예보일과의 간격을 그대로 노출해 조용히 낡아가는 것을 막는다.
    d0_gap = int((pd.to_datetime(fc["date"]).min().normalize()
                  - pd.Timestamp(d0_ts).normalize()).days)
    return {
        "run_id": req.run_id or dt.datetime.utcnow().strftime("%Y%m%dT%H%M%SZ"),
        "generated_at": dt.datetime.utcnow().isoformat(),
        "model_version": "v4-weather",
        "alloc_mode": alloc,                    # 어떤 규칙으로 낸 배분인지 기록 (시트에 남길 것)
        "m1_threshold": threshold,
        "d0_last_actual": d0,
        "d0_gap_days": d0_gap,        # 2~3 이 정상. 수십 일이면 data/ 갱신이 밀린 것

        "regime": {"prc_low_r7": reg["prc_low_r7"], "da_med": reg["da_med"]},
        "weather_missing_days": n_missing_wx,   # >0 이면 그 날은 날씨 피처·오버레이 없이 산출됨
        "rows": rows,
    }


class ScoreRow(BaseModel):
    date: str
    DA_fraction: float
    DA_actual: float
    RT_actual: float


def _no_urls(s):
    """오류 메시지에 섞여 나오는 주소를 지운다.

    웹 게시 CSV 주소는 그 자체가 열쇠라(주소를 아는 사람은 누구나 그 탭을 본다)
    진단 응답에 그대로 실리면 안 된다.
    """
    return re.sub(r"https?://\S+", "<주소 생략>", str(s))


@app.get("/diag")
def diag(x_api_key: Optional[str] = Header(None)):
    """구글 시트가 실제로 붙었는지 밖에서 확인하기 위한 진단.

    시트 읽기는 실패해도 CSV 로 조용히 넘어가도록(서비스가 죽지 않게) 만들어 뒀는데,
    그 때문에 '왜 안 붙었는지'를 Render 로그 없이는 알 수가 없었다. 여기서 답한다.
    """
    _auth(x_api_key)
    env = {name: bool(os.environ.get(name, "").strip())
           for name in sheets_source.URL_ENV.values()}
    env["GOOGLE_SERVICE_ACCOUNT_JSON"] = bool(
        os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip())

    try:
        read, mode = sheets_source.make_reader()
    except Exception as e:
        read, mode = None, f"읽기 준비 실패: {_no_urls(e)}"

    tabs = {}
    if read is not None:
        for key, envname in sheets_source.URL_ENV.items():
            if not os.environ.get(envname, "").strip() and mode.startswith("웹"):
                tabs[key] = {"설정됨": False}
                continue
            try:
                df = read(key)
            except Exception as e:
                tabs[key] = {"설정됨": True, "읽힘": False,
                             "오류": _no_urls(e)[:200]}
                continue
            if df is None or not len(df):
                tabs[key] = {"설정됨": True, "읽힘": False,
                             "오류": "빈 표가 왔다 (게시 형식이 CSV 인지, 탭이 맞는지 확인)"}
            else:
                tabs[key] = {"설정됨": True, "읽힘": True, "행": int(len(df)),
                             "컬럼앞부분": [str(c) for c in list(df.columns)[:8]]}

    # 위에서 '빈 표' 로만 나오면 왜인지 알 수 없다. 주소를 직접 한 번 받아
    # '무엇이 돌아왔는지'를 본다 — CSV 가 아니라 HTML 이 오는 경우가 대부분이다
    # (평소 시트 주소(/edit)를 넣었거나, 게시 형식을 웹페이지로 골랐을 때).
    probe = {}
    for key, envname in sheets_source.URL_ENV.items():
        u = os.environ.get(envname, "").strip()
        if not u:
            continue
        info = {"주소형태": ("웹게시(/d/e/…pub) 맞음" if "/d/e/" in u and "pub" in u
                          else "!! 평소 시트 주소(/edit) 로 보인다"
                               if "/edit" in u else "판단 불가")}
        info["output=csv 있음"] = "output=csv" in u
        # 실제로 읽을 때와 같은 보정을 거친 주소로 확인한다
        u = sheets_source._csv_url(u)
        info["보정후 csv"] = "output=csv" in u
        try:
            req = urllib.request.Request(u, headers={"User-Agent": "power-model/1.0"})
            with urllib.request.urlopen(req, timeout=20) as r:
                head = r.read(400).decode("utf-8", "replace")
                info["응답코드"] = r.status
                info["콘텐츠형식"] = r.headers.get("Content-Type", "")
        except Exception as e:
            info["가져오기실패"] = _no_urls(e)[:150]
            probe[key] = info
            continue
        low = head.lstrip().lower()
        if low.startswith("<!doctype") or low.startswith("<html"):
            info["받은것"] = "!! HTML 페이지 (CSV 가 아니다)"
        else:
            first = head.splitlines()[0] if head.splitlines() else ""
            info["받은것"] = "CSV 로 보임"
            info["첫줄앞부분"] = first[:120]
        probe[key] = info

    panel_last = None
    if _cache.get("panel") is not None:
        try:
            panel_last = str(pd.to_datetime(_cache["panel"]["date"]).max().date())
        except Exception:
            pass

    return {
        "환경변수_설정여부": env,
        "선택된_읽기방식": mode or "없음 (CSV 만 사용)",
        "탭별_상태": tabs,
        "주소_점검": probe,
        "학습데이터_마지막날": panel_last,
        "안내": ("환경변수를 넣었는데 '선택된_읽기방식'이 '없음'이면 Render 가 아직 "
                 "재배포되지 않았거나 이름이 다르다. 이름은 위 목록과 정확히 같아야 한다."),
    }


def _r(v, nd=3):
    """숫자를 반올림하되 NaN/무한대는 None 으로 바꾼다.

    JSON 은 NaN 을 표현할 수 없어서, 하나라도 섞이면 응답 전체가 500 으로 죽는다.
    값이 없다는 뜻을 null 로 돌려주는 편이 낫다 — 시트에는 빈 칸으로 들어간다.
    """
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return round(f, nd) if math.isfinite(f) else None


@app.post("/score")
def score(rows: List[ScoreRow], x_api_key: Optional[str] = Header(None)):
    """예측 배분 + 실적가격 → 실현원가/적중 지표. n8n look-back 워크플로가 호출."""
    _auth(x_api_key)
    d = pd.DataFrame([r.model_dump() for r in rows])
    if d.empty:
        raise HTTPException(400, "empty payload")
    d["DART"] = d.RT_actual - d.DA_actual
    d["blended"] = d.DA_fraction * d.DA_actual + (1 - d.DA_fraction) * d.RT_actual
    d["vs_rt"] = d.blended - d.RT_actual
    d["vs_da"] = d.blended - d.DA_actual
    d["pred_da_cheap"] = d.DA_fraction > 0.5
    d["actual_da_cheap"] = d.DART > 0
    d["hit"] = d.pred_da_cheap == d.actual_da_cheap
    big5 = d[d.DART.abs() >= 5]
    # |DART| >= $20 인 날이 진짜 관측 지표다. walk-forward 531일에서 절약액의 대부분이
    # 이 30일(전체의 5.6%)에서 나왔고, 그 날들에서만 모델이 '아무것도 안 하기'를 이겼다
    # (모델 60.0% vs 항상RT 40.0%). 전체 적중률은 오히려 항상RT 가 높아서 판단 기준이 못 된다.
    big20 = d[d.DART.abs() >= 20]
    return {
        "n_days": int(len(d)),
        "cost_blended": _r(d.blended.mean()),
        "cost_all_rt": _r(d.RT_actual.mean()),
        "cost_all_da": _r(d.DA_actual.mean()),
        "vs_rt": _r(d.vs_rt.mean()),
        "vs_da": _r(d.vs_da.mean()),
        # 표준편차는 행이 1개면 NaN 이다(ddof=1). NaN 은 JSON 으로 직렬화되지 않아
        # 응답 전체가 500 으로 죽는다 — 실제로 1행짜리 호출에서 그렇게 터졌다.
        "cost_std": _r(d.blended.std()),
        "hit_rate_all": _r(d.hit.mean() * 100, 1),
        "hit_rate_big5": (_r(big5.hit.mean() * 100, 1) if len(big5) >= 5 else None),
        "n_big5": int(len(big5)),
        # --- 핵심 감시 지표 ---
        "hit_rate_big20": (_r(big20.hit.mean() * 100, 1) if len(big20) >= 5 else None),
        "n_big20": int(len(big20)),
        "vs_rt_big20": (_r(big20.vs_rt.mean()) if len(big20) else None),
        "mean_da_fraction": _r(d.DA_fraction.mean()),
        "detail": json.loads(d.round(3).to_json(orient="records")),
    }
