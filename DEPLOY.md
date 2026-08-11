# 배포 절차서

이 문서는 **사람이 직접 해야 하는 것**만 순서대로 적었다. 코드 쪽 준비는 끝나 있다.

준비된 것: `data/`(학습 데이터), git 저장소(로컬 커밋 완료), `/health`·`/predict`·`/score` 로컬 검증 완료,
n8n 워크플로 2개 패치 완료.

아직 안 된 것: GitHub 원격 저장소, Render 서비스, Google 스프레드시트, n8n 인스턴스,
**ERCOT 예보/정산 데이터 소스 2개**(→ §5, 이게 최대 미결 항목).

---

## 1. Google 스프레드시트

스프레드시트 하나를 만들고 그 안에 **시트 탭 3개**를 만든다. 탭 이름은 아래와 정확히 같아야 한다
(n8n 이 이름으로 찾는다). 각 탭의 **1행에 헤더**를 넣는다.

아래 줄을 통째로 복사해 각 시트의 A1 셀에 붙여넣으면 탭 구분으로 자동 분리된다.

### 탭 이름: `predictions` (26열)

```
run_id	generated_at	model_version	d0_last_actual	horizon	target_date	M1_P_DA_cheaper	M2_DA_pred_USD	M3_gate_signal	M3_gate_reason	M4_premium_prob	vote_M1_DA	vote_M2_DA	vote_M4_DA	WX_t_anom_F	WX_overlay	base_DA	DA_fraction	RT_fraction	DA_MW	RT_MW	DA_actual	RT_actual	DART_actual	blended_cost	hit
```

앞의 21개는 예측 시점에 채워지고, 뒤의 5개(`DA_actual` 이후)는 다음 날 실적이 나온 뒤
두 번째 워크플로가 채운다. `run_id` + `horizon` 이 그 행을 찾는 열쇠다.

### 탭 이름: `analysis` (9열)

```
run_id	logged_at	kind	model	target_dates	mean_DA_fraction	commentary	input_tokens	output_tokens
```

### 탭 이름: `lookback` (17열)

```
logged_at	kind	window_all_days	cost_blended	cost_all_rt	cost_all_da	vs_rt	vs_da	cost_std	hit_rate_all	hit_rate_big5	n_big5	mean_da_fraction	recent30_hit	recent30_vs_rt	alert	commentary
```

만든 뒤 주소창의 `/d/` 와 `/edit` 사이 문자열이 **SHEET_ID** 다. 메모해 둘 것.

> 서비스 계정으로 붙일 거면, 스프레드시트를 그 서비스 계정 이메일에 **편집 권한**으로 공유해야 한다.

---

## 2. GitHub 원격 저장소

로컬 커밋은 이미 되어 있다. 원격만 연결해 올리면 된다.

```bash
git remote add origin https://github.com/<계정>/<저장소>.git
git push -u origin main
```

- **비공개 저장소를 권장한다.** `data/` 에 사내 데이터가 들어 있다.
- `3rd Model/` 폴더는 `.gitignore` 로 제외돼 있다(모델이 쓰지 않는 혼잡 데이터 3MB). 로컬엔 그대로 남는다.

---

## 3. Render 배포

1. Render 에서 **New → Web Service**, 위 저장소 연결.
2. `render.yaml` 이 자동 인식된다. 플랜은 **Starter($7/월) 이상**으로 둘 것.
   무료 플랜은 15분 놀면 잠들어서 다음 요청이 2~4분 걸린다.
3. 대시보드에서 환경변수 **`API_KEY`** 를 직접 입력한다(아무 긴 난수). n8n 과 같은 값을 쓴다.
   나머지(`DATA_DIR`, `PYTHON_VERSION`)는 `render.yaml` 에 이미 있다.
4. 배포 후 확인:

```bash
curl https://<서비스명>.onrender.com/health
```

`{"ok":true, ...}` 가 나오면 성공이다.

---

## 4. n8n

### 4-1. 환경변수

| 키 | 값 |
|---|---|
| `RENDER_URL` | `https://<서비스명>.onrender.com` (끝에 `/` 없이) |
| `MODEL_API_KEY` | Render 의 `API_KEY` 와 **같은 값** |
| `ANTHROPIC_API_KEY` | Claude API 키 |
| `SHEET_ID` | §1 에서 메모한 값 |
| `VOLUME_MW` | 기본 `100` |
| `CLAUDE_MODEL` | (선택) 비워두면 `claude-sonnet-5` |
| `ERCOT_FORECAST_URL` | **→ §5 참조. 아직 미정** |
| `ERCOT_SETTLE_URL` | **→ §5 참조. 아직 미정** |

### 4-2. Credential

Google Sheets OAuth2 또는 서비스 계정을 등록하고, 워크플로의 Sheets 노드 4곳에 지정한다.

### 4-3. 워크플로 가져오기

`n8n_1_daily_predict.json`, `n8n_2_backfill_lookback.json` 을 import.

| 워크플로 | 시각 | 왜 그 시각인가 |
|---|---|---|
| ① 예측·기록·해설 | 13:00 UTC | **DA 입찰 마감 10:00 CT 전**에 결과가 나와야 한다 |
| ② 실적 backfill | 16:00 UTC | 전일 정산이 확정된 뒤 |
| ③ 주간 리뷰 | 월 17:00 UTC | ②와 같은 워크플로 안에 있다 |

> 시각은 UTC 고정이라 서머타임에 따라 현지 시각이 한 시간 움직인다.
> 여름 08:00 CT / 겨울 07:00 CT — 둘 다 마감 전이라 문제는 없다.

---

## 5. 아직 막혀 있는 것 — ERCOT 데이터 소스 2개

이것만은 코드로 해결되지 않는다. **사내 소스를 연결해야 한다.**

**`ERCOT_FORECAST_URL`** — 시간별 예보를 주는 주소. 최소 24시간 이상,
`timestamp` / 부하 / 풍력 / 태양광을 포함해야 한다. 응답 형태가 다르면
워크플로의 `Shape Forecast Payload` 노드 상단 매핑만 고치면 된다.

**`ERCOT_SETTLE_URL`** — 확정 정산 DA/RT 가격. ⚠️ **반드시 정산 확정치여야 한다.**
TimeSeriesExport 파생값은 실제 정산치와 어긋나는 것이 확인됐고, 이걸 쓰면
성과 지표 전체가 오염된다(HANDOFF §9-6).

이 둘이 연결되기 전까지 워크플로는 첫 노드에서 실패한다. 그게 의도된 동작이다 —
불완전한 입력으로 배분을 내는 것보다 실패가 낫다.

---

## 6. 첫 실행 점검

1. n8n 에서 ①을 **수동 실행**한다.
2. `predictions` 시트에 4행(D+1~D+4)이 쌓이는지 본다.
3. `analysis` 시트에 해설 1행이 쌓이는지 본다.
4. 다음 날 ②를 수동 실행해 `DA_actual` 이 채워지는지 본다.
5. 20일 넘게 쌓이면 주간 리뷰가 `lookback` 에 기록된다(그 전엔 건너뛴다).

### 실패하면 볼 곳

| 증상 | 원인 |
|---|---|
| `예보 행이 부족합니다` | `ERCOT_FORECAST_URL` 응답 형태 불일치 → Shape 노드 매핑 수정 |
| `Open-Meteo 기온 파싱 실패` | Open-Meteo 응답 구조 변경 |
| `날씨 결측 N일` | 서버가 예보일의 기온을 못 받았다 → payload 의 `weather` 확인 |
| `/predict` 401 | `MODEL_API_KEY` ≠ Render `API_KEY` |
| `/predict` 500 `no ERCOT history CSV` | `data/` 가 배포에 안 올라감 → `.gitignore` 확인 |
| 첫 요청만 느림 | 정상. 요청당 학습이 일어난다(로컬 1초, Render 는 더 느림). 같은 날 두 번째부터 캐시 |

---

## 7. 배포 전 반드시 알아야 할 것

⚠️ **지금 배포되는 배분 규칙은 백테스트에서 100% RT 보다 나빴다.**
531일 검증 결과 3개 모델 평균 방식은 100% RT 대비 연 21만 달러(100MW 기준) **손해**였다.
모델1 단독으로 "DA 신호 나온 날만 DA" 규칙은 같은 기간 연 30만 달러 **이득**이었다.

배포 자체는 데이터를 쌓고 파이프라인을 검증하는 의미가 있지만,
**이 배분 결과를 그대로 매수 의사결정에 쓰기 전에 규칙 교체를 먼저 검토할 것.**
검증 도구는 `backtest_walkforward.py` 에 있다.

⚠️ **`data/` 는 자동으로 갱신되지 않는다.** 학습 데이터가 2026-06-24 에서 멈춰 있다.
날이 갈수록 "마지막 실측일" 과 예보일 사이 간격이 벌어지고, 모델이 낡은 시장 상태를 보게 된다.
월 1회 최신 CSV 로 `data/` 를 갱신하고 push 하면 Render 가 자동 재배포한다.
