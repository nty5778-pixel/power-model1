# ERCOT DA/RT 배분 파이프라인 — Render + n8n + Google Sheets + Claude

```
                    ┌─────────────── n8n (오케스트레이터) ───────────────┐
                    │                                                   │
  Open-Meteo ──┐    │  ① 08:00 CT  예보수집 → /predict → predictions시트 │
  ERCOT 예보 ──┴───▶│                              └→ Claude → analysis  │
                    │                                                   │
  ERCOT 정산 ──────▶│  ② 11:00 CT  실적 backfill → predictions 갱신      │
                    │                                                   │
                    │  ③ 월 12:00  /score → 드리프트 → Claude → lookback │
                    └───────────────────────────────────────────────────┘
                                        ▲
                                        │ HTTPS
                              ┌─────────┴──────────┐
                              │ Render Web Service │
                              │  FastAPI + XGBoost │
                              │  /predict /score   │
                              └────────────────────┘
```

---

## 1. Render 배포

```
repo/
├── app.py                            FastAPI 래퍼
├── run_models_d1_d4_v4_weather.py    모델 본체 (CLI + 라이브러리 겸용)
├── backtest_walkforward.py           walk-forward 검증 — 배분 규칙 변경은 전부 여기를 통과한다
├── requirements.txt
├── render.yaml
├── DEPLOY.md                         배포 절차 (사람이 직접 해야 하는 것만)
├── n8n_1_daily_predict.json
├── n8n_2_backfill_lookback.json
└── data/                             과거 CSV (레포에 커밋)
    ├── 2024_Historical_Data.csv / 2025_ / 2026_
    ├── texas_weather_daily.csv       일별 4-city 평균 기온
    └── GD_Katy.csv                   가스 (모델 미사용, 진단용)
```

**환경변수 (Render 대시보드)**

| 키 | 값 | 비고 |
|---|---|---|
| `API_KEY` | 임의 난수 | n8n 의 `MODEL_API_KEY` 와 동일하게 |
| `DATA_DIR` | `/opt/render/project/src/data` | `render.yaml` 에 이미 있음 |
| `PYTHON_VERSION` | `3.11` | 〃 |
| `ALLOC_MODE` | (선택) 기본 `m1_only` | 배분 규칙. `m1_only` / `ensemble` |
| `M1_DA_THRESHOLD` | (선택) 기본 `0.5` | m1_only 의 DA 전환 문턱. **올리지 말 것 — §5** |

**엔드포인트**

| | 용도 |
|---|---|
| `GET /health` | 헬스체크 |
| `POST /predict` | 예보(+기온) → D+1~D+4 배분 |
| `POST /score` | 예측+실적 → 실현원가·적중 지표 |

`POST /predict` 는 `forecast[]` 와 함께 **`weather[{date,temp_mean_f,temp_max_f}]` 를 받는다.**
예보 대상은 언제나 미래라 과거 날씨 CSV 로는 t_anom 을 만들 수 없다 — 이 필드가 없으면
M1 의 날씨 피처와 레짐 오버레이가 통째로 죽는다. 응답의 `weather_missing_days` 가 0 이 아니면
n8n 이 기록하지 않고 중단한다. 요청 단위로 `alloc` / `threshold` 를 덮어쓸 수도 있다(A/B 용).

### 배포 시 주의

- **무료 티어는 쓰지 말 것.** 15분 idle 후 spin-down → 콜드스타트가 붙어 요청이 2~4분 걸린다.
  Starter($7/mo) 이상 권장. 무료로 버티려면 n8n 에 5분 간격 `/health` ping 워크플로를 추가.
- **디스크는 ephemeral.** 런타임에 쓴 파일은 재배포/재시작 시 사라진다. 학습 데이터는 레포에 커밋하거나
  외부 스토리지에서 받아야 한다. 상태는 전부 Google Sheets 에 둔다.
- **학습이 요청당 발생**한다(~900행). 로컬 실측 약 1초이고 Render 는 더 느리지만
  분 단위로 걸릴 일은 없다. `app.py` 가 날짜 단위로 메모리 캐시하므로 같은 날 재호출은
  즉시 응답하고, 콜드스타트 시에만 캐시가 날아간다. n8n 타임아웃 240초는 그대로 두면 된다.
- **입력 CSV 판별은 컬럼으로 한다.** `is_ercot_history()` 가 NEEDLES 컬럼을 전부 갖춘
  파일만 과거데이터로 인정한다. `Congesiton_*.csv` 도 `DA LMP` 를 갖고 있어서 예전 판별
  (`DA LMP` 보유 여부)로는 함께 로드됐고, concat 후 정상 행을 덮어써 `fc_load`/ENV Net
  Load/PRC 가 조용히 NaN 이 됐다. `data/` 에 파일을 추가할 때 이 규칙을 기억할 것.
- 데이터 갱신은 별도 절차다. 월 1회 `data/` 를 최신 CSV 로 커밋 → 자동 재배포되는 흐름을 권장.

---

## 2. Google Sheets 스키마

같은 스프레드시트에 시트 3개. n8n 환경변수 `SHEET_ID` 로 참조.

### `predictions` — 예측 1행 + 사후 실적 backfill

| 컬럼 | 시점 | 비고 |
|---|---|---|
| `run_id`, `generated_at`, `model_version`, `d0_last_actual` | 예측 시 | `run_id`+`horizon` 이 복합키 |
| **`alloc_mode`, `m1_threshold`** | 예측 시 | **어떤 규칙으로 낸 행인지. 규칙 교체 전후를 섞지 않기 위해 필수** |
| `horizon`, `target_date` | 예측 시 | D+1~D+4 |
| `M1_P_DA_cheaper`, `M2_DA_pred_USD`, `M3_gate_signal`, `M3_gate_reason`, `M4_premium_prob` | 예측 시 | 모델별 원값 |
| `vote_M1_DA`, `vote_M2_DA`, `vote_M4_DA`, `base_DA` | 예측 시 | 진단용. `m1_only` 에서는 M2·M4 가 배분에 관여하지 않지만 값은 계속 남긴다 |
| `WX_t_anom_F`, `WX_overlay` | 예측 시 | 날씨 오버레이 발동 여부 |
| `DA_fraction`, `RT_fraction`, `DA_MW`, `RT_MW` | 예측 시 | 최종 배분 |
| `DA_actual`, `RT_actual`, `DART_actual`, `blended_cost`, `hit` | **backfill** | ②에서 채움 |

⚠️ `m1_only` 에서 `DA_fraction` 은 보통 **0 아니면 1** 이다("평소 RT, 신호일만 전량 DA").
단 **이상기온일에는 오버레이가 최대 0.30 만큼 밀어 중간값이 나온다** — 이때는 같은 행의
`WX_overlay` 에 `hot+9F` 같은 값이 찍힌다. `WX_overlay` 가 비어 있는데 중간값이면
`ALLOC_MODE` 가 `ensemble` 로 돌고 있다는 뜻이다. 판별표는 DEPLOY.md §7-1.

### `analysis` — Claude 일일 해설
`run_id`, `logged_at`, `kind`, `model`, `target_dates`, `mean_DA_fraction`, `commentary`, `input_tokens`, `output_tokens`

### `lookback` — 주간 성과 누적
`logged_at`, `kind`, `window_all_days`, `cost_blended`, `cost_all_rt`, `cost_all_da`, `vs_rt`, `vs_da`,
`cost_std`, `hit_rate_all`, `hit_rate_big5`, `n_big5`, **`hit_rate_big20`, `n_big20`, `vs_rt_big20`**,
`mean_da_fraction`, `recent30_hit`, `recent30_vs_rt`, `alert`, `commentary`

**주간 점검은 `hit_rate_big20` 하나로 한다.** |DART| ≥ $20 인 날의 방향 적중률이며, 실현 이익의
대부분이 이 날들에서 나온다(walk-forward 531일 중 30일에서 RT 대비 $8.77/MWh 우위, 나머지
501일은 오히려 소폭 열위). 50% 미만이면 `alert` 에 `[중요]` 가 찍힌다. `n_big20 < 10` 이면 미판정.

⚠️ `hit_rate_all` 로 판단하지 말 것 — **항상 RT만 사는 쪽이 이 지표에서 늘 이긴다**(61.2% vs 55.4%).
$0.5 차이 나는 날과 $50 차이 나는 날을 동일하게 한 번으로 세기 때문이다. 실제 원가는 반대다.

---

## 3. n8n 설정

**워크플로 2개 import**
- `n8n_1_daily_predict.json` — 예측·기록·Claude 해설
- `n8n_2_backfill_lookback.json` — 실적 backfill(일간) + look-back(주간)

**환경변수**

| 키 | 설명 |
|---|---|
| `RENDER_URL` | `https://ercot-dart-model.onrender.com` |
| `MODEL_API_KEY` | Render `API_KEY` 와 동일 |
| `ANTHROPIC_API_KEY` | Claude API 키 |
| `SHEET_ID` | 스프레드시트 ID |
| `ERCOT_FORECAST_URL` | 시간별 load/wind/solar 예보 소스 |
| `ERCOT_SETTLE_URL` | **정산 확정** DA/RT 소스 |
| `VOLUME_MW` | 기본 100 |
| `CLAUDE_MODEL` | (선택) 비우면 `claude-sonnet-5` |

날씨는 별도 환경변수가 없다 — 워크플로 ①이 Open-Meteo 를 직접 호출해
4개 도시 시간별 기온을 **도시별 일평균/일최고 → 도시 간 평균** 순으로 접어
(학습 데이터의 4-city average 와 같은 방식) `/predict` 의 `weather` 로 넘긴다.

**Credential:** Google Sheets OAuth2 (또는 서비스 계정 — 시트를 서비스 계정 이메일에 공유)

### 스케줄 근거

| 워크플로 | cron (UTC) | 현지 시각 | 이유 |
|---|---|---|---|
| ① 예측 | `0 13 * * *` | 여름 08:00 / 겨울 07:00 CT | **DA 입찰 마감 10:00 CT 이전**에 결과가 나와야 함 |
| ② backfill | `0 16 * * *` | 여름 11:00 / 겨울 10:00 CT | 전일 정산 확정 후 |
| ③ look-back | `0 17 * * 1` | 월요일 | 주 단위 누적 리뷰 |

cron 은 UTC 고정이라 서머타임에 따라 현지 시각이 한 시간 움직인다. 둘 다 마감(10:00 CT)
전이라 문제는 없지만, 마감 시각이 바뀌면 겨울 기준으로 여유가 3시간뿐인 점을 감안할 것.

---

## 4. Look-back 설계 — 무엇을 하고 무엇을 하지 않는가

### 하는 것
1. **2-pass 기록.** 예측(D-1)과 실적(D+1)을 `run_id`+`horizon` 키로 결합. 예측 시점에는 정답이
   존재하지 않으므로 append 후 update 구조가 필수다.
2. **D+1 만 채점.** D+2~D+4 는 같은 날짜가 다시 D+1 로 예측되므로, 함께 집계하면 한 날짜가
   4번 반영되어 지표가 왜곡된다.
3. **드리프트 경보.** 최근 30일 vs 누적 비교. 임계 초과 시 `alert` 컬럼에 사유 기록.
4. **분기 재학습용 축적.** 실측 데이터가 쌓이면 백테스트 표본이 늘어난다 — 특히 현재 최대 약점인
   여름 폭염·한파 레짐 표본.

### 하지 않는 것 (의도적)
**파라미터 자동 튜닝을 넣지 않았다.** 이유:

- 이 모델의 방향 예측력은 **AUC ≈ 0.52**. 30~60일 표본에서 관측되는 성과 변동은
  **거의 전부 노이즈**다. 여기에 반응해 임계값(`WX_COLD_Q`, `WX_COLD_FLOOR` 등)을 자동 조정하면
  최근 노이즈에 과적합된다.
- 실제로 이 프로젝트에서 백테스트가 그럴듯한 직관을 **10건 중 8건 반증**했다(HANDOFF §5).
  가장 최근 두 건이 "애매한 날은 반반 매수"(§5-8)와 "문턱을 올려 확신 강한 날만 매수"(§5-9)로,
  **둘 다 상식적으로 옳아 보였지만 데이터가 기각했다.** 짧은 창의 성과는 근거가 되지 못한다.
- 특히 `WX_COLD_FLOOR`(한파 하한)는 tail 리스크 정책이지 최적화 대상이 아니다. 데이터상
  최적은 "하한 없음"이지만, 표본 2.5년에 2021 Uri 급 사건이 없기 때문에 나온 결론이다.

→ **look-back 은 모니터링·축적용이고, 파라미터 변경은 (a) 충분한 표본이 쌓인 뒤 (b) 전체
walk-forward 재검증을 통해 (c) 사람이 결정한다.**

### 권장 운영 주기

| 주기 | 작업 |
|---|---|
| 매일 | 예측·기록·backfill (자동) |
| 매주 | `lookback` 시트에서 **`hit_rate_big20` 한 칸**과 `alert` 확인 (사람이 열람) |
| 반기 | 상·하반기를 나눠서 성과 확인 — 통합 평균은 계절성을 가린다 (HANDOFF §8-2) |
| 분기 | `data/` 갱신 → 전체 walk-forward 재검증 → 필요 시 파라미터 조정 (수동) |
| 연간 | 계절 기준선(t_norm) 재산출 — 현재 2.5년치라 표본이 얇음 |

---

## 5. 알려진 제약

- **Claude 해설은 참고용이다.** 시스템 프롬프트에 "AUC 0.52, 확신에 찬 전망 금지"를 명시했으나,
  LLM 출력은 검증되지 않은 텍스트다. 매수 의사결정 근거로 쓰지 말 것.
- **정산 소스가 authoritative 해야 한다.** TimeSeriesExport 파생값은 실제 정산치와 괴리가 확인됐다.
  `ERCOT_SETTLE_URL` 은 반드시 정산 확정 데이터를 반환해야 하며, 그렇지 않으면 look-back 지표 전체가 오염된다.
- **가스 파일은 여전히 필수 입력이다.** M2 는 `gas_lag1`, `gas_r7` 을 실제로 학습에 쓴다
  (`M2F` 확인). 과거에 "가스 피처 기각"으로 기록된 것은 **implied heat rate 등 파생 피처를
  추가로 넣는 안**이었고, 원 가격 두 개는 그대로 남아 있다. 가스 파일이 없으면 `_usable()` 이
  두 피처를 자동 제외하므로 오류 없이 돌지만 M2 정확도가 떨어진다.
  → HANDOFF §5-4 를 "IHR 파생 피처 기각"으로 읽을 것. 문서-코드 불일치였다(2026-08-14 정정).
- 예보 소스가 24행 미만이면 `Shape Forecast Payload` 에서 의도적으로 예외를 던진다 —
  불완전한 입력으로 배분을 내는 것보다 실패가 낫다. 기온이 한 날짜라도 비면
  (`weather_missing_days > 0`) `Flatten Rows` 에서도 같은 이유로 중단한다.
- **배분 규칙을 바꾸면 `predictions` 시트에 두 규칙이 섞인다.** `alloc_mode` 컬럼으로 구분은
  되지만, `/score` 와 주간 리뷰는 그 구분 없이 전체를 평균한다. 규칙을 바꿀 거라면
  시트를 새로 시작하거나, 리뷰 시 `alloc_mode` 로 걸러서 볼 것.
- **`DA_MW` 는 소수로 나온다.** 오버레이가 발동한 날(전체의 약 13%, 그중 배분이 중간값이 되는
  날은 4.1%)에는 `36.7` 같은 값이 산출된다. 집행은 MW 단위로 반올림하고 1MW 미만은 전량 RT 로
  처리한다. 백테스트는 반올림 없이 계산했으며 이 차이는 성과에 영향이 없다 (DEPLOY.md §7-1).
- n8n Google Sheets 노드의 `update` 는 매칭 컬럼 기준으로 전체 시트를 스캔한다.
  행이 수천 개를 넘으면 느려지므로, 연 단위로 시트를 분할하거나 DB 로 이전을 검토할 것.
