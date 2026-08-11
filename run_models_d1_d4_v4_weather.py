"""
run_models_d1_d4.py  —  ERCOT DA/RT 구매비중 산출기 (모델 1~4 통합)
===================================================================
같은 폴더의 CSV로 모델 1~4를 학습하고, 다가오는 D+1~D+4 각 날에 대해
  - 날씨: 4-city 기온 anomaly(평년比) → M1 피처 + 극단 레짐 오버레이
  - 모델 1: DA가 RT보다 쌀 확률 P(DA<RT) — 직접 분류 (dead-band + 비용가중)
  - 모델 2: 예상 DA 평균가격 + 비싼날 여부
  - 모델 3: reserve/수급 스트레스 Hard Gate (기본 OFF; --use_gate 로만 활성. 백테스트상 역효과)
  - 모델 4: Houston basis premium 확률
을 평가하고, 4모델 25% 동등가중 앙상블로 DA/RT 비중을 정해 CSV로 출력한다.

[입력 파일 — 같은 폴더에 둘 것]
  1) ERCOT_Main_*.csv   : 과거 실측+예보 (학습/최근 regime 계산용)  ← 이미 보유
  2) GD_Katy_*.csv      : 가스 가격                                   ← 이미 보유
  3) forecast_input.csv : D+1~D+4 의 예보 (당신이 채울 파일)         ← 템플릿 제공

[forecast_input.csv 컬럼]  (시간별 권장; 하루 1행만 줘도 동작)
  timestamp     : ISO 시각 (예 2026-06-10T14:00:00). D+1~D+4 의 모든 시간.
  fc_load_mw    : 해당 시각 system-wide Load 예보 (MW)
  fc_wind_mw    : Wind 발전 예보 (MW)
  fc_solar_mw   : Solar 발전 예보 (MW). 야간은 0.
  gas_price     : (선택) 그날 가스 $/MMBtu. 비우면 직전 실측값을 이어씀.

[출력]
  allocation_review.csv : D+1~D+4 각 날의 모델 평가 + DA/RT 비중 + MW 배분
  (콘솔에 각 모델의 과거 정확도도 출력 → 신뢰도 판단용)

[중요한 한계 — 정직하게]
  * 모델 1은 "DA가 RT보다 쌀 확률"을 직접 분류(dead-band+비용가중)한다. 방향 예측 자체가
    이 데이터로는 거의 동전던지기(AUC≈0.5)라, 이 시스템의 값어치는 '타이밍 알파'가 아니라
    '항상 한쪽 쏠림 같은 큰 실수 방지 + 안정성'이다. 실현 원가 개선폭은 작다.
  * NFE는 전 기간 ENV Net Load 단일 기준으로 계산(2024 Actual Load 결측 회피 + 방식 일관성).
    일별 NFE는 P1~P99 winsorize 후 피처(nfe_lag)로만 사용.
  * 모델 3 Hard Gate는 기본 OFF. 최근 ERCOT는 한파 등 스카시티 때 DA가 오히려 폭등(RT<DA)해,
    '위험 시 DA 강제' 게이트가 backtest 에서 원가를 악화시켰다(+$0.2 → +$1.2/MWh). --use_gate 로
    켤 수 있으나, 켜려면 'RT 폭등 위험'에만 발동하도록 트리거 재설계가 선행돼야 한다.
  * 모델 3/4 의 reserve·basis regime 은 결정시점 최근값을 D+1~D+4 에 복사한다(forward 예보 없음).
  * 먼 horizon(D+2~D+4)일수록 예보 자체가 부정확 → 신뢰도 하락.

사용:
  python run_models_d1_d4.py --folder . --volume_mw 100
"""
import argparse, glob, os, warnings
import numpy as np, pandas as pd
import xgboost as xgb
from sklearn.metrics import roc_auc_score
warnings.filterwarnings("ignore")

# ---------- 같은 폴더에서 입력 파일 자동 탐색 (유연·대소문자 무시) ----------
def list_csvs(folder):
    fs = glob.glob(os.path.join(folder, "*.csv")) + glob.glob(os.path.join(folder, "*.CSV"))
    return sorted(set(fs))

def pick_csv(csvs, keys, exclude=()):
    """파일명에 keys 중 하나라도 포함된 첫 CSV (exclude 경로는 제외)."""
    for f in csvs:
        if f in exclude:
            continue
        n = os.path.basename(f).lower()
        if any(k in n for k in keys):
            return f
    return None


# ---------- 날씨: 도시별 CSV → 4-city 평균 → 계절 기준선 → anomaly ----------
def load_weather(csvs):
    """temp_mean_f 컬럼이 있는 CSV 를 전부 날씨로 인식(과거+예보 자동 합침).
    반환: date, t_anom(평년比 편차,+더움), t_anom_abs, t_anom_max. 없으면 None."""
    parts = []
    for f in csvs:
        try:
            head = pd.read_csv(f, nrows=0).columns
        except Exception:
            continue
        if "temp_mean_f" in head and "date" in head:
            parts.append(pd.read_csv(f))
    if not parts:
        return None
    w = pd.concat(parts, ignore_index=True)
    w["date"] = pd.to_datetime(w["date"], errors="coerce")
    w = w.dropna(subset=["date"])
    # 도시가 여러 개면 평균(도시 컬럼 없으면 그대로)
    agg = {"temp_mean_f": "mean"}
    if "temp_max_f" in w.columns:
        agg["temp_max_f"] = "mean"
    W = w.groupby("date").agg(agg).reset_index().sort_values("date")
    W = W.drop_duplicates(subset=["date"], keep="last")
    W["doy"] = W.date.dt.dayofyear

    def _norm(col):
        b = W.groupby("doy")[col].mean()
        bb = pd.concat([b, b, b])                       # 순환 스무딩(연말↔연초 연결)
        s = bb.rolling(15, center=True, min_periods=1).mean().iloc[len(b):2 * len(b)]
        s.index = b.index
        return W.doy.map(s)

    # 계절 기준선(day-of-year 평균 + 15일 스무딩) 대비 편차.
    # 절대기온은 계절을 무시해 "7월 89F(평범)"과 "1월 89F(이상)"를 구분 못한다.
    W["t_anom"] = W.temp_mean_f - _norm("temp_mean_f")
    W["t_anom_abs"] = W.t_anom.abs()
    W["t_anom_max"] = (W.temp_max_f - _norm("temp_max_f")) if "temp_max_f" in W.columns else np.nan
    return W[["date", "t_anom", "t_anom_abs", "t_anom_max"]]


# ---------- 컬럼을 '이름'으로 인식 (파일마다 컬럼 순서가 달라도 동작) ----------
def _col(cols, *needles):
    for c in cols:
        cl = c.lower()
        if all(n.lower() in cl for n in needles):
            return c
    return None

NEEDLES = dict(
    DA=("DA LMP",), RT_LZ=("RT SPP", "LZ_HOUSTON"), RT_HB=("RT SPP", "HB_BUSAVG"),
    fc_load=("Forecast - Load", "Prior Day"), fc_solar=("Solar", "Forecast"),
    fc_wind=("Wind", "Forecast"), act_load=("ISO: Actual - Load",),
    act_solar=("Solar", "Actual - Generation"), act_wind=("Wind", "Actual - Generation"),
    env_act_nl=("ENV: Actual - Net Load",), prc=("PRC",))


def is_ercot_history(path):
    """과거 학습데이터 CSV 인가? — NEEDLES 컬럼을 '전부' 갖춰야 인정.

    'DA LMP 컬럼 보유' 만으로 판별하면 Congesiton_*.csv(존별 부하/지역별 발전) 가 함께 잡힌다.
    그 파일들에는 fc_load / ENV Net Load / PRC 가 없는데, concat 후 drop_duplicates(keep='last')
    가 같은 타임스탬프의 정상 행을 덮어써서 핵심 피처가 통째로 NaN 이 된다(조용한 오염).
    실패는 조용한 NaN 보다 낫다 — 부족한 파일은 사유와 함께 제외한다.
    반환: (인정여부, 없는 컬럼 목록)
    """
    try:
        cols = pd.read_csv(path, nrows=0).columns
    except Exception:
        return False, ["읽기실패"]
    if _col(cols, "DA LMP") is None:
        return False, []                       # 애초에 후보가 아님 — 조용히 넘김
    missing = [k for k, nd in NEEDLES.items() if _col(cols, *nd) is None]
    return (not missing), missing


# ---------- 과거 데이터(실측+예보) -> 시간별. 파일 여러 개 자동 병합. 가스 선택 ----------
def load_history(ercot_files, gas):
    if isinstance(ercot_files, str):
        ercot_files = [ercot_files]
    parts = []
    for f in ercot_files:
        d = pd.read_csv(f); d.columns = [c.replace("\ufeff", "") for c in d.columns]
        parts.append(d)
    raw = pd.concat(parts, ignore_index=True)
    raw["ts"] = pd.to_datetime(raw["Timestamp"].str.replace(r"[+-]\d{2}:\d{2}$", "", regex=True),
                               errors="coerce")
    raw = (raw.dropna(subset=["ts"]).drop_duplicates(subset=["ts"], keep="last")
              .sort_values("ts").reset_index(drop=True))
    pick = {k: _col(raw.columns, *nd) for k, nd in NEEDLES.items()}
    missing = [k for k, v in pick.items() if v is None]
    if missing:
        raise ValueError(f"필요 컬럼을 못 찾음: {missing}. 보유 컬럼: {list(raw.columns)[:6]} ...")
    G = lambda k: pd.to_numeric(raw[pick[k]], errors="coerce")
    mn = pd.DataFrame({"ts": raw["ts"]}); mn["date"] = mn.ts.dt.normalize()
    mn["DA"] = G("DA"); mn["fc_load"] = G("fc_load"); mn["fc_solar"] = G("fc_solar").fillna(0); mn["fc_wind"] = G("fc_wind")
    mn["RT_LZ"] = G("RT_LZ"); mn["RT_HB"] = G("RT_HB")
    mn["act_load"] = G("act_load"); mn["act_solar"] = G("act_solar"); mn["act_wind"] = G("act_wind")
    mn["env_act_nl"] = G("env_act_nl"); mn["prc"] = G("prc")
    mn["fc_netload"] = mn.fc_load - mn.fc_wind - mn.fc_solar
    mn["ren_share"] = (mn.fc_wind + mn.fc_solar) / mn.fc_load
    # (변경, 항목4) act net load 를 전 기간 ENV 단일 기준으로 통일.
    #   2024 는 Actual Load 100% 결측이라 direct(L-W-S)가 아예 불가 → 방식 혼재가 생겼음.
    #   ENV Net Load 는 3개 연도 모두 존재하고 ERCOT 공식 정산치라 direct 의 component 글리치
    #   (최대 ~17.6GW 방식차)도 없음. 단일 기준으로 방식 불일치 제거 + 학습구간 확대.
    mn["act_netload"] = mn.env_act_nl
    mn["nfe"] = mn.act_netload - mn.fc_netload
    mn["hou_basis"] = mn.RT_LZ - mn.RT_HB
    if gas:
        gas_df = pd.read_csv(gas, skiprows=1); gas_df.columns = ["date", "gas"]
        gas_df["date"] = pd.to_datetime(gas_df["date"], errors="coerce")
        gas_df["gas"] = pd.to_numeric(gas_df["gas"], errors="coerce")
        gas_df = gas_df.dropna(subset=["date"])
    else:
        gas_df = pd.DataFrame({"date": pd.to_datetime([]), "gas": pd.Series([], dtype=float)})
    return mn, gas_df


# ---------- 시간별 -> 일별 패널 + 타깃/피처 ----------
def daily_panel(mn, gas_df, wx=None):
    d = mn.groupby("date").agg(
        DA_avg=("DA", "mean"), RT_avg=("RT_LZ", "mean"), nfe=("nfe", "mean"),
        fc_nl_mean=("fc_netload", "mean"), fc_nl_max=("fc_netload", "max"),
        fc_load_max=("fc_load", "max"), ren_mean=("ren_share", "mean"),
        ren_min=("ren_share", "min"), basis_mean=("hou_basis", "mean"),
        prc_low=("prc", "min")).reset_index().sort_values("date")
    d = d.merge(gas_df, on="date", how="left"); d["gas"] = d.gas.ffill()
    d["ren_share"] = d.ren_mean
    # (신규, 항목5) NFE winsorize: 일별 NFE 를 P1~P99 로 제한.
    #   NFE 는 이제 타깃이 아니라 피처(nfe_lag1/nfe_r7)이므로 극단 clip 이 tail 신호를 해치지 않고
    #   XGBoost 입력 왜곡만 줄인다. 항목4(ENV 통일) 이후라 direct 글리치성 극단은 이미 제거됨.
    _lo, _hi = d.nfe.quantile(0.01), d.nfe.quantile(0.99)
    d["nfe"] = d.nfe.clip(_lo, _hi)
    d["month"] = d.date.dt.month; d["weekend"] = (d.date.dt.dayofweek >= 5).astype(int)
    # 최근 regime / lag (모두 전일까지만 → 누수 없음)
    d["nfe_lag1"] = d.nfe.shift(1); d["nfe_r7"] = d.nfe.shift(1).rolling(7, min_periods=3).mean()
    d["gas_lag1"] = d.gas.shift(1); d["gas_r7"] = d.gas.shift(1).rolling(7, min_periods=3).mean()
    d["DA_r3"] = d.DA_avg.shift(1).rolling(3, min_periods=2).mean()
    d["DA_r7"] = d.DA_avg.shift(1).rolling(7, min_periods=3).mean()
    d["DA_r14"] = d.DA_avg.shift(1).rolling(14, min_periods=5).mean()
    d["da_med"] = d.DA_avg.shift(1).rolling(30, min_periods=10).median()
    d["basis_lag1"] = d.basis_mean.shift(1)
    d["basis_r7"] = d.basis_mean.shift(1).rolling(7, min_periods=3).mean()
    d["prem_rate_r7"] = (d.basis_mean > 2).shift(1).rolling(7, min_periods=3).mean()
    d["prc_low_r7"] = d.prc_low.shift(1).rolling(7, min_periods=3).mean()
    d["basis_prem"] = np.where(d.basis_mean.notna(), (d.basis_mean > 2).astype(float), np.nan)
    # (신규) 모델 1의 새 타깃: DART = RT - DA. >0 이면 RT가 비쌌음 = DA 매수가 유리했음.
    d["DART"] = d.RT_avg - d.DA_avg
    d["da_cheaper"] = np.where(d.DART.notna(), (d.DART > 0).astype(float), np.nan)
    d["da_dev"] = d.DA_avg - d.da_med          # DA가 30일 중앙값 대비 얼마나 비싼지(모델2 vote용)
    # (신규) 날씨 anomaly 병합. 없으면 NaN 컬럼 → _usable() 이 자동 제외(모델은 그대로 동작).
    for c in ("t_anom", "t_anom_abs", "t_anom_max"):
        d[c] = np.nan
    if wx is not None:
        d = d.drop(columns=["t_anom", "t_anom_abs", "t_anom_max"]).merge(wx, on="date", how="left")
    return d


M1F = ["fc_nl_mean", "fc_nl_max", "ren_share", "nfe_lag1", "nfe_r7", "month", "weekend",
       "t_anom", "t_anom_abs", "t_anom_max"]   # 날씨 anomaly (없으면 자동 제외)
M2F = ["fc_nl_mean", "fc_nl_max", "ren_mean", "ren_min", "fc_load_max",
       "gas_lag1", "gas_r7", "DA_r3", "DA_r7", "DA_r14", "month", "weekend"]
M4F = ["ren_share", "fc_nl_mean", "month", "basis_lag1", "basis_r7", "prem_rate_r7"]
XGBR = dict(n_estimators=350, max_depth=4, learning_rate=0.03, subsample=0.8,
            colsample_bytree=0.8, n_jobs=-1, random_state=0)
XGBC = dict(n_estimators=250, max_depth=3, learning_rate=0.05, subsample=0.9,
            n_jobs=-1, random_state=0, eval_metric="logloss")
DEADBAND = 3.0     # |RT-DA| <= $3 인 날은 학습 제외(경제적으로 무의미한 near-zero 노이즈)
CW_CAP   = 100.0   # 비용가중 상한($/MWh): 극단 1~2일이 학습을 지배하지 않게
GATE_FLOOR = 0.75  # (항목3) Hard Gate 발동 시 최소 DA 비중 = RT 상한 25%. 위험할 때만 적용(비대칭).
# ---- 날씨 레짐 오버레이 (극단 anomaly 일에만 발동; 평년 근처엔 미개입) ----
WX_COLD_Q  = 0.05   # 학습분포 하위 5% = 이상한파 임계
WX_HOT_Q   = 0.95   # 학습분포 상위 5% = 이상고온 임계
WX_SPAN_F  = 5.0    # 임계 초과 5F 에서 최대 강도
WX_COLD_LEAN = 0.30 # 이상한파: DA 비중 최대 -0.30 (한파엔 DA 가 폭등 → RT 로 기울임)
WX_HOT_LEAN  = 0.30 # 이상고온: DA 비중 최대 +0.30 (폭염엔 RT 가 튐 → DA 로 기울임)
WX_COLD_FLOOR = 0.30  # 한파에도 DA 를 이 아래로는 내리지 않음(Uri 급 tail 보험. 비용 ~$0.04/MWh)


def sigmoid(x):
    return float(1.0 / (1.0 + np.exp(-np.clip(x, -30, 30))))


def _usable(d, feats):
    return [c for c in feats if c in d.columns and d[c].notna().any()]


def weather_overlay(f_da, anom, cold_t, hot_t):
    """극단 기온 anomaly 일에만 배분을 '방향에 맞게' 조정.
    - 이상한파: ERCOT 는 한파에 DA 가 과잉 프리미엄(실적: DA $76 vs RT $48) → RT 로 기울임.
    - 이상고온: 여름 폭염엔 RT 가 튐(실적: DART +$17) → DA 로 기울임.
    - 평년 근처(대부분의 날)에는 아무것도 하지 않고 앙상블에 맡긴다.
    한파 쪽에는 FLOOR 를 둬 헤지를 완전히 풀지 않는다(표본 2.5년에 Uri 급 사건이 없음).
    반환: (조정된 DA 비중, 사유 문자열)"""
    if anom is None or np.isnan(anom) or cold_t is None or np.isnan(cold_t):
        return f_da, "-"
    if anom < cold_t:
        mag = min(max((cold_t - anom) / WX_SPAN_F, 0.0), 1.0)
        floor = min(f_da, WX_COLD_FLOOR)          # 이미 낮으면 끌어올리지 않음
        return max(f_da - WX_COLD_LEAN * mag, floor), f"cold{anom:+.0f}F"
    if anom > hot_t:
        mag = min(max((anom - hot_t) / WX_SPAN_F, 0.0), 1.0)
        return min(f_da + WX_HOT_LEAN * mag, 1.0), f"hot{anom:+.0f}F"
    return f_da, "-"


def train_models(d):
    """각 모델: (사용피처 리스트, 학습된 모델). 전부 NaN인 피처(예: 가스 없음)는 자동 제외."""
    m = {}
    # 모델 1 (변경): NFE 회귀 → "DA가 더 쌀 확률" 직접 분류.
    #   dead-band 로 near-zero 노이즈 제외 + |DART| 비용가중 → 큰 날에 집중 학습.
    #   NFE 자체는 좋은 '피처'라 nfe_lag1/nfe_r7 는 입력으로 유지(M1F).
    f1 = _usable(d, M1F)
    a1 = d.dropna(subset=["da_cheaper", "DART"]); a1 = a1[a1.DART.abs() > DEADBAND]
    if a1["da_cheaper"].nunique() > 1:
        w1 = a1.DART.abs().clip(upper=CW_CAP)
        m["m1"] = (f1, xgb.XGBClassifier(**XGBC).fit(a1[f1], a1.da_cheaper, sample_weight=w1))
    else:
        m["m1"] = (f1, None)
    # 모델 2 (유지): DA 평균가격 회귀
    f2 = _usable(d, M2F); a2 = d.dropna(subset=["DA_avg"]); m["m2"] = (f2, xgb.XGBRegressor(**XGBR).fit(a2[f2], a2.DA_avg))
    # 모델 4 (유지): Houston basis 프리미엄 분류
    f4 = _usable(d, M4F); a4 = d.dropna(subset=["basis_prem"]); m["m4"] = (f4, xgb.XGBClassifier(**XGBC).fit(a4[f4], a4.basis_prem))
    # vote 정규화용 중심·스케일 (매직넘버 제거 → 학습분포에서 robust 하게 적합; IQR/1.349 ≈ std)
    dev = d.da_dev.dropna(); prc = d.prc_low_r7.dropna()
    iqr = lambda s: float((s.quantile(.75) - s.quantile(.25)) / 1.349) or 1.0
    m["scales"] = dict(dev_c=float(dev.median()), dev_s=(iqr(dev) or 1.0),
                       prc_c=float(prc.median()), prc_s=(iqr(prc) or 1.0))
    # (항목3) Hard Gate 임계: 학습분포 위험분위. 예비력 하위10%, 예보 net load 상위10%, 재생 하위10%.
    # 날씨 오버레이 임계: 학습분포 분위수(하드코딩 금지)
    an = d.t_anom.dropna()
    m["wx"] = (dict(cold=float(an.quantile(WX_COLD_Q)), hot=float(an.quantile(WX_HOT_Q)))
               if len(an) > 100 else dict(cold=float("nan"), hot=float("nan")))
    m["gate"] = dict(prc_danger=float(prc.quantile(0.10)),
                     nl_high=float(d.fc_nl_max.dropna().quantile(0.90)),
                     wind_low=float(d.ren_min.dropna().quantile(0.10)))
    return m


def historical_accuracy(d):
    """과거 시간순 holdout 으로 각 모델 정확도 (신뢰도 판단용)."""
    cut = d.date.quantile(0.7)
    tr, te = d[d.date < cut], d[d.date >= cut]
    out = {}
    f1 = _usable(d, M1F)
    a = tr.dropna(subset=["da_cheaper", "DART"]); a = a[a.DART.abs() > DEADBAND]
    b = te.dropna(subset=["da_cheaper"])
    if len(a) > 200 and len(b) > 20 and a.da_cheaper.nunique() > 1 and b.da_cheaper.nunique() > 1:
        w = a.DART.abs().clip(upper=CW_CAP)
        p = xgb.XGBClassifier(**XGBC).fit(a[f1], a.da_cheaper, sample_weight=w).predict_proba(b[f1])[:, 1]
        out["M1_DA_cheaper_AUC"] = float(roc_auc_score(b.da_cheaper, p))
        # 비용 관점 보조지표: 이 신호로 DA/RT 선택 시 vs 항상RT 의 실현 원가차($/MWh)
        f_da = (p > 0.5).astype(float)
        blended = (f_da * b.DA_avg.values + (1 - f_da) * b.RT_avg.values)
        out["M1_cost_vs_allRT_USD"] = float((blended - b.RT_avg.values).mean())
    f2 = _usable(d, M2F); a = tr.dropna(subset=["DA_avg"]); b = te.dropna(subset=["DA_avg"])
    if len(a) > 200 and len(b) > 20:
        p = xgb.XGBRegressor(**XGBR).fit(a[f2], a.DA_avg).predict(b[f2])
        bn = b[b.DA_avg < 100]
        out["M2_DA_MAE_USD_normaldays"] = float(np.abs(p[b.DA_avg.values < 100] - bn.DA_avg).mean())
        out["M2_expensive_AUC"] = float(roc_auc_score((b.DA_avg >= 50).astype(int), p))
    f4 = _usable(d, M4F); a = tr.dropna(subset=["basis_prem"]); b = te.dropna(subset=["basis_prem"])
    if len(a) > 200 and len(b) > 20 and b.basis_prem.nunique() > 1:
        p = xgb.XGBClassifier(**XGBC).fit(a[f4], a.basis_prem).predict_proba(b[f4])[:, 1]
        out["M4_premium_AUC"] = float(roc_auc_score(b.basis_prem, p))
    return out


# ---------- forecast_input -> 일별 예보 피처 ----------
def forecast_daily(fc):
    fc = fc.copy(); fc["ts"] = pd.to_datetime(fc["timestamp"])
    fc["date"] = fc.ts.dt.normalize()
    fc["fc_netload"] = fc.fc_load_mw - fc.fc_wind_mw - fc.fc_solar_mw.fillna(0)
    fc["ren_share"] = (fc.fc_wind_mw + fc.fc_solar_mw.fillna(0)) / fc.fc_load_mw
    agg = {"fc_load_mw": "max", "fc_netload": ["mean", "max"], "ren_share": ["mean", "min"]}
    g = fc.groupby("date").agg(fc_load_max=("fc_load_mw", "max"),
        fc_nl_mean=("fc_netload", "mean"), fc_nl_max=("fc_netload", "max"),
        ren_mean=("ren_share", "mean"), ren_min=("ren_share", "min")).reset_index()
    if "gas_price" in fc.columns:
        g = g.merge(fc.groupby("date")["gas_price"].mean().rename("gas_fc").reset_index(), on="date", how="left")
    g["ren_share"] = g.ren_mean
    g["month"] = g.date.dt.month; g["weekend"] = (g.date.dt.dayofweek >= 5).astype(int)
    return g.sort_values("date").reset_index(drop=True)


def clip01(x): return float(np.clip(x, 0, 1))


def forecast_rows(fc, models, reg, volume_mw, use_gate=False):
    """예보 패널 → D+1..D+N 배분 행 리스트. run() 과 API 서버가 공유."""
    f1, mdl1 = models["m1"]; f2, mdl2 = models["m2"]; f4, mdl4 = models["m4"]
    sc = models["scales"]; gt = models["gate"]; wx = models["wx"]
    rows = []
    for h, (_, r) in enumerate(fc.iterrows(), start=1):
        feat = dict(reg)
        for c in ["fc_load_max", "fc_nl_mean", "fc_nl_max", "ren_mean", "ren_min",
                  "ren_share", "month", "weekend"]:
            feat[c] = r[c]
        if "gas_fc" in r and pd.notna(r["gas_fc"]):
            feat["gas_lag1"] = r["gas_fc"]; feat["gas_r7"] = r["gas_fc"]
        X = pd.DataFrame([feat])
        for c in set(f1 + f2 + f4):                  # 누락 피처는 NaN 으로 (XGB가 처리)
            if c not in X.columns: X[c] = np.nan
        # 모델 1 (변경): "DA가 더 쌀 확률"을 직접 출력 → 변환/매직넘버 불필요, 포화 없음.
        v1 = clip01(mdl1.predict_proba(X[f1])[0, 1]) if mdl1 is not None else 0.5
        # 모델 2: DA 평균가격 예측 → 30일 중앙값 대비 편차를 데이터 적합 sigmoid 로 (DA 비쌈 -> RT)
        m2 = float(mdl2.predict(X[f2])[0]); damed = reg["da_med"]
        z2 = ((m2 - damed) - sc["dev_c"]) / sc["dev_s"]
        v2 = 1.0 - sigmoid(z2)
        # 모델 3 (변경, 항목3): 방향 투표 제거 → 비대칭 Hard Gate 로 사용.
        #   최근 예비력 + 예보상 수급 스트레스(고 net load / 저 재생) 중 하나라도 위험이면 발동.
        prc7 = reg["prc_low_r7"]; fcnlmax = float(r["fc_nl_max"]); renmin = float(r["ren_min"])
        reason = []
        if prc7 < gt["prc_danger"]:  reason.append("reserve")
        if fcnlmax > gt["nl_high"]:  reason.append("highNL")
        if renmin < gt["wind_low"]:  reason.append("lowRen")
        gate = len(reason) > 0
        # 모델 4 (유지): Houston basis 프리미엄 확률 (이미 [0,1])
        m4 = float(mdl4.predict_proba(X[f4])[0, 1])
        v4 = clip01(m4)
        # === 앙상블: 예측형 3모델(1·2·4) 평균이 기본 DA 비중 ===
        base_da = clip01((v1 + v2 + v4) / 3.0)
        # === Hard Gate: 위험 시에만 DA 하한(=RT 상한) 강제. 안전 시엔 DA 를 낮추지 않음(비대칭) ===
        # 기본(use_gate=False): 게이트 조건은 '정보'로만 표기하고 배분엔 미적용.
        #   백테스트상 현행 게이트는 이 시장(한파 때 DA가 오히려 폭등)에서 역효과였음.
        gate_applied = gate and use_gate
        f_da = clip01(max(base_da, GATE_FLOOR)) if gate_applied else base_da
        # 날씨 레짐 오버레이 (극단 anomaly 일에만 발동)
        anom = float(r["t_anom"]) if "t_anom" in r and pd.notna(r["t_anom"]) else np.nan
        f_da, wx_reason = weather_overlay(f_da, anom, wx["cold"], wx["hot"])
        f_da = clip01(f_da)
        rows.append(dict(
            horizon=f"D+{h}", date=r["date"].date(),
            M1_P_DA_cheaper=round(v1, 3),
            M2_DA_pred_USD=round(m2, 2), M2_expensive=bool(m2 > damed * 1.15),
            M3_prc_low_r7_MW=round(prc7, 0), M3_gate_signal=bool(gate),
            M3_gate_applied=bool(gate_applied),
            M3_gate_reason=("+".join(reason) if gate else "-"),
            M4_premium_prob=round(m4, 3),
            vote_M1_DA=round(v1, 2), vote_M2_DA=round(v2, 2), vote_M4_DA=round(v4, 2),
            WX_t_anom_F=(round(anom, 1) if not np.isnan(anom) else None), WX_overlay=wx_reason,
            base_DA=round(base_da, 3),
            DA_fraction=round(f_da, 3), RT_fraction=round(1 - f_da, 3),
            DA_MW=round(f_da * volume_mw, 1), RT_MW=round((1 - f_da) * volume_mw, 1)))
    return rows




def run(folder, ercot, gas, forecast, out, volume_mw, use_gate=False):
    csvs = list_csvs(folder)
    forecast = forecast or pick_csv(csvs, ["forecast"])

    if ercot:
        ercot_files = [ercot]
    else:
        ercot_files = []
        for c in csvs:
            if c == forecast:
                continue
            ok, missing = is_ercot_history(c)
            if ok:
                ercot_files.append(c)
            elif missing:
                print(f"[제외] {os.path.basename(c)} — DA LMP 는 있으나 {missing} 없음")
    gas = gas or pick_csv(csvs, ["katy", "gd_", "platts", "henry"],
                          exclude=tuple([forecast] + ercot_files))
    if not ercot_files:
        print("[오류] ERCOT 과거 데이터 CSV(=DA LMP 컬럼 보유)를 찾지 못했습니다.")
        print(f"  탐색 폴더: {os.path.abspath(folder)} | 폴더 안 CSV:")
        for f in csvs: print("    -", os.path.basename(f))
        print('\n  해결: 직접 지정 예)  python run_models_d1_d4.py --ercot "2026_Historical_Data.csv"')
        return
    print("입력 ERCOT 파일:", ", ".join(os.path.basename(f) for f in ercot_files))
    print("가스 파일:", os.path.basename(gas) if gas else "없음 (→ 모델2 가스피처 제외, 정확도 다소↓)")
    print("forecast:", os.path.basename(forecast) if forecast else "없음")
    wxdf = load_weather([c for c in csvs if c not in ercot_files])
    print("날씨:", f"{len(wxdf)}일 (anomaly 피처+오버레이 사용)" if wxdf is not None
          else "없음 (→ 날씨 피처/오버레이 비활성, 나머지는 정상 동작)")
    mn, gas_df = load_history(ercot_files, gas)
    d = daily_panel(mn, gas_df, wxdf)
    self_wx = wxdf
    models = train_models(d)
    acc = historical_accuracy(d)
    print("=== 모델 과거 정확도 (신뢰도 판단용) ===")
    for k, v in acc.items(): print(f"  {k:32s}: {v:.3f}")

    # 결정시점(D0) = 마지막 실측일. 그 시점의 최근 regime 값 사용.
    reg_cols = ["nfe_lag1", "nfe_r7", "gas_lag1", "gas_r7", "DA_r3", "DA_r7", "DA_r14",
                "da_med", "basis_lag1", "basis_r7", "prem_rate_r7", "prc_low_r7"]
    last = d.dropna(subset=["da_med"]).iloc[-1]
    reg = {c: last[c] for c in reg_cols}
    d0 = last["date"]
    print(f"\n결정시점 D0 = {d0.date()} | 최근 reserve regime prc_low_r7 = {reg['prc_low_r7']:.0f} MW")
    print(f"M3 Hard Gate: {'ON' if use_gate else 'OFF(기본)'}  "
          f"{'' if use_gate else '— 게이트 조건은 정보로만 표기, 배분 미적용'}")

    if not forecast:
        print("\n[알림] forecast_input.csv 가 없습니다. 템플릿을 채워 다시 실행하세요.")
        return
    fc = forecast_daily(pd.read_csv(forecast))
    # 예보일자에 날씨 anomaly 병합(기상예보는 16일까지 있어 net load 예보보다 선행시간이 길다)
    for c in ("t_anom", "t_anom_abs", "t_anom_max"):
        fc[c] = np.nan
    if self_wx is not None:
        fc = fc.drop(columns=["t_anom", "t_anom_abs", "t_anom_max"]).merge(self_wx, on="date", how="left")
        miss = fc.t_anom.isna().sum()
        if miss:
            print(f"[알림] 예보 {miss}일에 날씨 데이터가 없습니다 → 그 날은 오버레이 미적용.")

    # regime 신선도 점검: D0(마지막 실측)과 D+1 사이 공백이 크면 경고
    gap = (fc["date"].min().normalize() - pd.Timestamp(d0).normalize()).days
    if gap > 2:
        print("\n" + "!" * 70)
        print(f"[경고] 최근 regime 데이터가 오래됐습니다.")
        print(f"  마지막 실측(D0) = {pd.Timestamp(d0).date()} 인데 예보 시작 = {fc['date'].min().date()}")
        print(f"  → 약 {gap}일 공백. 모델 3/4 reserve·basis regime과 모델 1/2 의 최근 lag")
        print(f"     (prc_low_r7, nfe_r7, DA_r7, gas, basis 등)이 {gap}일 전 값으로 들어갑니다.")
        print(f"  → ERCOT_Main / 가스 CSV 를 D+1 전날까지 채운 뒤 다시 실행하세요.")
        print("!" * 70)

    f1, mdl1 = models["m1"]; f2, mdl2 = models["m2"]; f4, mdl4 = models["m4"]
    rows = forecast_rows(fc, models, reg, volume_mw, use_gate)
    res = pd.DataFrame(rows)
    path = os.path.join(out, "allocation_review.csv")
    res.to_csv(path, index=False)
    pd.set_option("display.width", 200, "display.max_columns", 30)
    print("\n=== D+1~D+4 모델 평가 & 구매비중 ===")
    print(res.to_string(index=False))
    print(f"\nsaved -> {path}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--folder", default=".")
    ap.add_argument("--ercot"); ap.add_argument("--gas"); ap.add_argument("--forecast")
    ap.add_argument("--out", default=".")
    ap.add_argument("--volume_mw", type=float, default=100.0)
    ap.add_argument("--use_gate", action="store_true",
                    help="M3 Hard Gate 사용(기본 off). 백테스트상 현행 설계는 이 시장에서 역효과였음.")
    a = ap.parse_args()
    run(a.folder, a.ercot, a.gas, a.forecast, a.out, a.volume_mw, a.use_gate)
