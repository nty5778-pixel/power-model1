# -*- coding: utf-8 -*-
"""워크플로 JSON 을 n8n 에 밀어넣는다 — 매번 손으로 import 하지 않기 위해.

    python push_n8n.py              두 워크플로를 n8n 에 반영
    python push_n8n.py --dry-run    뭐가 바뀌는지만 보고 아무것도 안 함
    python push_n8n.py --pull       반대 방향. n8n 에 있는 걸 로컬 파일로 가져온다
    python push_n8n.py --only 1     1번 워크플로만

왜 단순 업로드가 아닌가
-----------------------
n8n 화면에서 손으로 넣은 것들(ERCOT 비밀번호, API 키 3개, Google Sheets 연결)은
레포의 JSON 에 없다. 있으면 안 된다 — 깃에 비밀값을 올릴 수는 없으니까.
그래서 파일을 그대로 덮어쓰면 **그 값들이 CHANGE-ME 로 되돌아가 워크플로가 죽는다.**

이 스크립트는 밀어넣기 전에 n8n 에서 현재 값을 먼저 읽어와, 그 자리에 도로 채운다.
즉 **비밀값은 계속 n8n 에만 있고, 로직만 깃에서 온다.**
하나라도 못 채우면 아예 밀어넣지 않고 멈춘다(--force 로 무시 가능).

준비 (한 번만)
-------------
1. n8n 화면 → Settings → n8n API → Create an API key
   (자가 호스팅은 공개 API 가 기본으로 켜져 있다. 유료 플랜 얘기는 n8n Cloud 쪽 제약.)
2. 이 폴더에 `n8n_push.local.json` 파일을 만들고 아래 두 줄을 채운다.
   이 파일은 .gitignore 에 들어 있어 깃에 올라가지 않는다.

   {
     "base_url": "https://n8n.srv931005.hstgr.cloud",
     "api_key":  "여기에 위에서 만든 키"
   }
"""
import argparse
import copy
import io
import json
import os
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
CONF = os.path.join(HERE, "n8n_push.local.json")
FILES = ["n8n_1_daily_predict.json", "n8n_2_backfill_lookback.json"]

# n8n 이 PUT 본문에서 받아주는 키. 이것 말고 뭐라도 더 있으면 400 이 난다
# ("request/body must NOT have additional properties"). id 는 URL 에만 넣는다.
PUT_KEYS = ("name", "nodes", "connections", "settings")

PLACEHOLDER = "CHANGE-ME"


def die(msg):
    print(f"\n  ✗ {msg}\n", file=sys.stderr)
    sys.exit(1)


# --------------------------------------------------------------------------
# 설정 / 통신
# --------------------------------------------------------------------------
def load_conf():
    if not os.path.exists(CONF):
        die("설정 파일이 없다: n8n_push.local.json\n"
            "    아래 내용으로 만들고 두 칸을 채울 것 (파일 맨 위 설명 참고).\n\n"
            '    {\n      "base_url": "https://n8n.srv931005.hstgr.cloud",\n'
            '      "api_key":  "여기에 n8n Settings → n8n API 에서 만든 키"\n    }')
    try:
        c = json.loads(io.open(CONF, encoding="utf-8").read().lstrip("﻿"))
    except json.JSONDecodeError as e:
        die(f"n8n_push.local.json 을 읽을 수 없다 ({e}).\n"
            "    따옴표나 쉼표가 빠졌는지 확인할 것. 형식은 파일 맨 위 설명 참고.")
    for k in ("base_url", "api_key"):
        v = str(c.get(k, "")).strip()
        # 이 파일의 예시가 <이렇게> 되어 있어서, 꺾쇠까지 같이 붙여넣기 쉽다.
        # 안을 실제로 채웠으면 꺾쇠만 떼고 받아준다.
        if len(v) > 2 and v[0] == "<" and v[-1] == ">":
            v = v[1:-1].strip()
            print(f"   (설정의 {k} 에서 꺾쇠 < > 를 떼고 씀)")
        if not v or "<" in v or ">" in v:
            die(f"n8n_push.local.json 의 {k} 가 아직 안 채워져 있다.\n"
                "    예시의 <꺾쇠> 는 지우고 그 안에 실제 값만 넣을 것.")
        c[k] = v

    # 키에 한글이나 눈에 안 보이는 문자가 섞이면 HTTP 헤더에 못 넣는다.
    # 그냥 두면 알아볼 수 없는 오류가 쏟아지므로 여기서 먼저 잡는다.
    bad = [ch for ch in c["api_key"] if not (32 <= ord(ch) < 127)]
    if bad:
        die("api_key 에 들어가면 안 되는 문자가 섞여 있다 "
            f"(예: {bad[0]!r}).\n"
            "    복사할 때 앞뒤 글자나 줄바꿈이 딸려 왔을 수 있다. 다시 붙여넣을 것.")
    c["base_url"] = normalize_base_url(c["base_url"])
    return c


def normalize_base_url(raw):
    """주소창에 보이는 걸 통째로 붙여넣어도 되게 앞부분만 남긴다.

    https://내주소.app.n8n.cloud/home/workflows  →  https://내주소.app.n8n.cloud
    """
    u = raw.strip().strip('"').strip("'")
    if not u.startswith(("http://", "https://")):
        if "/" in u or "." in u:            # app.n8n.cloud/... 처럼 스킴만 빠뜨린 경우
            u = "https://" + u
        else:
            die(f"base_url 이 주소로 보이지 않는다 (지금: {raw!r}).\n"
                "    n8n 을 브라우저에서 열고 주소창에 보이는 주소를 그대로 붙여넣을 것.")
    parts = urllib.parse.urlsplit(u)
    if not parts.netloc:
        die(f"base_url 에서 주소를 못 읽었다 (지금: {raw!r})")

    host = parts.netloc.lower()
    if host in ("app.n8n.cloud", "n8n.io", "www.n8n.io", "n8n.cloud"):
        die(f"'{host}' 은 계정/요금 관리 화면이지 내 n8n 이 아니다.\n"
            "    거기서 내 인스턴스를 열면 주소가 "
            "https://<내주소>.app.n8n.cloud 로 바뀐다. 그 주소를 넣을 것.")

    clean = f"{parts.scheme}://{parts.netloc}"
    if clean != raw.strip().rstrip("/"):
        print(f"   (주소를 {clean} 로 줄여서 씀)")
    return clean


# 인증서 검증용 컨텍스트. 처음엔 파이썬 기본값(가장 엄격)을 쓰고,
# 아래 _relax_ssl() 이 딱 한 번 필요한 만큼만 완화한다.
_SSL_CTX = None


def _relax_ssl():
    """파이썬 3.13+ 의 추가 엄격 검사(VERIFY_X509_STRICT)만 끈다.

    회사 네트워크가 TLS 를 가로채 자체 인증서로 바꿔치기하는 환경에서는,
    그 인증서에 Authority Key Identifier 확장이 없어 이 검사에 걸린다.
    (브라우저는 윈도우 인증서 저장소를 쓰고 이 검사를 안 해서 멀쩡히 열린다.)

    끄는 것은 그 형식 검사뿐이다 — **서명 사슬·호스트 이름·유효기간 확인은
    그대로 살아 있다.** 검증을 통째로 끄는 것(verify_mode=CERT_NONE)과는 다르다.
    """
    global _SSL_CTX
    ctx = ssl.create_default_context()
    ctx.verify_flags &= ~ssl.VERIFY_X509_STRICT
    _SSL_CTX = ctx


def _open(req):
    return urllib.request.urlopen(req, timeout=60, context=_SSL_CTX)


def api(conf, method, path, body=None, soft=False):
    """soft=True 면 실패해도 프로그램을 끝내지 않고 None 을 돌려준다."""
    url = f"{conf['base_url']}/api/v1{path}"
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("X-N8N-API-KEY", conf["api_key"])
    req.add_header("Accept", "application/json")
    if data:
        req.add_header("Content-Type", "application/json")
    try:
        try:
            with _open(req) as r:
                raw = r.read().decode("utf-8")
        except urllib.error.URLError as e:
            # 가로채기 인증서 때문이면 한 번만 완화해서 재시도한다
            if _SSL_CTX is None and isinstance(getattr(e, "reason", None),
                                               ssl.SSLCertVerificationError) \
                    and "Authority Key Identifier" in str(e.reason):
                print("   (이 서버 인증서에 Authority Key Identifier 가 없다 — "
                      "파이썬 3.13+ 의 추가 엄격 검사만 끄고 다시 시도한다.\n"
                      "    서명 사슬·호스트 이름·유효기간 확인은 그대로다.)")
                _relax_ssl()
                with _open(req) as r:
                    raw = r.read().decode("utf-8")
            else:
                raise
        return json.loads(raw) if raw.strip() else {}
    except urllib.error.HTTPError as e:
        if soft:
            return None
        detail = e.read().decode("utf-8", "replace")[:400]
        hint = ""
        if e.code == 401:
            hint = "\n    → api_key 가 틀렸거나 만료됐다. n8n Settings → n8n API 에서 다시 만들 것."
        elif e.code == 404 and path == "/workflows":
            hint = ("\n    → base_url 이 틀렸을 수 있다. n8n 화면 주소창의 "
                    "https://xxxx.app.n8n.cloud 까지만 넣는다(뒤에 /home 등은 뺀다).")
        die(f"n8n 응답 {e.code} — {method} {path}\n    {detail}{hint}")
    except urllib.error.URLError as e:
        die(f"n8n 에 접속할 수 없다 ({e.reason}). base_url 을 확인할 것.")
    except Exception as e:                      # 예상 못 한 것도 읽을 수 있게 내보낸다
        die(f"{method} {path} 중 문제가 생겼다 — {type(e).__name__}: {e}")


def remember_id(fname, wid):
    """찾은 워크플로 id 를 설정 파일에 적어 둔다.

    이름으로만 찾으면, 사람이 n8n 화면에서 이름을 바꾸는 순간(실제로 'M1 run' 으로
    바뀌어 있었다) 못 찾고 **똑같은 워크플로를 하나 더 만들어 버린다.**
    id 는 이름을 바꿔도 그대로라, 한 번 적어 두면 다시는 헷갈리지 않는다.
    """
    c = json.loads(io.open(CONF, encoding="utf-8").read().lstrip("﻿"))
    c.setdefault("workflows", {})[fname] = wid
    io.open(CONF, "w", encoding="utf-8", newline="\n").write(
        json.dumps(c, ensure_ascii=False, indent=2) + "\n")
    print(f"   (이 워크플로의 id {wid} 를 설정 파일에 적어 뒀다 — 다음부터는 바로 찾는다)")


def resolve_remote(conf, fname, local, idx):
    """이 파일이 n8n 의 어느 워크플로인지 정한다. (워크플로 or None, 설명)"""
    pinned = (conf.get("workflows") or {}).get(fname)
    if pinned:
        w = api(conf, "GET", f"/workflows/{pinned}", soft=True)
        if w:
            return w, f"설정에 적어둔 id {pinned}"
        print(f"   ! 설정의 id {pinned} 를 n8n 에서 못 찾았다 (지워졌나?) — 이름으로 다시 찾는다")

    stub = idx.get(local["name"])
    if stub:
        w = api(conf, "GET", f"/workflows/{stub['id']}")
        return w, f"이름이 같은 것 (id {stub['id']})"
    return None, None


def remote_index(conf):
    """이름 → 워크플로 요약. 이름으로 찾는다(ID 를 어딘가 적어둘 필요가 없게)."""
    out, cursor = {}, None
    while True:
        q = "/workflows?limit=250" + (f"&cursor={cursor}" if cursor else "")
        page = api(conf, "GET", q)
        for w in page.get("data", []):
            out[w["name"]] = w
        cursor = page.get("nextCursor")
        if not cursor:
            break
    return out


# --------------------------------------------------------------------------
# 비밀값 이어붙이기
# --------------------------------------------------------------------------
def _paths_with_placeholder(obj, path=()):
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield from _paths_with_placeholder(v, path + (k,))
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            yield from _paths_with_placeholder(v, path + (i,))
    elif isinstance(obj, str) and PLACEHOLDER in obj:
        yield path


def _get(obj, path):
    for p in path:
        try:
            obj = obj[p]
        except (KeyError, IndexError, TypeError):
            return None
    return obj


def _set(obj, path, val):
    for p in path[:-1]:
        obj = obj[p]
    obj[path[-1]] = val


# Google Sheets 노드는 화면에서 열어볼 때 시트의 컬럼 목록을 읽어 여기에 캐시해 둔다.
# 레포 파일에는 그게 없으므로, 그냥 덮어쓰면 캐시가 지워진다(동작은 하지만 화면에서
# 컬럼 목록이 빈 채로 보인다). 로컬에 없는 항목만 n8n 것을 그대로 살려 둔다.
UI_CACHE_KEYS = ("schema", "attemptToConvertTypes", "convertFieldsToString", "value")


def _keep_ui_cache(local_params, remote_params):
    lc, rc = local_params.get("columns"), remote_params.get("columns")
    if not isinstance(lc, dict) or not isinstance(rc, dict):
        return
    for k in UI_CACHE_KEYS:
        if k not in lc and k in rc:
            lc[k] = copy.deepcopy(rc[k])


def carry_over_secrets(local_wf, remote_wf):
    """로컬의 CHANGE-ME 자리에 n8n 의 현재 값을 도로 채운다.

    반환: (채운 개수, 못 채운 목록). 노드 이름이 같은 것끼리 같은 위치를 본다.
    """
    rnodes = {n["name"]: n for n in (remote_wf.get("nodes") or [])}
    filled, missing = 0, []
    for node in local_wf["nodes"]:
        rn = rnodes.get(node["name"])

        # 화면에서 붙인 Credential(Google Sheets)도 파일에는 없다. 같이 살린다.
        if rn and rn.get("credentials"):
            node["credentials"] = copy.deepcopy(rn["credentials"])

        params = node.get("parameters", {})
        if rn:
            _keep_ui_cache(params, rn.get("parameters", {}))
        for path in list(_paths_with_placeholder(params)):
            here = f"{node['name']} → {'.'.join(str(p) for p in path)}"
            cur = _get(rn.get("parameters", {}), path) if rn else None
            if isinstance(cur, str) and cur.strip() and PLACEHOLDER not in cur:
                _set(params, path, cur)
                filled += 1
            else:
                missing.append(here)
    return filled, missing


# --------------------------------------------------------------------------
# 비교 / 출력
# --------------------------------------------------------------------------
def summarize(local_wf, remote_wf):
    """사람이 읽을 수 있는 변경 요약. 비밀값은 절대 찍지 않는다."""
    if not remote_wf:
        return [f"새로 만든다 — 노드 {len(local_wf['nodes'])}개"]
    ln = {n["name"]: n for n in local_wf["nodes"]}
    rn = {n["name"]: n for n in (remote_wf.get("nodes") or [])}
    lines = []
    for name in ln.keys() - rn.keys():
        lines.append(f"+ 노드 추가   {name}")
    for name in rn.keys() - ln.keys():
        lines.append(f"- 노드 삭제   {name}")
    for name in sorted(ln.keys() & rn.keys()):
        a = json.dumps(ln[name].get("parameters"), sort_keys=True, ensure_ascii=False)
        b = json.dumps(rn[name].get("parameters"), sort_keys=True, ensure_ascii=False)
        if a != b:
            code = ln[name].get("parameters", {}).get("jsCode")
            extra = ""
            if code is not None:
                old = rn[name].get("parameters", {}).get("jsCode", "") or ""
                extra = f" (코드 {old.count(chr(10)) + 1}줄 → {code.count(chr(10)) + 1}줄)"
            lines.append(f"~ 내용 변경   {name}{extra}")
    la = json.dumps(local_wf.get("connections"), sort_keys=True)
    rb = json.dumps(remote_wf.get("connections"), sort_keys=True)
    if la != rb:
        lines.append("~ 연결선 변경")
    return lines or ["바뀐 것 없음"]


def read_local(fname):
    p = os.path.join(HERE, fname)
    if not os.path.exists(p):
        die(f"파일이 없다: {fname}")
    wf = json.loads(io.open(p, encoding="utf-8").read().lstrip("﻿"))
    for k in PUT_KEYS:
        if k not in wf:
            die(f"{fname} 에 '{k}' 가 없다 — n8n 이 받아주지 않는 모양이다")
    return wf


# --------------------------------------------------------------------------
def do_push(conf, files, dry, force):
    idx = remote_index(conf)
    print(f"n8n 에 있는 워크플로 {len(idx)}개 확인\n")

    # 1단계 — 전부 먼저 검사한다. 하나라도 문제가 있으면 아무것도 밀어넣지 않는다.
    # (앞의 것만 반영되고 뒤의 것이 막히면 두 워크플로가 서로 안 맞는 상태가 된다.)
    plans, blocked = [], False
    for fname in files:
        local = read_local(fname)
        print(f"── {fname}")
        remote_full, how = resolve_remote(conf, fname, local, idx)

        if remote_full:
            state = "켜짐" if remote_full.get("active") else "꺼짐"
            print(f"   n8n 쪽 이름  : {remote_full['name']}")
            print(f"   찾은 방법    : {how}")
            print(f"   상태         : 자동실행 {state}")
            if remote_full["name"] != local["name"]:
                # 화면에서 붙인 이름이 사람의 선택이다. 파일 이름으로 되돌리지 않는다.
                print(f"   (파일상 이름 '{local['name']}' 과 다르지만 그대로 둔다)")
        else:
            print(f"   n8n 쪽 상태  : 없음 → '{local['name']}' 으로 새로 만든다")

        filled, missing = carry_over_secrets(local, remote_full or {})
        print(f"   비밀값       : {filled}개 이어받음", end="")
        if missing:
            print(f" · {len(missing)}개 못 찾음")
            for m in missing:
                print(f"                  ! {m}")
            if not remote_full:
                print("                  (새 워크플로라 당연하다 — 만든 뒤 화면에서 채울 것)")
            elif force:
                print("                  (--force 라 그대로 올린다)")
            else:
                blocked = True
        else:
            print()

        for line in summarize(local, remote_full):
            print(f"   {line}")
        print()
        plans.append((fname, local, remote_full))

    if blocked:
        die("n8n 에서 비밀값을 못 찾은 자리가 있어 **아무것도** 밀어넣지 않았다.\n"
            "    그대로 덮어썼다면 워크플로가 CHANGE-ME 상태로 죽었을 것이다.\n"
            "    n8n 화면에서 해당 노드에 값이 들어 있는지 확인하거나,\n"
            "    정말 그대로 올리려면 --force 를 붙일 것.")
    if dry:
        print("--dry-run 이라 아무것도 바꾸지 않았다.")
        return

    # 2단계 — 여기부터 실제 반영
    for fname, local, remote_full in plans:
        body = {k: local[k] for k in PUT_KEYS}
        if remote_full:
            body["name"] = remote_full["name"]      # 화면에서 붙인 이름을 지키다
            wid = remote_full["id"]
            api(conf, "PUT", f"/workflows/{wid}", body)
            print(f"   ✓ {fname} → '{body['name']}' 반영 완료 "
                  f"(이름·자동실행 상태는 건드리지 않았다)")
        else:
            new = api(conf, "POST", "/workflows", body)
            wid = new.get("id")
            print(f"   ✓ {fname} 새로 만들었다 (id {wid}). "
                  f"화면에서 비밀값을 채우고 자동실행을 켤 것")
        if (conf.get("workflows") or {}).get(fname) != wid:
            remember_id(fname, wid)
    print()


def do_pull(conf, files):
    """n8n 쪽을 로컬 파일로 가져온다. 화면에서 고친 걸 잃지 않기 위해.
    비밀값은 다시 CHANGE-ME 로 가려서 저장한다 — 깃에 올라가는 파일이므로."""
    idx = remote_index(conf)
    for fname in files:
        local = read_local(fname)
        remote, how = resolve_remote(conf, fname, local, idx)
        if not remote:
            print(f"── {fname}: n8n 에서 짝을 못 찾았다 — 건너뜀")
            continue

        # 로컬에서 CHANGE-ME 였던 자리는 다시 CHANGE-ME 로 되돌린다.
        # 이름은 파일 쪽 것을 유지한다 — 화면 이름('M1 run' 등)은 그 사람의 것이고
        # 레포 파일은 어떤 워크플로인지 알 수 있는 이름이어야 한다.
        out = {k: copy.deepcopy(remote.get(k)) for k in PUT_KEYS}
        out["name"] = local["name"]
        onodes = {n["name"]: n for n in out["nodes"]}
        masked = 0
        for node in local["nodes"]:
            tgt = onodes.get(node["name"])
            if not tgt:
                continue
            for path in _paths_with_placeholder(node.get("parameters", {})):
                if _get(tgt.get("parameters", {}), path) is not None:
                    _set(tgt["parameters"], path, _get(node["parameters"], path))
                    masked += 1
            tgt.pop("credentials", None)

        p = os.path.join(HERE, fname)
        io.open(p, "w", encoding="utf-8", newline="\n").write(
            json.dumps(out, ensure_ascii=False, indent=2) + "\n")
        print(f"── {fname}: '{remote['name']}' 에서 내려받음 ({how}) — "
              f"노드 {len(out['nodes'])}개, 비밀값 {masked}자리 가림")
        if (conf.get("workflows") or {}).get(fname) != remote["id"]:
            remember_id(fname, remote["id"])
    print("\n`git diff` 로 뭐가 달라졌는지 확인할 것.")


def main():
    ap = argparse.ArgumentParser(description="워크플로 JSON 을 n8n 에 반영한다")
    ap.add_argument("--dry-run", action="store_true", help="바뀔 내용만 보여주고 끝낸다")
    ap.add_argument("--pull", action="store_true", help="반대 방향 — n8n → 로컬 파일")
    ap.add_argument("--only", choices=["1", "2"], help="1번 또는 2번 워크플로만")
    ap.add_argument("--force", action="store_true",
                    help="비밀값을 못 찾아도 그냥 올린다 (워크플로가 멈출 수 있다)")
    a = ap.parse_args()

    files = FILES if not a.only else [FILES[int(a.only) - 1]]
    conf = load_conf()
    print(f"\n대상: {conf['base_url']}\n")
    if a.pull:
        do_pull(conf, files)
    else:
        do_push(conf, files, a.dry_run, a.force)


if __name__ == "__main__":
    main()
