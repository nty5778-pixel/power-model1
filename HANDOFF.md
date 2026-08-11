# ERCOT DA/RT 조달 모델 — Claude Code 핸드오프

> 이 문서는 **새 세션에서 작업을 이어받기 위한 컨텍스트**다.
> 모델 스펙 상세는 `MODEL_CONFIG.md`, 분석 근거·시사점은 `REVIEW_HISTORY.md` 참조.

---

## 0. 30초 요약

LZ_HOUSTON 전력 조달에서 **DA(전일) / RT(실시간) 매수 비중**을 D+1~D+4로 산출하는 모델.
XGBoost 3개 + 규칙 1개 + 날씨 오버레이. Render(FastAPI) 배포 → n8n 오케스트레이션 → Google Sheets 기록.

**가장 중요한 전제:** 이 모델의 방향 예측력은 **AUC ≈ 0.52**, 사실상 동전 던지기다.
DART 스프레드는 본질적으로 *예측오차(forecast error)* 변수이고, 레벨 변수로는 R² ≈ 1%밖에 설명이 안 된다.
**값어치는 "알파"가 아니라 "분산 감소 + 한쪽 쏠림 방지"에 있다.** 이 전제를 잊고 성능 개선을 시도하면
반드시 과적합으로 끝난다 — 실제로 이 프로젝트에서 그럴듯한 가설이 백테스트로 4번 반증됐다(§5).

---

## 1. 레포 구조 (권장)

```
ercot-dart/
├── run_models_d1_d4_v4_weather.py   # 모델 본체 (CLI + 라이브러리 겸용)
├── app.py                           # FastAPI 래퍼 (Render 배포용)
├── requirements.txt
├── render.yaml
├── data/                            # 학습 데이터 (레포에 커밋 — Render 디스크는 ephemeral)
│   ├── 2024_Historical_Data.csv
│   ├── 2025_Historical_Data.csv
│   ├── 2026_Historical_Data.csv
│   ├── texas_open_meteo_historical_2024_2026-06-30.csv
│   ├── texas_open_meteo_daily.csv   # 예보 (16일)
│   └── GD_Katy.csv                  # 가스 (모델 미사용, 진단용)
├── n8n/
│   ├── n8n_1_daily_predict.json
│   └── n8n_2_backfill_lookback.json
└── docs/
    ├── MODEL_CONFIG.md
    ├── REVIEW_HISTORY.md
    └── HANDOFF.md                   # 이 문서
```

**미사용 데이터:** `Congesiton_*.csv`(존별 부하/지역별 풍력·태양광 5개 파일)는 분석·가설검증에만
썼고 프로덕션 코드에 반영돼 있지 않다. 필요 시 §6-2 참조.

---

## 2. 실행

```bash
pip install -r requirements.txt

# CLI (폴더 내 CSV 자동 탐색)
python run_models_d1_d4_v4_weather.py --folder ./data --volume_mw 100
# → allocation_review.csv 생성 + stdout 표 출력

# Hard Gate 실험 활성화 (기본 OFF, 백테스트상 역효과 — §5 참조)
python run_models_d1_d4_v4_weather.py --folder ./data --use_gate

# API 로컬 실행
DATA_DIR=./data uvicorn app:app --reload --port 8000
curl localhost:8000/health
```

**필수 입력:** `forecast_input.csv` (없으면 학습·정확도 출력 후 종료)

| 컬럼 | 타입 |
|---|---|
| `timestamp` | ISO8601 시간별 |
| `fc_load_mw` | float |
| `fc_wind_mw` | float |
| `fc_solar_mw` | float |
| `gas_price` | (선택) 있으면 `gas_fc`로 사용 |

---

## 3. 데이터 계약 (중요)

### 3.1 ERCOT CSV 컬럼 해석 — 이름 부분매칭

컬럼명이 길고 파일마다 순서가 달라, **하드코딩이 아니라 `NEEDLES` 부분매칭**으로 찾는다
(`_col(cols, *needles)` — 모든 needle이 소문자 포함되면 매치).

```python
NEEDLES = dict(
    DA=("DA LMP",), RT_LZ=("RT SPP", "LZ_HOUSTON"), RT_HB=("RT SPP", "HB_BUSAVG"),
    fc_load=("Forecast - Load", "Prior Day"), fc_solar=("Solar", "Forecast"),
    fc_wind=("Wind", "Forecast"), act_load=("ISO: Actual - Load",),
    act_solar=("Solar", "Actual - Generation"), act_wind=("Wind", "Actual - Generation"),
    env_act_nl=("ENV: Actual - Net Load",), prc=("PRC",))
```

컬럼을 못 찾으면 `ValueError`로 즉시 실패한다(조용한 NaN 전파 방지). 새 데이터 소스를 붙일 때는
NEEDLES만 수정하면 된다.

**파일 판별 규칙**
- ERCOT 과거 데이터 = `DA LMP` 컬럼 보유 CSV (여러 개면 자동 concat)
- 날씨 = `temp_mean_f` + `date` 컬럼 보유 CSV (과거+예보 자동 합침)
- 가스 = 파일명에 `katy|gd_|platts|henry` (헤더 2줄 구조, `skiprows=1`)

### 3.2 타임스탬프
`Timestamp`의 tz 오프셋을 **정규식으로 제거**하고 wall-clock으로 파싱한다
(`[+-]\d{2}:\d{2}$` → ""). DST 때문에 파일마다 오프셋이 −06:00/−05:00으로 다르기 때문.
UTC 변환하면 시간대별 분석이 어긋난다.

### 3.3 net load 정의 — ENV 단일 기준 (변경 금지)
```python
mn["act_netload"] = mn.env_act_nl     # direct(L−W−S) 사용 금지
```
2024는 Actual Load가 **100% 결측**이라 direct 계산이 불가능하다. 예전 "direct 우선 / ENV fallback"
로직은 연도별로 계산 방식이 갈려(2024=ENV, 2025~26=direct) 방식차가 최대 **17.6 GW**까지 났다.
ENV 통일로 학습 행이 12,790 → 21,719 (+70%) 늘었다.

### 3.4 누수 방지 규칙
- 모든 lag/rolling은 `shift(1)` 이후 계산
- vote 정규화 스케일(`dev_c`, `dev_s`), 오버레이 임계(`wx.cold/hot`), 게이트 임계는
  **학습 구간 분포에서만** 산출 — 하드코딩 금지
- ⚠️ `gas_lag1`은 엄밀히는 `lag2`여야 한다(DA 마감 10:00 CT 시점에 D-1 종가는 미확정).
  현재 미수정 — 가스 피처가 모델에서 기각됐으므로(§5) 영향 없으나, 재도입 시 반드시 고칠 것.

---

## 4. 아키텍처 요약

```
v1 = M1.predict_proba()                       # P(DA가 더 쌈) 직접 분류
v2 = 1 − sigmoid((DA예측 − 30일중앙값 − dev_c) / dev_s)
v4 = M4.predict_proba()                       # P(Houston basis > $2)
base_DA = mean(v1, v2, v4)

if use_gate and gate_signal:                  # 기본 OFF
    f_da = max(base_DA, GATE_FLOOR=0.75)

f_da = weather_overlay(f_da, t_anom, cold_t, hot_t)   # 극단 기온일에만
```

| 모델 | 알고리즘 | 타깃 |
|---|---|---|
| M1 | XGBClassifier | `da_cheaper = (RT − DA > 0)`, dead-band $3 + 비용가중 |
| M2 | XGBRegressor | 일평균 DA 가격 |
| M3 | **규칙 (ML 아님)** | reserve/수급 스트레스 게이트 — **기본 OFF** |
| M4 | XGBClassifier | `basis > $2` |

**날씨 오버레이 (비대칭)**
- 이상한파(하위 5%) → **RT로** 기울임 (한파엔 DA가 폭등: 실적 DA $76 vs RT $48)
- 이상고온(상위 5%) → **DA로** 기울임 (폭염엔 RT가 튐: DART +$17)
- 평년 근처(~87%) → 미개입
- `WX_COLD_FLOOR = 0.30`: 한파에도 DA를 이 아래로 내리지 않음. **최적화 대상이 아니라 tail 리스크 정책**

**핵심 상수** (`run_models_d1_d4_v4_weather.py` 상단)
`DEADBAND=3.0`, `CW_CAP=100.0`, `GATE_FLOOR=0.75`, `WX_COLD_Q=0.05`, `WX_HOT_Q=0.95`,
`WX_SPAN_F=5.0`, `WX_COLD_LEAN=0.30`, `WX_HOT_LEAN=0.30`, `WX_COLD_FLOOR=0.30`

---

## 5. 결정 로그 — 반증된 가설 (재시도 금지)

> 아래는 전부 **그럴듯했지만 walk-forward 백테스트가 기각**한 것들이다.
> 다시 시도하기 전에 반드시 이 근거를 넘어설 새 증거가 있어야 한다.

| # | 가설 | 결과 | 근거 |
|---|---|---|---|
| 1 | M1 학습을 2025+로 제한 (레짐 적합성) | **기각** | 원가 +$1.19 → +$1.81 악화. 오래된 레짐이라도 표본이 많은 쪽이 유리 |
| 2 | vote를 분위수(rank) 정규화 | **기각** | 전체 81%가 ±$10 이내 노이즈인데, rank는 그 구간까지 펴서 "무의미한 날에 확신 투표" 유발. sigmoid 채택 |
| 3 | "위험 시 DA 강제" Hard Gate | **기각** | 원가 +$0.2 → +$1.2 악화. ERCOT는 한파에 **DA가 폭등**하므로 가장 비싼 쪽을 강제 매수하는 꼴 |
| 4 | 가스 피처 / implied heat rate 추가 | **기각** | M2 MAE $8.76 → $9.04 악화, 앙상블 원가 ±$0.03(노이즈). 이유: D-2 종가는 한파 스파이크를 못 보고, 평상시엔 `DA_r3/r7`이 이미 가스 정보를 품음 |
| 5 | "RT 스파이크 신호 시 DA 매수" | **구조적 불가** | DA는 전일 10:00 CT 마감. 스파이크를 보고 나서 DA를 살 수 없음 — 반응형이 아니라 예측형만 가능 |
| 6 | 일 단위 저풍량 임계로 DA 헤지 | **기각** | 사후(hindsight) 정보로도 절감 상한 $0.56, 과헤지 시 손해 |

**채택된 것**
- M1 타깃을 `P(DA>RT)` 직접 분류로 (원가 −$0.29, "94% DA 쏠림" 병리 제거)
- NFE ENV 단일화 + P1~P99 winsorize
- 날씨 anomaly를 M1 피처로 (1월 구간 −$1.81, 변동성 38.4→32.5) — **오버레이보다 피처가 주역**

---

## 6. 현재 성능 (walk-forward OOS, 662일, 부하가중 $/MWh)

| 구간 | 100% RT | 100% DA | 50/50 | **v4** |
|---|---|---|---|---|
| 전체 | $32.29 (std 17.0) | $34.20 (std 31.3) | $33.24 | **$32.43 (std 15.5)** |
| 1월 제외 | $31.80 | $32.15 | $31.98 | $31.89 |
| 1월만 | $37.16 | **$54.21 (std 95.2)** | $45.69 | **$37.75** |

**2026 방향 적중률**

| \|DART\| | 적중 | 기준선 대비 |
|---|---|---|
| ≥$0 | 54% | +1p |
| **≥$5** | **64%** | **+12p** |
| ≥$10 | 66% | +9p |
| ≥$20 | 40% ⚠️ | −30p |

≥$20 붕괴는 `WX_COLD_FLOOR`의 **의도된 대가**다(극단 한파에 RT로 완전히 못 기울게 막음).
방향 정확도와 tail 안전이 상충하는 지점.

**해석:** RT 대비 +$0.14(약간 비쌈), DA 대비 −$1.76(크게 쌈), 변동성은 최저.
성과 판단은 **현재 기본 조달 방식이 RT냐 DA냐**에 달렸다 — 이건 아직 확인이 필요한 미해결 항목.

---

## 7. 알려진 이슈 / 정리 필요

| 위치 | 내용 | 우선순위 |
|---|---|---|
| `run()` | `f1, mdl1 = models["m1"] ...` 줄이 `forecast_rows` 추출 후 **dead code**로 남음 | 낮음(정리) |
| `daily_panel` | `gas_lag1` → `gas_lag2`로 교정 필요(게이트 규칙) | 중간(가스 재도입 시 필수) |
| `forecast_daily` | `agg` 지역변수 미사용 | 낮음 |
| `app.py` | 학습이 요청당 발생(10~30초), 날짜 단위 메모리 캐시만 존재 → 콜드스타트 시 무효 | 중간 |
| n8n Sheets `update` | 행 수천 개 넘으면 전체 스캔으로 느려짐 → 연 단위 시트 분할 또는 DB 이전 | 중간 |
| 계절 기준선 | 2.5년치로 산출(정석은 30년 NOAA normal) | 낮음 |

---

## 8. 한계 — 코드 수정으로 해결되지 않는 것

1. **표본 의존성 (최대 취약점).** DA 대비 우위 −$1.76의 대부분이 1월 62일, 사실상 **2026-01 한파
   며칠**에서 나온다. 2021 Uri(RT $9,000 폭등), 2023 Elliott을 넣으면 **"한파→RT" 방향이 반대로
   작동할 수 있다.** `WX_COLD_FLOOR`를 남긴 이유가 정확히 이것이다.
2. **여름 이상고온 표본 6일.** 폭염 오버레이(+DA)는 방향은 그럴듯하나 통계 근거가 얇다.
3. **forward regime 부재.** M3·M4의 PRC·basis는 D0 값을 D+1~D+4에 복사한다. 발전기 트립,
   한파 급습 같은 미래 surprise를 반영 못 한다.
4. **원가차 $0.1~0.3은 노이즈 범위.** 단일 경로 walk-forward이고 bootstrap CI 미수행.
5. **RT basis와 DART의 상관(+0.18, 낮 +0.75)은 내생성.** 둘 다 `RT_LZ`를 공유하므로 예측 변수로 쓰면 안 된다.

---

## 9. 다음 작업 후보 (우선순위)

1. **2022~2023 데이터 추가** — §8-1, §8-2를 직접 해소하는 유일한 방법. 최우선.
2. **`WX_COLD_FLOOR` 민감도 표** — 0.30 / 0.15 / 0 에서 ≥$20 적중률 vs tail 노출 트레이드오프 정량화.
   *리스크 정책 결정이므로 코드가 아니라 사람이 판단할 자료를 만드는 작업.*
3. **bootstrap 신뢰구간** — 원가차·변동성 개선의 통계적 유의성 확정.
4. **congestion 데이터 통합** — 존별 부하·지역별 풍력으로 M4(basis) 재학습.
   (구조: 부하는 동부 83%, 풍력은 서부 69% → west→east 혼잡이 basis의 원천)
5. **EEA / 강제정지 forward** — 극단 레짐 조기 신호.
6. **파이프라인 배선** — `ERCOT_FORECAST_URL`, `ERCOT_SETTLE_URL`을 사내 소스에 연결.
   ⚠️ 정산 소스는 반드시 **authoritative**여야 한다. TimeSeriesExport 파생값은 실제 정산치와
   괴리가 확인됐고, 쓰면 look-back 지표 전체가 오염된다.

---

## 10. 작업 규칙 (이 프로젝트의 컨벤션)

- **설계 논쟁은 백테스트로 결판낸다.** 직관이 4번 반증됐다(§5). 변경 제안은 동일 OOS 비교 수치와 함께.
- **평가는 AUC가 아니라 비용으로.** $50 날과 $0.5 날을 동등 취급하는 지표는 이 문제에 부적합.
  실현 원가 + regret + \|DART\|≥$5 적중률을 본다.
- **파라미터 자동 튜닝 금지.** AUC 0.52 모델에서 30~60일 성과는 거의 전부 노이즈다.
  look-back은 모니터링·축적용이고, 변경은 분기별 전체 walk-forward 후 사람이 결정한다.
- **basis는 평균이 아니라 중앙값으로.** 한파월 평균은 극단 혼잡 스파이크가 지배한다
  (1월 평균 −$8.4 vs 중앙값 −$0.2).
- **1월(한파)은 항상 별도 레짐으로 분리해서 본다.** 통합 평균은 오독을 부른다.
- 실패는 조용한 NaN보다 예외가 낫다(컬럼 미발견 시 `ValueError`, 예보 24행 미만 시 n8n에서 throw).
