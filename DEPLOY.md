# 배포 절차서

이 문서는 **사람이 직접 해야 하는 것**만 순서대로 적었다. 코드 쪽 준비는 끝나 있다.

준비된 것: `data/`(학습 데이터), GitHub 원격 연결, `/health`·`/predict`·`/score` 로컬 검증 완료,
n8n 워크플로 2개(Render 주소·시트ID·ERCOT 아이디까지 채워진 상태),
ERCOT 데이터는 공개 API 로 직접 연결 — **사내 소스를 기다릴 필요가 없어졌다.**

남은 것: ① Google 스프레드시트 3탭 만들기 · ② Render 서비스 생성 ·
③ ERCOT 무료 계정(→ §5-1) · ④ n8n 에 import 하고 **비밀값 4개 입력**(→ §4-1-1).

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

원격은 이미 연결돼 있다(`nty5778-pixel/power-model1`). 새 커밋이 생기면 올리기만 하면 된다.

```bash
git push
```

Render 는 push 를 감지해 자동으로 다시 배포한다.

- **비공개 저장소를 권장한다.** `data/` 에 사내 데이터가 들어 있다.
- `3rd Model/` 폴더는 `.gitignore` 로 제외돼 있다(모델이 쓰지 않는 혼잡 데이터 3MB). 로컬엔 그대로 남는다.

---

## 3. Render 배포 (Free 플랜 기준)

⚠️ **`render.yaml` 은 `New → Blueprint` 로 만들 때만 읽힌다.**
`New → Web Service` 로 만들면 무시되므로 아래 값을 **대시보드에 직접 입력**해야 한다.

1. Render 에서 **New → Web Service**, 2단계 저장소 연결.
2. 아래를 입력한다.

   | 항목 | 값 |
   |---|---|
   | Language | `Python 3` |
   | Branch | `main` |
   | Root Directory | 비워둠 |
   | **Build Command** | `pip install -r requirements.txt` |
   | **Start Command** | `uvicorn app:app --host 0.0.0.0 --port $PORT --timeout-keep-alive 120` |
   | Health Check Path | `/health` |
   | Instance Type | **Free** |

3. 환경변수를 넣는다.

   | 키 | 값 | |
   |---|---|---|
   | `API_KEY` | 아무 긴 난수 | n8n 과 같은 값을 쓴다. 복사해 둘 것 |
   | `DATA_DIR` | `/opt/render/project/src/data` | 없으면 학습 데이터를 못 찾는다 |
   | `PYTHON_VERSION` | `3.11` | |
   | `OMP_NUM_THREADS` | `1` | **Free 에서는 넣을 것** — 이유는 §3-1 |
   | `ALLOC_MODE` | (선택) 기본 `m1_only` | 배분 규칙 |
   | `M1_DA_THRESHOLD` | (선택) 기본 `0.5` | DA 전환 문턱 |
   | `SHEET_CSV_*` | (선택) | **학습 데이터를 시트에서 최신으로 받기 — §3-3** |

   ⚠️ 뒤의 둘을 바꾸면 **실제 매수 금액이 바뀐다.** 바꾸기 전에 반드시
   `backtest_walkforward.py --alloc <규칙> --threshold <값>` 으로 전후를 비교할 것.

4. 배포 후 확인:

```bash
curl https://<서비스명>.onrender.com/health
```

`{"ok":true, ...}` 가 나오면 성공이다.

### 3-1. Free 플랜에서 실제로 어떻게 도는가

| | 실측/한도 | 판단 |
|---|---|---|
| 메모리 | **188 MB** 사용 / 512 MB 한도 | 여유 있음 |
| CPU | 0.1 | 학습이 로컬(1초)보다 훨씬 느리다 |
| 잠들기 | 15분 무응답 시 | 다음 요청이 1~3분 걸림 |

**잠드는 문제는 n8n 쪽에서 해결했다.** 본 작업 전에 `/health` 를 두드려 서버를 깨우고,
깨어날 때까지 기다린 뒤 본 작업을 시작한다. 실행 시각도 20분 앞당겼다.

⚠️ **Render 는 잠든 동안 연결을 붙잡아 주지 않는다.** 즉시 `HTTP 503` 과 `Retry-After: 5`,
그리고 "Application loading" 안내 페이지를 돌려준다(실측 확인). 그래서 타임아웃을 길게 잡는
방식은 소용이 없고 — **200 이 나올 때까지 반복해서 다시 두드려야 한다.**
n8n 기본 재시도는 최대 5회·간격 5초(=25초)라 콜드스타트에 못 미친다.
그래서 `IF` + `Wait` 로 **20초 간격 최대 20회(약 6분 30초)** 재시도 루프를 만들었다(§4-3).

**`OMP_NUM_THREADS=1` 을 넣는 이유** — 기본값이면 XGBoost 가 호스트의 코어 수만큼 스레드를
띄우는데, 실제로 쓸 수 있는 건 0.1 CPU 뿐이라 서로 다투기만 하고 더 느려진다.

⚠️ **스레드 수는 모델 점수를 미세하게 바꾼다.** 로컬 실측에서 M4 점수가 0.53 ↔ 0.56 정도
움직였다(XGBoost 의 성질이며 버그가 아니다). 최종 배분은 모델1 점수가 0.50 을 넘느냐로만
갈리므로 대개 결과가 같지만, **점수가 0.50 근처인 날에는 판단이 뒤집힐 수 있다.**
과거 531일 중 0.50 ±0.02 안에 든 날이 23일(4.3%)이었다 — 월 1~2일 꼴이다.
로컬과 Render 는 CPU·라이브러리 빌드가 달라 스레드 설정과 무관하게 원래도 완전히 같지는 않다.

### 3-2. 나중에 Starter($7/월)로 올려야 할 때

- 아침 결과를 **더 이른 시각에** 받아야 할 때
- `/predict` 가 자주 타임아웃될 때 (n8n 실행 기록에서 확인)
- 하루 여러 번 호출하게 될 때

올릴 때는 Render 대시보드에서 Instance Type 만 바꾸면 되고, n8n 은 그대로 둬도 된다
(깨우기 단계는 이미 깨어 있는 서버에는 즉시 응답하므로 손해가 없다).

### 3-3. 학습 데이터를 구글 시트에서 읽기 (권장)

**왜 필요한가.** 저장소의 `data/*.csv` 는 **2026-06-24 에서 멈춰 있다.** 그대로 두면
모델은 넉 달 전 세상을 기준으로 판단하게 된다. 시트는 매일 갱신되므로, 시트를 붙여 두면
사람이 손대지 않아도 학습 데이터가 계속 최신으로 유지된다.

CSV 를 버리는 게 아니다. **2024~2025 는 계속 CSV 가 담당하고, 2026-01-01 부터는 시트가
덮어쓴다.** 시트를 못 읽으면 CSV 만으로 그냥 돈다(서비스가 죽지 않는다).

붙이는 방법은 두 가지고, **① 웹 게시** 를 권한다. Google Cloud Console 도, 키 파일도,
추가 라이브러리도 필요 없다.

#### ① 웹 게시 CSV — Cloud Console 없이

⚠️ **반드시 "탭 하나"씩 고를 것.** 게시 화면의 기본값이 *전체 문서* 인데,
그대로 두면 같은 파일에 들어 있는 **Criterion 리포트 탭까지 주소를 아는 사람 누구나
보게 된다.** 아래 5개 탭만 따로따로 게시한다.

탭 하나마다 이 순서를 반복한다.

1. 그 스프레드시트를 연다.
2. **파일 → 공유 → 웹에 게시**
3. **링크** 탭에서
   - 왼쪽 드롭다운: *전체 문서* 를 → **게시할 탭 하나**로 바꾼다
   - 오른쪽 드롭다운: **쉼표로 구분된 값(.csv)**
4. **게시** → 경고창에서 **확인**
5. 나온 주소를 복사한다.
   (`https://docs.google.com/spreadsheets/d/e/2PACX-.../pub?gid=...&single=true&output=csv` 모양)

그 주소를 Render 환경변수에 넣는다.

| 환경변수 | 어느 탭인가 | 안 넣으면 |
|---|---|---|
| `SHEET_CSV_HIST` | Energy Price Tracker DB → **Historical data** | 시트 연동 자체가 꺼짐 |
| `SHEET_CSV_DEMAND` | 같은 파일 → **demand** | 부하 예보만 CSV 값 사용 |
| `SHEET_CSV_GAS` | 같은 파일 → **GD Katy** | 가스는 CSV 값 사용 |
| `SHEET_CSV_WX` | 날씨 파일 → 실측 기온 탭 | 기온은 CSV 값 사용 |
| `SHEET_CSV_WXNORM` | 날씨 파일 → 30년 평년값 탭 | 평년값을 코드가 직접 계산(정확도 떨어짐) |

**하나만 넣어도 된다.** 넣은 것만 시트에서 읽고 나머지는 CSV 를 쓴다.

**되돌리기** — 같은 화면(파일 → 공유 → 웹에 게시)에서 **게시 중지**. 주소는 즉시 죽는다.
Render 환경변수도 같이 지운다.

#### ② 서비스 계정 — Cloud Console 을 쓸 수 있다면

`SHEET_CSV_*` 를 하나도 안 넣으면 이쪽으로 넘어간다.

1. Google Cloud Console 에서 서비스 계정을 만들고 JSON 키를 받는다.
2. 두 스프레드시트를 그 계정 이메일에 **보기 권한**으로 공유한다.
3. Render 환경변수 `GOOGLE_SERVICE_ACCOUNT_JSON` 에 JSON 전체를 붙여넣는다.
4. `requirements.txt` 아래쪽 `gspread` / `google-auth` 두 줄이 필요하다(이미 들어 있다).

탭을 공개하지 않는 게 장점이고, 대신 준비가 번거롭다.

#### 각 탭에 있어야 하는 컬럼

읽는 쪽이 컬럼 **이름**으로 찾는다. 이름이 다르면 그 항목만 조용히 비므로
아래와 맞는지 확인할 것.

| 탭 | 필요한 컬럼 |
|---|---|
| Historical data | `Timestamp`, `DAM`, `RT_LZ`, `RT_HB`, `Load`, `NetLoad`, `Temp`, `Solar`, `Solar_f`, `Wind`, `Wind_f`, `PRC` |
| demand | 시각 컬럼 + `Demand` (= 전일 발표 부하 예보) |
| GD Katy | 이름에 `date` 가 들어간 컬럼 + 종가 컬럼 |
| 기온 | `date`, `temp_mean_f`, (선택) `temp_max_f` |
| 평년값 | `date` 또는 `mm-dd`, `normal_temp_mean_f`, (선택) `normal_temp_max_f` |

#### 잘 붙었는지 확인하는 법

Render 대시보드 → **Logs**. `/predict` 가 처음 도는 순간 이런 줄이 찍힌다.

```
[sheets] 읽기 방식: 웹 게시 CSV (hist, demand, gas, wx, wxnorm)
[sheets] 부하 예보 5,832/5,832 시간 결합 (Demand @ demand 탭)
[sheets] ERCOT 5,832행 (2026-01-01 ~ 2026-09-03) → /tmp/sheet_cache/sheet_ercot.csv
```

**끝 날짜가 어제인지** 보면 된다. 한참 전 날짜면 시트 갱신이 멈춘 것이다.

시트가 아예 안 붙었으면 `읽기 방식:` 줄 자체가 안 나온다. 그때는
`[sheets] ... 못 찾음` / `... 실패` 줄에서 원인을 본다.

> 예측 응답의 `d0_gap_days` 도 같은 걸 본다. 이 값이 커지면 학습 데이터가 낡았다는 뜻이다.

---

## 4. n8n (Cloud 기준)

**Variables 도 Credential 도 쓰지 않는다**(Google Sheets 제외). 값은 전부 워크플로 파일에 있고,
import 전에 찾아 바꾸기 한 번으로 끝난다. 이유는 §4-1-1.

### 4-1. 파일에 이미 들어 있는 값

주소·시트ID·ERCOT 아이디는 **이미 채워져 있다.** 그대로 import 하면 된다.

| 값 | 들어간 내용 |
|---|---|
| Render 주소 | `https://powermodel1.onrender.com` |
| 시트 ID | `1d1p7Y5V_uOShbkVqfDVjyEfjGGFcGGNZKHq5L8cbU24` |
| ERCOT 아이디 | `ty.noh@sk.com` |
| 거래 규모 | `100` MW |
| Claude 모델 | `claude-sonnet-5` |

바꿔야 할 때는 두 JSON 파일에서 해당 문자열을 찾아 바꾸면 된다
(주소 4곳, 시트ID 6곳, 아이디 2곳).

### 4-1-1. import 전에 비밀값 4개를 찾아 바꾼다

**Credential 로 빼지 않았다.** n8n 은 import 할 때 Credential 을 *이름*으로 찾는데,
그 시점에 같은 이름이 없으면 연결이 조용히 비고 **헤더 없이 요청이 나간다.**
그러면 ERCOT 은 `Access denied due to missing subscription key`, Render 는 `401` 을 준다.
키가 틀린 게 아니라 아예 안 보낸 것이라 원인을 찾기 어렵다. 그래서 노드에 직접 넣는다.

두 JSON 파일을 텍스트 편집기로 열고 아래를 **찾아 바꾸기** 한 뒤 import.

| 찾을 문자열 | 바꿀 값 | 곳 |
|---|---|---|
| `CHANGE-ME-ERCOT-PW` | ERCOT 계정 비밀번호 | 2 |
| `CHANGE-ME-ERCOT-KEY` | ERCOT 구독키 (Subscription Key) | 5 |
| `CHANGE-ME-RENDER-API-KEY` | Render 의 `API_KEY` 와 **같은 값** | 2 |
| `CHANGE-ME-CLAUDE-API-KEY` | Claude API 키 | 2 |

⚠️ **바꾸고 나면 워크플로 파일에 비밀값이 그대로 담긴다.** 외부로 내보내거나 공유하지 말 것.
(ERCOT 은 토큰을 아이디·비밀번호로 받는 방식이라 어차피 본문에 들어가야 했다.)

### 4-2. Credential 은 Google Sheets 하나뿐

**Credentials → Add credential → Google Sheets OAuth2**(또는 서비스 계정).
import 후 **시트 노드 6곳**에서 드롭다운으로 골라준다.

서비스 계정으로 하면 §1 스프레드시트를 그 계정 이메일에 **편집 권한**으로 공유해야 한다.

> `/health`(깨우기) 노드에는 인증이 없다 — 서버도 이 경로만 인증을 요구하지 않는다.

### 4-3. 워크플로 가져오기

`n8n_1_daily_predict.json`, `n8n_2_backfill_lookback.json` 을 import.

| 워크플로 | cron (UTC) | 현지 시각 | 왜 그 시각인가 |
|---|---|---|---|
| ① 예측·기록·해설 | `40 12 * * *` | 여름 07:40 / 겨울 06:40 CT | **DA 입찰 마감 10:00 CT 전.** Free 웨이크업 몫으로 20분 앞당겼다 |
| ② 실적 backfill | `0 16 * * *` | 여름 11:00 / 겨울 10:00 CT | 전일 정산이 확정된 뒤 |
| ③ 주간 리뷰 | `40 16 * * 1` | 월요일 | ②가 그날 실적을 채운 40분 뒤 |

> 시각은 UTC 고정이라 서머타임에 따라 현지 시각이 한 시간 움직인다. 셋 다 여유가 있다.

**Free 티어용으로 워크플로에 들어간 것**

- ①과 ③ 맨 앞에 **깨우기 루프 4개 노드** — `Wake Render (Free)` → `Awake?` →
  (아직이면) `Wake Retry Guard` → `Wait 20s` → 다시 `Wake Render (Free)`.
  `200` 이 나오면 통과하고, `503` 이면 20초 뒤 다시 두드린다. 최대 20회(약 6분 30초).
  `Wake` 노드는 `neverError` 라 503 에도 실패하지 않고, 판정은 `Awake?` 가 statusCode 로 한다.
  **실행 기록에서 이 구간이 몇 분 걸리는 건 정상이다** — 서버가 일어나는 중이다.
- `/predict`·`/score` 타임아웃을 **300초**로 늘리고 재시도 4회.
- ②(실적 backfill)에는 깨우기가 없다 — 이 경로는 Render 를 부르지 않고
  정산 소스와 시트만 다루기 때문이다.

> **고친 것 하나 더** — 원본은 재시도 설정이 `parameters.options.retry` 안에 있었는데,
> n8n 에서 이 셋은 노드 레벨 속성이라 **그 위치에서는 무시된다.** Starter 였다면 티가 안
> 났겠지만 Free 에서는 재시도가 실제로 필요해서 제자리로 옮겼다.

### 4-4. 두 번째부터는 import 하지 말고 명령 한 줄로 (`push_n8n.py`)

워크플로를 고칠 때마다 파일을 다시 import 하면, **화면에서 손으로 넣은 것들이 다 날아간다** —
ERCOT 비밀번호, API 키 3개, Google Sheets 연결. 레포의 JSON 에는 그 값이 없기 때문이다
(있으면 안 된다 — 깃에 비밀값을 올릴 수는 없다).

`push_n8n.py` 는 그 문제를 없앤다. **밀어넣기 전에 n8n 에서 현재 값을 읽어 제자리에 도로
채운다.** 비밀값은 계속 n8n 에만 있고, 로직만 깃에서 간다.

**준비 (한 번만)**

1. n8n 화면 → **Settings → n8n API → Create an API key**
   ⚠️ **유료 플랜(Starter $20/월 이상)에서만 보이는 메뉴다.** 무료 체험 중에는 없다 —
   그때는 기존대로 import 해야 한다.
2. 이 폴더에 `n8n_push.local.json` 을 만든다. (`.gitignore` 에 들어 있어 커밋되지 않는다.)

```json
{
  "base_url": "https://<내주소>.app.n8n.cloud",
  "api_key":  "<위에서 만든 키>"
}
```

**쓰는 법**

```bash
python push_n8n.py --dry-run   # 뭐가 바뀔지만 보여준다. 아무것도 안 건드림
python push_n8n.py             # 반영
python push_n8n.py --pull      # 반대 방향 — 화면에서 고친 걸 파일로 가져온다
```

**안전장치 — 이렇게 동작한다**

- **비밀값을 하나라도 못 찾으면 아무것도 밀어넣지 않는다.** 그대로 덮어썼다면 워크플로가
  `CHANGE-ME` 상태로 죽었을 것이다. 두 워크플로 중 하나만 문제여도 **둘 다 멈춘다** —
  절반만 반영돼 서로 안 맞는 상태가 되는 게 더 나쁘기 때문이다. (정말 필요하면 `--force`)
- **자동실행 켜짐/꺼짐은 건드리지 않는다.** n8n 은 `active` 를 이 경로로 바꾸지 못하게 막아
  뒀고, 그래서 켜둔 워크플로는 켜둔 채로 갱신된다.
- **`--pull` 은 비밀값을 다시 `CHANGE-ME` 로 가려서 저장한다.** 화면에서 고친 걸 파일로
  가져오면서 실제 키가 깃에 섞여 들어가는 사고를 막는다. 가져온 뒤 `git diff` 로 확인할 것.
- n8n 은 PUT 본문에서 `name`/`nodes`/`connections`/`settings` **네 개만** 받는다.
  다른 게 하나라도 있으면 400 이 난다. 스크립트가 그 넷만 보낸다.

**직접 확인한 것** — 가짜 n8n 서버를 세워 5가지를 돌렸다: 평소 갱신(비밀값 4/4 유지,
Google Sheets 연결 유지, 자동실행 유지) · 비밀값 누락 시 전면 중단(PUT 0회) ·
`--force` 강행 · 없는 워크플로 신규 생성 · `--pull` 시 비밀값 유출 0건.
설정 파일 오타 6가지도 전부 사람이 읽을 수 있는 안내로 끝난다.

---

## 5. ERCOT 데이터 — 공개 API 를 직접 쓴다

사내 소스를 기다리지 않고 **ERCOT 공개 API** 를 워크플로에서 직접 호출하도록 배선했다.
필요한 건 **무료 계정 하나**뿐이다.

### 5-1. 계정 만들기

1. <https://apiexplorer.ercot.com> 에서 가입한다 (무료).
2. **Public API** 에 구독(subscribe)하면 **구독키(Subscription Key)** 가 나온다.
3. 준비물 3개: **아이디**, **비밀번호**, **구독키**. §4 에서 쓴다.

### 5-2. 워크플로가 부르는 엔드포인트

인증이 2단계다. 아이디·비밀번호로 토큰을 받고, 그 토큰과 구독키를 함께 보낸다.
`ERCOT Token` 노드가 매 실행 토큰을 새로 받으므로 만료를 신경 쓸 필요는 없다.

| 쓰는 곳 | 리포트 | 경로 |
|---|---|---|
| 예측 ① | Seven-Day Load Forecast | `/np3-565-cd/lf_by_model_weather_zone` |
| 예측 ① | Hourly Wind Power Production | `/np4-742-cd/wpp_hrly_actual_fcast_geo` |
| 예측 ① | Hourly Solar Power Production | `/np4-737-cd/spp_hrly_avrg_actl_fcast` |
| backfill ② | DAM Settlement Point Prices | `/np4-190-cd/dam_stlmnt_pnt_prices` |
| backfill ② | Settlement Point Prices (15분) | `/np6-905-cd/spp_node_zone_hub` |

기준 주소는 `https://api.ercot.com/api/public-reports`, 토큰은 ERCOT B2C ROPC 엔드포인트.

### 5-3. 첫 실행에서 한 번은 컬럼 이름을 맞춰야 할 수 있다

ERCOT 응답은 `{ fields:[{name}], data:[[...]] }` 형태이고, 컬럼 이름은 리포트 개정 때 바뀐다.
그래서 워크플로는 **이름 부분매칭**으로 컬럼을 찾는다(파이썬 모델의 `NEEDLES` 와 같은 방식).
못 찾으면 이런 오류를 낸다:

```
풍력 예보: 시스템 전체 발전 컬럼을 못 찾았습니다.
  찾아본 이름: STWPF_SYSTEM_WIDE, stwpfSystemWide, systemWide, ...
  실제 컬럼: deliveryDate, hourEnding, genCoastal, genSouth, ...
```

**실제 컬럼 목록이 오류에 그대로 찍히므로**, 맞는 이름을 `Shape Forecast Payload` 노드의
후보 목록에 한 줄 추가하면 끝난다. 한 번만 하면 된다.

### 5-4. 정산 데이터에 대한 경고 (그대로 유효)

⚠️ **반드시 확정 정산치여야 한다.** 위 두 가격 리포트는 ERCOT 이 직접 내는 정산가라
이 조건을 만족한다. 다른 소스로 바꿀 때는 반드시 확인할 것 — TimeSeriesExport 파생값은
실제 정산치와 어긋나는 것이 확인됐고, 쓰면 성과 지표 전체가 오염된다(HANDOFF §9-6).

⚠️ 당일 아침에는 **전날 정산이 아직 확정 전**일 수 있다. 그런 날짜는 건너뛰고
다음 실행에서 다시 시도한다(`Join & Score` 가 로그에 남긴다).

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
| `D+1~D+4 예보가 부족합니다` | ERCOT 조회 날짜에 데이터가 없다. 오류에 찍힌 '받은 날짜'를 확인 |
| `... 컬럼을 못 찾았습니다` | ERCOT 컬럼 이름이 바뀌었다. 오류에 실제 컬럼이 찍힌다 → §5-3 |
| `ERCOT 응답이 비었습니다` | 구독키 또는 토큰 문제. `ERCOT Token` 노드 실행 결과부터 확인 |
| 토큰 노드 `401`/`400` | ERCOT 아이디·비밀번호 오타 (§4-1 의 `CHANGE-ME-ERCOT-*`) |
| `Open-Meteo 기온 파싱 실패` | Open-Meteo 응답 구조 변경 |
| `날씨 결측 N일` | 서버가 예보일의 기온을 못 받았다 → payload 의 `weather` 확인 |
| `/predict` 401 | Credential `Render Model API` 의 값 ≠ Render `API_KEY` |
| 주소가 `/predict` 만 남고 앞이 비어 있음 | Variable `RENDER_URL` 이 없거나 이름이 다름 (§4-1) |
| 표현식에 `[undefined]` 표시 | 같은 원인. Variables 메뉴가 없는 요금제면 §4-1 아래 안내대로 직접 입력 |
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
