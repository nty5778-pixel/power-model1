"""
run_models_d1_d4.py  —  ERCOT DA/RT 구매비중 산출기 (모델 1~4 통합)
===================================================================
같은 폴더의 CSV로 모델 1~4를 학습하고, 다가오는 D+1~D+4 각 날에 대해
  - 날씨: 4-city 기온 anomaly(평년比) → M1 피처 + 극단 레짐 오버레이
  - 모델 1: DA가 RT보다 쌀 확률 P(DA<RT) — 직접 분류 (dead-band + 비용가중)
  - 모델 2: 예상 DA 평균가격 + 비싼날 여부
  - 모델 3: reserve/수급 스트레스 Hard Gate (기본 OFF; --use_gate 로만 활성. 백테스트상 역효과)
  - 모델 4: Houston basis premium 확률
을 평가하고, 배분 규칙(combine_votes)에 따라 DA/RT 비중을 정해 CSV로 출력한다.
기본 규칙은 m1_only — 평소 RT, 모델 1 점수가 0.50 을 넘는 날만 DA.
(세 표를 1/3씩 평균하던 기존 ensemble 은 --alloc ensemble 로 남아 있다. 백테스트 531일에서
 ensemble 은 100% RT 대비 연 -18만달러 손해, m1_only 는 +30만달러 절약이었다.)

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
    """폴더 안의 표 형식 데이터 파일. 확장자가 .txt 여도 내용이 CSV 면 받아들인다.

    (항목1) 날씨가 'Weather Data.txt' 로 배포된 적이 있는데, *.csv 만 스캔하던 탓에
    파일이 통째로 무시되어 t_anom 이 전 구간 결측 → 날씨 피처와 오버레이가 조용히
    비활성됐다. 확장자 관례는 소스마다 달라지므로 스캔 쪽을 넓히는 게 근본적이다.
    """
    fs = []
    for p in ("*.csv", "*.CSV", "*.txt", "*.TXT"):
        fs += glob.glob(os.path.join(folder, p))
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


# ---------- 날씨: 도시별 파일 → 지역 평균 → 평년 대비 편차(anomaly) ----------
# 숫자로 강제할 날씨 컬럼. 제공자가 30년 평년(normal_*)이나 편차(*_departure_f)를
# 함께 주면 그쪽을 우선 쓴다 — 자체 산출 평년은 표본이 2.5년뿐이라 얇다.
_WX_NUM = ("temp_mean_f", "temp_max_f", "temp_mean_departure_f", "temp_max_departure_f",
           "normal_temp_mean_f", "normal_temp_max_f")


def _weather_frame(csvs):
    """temp_mean_f + date 를 가진 파일을 전부 날씨로 인식(과거+예보 자동 합침).

    (항목4) 같은 날짜가 여러 번 실린 소스를 위해 '실측 > 예보, 동률이면 최신 run' 순으로
    정렬한 뒤 마지막 행만 남긴다. 갱신할 때마다 run_id 를 달리해 append 하는 시트가 그렇다.
    정렬 없이 keep='last' 만 하면 오래된 예보가 최신 실측을 덮어쓸 수 있다.
    반환: date, temp_mean_f, (temp_max_f / 평년 / 편차 컬럼), doy. 없으면 None.
    """
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
    for c in _WX_NUM:
        if c in w.columns:
            w[c] = pd.to_numeric(w[c], errors="coerce")

    w["_actual"] = ((w["data_type"].astype(str).str.lower() == "actual").astype(int)
                    if "data_type" in w.columns else 0)
    runcol = next((c for c in ("run_id", "run_date") if c in w.columns), None)
    w["_run"] = w[runcol].astype(str) if runcol else ""
    key = ["date"] + (["region"] if "region" in w.columns else [])
    w = (w.sort_values(key + ["_actual", "_run"])
           .drop_duplicates(subset=key, keep="last"))

    agg = {c: "mean" for c in _WX_NUM if c in w.columns}   # 도시/지역이 여럿이면 평균
    W = w.groupby("date").agg(agg).reset_index().sort_values("date")
    W["doy"] = W.date.dt.dayofyear
    return W


def _doy_norm(W, col):
    """day-of-year 별 계절 기준선(평년값). 인덱스=doy, 값=기온(F).
    연말↔연초가 이어지도록 3배 복제 후 15일 스무딩."""
    b = W.groupby("doy")[col].mean()
    bb = pd.concat([b, b, b])
    s = bb.rolling(15, center=True, min_periods=1).mean().iloc[len(b):2 * len(b)]
    s.index = b.index
    return s


def _normal_doy(W, col, dep_col, normal_col):
    """day-of-year → 평년값 표. (항목3) 출처 우선순위:

      1) normal_* 컬럼            제공자가 준 평년값 (예: 1991-2020 NOAA 30년)
      2) 실측 − *_departure_f     편차만 줬으면 평년을 역산
      3) 보유 데이터 day-of-year 평균 (표본 2.5년 → 얇음. 1·2가 없을 때만)

    1·2 로 채우지 못한 날짜(doy)는 3 으로 메운다. 편차 컬럼을 그대로 t_anom 에
    쓰지 않고 '평년값'으로 되돌려 쓰는 이유는, 과거 파일(편차 없음)과 신규 시트
    (편차 있음)가 섞였을 때 기준이 날짜마다 달라지는 것을 막기 위해서다.
    반환: (Series(doy→F) 또는 None, 출처 설명)
    """
    if col not in W.columns or W[col].isna().all():
        return None, "없음"
    obs, how = None, None
    if normal_col in W.columns and W[normal_col].notna().any():
        obs, how = W[normal_col], f"제공 평년값({normal_col})"
    elif dep_col in W.columns and W[dep_col].notna().any():
        obs, how = W[col] - W[dep_col], f"제공 편차({dep_col})에서 역산"

    self_norm = _doy_norm(W, col)
    if obs is None:
        return self_norm, "자체 산출(보유 데이터 day-of-year 평균)"
    given = obs.groupby(W.doy).mean().dropna()
    filled = self_norm.copy()
    filled.loc[given.index] = given
    n_gap = int(len(filled) - len(given))
    return filled, how + (f" + 미제공 {n_gap}일은 자체 산출" if n_gap else "")


def weather_normals(csvs):
    """예보일의 anomaly 를 계산하기 위한 평년값 표.

    과거 파일에 없는 '미래 날짜'의 t_anom 을 서버가 직접 구할 수 있게 한다.
    (예보 기온 − 그 날짜의 평년값). 없으면 None.
    반환: {"mean": Series(doy→F), "max": Series(doy→F) 또는 None}
    """
    W = _weather_frame(csvs)
    if W is None:
        return None
    mean, _ = _normal_doy(W, "temp_mean_f", "temp_mean_departure_f", "normal_temp_mean_f")
    mx, _ = _normal_doy(W, "temp_max_f", "temp_max_departure_f", "normal_temp_max_f")
    return {"mean": mean, "max": mx}


def load_weather(csvs, verbose=True):
    """날씨 파일 → 날짜별 기온 anomaly.

    절대기온은 계절을 무시해 "7월 89F(평범)"과 "1월 89F(이상)"를 구분 못한다.
    그래서 평년 대비 편차로 바꿔서 쓴다. 평년값 출처는 _normal_doy() 참조.
    반환: date, t_anom(평년比 편차,+더움), t_anom_abs, t_anom_max. 없으면 None."""
    W = _weather_frame(csvs)
    if W is None:
        return None
    nmean, src = _normal_doy(W, "temp_mean_f", "temp_mean_departure_f", "normal_temp_mean_f")
    nmax, _ = _normal_doy(W, "temp_max_f", "temp_max_departure_f", "normal_temp_max_f")
    W["t_anom"] = W.temp_mean_f - W.doy.map(nmean)
    W["t_anom_abs"] = W.t_anom.abs()
    W["t_anom_max"] = (W.temp_max_f - W.doy.map(nmax)) if nmax is not None else np.nan
    if verbose:
        print(f"  평년 기준: {src}")
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

# (항목1) 필수 컬럼을 '실제로 쓰이는 것' 으로 좁힌다.
#   기존에는 NEEDLES 11개를 전부 요구했는데, act_load/act_solar/act_wind 는 ENV 모드에서
#   읽히기만 하고 어떤 피처에도 들어가지 않았고, prc 는 기본 OFF 인 M3 게이트 전용이다.
#   ERCOT API 로 옮기면서 안 쓰는 항목까지 받아오지 않아도 되게 요구사항을 명확히 한다.
CORE = ["DA", "RT_LZ", "RT_HB", "fc_load", "fc_solar", "fc_wind"]   # 가격·예보: 항상 필수
NL_NEEDS = dict(env=["env_act_nl"],                                 # 순부하 산출 방식별 추가
                direct=["act_load", "act_wind", "act_solar"])
OPTIONAL = ["prc"]                                                  # 없으면 M3 게이트만 비활성


def required_keys(netload="env"):
    """이 설정에서 반드시 있어야 하는 NEEDLES 키 목록."""
    return CORE + NL_NEEDS[netload]


def is_ercot_history(path, netload="env"):
    """과거 학습데이터 파일인가? — 필수 컬럼을 '전부' 갖춰야 인정.

    'DA LMP 컬럼 보유' 만으로 판별하면 Congesiton_*.csv(존별 부하/지역별 발전) 가 함께 잡힌다.
    그 파일들에는 fc_load / Net Load / PRC 가 없는데, concat 후 drop_duplicates(keep='last')
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
    missing = [k for k in required_keys(netload) if _col(cols, *NEEDLES[k]) is None]
    return (not missing), missing


# ---------- 과거 데이터(실측+예보) -> 시간별. 파일 여러 개 자동 병합. 가스 선택 ----------
def load_history(ercot_files, gas, netload="env", verbose=True):
    """과거 시간별 패널 + 가스 일별 시계열.

    netload: 실측 순부하(act_netload)를 무엇으로 볼지. (항목2)
      "env"    ENV Net Load 컬럼을 그대로 사용 (기존 기본값)
      "direct" 부하 실측 − 풍력 − 태양광 으로 직접 계산

    두 방식을 섞으면 안 된다. 예전에 'direct 우선 / ENV fallback' 이던 시절
    2024=ENV, 2025~26=direct 로 연도마다 기준이 갈려 최대 ~17.6GW 방식차가 났다.
    direct 는 부하 실측이 있어야 하는데 기존 소스는 2024년 부하 실측이 100% 결측이라
    그 해가 통째로 빠진다. ERCOT API 로 직접 받으면 해소되는 문제라 선택지로 열어두되,
    바꿀 때는 backtest_walkforward.py 로 전후를 반드시 비교할 것.
    """
    if netload not in NL_NEEDS:
        raise ValueError(f"netload 는 {list(NL_NEEDS)} 중 하나여야 합니다: {netload!r}")
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
    missing = [k for k in required_keys(netload) if pick[k] is None]
    if missing:
        raise ValueError(f"필요 컬럼을 못 찾음(netload={netload}): {missing}. "
                         f"보유 컬럼: {list(raw.columns)[:6]} ...")
    # 선택 컬럼(prc 등)은 없으면 NaN 으로 두고 계속 간다 — 해당 기능만 비활성된다.
    G = lambda k: (pd.to_numeric(raw[pick[k]], errors="coerce") if pick[k] is not None
                   else pd.Series(np.nan, index=raw.index, dtype=float))
    mn = pd.DataFrame({"ts": raw["ts"]}); mn["date"] = mn.ts.dt.normalize()
    mn["DA"] = G("DA"); mn["fc_load"] = G("fc_load"); mn["fc_solar"] = G("fc_solar").fillna(0); mn["fc_wind"] = G("fc_wind")
    mn["RT_LZ"] = G("RT_LZ"); mn["RT_HB"] = G("RT_HB")
    mn["act_load"] = G("act_load"); mn["act_solar"] = G("act_solar"); mn["act_wind"] = G("act_wind")
    mn["env_act_nl"] = G("env_act_nl"); mn["prc"] = G("prc")
    mn["fc_netload"] = mn.fc_load - mn.fc_wind - mn.fc_solar
    mn["ren_share"] = (mn.fc_wind + mn.fc_solar) / mn.fc_load
    # act net load 는 전 기간 '한 가지 기준' 으로만 만든다 (docstring 참조).
    if netload == "direct":
        mn["act_netload"] = mn.act_load - mn.act_wind - mn.act_solar.fillna(0)
    else:
        mn["act_netload"] = mn.env_act_nl
    mn["nfe"] = mn.act_netload - mn.fc_netload
    mn["hou_basis"] = mn.RT_LZ - mn.RT_HB
    if verbose:
        ok = int(mn.act_netload.notna().sum())
        print(f"  순부하 기준: {netload} (유효 {ok:,}/{len(mn):,}행 = {ok/max(len(mn),1)*100:.1f}%)")
        gap = mn[mn.act_netload.isna()]
        if len(gap):
            yrs = gap.ts.dt.year.value_counts().sort_index()
            print("    ! 결측 시간대는 학습에서 빠집니다 —",
                  ", ".join(f"{y}년 {n:,}행" for y, n in yrs.items()))
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


# ---------- 배분 규칙: 세 모델의 판단 → DA 비중 ----------
#   여기가 '유일한' 정의다. forecast_rows() 도 backtest_walkforward.py 도 이 함수를 부른다.
#   (예전엔 백테스트가 같은 식을 따로 복제해 갖고 있어서 한쪽만 고치면 조용히 어긋났다.)
ALLOC_MODES = ("ensemble", "m1_only")
M1_DA_THRESHOLD = 0.50     # m1_only: 모델1 점수가 이 값을 넘는 날만 DA
ALLOC_DEFAULT = "m1_only"  # 기본 규칙. 근거는 combine_votes() 주석의 백테스트 수치.


def combine_votes(v1, v2, v4, mode=ALLOC_DEFAULT, threshold=M1_DA_THRESHOLD):
    """세 모델의 판단 → 기본 DA 비중.

    ensemble  세 표의 단순평균. v4 도입 이후의 기존 방식.
    m1_only   평소 RT(0), 모델1 점수가 문턱을 넘는 날만 DA(1). 조달 실무의
              "평소 실시간, DA 가 싼 날만 전일" 방식을 그대로 옮긴 것. (기본값)

    근거 — walk-forward 531일(2025-01-10~2026-06-24), 100MW, RT 대비 연간 절약:
        ensemble  -18만달러(손해)  |  m1_only  +30만달러(절약)   → 차이 49만달러
        |DART|>=$20 인 날 방향 적중  40.0%  |  60.0%
        1월(한파) 절약                 -7만  |  +166만
    모델2는 가격 '수준', 모델4는 지역 간 가격차를 맞히는 모델이라 "DA 가 쌀까"
    라는 질문에는 간접적이다. 평균을 내면 그 질문에 직접 답하는 모델1의 신호가 희석된다.
    세 표의 상관이 낮다는 사실은 '평균이 이득'을 뜻하지 않는다 — 실현 원가로 재면 반대였다.

    문턱 민감도(오버레이 없이): 0.40/0.45/0.48/0.50 = +25/+29/+29/+30만으로 넓게 평평하고,
    0.52 부터 +7만 → 0.60 에서 -8만으로 꺾인다. 낮은 쪽은 안전하고 높은 쪽이 위험하다.
    "확신이 강한 날만 사자"는 직관이 이 모델에는 통하지 않으므로 문턱을 올리지 말 것.

    한계: 절약 +30만의 95% 구간은 -20만~+88만이고 손해로 끝날 확률이 13% 남아 있다.
    표본 531일 중 |DART|>=$20 인 30일이 절약의 대부분을 만든다.
    """
    if mode == "m1_only":
        return 1.0 if v1 > threshold else 0.0
    if mode != "ensemble":
        raise ValueError(f"alloc 은 {ALLOC_MODES} 중 하나여야 합니다: {mode!r}")
    return clip01((v1 + v2 + v4) / 3.0)


def allocate(v1, v2, v4, *, mode=ALLOC_DEFAULT, threshold=M1_DA_THRESHOLD,
             gate=False, use_gate=False, anom=np.nan, wx=None, overlay=True):
    """투표 → 최종 DA 비중. 반환: (f_da, base_da, 오버레이 사유)"""
    base = combine_votes(v1, v2, v4, mode, threshold)
    f = clip01(max(base, GATE_FLOOR)) if (gate and use_gate) else base
    reason = "-"
    if overlay and wx is not None:
        f, reason = weather_overlay(f, anom, wx["cold"], wx["hot"])
    return clip01(f), base, reason


def regime_from_last(d):
    """마지막 실측일(D0)의 행에서 예보용 regime 피처를 만든다. run() 과 app.py 공용.

    주의 — lag1 계열은 패널의 `*_lag1` 컬럼이 아니라 D0 의 '당일값'을 써야 한다.
    패널에서 basis_lag1[t] = basis_mean[t-1] 이므로, 타깃 D0+1 에 대한 basis_lag1 은
    basis_mean[D0] 이다. `last.basis_lag1` 을 쓰면 basis_mean[D0-1] 이 되어 하루 더 낡는다.
    (예전에 run() 은 낡은 값, app.py 는 올바른 값을 써서 같은 데이터로 다른 배분이 나왔다.)
    반환: forecast_rows() 가 기대하는 regime dict
    """
    last = d.dropna(subset=["da_med"]).iloc[-1]
    f = lambda c: float(last[c])
    return dict(prc_low_r7=f("prc_low_r7"), da_med=f("da_med"),
                basis_lag1=f("basis_mean"), basis_r7=f("basis_r7"),
                prem_rate_r7=f("prem_rate_r7"),
                gas_lag1=f("gas"), gas_r7=f("gas_r7"),
                nfe_lag1=f("nfe"), nfe_r7=f("nfe_r7"),
                DA_r3=f("DA_r3"), DA_r7=f("DA_r7"), DA_r14=f("DA_r14")), last["date"]


def forecast_rows(fc, models, reg, volume_mw, use_gate=False,
                  alloc=ALLOC_DEFAULT, threshold=M1_DA_THRESHOLD, overlay=True):
    """예보 패널 → D+1..D+N 배분 행 리스트. run() 과 API 서버가 공유.

    alloc: 배분 규칙. combine_votes() 참조. 바꿀 때는 backtest_walkforward.py 로 검증할 것.
    """
    f1, mdl1 = models["m1"]; f2, mdl2 = models["m2"]; f4, mdl4 = models["m4"]
    sc = models["scales"]; gt = models["gate"]; wx = models["wx"]
    rows = []
    for h, (_, r) in enumerate(fc.iterrows(), start=1):
        feat = dict(reg)
        # t_anom* 는 M1F 피처다. 여기서 안 넘기면 아래 '누락 피처는 NaN' 에 걸려
        # 학습 때는 쓰고 예측 때는 못 쓰는 상태가 된다(오버레이만 남고 피처는 죽음).
        # HANDOFF 5항 기준 날씨는 오버레이보다 '피처' 쪽 기여가 크다.
        for c in ["fc_load_max", "fc_nl_mean", "fc_nl_max", "ren_mean", "ren_min",
                  "ren_share", "month", "weekend", "t_anom", "t_anom_abs", "t_anom_max"]:
            if c in r:
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
        # === 배분 규칙 적용 (allocate() 가 유일한 정의) ===
        # Hard Gate 는 위험 시에만 DA 하한 강제(비대칭). 기본 use_gate=False 라 정보 표기만 한다
        #   — 백테스트상 현행 게이트는 이 시장(한파에 DA 가 오히려 폭등)에서 역효과였다.
        anom = float(r["t_anom"]) if "t_anom" in r and pd.notna(r["t_anom"]) else np.nan
        gate_applied = gate and use_gate
        f_da, base_da, wx_reason = allocate(
            v1, v2, v4, mode=alloc, threshold=threshold, gate=gate, use_gate=use_gate,
            anom=anom, wx=wx, overlay=overlay)
        rows.append(dict(
            horizon=f"D+{h}", date=r["date"].date(), alloc_mode=alloc,
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




def run(folder, ercot, gas, forecast, out, volume_mw, use_gate=False, netload="env",
        alloc=ALLOC_DEFAULT, threshold=M1_DA_THRESHOLD, overlay=True):
    csvs = list_csvs(folder)
    forecast = forecast or pick_csv(csvs, ["forecast"])

    if ercot:
        ercot_files = [ercot]
    else:
        ercot_files = []
        for c in csvs:
            if c == forecast:
                continue
            ok, missing = is_ercot_history(c, netload)
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
    mn, gas_df = load_history(ercot_files, gas, netload)
    d = daily_panel(mn, gas_df, wxdf)
    self_wx = wxdf
    models = train_models(d)
    acc = historical_accuracy(d)
    print("=== 모델 과거 정확도 (신뢰도 판단용) ===")
    for k, v in acc.items(): print(f"  {k:32s}: {v:.3f}")

    # 결정시점(D0) = 마지막 실측일. 그 시점의 최근 regime 값 사용.
    reg, d0 = regime_from_last(d)
    print(f"\n결정시점 D0 = {d0.date()} | 최근 reserve regime prc_low_r7 = {reg['prc_low_r7']:.0f} MW")
    print(f"M3 Hard Gate: {'ON' if use_gate else 'OFF(기본)'}  "
          f"{'' if use_gate else '— 게이트 조건은 정보로만 표기, 배분 미적용'}")
    print("배분 규칙:", "세 모델 평균(ensemble)" if alloc == "ensemble" else
          f"평소 RT, 모델1 > {threshold:.2f} 인 날만 DA (m1_only)",
          "| 날씨 오버레이", "ON" if overlay else "OFF")

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

    rows = forecast_rows(fc, models, reg, volume_mw, use_gate, alloc, threshold, overlay)
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
    ap.add_argument("--netload", choices=["env", "direct"], default="env",
                    help="실측 순부하 산출: env=ENV Net Load 컬럼, "
                         "direct=부하-풍력-태양광 직접 계산. 바꾸면 반드시 백테스트로 전후 비교할 것.")
    ap.add_argument("--alloc", choices=list(ALLOC_MODES), default=ALLOC_DEFAULT,
                    help="배분 규칙. ensemble=세 모델 평균(기존), "
                         "m1_only=평소 RT + 모델1 신호일만 DA. combine_votes() 주석 참조.")
    ap.add_argument("--threshold", type=float, default=M1_DA_THRESHOLD,
                    help="m1_only 에서 DA 로 전환할 모델1 점수 문턱 (기본 0.50)")
    ap.add_argument("--no_overlay", action="store_true",
                    help="날씨 레짐 오버레이 비활성")
    a = ap.parse_args()
    run(a.folder, a.ercot, a.gas, a.forecast, a.out, a.volume_mw, a.use_gate, a.netload,
        a.alloc, a.threshold, not a.no_overlay)
