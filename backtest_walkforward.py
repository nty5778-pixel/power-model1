"""walk-forward 백테스트 — 배분 규칙의 실현 원가를 과거 데이터로 채점한다.

무엇을 하는가
  과거의 어느 날로 돌아가 "그 날까지 알 수 있었던 정보만" 으로 모델을 학습하고, 다음 날
  DA/RT 비중을 정한다. 그리고 실제 가격으로 채점한다. 이걸 하루씩 앞으로 밀며 반복한다.
  학습에 쓰지 않은 날로만 채점하므로, "이미 답을 본 채로 맞혔다" 가 되지 않는다.

왜 필요한가
  HANDOFF §10 = "설계 논쟁은 백테스트로 결판낸다". 그런데 그 백테스트 코드가 레포에 없었다.
  이 파일이 그 기준선을 만든다. 배분 규칙을 바꾸자는 제안은 전부 여기를 통과해야 한다.

사용법
  python backtest_walkforward.py --folder ./data
  python backtest_walkforward.py --folder ./data --train_days 365 --retrain_every 30

출력
  화면: 전략별 실현원가/변동성/적중률 표 (전체 / 1월 / 1월제외)
  파일: backtest_daily.csv (하루 단위 원장), backtest_summary.csv (요약)
"""
import argparse, os, sys
import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import run_models_d1_d4_v4_weather as M


# ---------- 입력 파일 고르기 ----------
def find_inputs(folder):
    """ERCOT 과거파일은 'DA LMP 보유' 가 아니라 '필요 컬럼 전부 보유' 로 고른다.

    Congesiton_*.csv 도 DA LMP 컬럼을 갖고 있어서, 기존 run() 의 has_da_lmp() 판별로는
    과거데이터로 오인된다. 그대로 concat 되면 같은 타임스탬프에서 정상 행을 덮어버려
    fc_load / ENV Net Load / PRC 가 통째로 NaN 이 된다(조용한 오염). 여기서는 그걸 막는다.
    """
    csvs = M.list_csvs(folder)
    ercot = []
    for f in csvs:
        ok, missing = M.is_ercot_history(f)
        if ok:
            ercot.append(f)
        elif missing:
            print(f"[제외] {os.path.basename(f)} — DA LMP 는 있으나 {missing} 없음 (과거데이터 아님)")
    if not ercot:
        raise SystemExit(f"[오류] ERCOT 과거 데이터 CSV 를 찾지 못했습니다: {os.path.abspath(folder)}")
    gas = M.pick_csv(csvs, ["katy", "gd_", "platts", "henry"], exclude=tuple(ercot))
    wx = M.load_weather([c for c in csvs if c not in ercot])
    return ercot, gas, wx


# ---------- 현행 배분 규칙을 패널 1행에 적용 ----------
def allocate_current(row, models, use_gate=False):
    """forecast_rows() 와 동일한 결합식. 입력만 예보프레임 대신 패널 1행."""
    f1, mdl1 = models["m1"]; f2, mdl2 = models["m2"]; f4, mdl4 = models["m4"]
    sc, gt, wx = models["scales"], models["gate"], models["wx"]
    X = row.to_frame().T
    for c in set(f1 + f2 + f4):
        if c not in X.columns:
            X[c] = np.nan
    X = X.apply(pd.to_numeric, errors="coerce")

    v1 = M.clip01(mdl1.predict_proba(X[f1])[0, 1]) if mdl1 is not None else 0.5
    m2 = float(mdl2.predict(X[f2])[0])
    damed = float(row["da_med"])
    v2 = 1.0 - M.sigmoid(((m2 - damed) - sc["dev_c"]) / sc["dev_s"])
    v4 = M.clip01(float(mdl4.predict_proba(X[f4])[0, 1]))
    base = M.clip01((v1 + v2 + v4) / 3.0)

    gate = ((row["prc_low_r7"] < gt["prc_danger"]) or (row["fc_nl_max"] > gt["nl_high"])
            or (row["ren_min"] < gt["wind_low"]))
    f_da = M.clip01(max(base, M.GATE_FLOOR)) if (gate and use_gate) else base

    anom = float(row["t_anom"]) if pd.notna(row.get("t_anom")) else np.nan
    f_da, wx_reason = M.weather_overlay(f_da, anom, wx["cold"], wx["hot"])
    return dict(v1=v1, v2=v2, v4=v4, base_DA=base, DA_fraction=M.clip01(f_da),
                wx_overlay=wx_reason, gate_signal=bool(gate))


# ---------- walk-forward ----------
def walk_forward(d, train_days, retrain_every, use_gate=False):
    d = d.dropna(subset=["DA_avg", "RT_avg", "da_med"]).reset_index(drop=True)
    n = len(d)
    if n <= train_days + 1:
        raise SystemExit(f"[오류] 데이터 {n}일 < 학습기간 {train_days}일. --train_days 를 줄이세요.")

    out, folds = [], 0
    start = train_days
    while start < n:
        stop = min(start + retrain_every, n)
        tr = d.iloc[:start]                      # 확장창: 테스트일 이전 전부
        models = M.train_models(tr)
        folds += 1
        print(f"  fold {folds:>2}: 학습 {tr.date.min().date()}~{tr.date.max().date()} "
              f"({len(tr)}일) → 채점 {d.date.iloc[start].date()}~{d.date.iloc[stop-1].date()}")
        for i in range(start, stop):
            row = d.iloc[i]
            a = allocate_current(row, models, use_gate)
            a.update(date=row["date"], DA_avg=row["DA_avg"], RT_avg=row["RT_avg"],
                     DART=row["RT_avg"] - row["DA_avg"], t_anom=row.get("t_anom"))
            out.append(a)
        start = stop
    return pd.DataFrame(out)


# ---------- 채점 ----------
def score(r, f, label):
    cost = f * r.DA_avg.values + (1 - f) * r.RT_avg.values
    pred_da = np.asarray(f) > 0.5
    act_da = r.DART.values > 0
    hit = pred_da == act_da
    big = np.abs(r.DART.values) >= 5
    return dict(
        전략=label, 일수=len(r),
        실현원가=round(float(cost.mean()), 2),
        변동성=round(float(cost.std(ddof=1)), 2),
        vs_RT=round(float(cost.mean() - r.RT_avg.mean()), 3),
        vs_DA=round(float(cost.mean() - r.DA_avg.mean()), 3),
        평균DA비중=round(float(np.mean(f)), 3),
        적중률=round(float(hit.mean() * 100), 1),
        적중률_5불이상=(round(float(hit[big].mean() * 100), 1) if big.sum() >= 5 else None),
        n_5불이상=int(big.sum()))


def table(r, label):
    rows = [
        score(r, r.DA_fraction.values, "현행 모델 (투표 평균)"),
        score(r, np.zeros(len(r)), "100% RT"),
        score(r, np.ones(len(r)), "100% DA"),
        score(r, np.full(len(r), 0.5), "고정 50/50"),
    ]
    for f in (0.2, 0.4, 0.6, 0.8):
        rows.append(score(r, np.full(len(r), f), f"고정 {int(f*100)}% DA"))
    t = pd.DataFrame(rows)
    print(f"\n{'='*100}\n[{label}]  {r.date.min().date()} ~ {r.date.max().date()}\n{'='*100}")
    print(t.to_string(index=False))
    t.insert(0, "구간", label)
    return t


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--folder", default="./data")
    ap.add_argument("--out", default=".")
    ap.add_argument("--train_days", type=int, default=365, help="최초 학습기간(일)")
    ap.add_argument("--retrain_every", type=int, default=30, help="재학습 주기(일)")
    ap.add_argument("--use_gate", action="store_true")
    a = ap.parse_args()

    ercot, gas, wx = find_inputs(a.folder)
    print("ERCOT :", ", ".join(os.path.basename(f) for f in ercot))
    print("가스   :", os.path.basename(gas) if gas else "없음")
    print("날씨   :", f"{len(wx)}일" if wx is not None else "없음")

    mn, gas_df = M.load_history(ercot, gas)
    d = M.daily_panel(mn, gas_df, wx)
    print(f"\n일별 패널 {len(d)}일 | 학습 {a.train_days}일, {a.retrain_every}일마다 재학습")

    r = walk_forward(d, a.train_days, a.retrain_every, a.use_gate)
    r["month"] = pd.to_datetime(r.date).dt.month

    parts = [table(r, "전체")]
    jan, non = r[r.month == 1], r[r.month != 1]
    if len(jan) >= 20:
        parts.append(table(jan, "1월만 (한파 레짐)"))
    if len(non) >= 20:
        parts.append(table(non, "1월 제외"))

    dp = os.path.join(a.out, "backtest_daily.csv")
    sp = os.path.join(a.out, "backtest_summary.csv")
    r.round(4).to_csv(dp, index=False)
    pd.concat(parts, ignore_index=True).to_csv(sp, index=False)
    print(f"\n하루단위 원장 -> {dp}\n요약        -> {sp}")


if __name__ == "__main__":
    pd.set_option("display.width", 220, "display.max_columns", 40)
    main()
