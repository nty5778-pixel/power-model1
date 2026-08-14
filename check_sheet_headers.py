"""DEPLOY.md 의 시트 헤더 3개가 n8n 이 실제로 내보내는 키와 맞는지 대조한다.

왜 필요한가
  Google Sheets 노드는 autoMapInputData 라 '이름'으로 매핑한다. 헤더에 없는 키는
  기록되지 않고, 오류도 나지 않는다. 실제로 alloc_mode 가 그렇게 통째로 누락돼 있었다.
  워크플로 코드나 시트 헤더를 건드린 뒤에는 이걸 돌려서 어긋남을 잡는다.

사용
  python check_sheet_headers.py          # 종료코드 0=일치, 1=불일치
"""
import io, json, os, re, sys

P = os.path.dirname(os.path.abspath(__file__))
LINES = io.open(os.path.join(P, "DEPLOY.md"), encoding="utf-8").read().splitlines()


def header(first_key, ncol):
    """DEPLOY.md 안의 탭 구분 헤더 줄을 (첫 키, 열 수)로 찾는다."""
    for l in LINES:
        if l.startswith(first_key + "\t") and len(l.split("\t")) == ncol:
            return l.split("\t")
    raise SystemExit(f"[오류] DEPLOY.md 에서 헤더를 못 찾음: {first_key} ({ncol}열)")


def js(fname, node_id):
    wf = json.load(io.open(os.path.join(P, fname), encoding="utf-8"))
    for n in wf["nodes"]:
        if n["id"] == node_id:
            return n["parameters"]["jsCode"]
    raise SystemExit(f"[오류] {fname} 에 노드 '{node_id}' 없음")


def out_keys(code):
    """return [{ json: {...} }] 블록의 키. 한 줄에 여러 키가 있어도 잡는다."""
    m = re.search(r"return \[\{\s*json:\s*\{(.*)\}\}\];", code, re.S)
    body = m.group(1) if m else code
    keys, seen = [], {"json"}                 # 'json' 은 아이템 래퍼라 키가 아니다
    for k in re.findall(r"(?:^|[\s,{])(\w+)\s*:", body, re.M):
        if k not in seen:
            seen.add(k)
            keys.append(k)
    return keys


W1, W2 = "n8n_1_daily_predict.json", "n8n_2_backfill_lookback.json"
# lookback 은 Parse Weekly Review 가 logged_at/kind + Drift 결과 스프레드 + commentary 로 만든다
CHECKS = [
    ("predictions", header("run_id", 28), out_keys(js(W1, "flat")), True),
    ("analysis", header("run_id", 9), out_keys(js(W1, "parse")), True),
    ("lookback", header("logged_at", 20),
     ["logged_at", "kind"] + out_keys(js(W2, "drift")) + ["commentary"], False),
]

bad = False
for name, hdr, keys, check_order in CHECKS:
    miss = [k for k in keys if k not in hdr]          # 코드가 내보내는데 헤더에 없음 → 조용히 유실
    extra = [h for h in hdr if h not in keys]         # 헤더에 있는데 아무도 안 채움 → 빈 칸
    print(f"{name:<12} 헤더 {len(hdr):>2}열 / 코드 {len(keys):>2}키 "
          f"→ {'OK' if not (miss or extra) else '불일치'}")
    if miss:
        print(f"   ⚠ 기록되지 않고 사라지는 키: {miss}")
    if extra:
        print(f"   ⚠ 아무도 채우지 않는 헤더  : {extra}")
    if check_order and not miss and not extra and keys != hdr:
        print(f"   (순서만 다름 — 이름으로 매핑하므로 동작에는 지장 없음)")
    bad = bad or bool(miss or extra)

print("\n전체:", "불일치 — DEPLOY.md §1 과 워크플로를 맞출 것" if bad else "일치")
sys.exit(1 if bad else 0)
