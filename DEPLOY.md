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

### 탭 이름: `predictions` (28열)

```
run_id	generated_at	model_version	alloc_mode	m1_threshold	d0_last_actual	horizon	target_date	M1_P_DA_cheaper	M2_DA_pred_USD	M3_gate_signal	M3_gate_reason	M4_premium_prob	vote_M1_DA	vote_M2_DA	vote_M4_DA	WX_t_anom_F	WX_overlay	base_DA	DA_fraction	RT_fraction	DA_MW	RT_MW	DA_actual	RT_actual	DART_actual	blended_cost	hit
```

앞의 23개는 예측 시점에 채워지고, 뒤의 5개(`DA_actual` 이후)는 다음 날 실적이 나온 뒤
두 번째 워크플로가 채운다. `run_id` + `horizon` 이 그 행을 찾는 열쇠다.

> **`alloc_mode` 는 지우지 말 것.** 어떤 배분 규칙으로 낸 행인지 기록한다.
> 나중에 규칙을 바꾸면(`m1_only` ↔ `ensemble`) 이 칸이 없는 한 옛 행과 새 행이 구분되지 않고,
> 주간 리뷰가 서로 다른 규칙의 성과를 한 덩어리로 평균내 버린다.
> 기본값은 `m1_only`, `m1_threshold` 는 `0.5` 다.

### 탭 이름: `analysis` (9열)

```
run_id	logged_at	kind	model	target_dates	mean_DA_fraction	commentary	input_tokens	output_tokens
```

### 탭 이름: `lookback` (20열)

```
logged_at	kind	window_all_days	cost_blended	cost_all_rt	cost_all_da	vs_rt	vs_da	cost_std	hit_rate_all	hit_rate_big5	n_big5	hit_rate_big20	n_big20	vs_rt_big20	mean_da_fraction	recent30_hit	recent30_vs_rt	alert	commentary
```

> **매주 볼 칸은 `hit_rate_big20` 하나다.** 가격차가 $20 넘게 벌어진 날의 방향 적중률이고,
> 실현 이익의 대부분이 이 날들에서 나온다(walk-forward 531일 중 30일, 그 구간에서 RT 대비
> $8.77/MWh 우위). **50% 아래로 떨어지면 경보**이며 `alert` 칸에 `[중요]` 로 표시된다.
> `n_big20` 이 10 미만이면 표본이 얇아 판정하지 않는다.
>
> `hit_rate_all`(전체 적중률)로 판단하지 말 것. 이 지표는 **아무것도 안 하고 RT만 사는 쪽이
> 항상 더 높게** 나온다(61.2% vs 55.4%). 하루 $0.5 차이와 $50 차이를 똑같이 한 번으로 세기
> 때문이다. 그런데 실제 돈은 m1_only 쪽이 연 30만달러(100MW) 앞선다.

만든 뒤 주소창의 `/d/` 와 `/edit` 사이 문자열이 **SHEET_ID** 다. 메모해 둘 것.

> 서비스 계정으로 붙일 거면, 스프레드시트를 그 서비스 계정 이메일에 **편집 권한**으로 공유해야 한다.

### 헤더가 맞는지 확인하는 법

Sheets 노드는 **이름으로 매핑**한다. 헤더에 없는 항목은 **오류 없이 그냥 버려진다.**
(실제로 `alloc_mode` 가 이렇게 통째로 누락돼 있었다.) 워크플로나 헤더를 건드린 뒤에는
아래를 돌려서 어긋남을 잡는다.

```bash
python check_sheet_headers.py
```

`전체: 일치` 가 나오면 위 세 줄을 그대로 붙여넣어도 안전하다는 뜻이다.

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

   선택 사항 — 넣지 않으면 아래 기본값으로 동작한다:

   | 키 | 기본값 | 의미 |
   |---|---|---|
   | `ALLOC_MODE` | `m1_only` | 배분 규칙. 평소 RT, 모델1 점수가 문턱 넘는 날만 DA |
   | `M1_DA_THRESHOLD` | `0.5` | 그 문턱 |

   ⚠️ **이 둘을 바꾸면 실제 매수 금액이 바뀐다.** 바꾸기 전에 반드시
   `backtest_walkforward.py --alloc <규칙> --threshold <값>` 으로 전후를 비교할 것.

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
3. 그 4행에서 아래 3개를 눈으로 확인한다. **여기서 걸러야 할 오작동이 다 잡힌다.**
   - `alloc_mode` 가 `m1_only` 로 채워졌나 (빈칸이면 시트 헤더 오타)
   - `WX_t_anom_F` 에 숫자가 있나 (빈칸이면 날씨가 안 넘어온 것)
   - `DA_fraction` 이 0 또는 1 인가. 중간값이면 같은 행의 `WX_overlay` 를 볼 것 —
     `hot+9F` 처럼 채워져 있으면 정상(아래), **비어 있는데 중간값이면 규칙이 `ensemble` 이다**
4. `analysis` 시트에 해설 1행이 쌓이는지 본다.
5. 다음 날 ②를 수동 실행해 `DA_actual` 이 채워지는지 본다.
6. 20일 넘게 쌓이면 주간 리뷰가 `lookback` 에 기록된다(그 전엔 건너뛴다).

### 실패하면 볼 곳

| 증상 | 원인 |
|---|---|
| `예보 행이 부족합니다` | `ERCOT_FORECAST_URL` 응답 형태 불일치 → Shape 노드 매핑 수정 |
| `Open-Meteo 기온 파싱 실패` | Open-Meteo 응답 구조 변경 |
| `날씨 결측 N일` | 서버가 예보일의 기온을 못 받았다 → payload 의 `weather` 확인 |
| `/predict` 401 | `MODEL_API_KEY` ≠ Render `API_KEY` |
| `/predict` 500 `no ERCOT history CSV` | `data/` 가 배포에 안 올라감 → `.gitignore` 확인 |
| `/predict` 400 `alloc 은 ... 중 하나` | `ALLOC_MODE` 오타. `m1_only` 또는 `ensemble` |
| `DA_fraction` 이 중간값 + `WX_overlay` 비어 있음 | `ALLOC_MODE` 가 `ensemble` 로 설정돼 있다 → §7 |
| `DA_fraction` 이 중간값 + `WX_overlay` 채워짐 | 정상. 이상기온일에 오버레이가 배분을 민 것 → §7-1 |
| `DA_MW` 가 0.4 같은 소수 | 정상이지만 실무상 매수 불가 → §7-1 의 반올림 안내 |
| `alloc_mode` 칸이 비어 있음 | 시트 헤더에 `alloc_mode` 가 없거나 철자가 다름 (§1) |
| 첫 요청만 느림 | 정상. 요청당 학습이 일어난다(로컬 1초, Render 는 더 느림). 같은 날 두 번째부터 캐시 |

---

## 7. 배포 전 반드시 알아야 할 것

### 7-1. 시트에 찍히는 DA 비중을 어떻게 읽나

기본 규칙 `m1_only` 는 **"평소 RT, 신호 나온 날만 전량 DA"** 라서 `DA_fraction` 이
보통 **0 아니면 1** 로 나온다. 고장이 아니다.

다만 **이상기온일에는 날씨 오버레이가 그 값을 최대 0.30 만큼 민다.** 그래서 중간값이 섞인다.

| `WX_overlay` | `DA_fraction` 이 나올 수 있는 범위 | 뜻 |
|---|---|---|
| 비어 있음(`-`) | 0 또는 1 | 평범한 날. 모델1 판단 그대로 |
| `hot+NF` | 0 → 0 ~ 0.30 / 1 → 1 | 이상고온. RT 로 잡힌 날을 DA 쪽으로 조금 민다 |
| `cold+NF` | 0 → 0 / 1 → 0.30 ~ 1 | 이상한파. DA 로 잡힌 날을 RT 쪽으로 민다(하한 0.30) |

**얼마나 자주 중간값이 나오나** — walk-forward 531일 기준:

| | 일수 | 비율 |
|---|---|---|
| 오버레이 발동 | 68일 | 12.8% |
| `DA_fraction` 이 0/1 이 아님 | 22일 | **4.1%** |

즉 **20일에 한 번꼴**이고 나머지는 전량 RT 또는 전량 DA 다.

⚠️ **집행할 때는 MW 단위로 반올림할 것.** `DA_MW` 가 `36.7` 처럼 소수로 나온다.
과거 22일은 모두 1~99MW 범위여서 "0 또는 100 으로 스냅"이 필요했던 적은 없지만,
극단적인 이상기온에서는 `DA_MW = 0.4` 같은 값도 산출된다(로컬 테스트에서 확인).
**1MW 미만이면 그냥 전량 RT 로 집행하면 된다.** 백테스트는 반올림 없이 계산했으므로
이 정도 차이는 성과에 영향이 없다.

### 7-2. 배분 규칙

✅ **배분 규칙은 교체됐다 (2026-08-14).** 기본값이 `m1_only` 다 — 평소 RT, 모델1 점수가
0.50 을 넘는 날만 DA. 531일 검증에서 100% RT 대비 연 **+30만 달러 절약**(100MW), 종전
3개 모델 평균 방식(연 −18만 손해) 대비 **+49만 달러**. 요금과 변동성을 동시에 이긴 첫 규칙이다.

Render 환경변수 `ALLOC_MODE` 로 바꿀 수 있다(`m1_only` / `ensemble`). 건드릴 이유는 없다.
⚠️ **`M1_DA_THRESHOLD` 를 0.50 보다 올리지 말 것.** 0.52 부터 이익이 급감하고 0.60 에서는
손해로 돌아선다(HANDOFF §5-9). "더 보수적으로" 가 여기서는 반대로 작동한다.

⚠️ **여전히 확실한 이득은 아니다.** 절약 +30만의 95% 구간은 −20만~+88만이고,
손해로 끝날 확률이 13% 남아 있다. 이익의 대부분이 531일 중 30일에서 나온다.

⚠️ **성과는 계절을 탄다.** 상반기 +71만 / 하반기 −47만이었다(HANDOFF §8-2).
**하반기에 성적이 나빠도 곧바로 고장으로 읽지 말 것.** 다만 하반기 표본은 2025년 한 해뿐이라
"여름엔 원래 그렇다" 인지도 아직 확정되지 않았다. 주간 리뷰는 `hit_rate_big20` 으로 본다.

⚠️ **`data/` 는 자동으로 갱신되지 않는다.** 학습 데이터가 2026-06-24 에서 멈춰 있다.
날이 갈수록 "마지막 실측일" 과 예보일 사이 간격이 벌어지고, 모델이 낡은 시장 상태를 보게 된다.
월 1회 최신 CSV 로 `data/` 를 갱신하고 push 하면 Render 가 자동 재배포한다.
