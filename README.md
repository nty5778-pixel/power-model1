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
├── run_models_d1_d4_v4_weather.py    모델 (forecast_rows 로 재사용 가능하게 리팩터)
├── requirements.txt
├── render.yaml
└── data/                             과거 CSV (레포에 커밋)
    ├── 2024~2026_Historical_Data.csv
    ├── texas_open_meteo_*.csv
    └── GD_Katy.csv
```

**환경변수 (Render 대시보드)**

| 키 | 값 |
|---|---|
| `API_KEY` | 임의 난수 — n8n 의 `MODEL_API_KEY` 와 동일하게 |
| `DATA_DIR` | `/opt/render/project/src/data` |
| `PYTHON_VERSION` | `3.11` |

**엔드포인트**

| | 용도 |
|---|---|
| `GET /health` | 헬스체크 |
| `POST /predict` | 예보 → D+1~D+4 배분 |
| `POST /score` | 예측+실적 → 실현원가·적중 지표 |

### 배포 시 주의

- **무료 티어는 쓰지 말 것.** 15분 idle 후 spin-down → 콜드스타트가 붙어 요청이 2~4분 걸린다.
  Starter($7/mo) 이상 권장. 무료로 버티려면 n8n 에 5분 간격 `/health` ping 워크플로를 추가.
- **디스크는 ephemeral.** 런타임에 쓴 파일은 재배포/재시작 시 사라진다. 학습 데이터는 레포에 커밋하거나
  외부 스토리지에서 받아야 한다. 상태는 전부 Google Sheets 에 둔다.
- **학습이 요청당 발생**한다(~900행, 10~30초). `app.py` 는 날짜 단위로 메모리 캐시하므로
  같은 날 재호출은 빠르지만, 콜드스타트 시 캐시가 날아간다.
- 데이터 갱신은 별도 절차다. 월 1회 `data/` 를 최신 CSV 로 커밋 → 자동 재배포되는 흐름을 권장.

---

## 2. Google Sheets 스키마

같은 스프레드시트에 시트 3개. n8n 환경변수 `SHEET_ID` 로 참조.

### `predictions` — 예측 1행 + 사후 실적 backfill

| 컬럼 | 시점 | 비고 |
|---|---|---|
| `run_id`, `generated_at`, `model_version`, `d0_last_actual` | 예측 시 | `run_id`+`horizon` 이 복합키 |
| `horizon`, `target_date` | 예측 시 | D+1~D+4 |
| `M1_P_DA_cheaper`, `M2_DA_pred_USD`, `M3_gate_signal`, `M3_gate_reason`, `M4_premium_prob` | 예측 시 | 모델별 원값 |
| `vote_M1_DA`, `vote_M2_DA`, `vote_M4_DA`, `base_DA` | 예측 시 | 앙상블 추적용 |
| `WX_t_anom_F`, `WX_overlay` | 예측 시 | 날씨 오버레이 발동 여부 |
| `DA_fraction`, `RT_fraction`, `DA_MW`, `RT_MW` | 예측 시 | 최종 배분 |
| `DA_actual`, `RT_actual`, `DART_actual`, `blended_cost`, `hit` | **backfill** | ②에서 채움 |

### `analysis` — Claude 일일 해설
`run_id`, `logged_at`, `kind`, `model`, `target_dates`, `mean_DA_fraction`, `commentary`, `input_tokens`, `output_tokens`

### `lookback` — 주간 성과 누적
`logged_at`, `kind`, `window_all_days`, `cost_blended`, `cost_all_rt`, `cost_all_da`, `vs_rt`, `vs_da`,
`cost_std`, `hit_rate_all`, `hit_rate_big5`, `n_big5`, `mean_da_fraction`, `recent30_hit`, `recent30_vs_rt`,
`alert`, `commentary`

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

**Credential:** Google Sheets OAuth2 (또는 서비스 계정 — 시트를 서비스 계정 이메일에 공유)

### 스케줄 근거

| 워크플로 | 시각 | 이유 |
|---|---|---|
| ① 예측 | 08:00 CT | **DA 입찰 마감 10:00 CT 이전**에 결과가 나와야 함 |
| ② backfill | 11:00 CT | 전일 정산 확정 후 |
| ③ look-back | 월 12:00 CT | 주 단위 누적 리뷰 |

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
- 실제로 이 프로젝트에서 백테스트가 직관을 3번 반증했다(2025+ 학습 제한, 분위수 정규화,
  DA 강제 게이트). **짧은 창의 성과는 근거가 되지 못한다**는 것이 반복 확인된 결론이다.
- 특히 `WX_COLD_FLOOR`(한파 하한)는 tail 리스크 정책이지 최적화 대상이 아니다. 데이터상
  최적은 "하한 없음"이지만, 표본 2.5년에 2021 Uri 급 사건이 없기 때문에 나온 결론이다.

→ **look-back 은 모니터링·축적용이고, 파라미터 변경은 (a) 충분한 표본이 쌓인 뒤 (b) 전체
walk-forward 재검증을 통해 (c) 사람이 결정한다.**

### 권장 운영 주기

| 주기 | 작업 |
|---|---|
| 매일 | 예측·기록·backfill (자동) |
| 매주 | 드리프트 경보 확인 (자동 기록, 사람이 열람) |
| 분기 | `data/` 갱신 → 전체 walk-forward 재검증 → 필요 시 파라미터 조정 (수동) |
| 연간 | 계절 기준선(t_norm) 재산출 — 현재 2.5년치라 표본이 얇음 |

---

## 5. 알려진 제약

- **Claude 해설은 참고용이다.** 시스템 프롬프트에 "AUC 0.52, 확신에 찬 전망 금지"를 명시했으나,
  LLM 출력은 검증되지 않은 텍스트다. 매수 의사결정 근거로 쓰지 말 것.
- **정산 소스가 authoritative 해야 한다.** TimeSeriesExport 파생값은 실제 정산치와 괴리가 확인됐다.
  `ERCOT_SETTLE_URL` 은 반드시 정산 확정 데이터를 반환해야 하며, 그렇지 않으면 look-back 지표 전체가 오염된다.
- **가스 피처는 현재 모델에서 실익이 없다** (백테스트 기각). `data/GD_Katy.csv` 는 IHR 진단용으로만 둔다.
- 예보 소스가 24행 미만이면 `Shape Forecast Payload` 에서 의도적으로 예외를 던진다 —
  불완전한 입력으로 배분을 내는 것보다 실패가 낫다.
- n8n Google Sheets 노드의 `update` 는 매칭 컬럼 기준으로 전체 시트를 스캔한다.
  행이 수천 개를 넘으면 느려지므로, 연 단위로 시트를 분할하거나 DB 로 이전을 검토할 것.
