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
import os, sys, json, tempfile, datetime as dt
from typing import Optional, List, Dict, Any

import pandas as pd
from fastapi import FastAPI, HTTPException, Header
from pydantic import BaseModel

MODEL_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.environ.get("DATA_DIR", os.path.join(MODEL_DIR, "data"))
API_KEY = os.environ.get("API_KEY")  # n8n 과 공유하는 단순 인증키

sys.path.insert(0, MODEL_DIR)
import run_models_d1_d4_v4_weather as M  # noqa: E402

app = FastAPI(title="ERCOT DA/RT Allocation", version="4.0")
_cache: Dict[str, Any] = {"key": None, "models": None, "panel": None}


class ForecastRow(BaseModel):
    timestamp: str
    fc_load_mw: float
    fc_wind_mw: float
    fc_solar_mw: float


class PredictRequest(BaseModel):
    forecast: List[ForecastRow]
    volume_mw: float = 100.0
    use_gate: bool = False
    run_id: Optional[str] = None


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
    if not ercot_files:
        raise HTTPException(500, "no ERCOT history CSV (필요 컬럼 전부 보유) found")
    gas = M.pick_csv(csvs, ["katy", "gas", "gd_"], exclude=set(ercot_files))
    wx = M.load_weather([c for c in csvs if c not in ercot_files])

    mn, gas_df = M.load_history(ercot_files, gas)
    panel = M.daily_panel(mn, gas_df, wx)
    models = M.train_models(panel)
    models["_wx"] = wx
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

    last = panel.dropna(subset=["DA_avg"]).iloc[-1]
    reg = dict(prc_low_r7=float(last.prc_low_r7), da_med=float(last.da_med),
               basis_lag1=float(last.basis_mean), basis_r7=float(last.basis_r7),
               prem_rate_r7=float(last.prem_rate_r7), gas_lag1=float(last.gas),
               gas_r7=float(last.gas_r7), nfe_lag1=float(last.nfe), nfe_r7=float(last.nfe_r7),
               DA_r3=float(last.DA_r3), DA_r7=float(last.DA_r7), DA_r14=float(last.DA_r14))

    rows = M.forecast_rows(fc, models, reg, req.volume_mw, use_gate=req.use_gate)
    d0 = pd.Timestamp(last["date"]).date().isoformat()
    return {
        "run_id": req.run_id or dt.datetime.utcnow().strftime("%Y%m%dT%H%M%SZ"),
        "generated_at": dt.datetime.utcnow().isoformat(),
        "model_version": "v4-weather",
        "d0_last_actual": d0,
        "regime": {"prc_low_r7": reg["prc_low_r7"], "da_med": reg["da_med"]},
        "rows": rows,
    }


class ScoreRow(BaseModel):
    date: str
    DA_fraction: float
    DA_actual: float
    RT_actual: float


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
    big = d[d.DART.abs() >= 5]
    return {
        "n_days": int(len(d)),
        "cost_blended": round(float(d.blended.mean()), 3),
        "cost_all_rt": round(float(d.RT_actual.mean()), 3),
        "cost_all_da": round(float(d.DA_actual.mean()), 3),
        "vs_rt": round(float(d.vs_rt.mean()), 3),
        "vs_da": round(float(d.vs_da.mean()), 3),
        "cost_std": round(float(d.blended.std()), 3),
        "hit_rate_all": round(float(d.hit.mean() * 100), 1),
        "hit_rate_big5": (round(float(big.hit.mean() * 100), 1) if len(big) >= 5 else None),
        "n_big5": int(len(big)),
        "mean_da_fraction": round(float(d.DA_fraction.mean()), 3),
        "detail": d.round(3).to_dict("records"),
    }
