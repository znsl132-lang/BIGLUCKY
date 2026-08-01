# -*- coding: utf-8 -*-
"""
필터 회귀 테스트 — samples.json 의 고정 샘플 20건이 계속 올바르게 판정되는지 확인한다.

    python3 test_filters.py

필터를 고칠 때마다 반드시 돌린다. 전부 PASS 여야 한다.
FAIL 이 나오면 방금 고친 필터가 과거에 잡았던 것을 놓쳤거나, 정상 글을 죽인 것이다.

새 오탐을 발견하면 samples.json 에 케이스를 추가한다.
필터만 고치고 샘플을 안 남기면 같은 실수가 반복된다. (감독관 지시 ⑤)
"""

import json
import os
import sys

os.environ.setdefault("NAVER_CLIENT_ID", "test")
os.environ.setdefault("NAVER_CLIENT_SECRET", "test")

import collect_feeds as C


def judge(case):
    """수집기의 필터 규칙을 그대로 적용해 통과/차단을 판정한다."""
    g = next((x for x in C.GROUPS if x["id"] == case["group"]), None)
    if not g:
        return "block", f"그룹 없음: {case['group']}"

    title, desc = case["title"], case["desc"]
    where, src = case.get("where", ""), case.get("src", "news")
    blob = f"{title} {desc}"
    low = blob.lower()
    link = f"https://example.com/{case['id']}"

    if any(w in blob for w in C.HARD_BLOCK):
        return "block", "HARD_BLOCK"
    if C.is_keyword_spam(title, desc, where):
        return "block", "키워드 나열 스팸"
    if any(d in link.lower() for d in C.BAD_DOMAINS):
        return "block", "차단 도메인"

    hw = g.get("hard_words") or []
    if hw and any(w in blob for w in hw):
        return "block", "구제 불가 차단어"

    dw = g.get("drop_words") or []
    if dw and any(w in blob for w in dw):
        rs = g.get("rescue_any")
        if not (rs and all(any(w in low for w in grp) for grp in rs)):
            hit = next(w for w in dw if w in blob)
            return "block", f"차단어({hit})"

    ra = g.get("require_any") or []
    if ra:
        sets = ra if isinstance(ra[0], list) else [ra]
        if not all(any(w in low for w in s) for s in sets):
            return "block", "소재 불일치"

    rw = g.get("require_where") or []
    if rw and not any(x in where.lower() for x in rw):
        return "block", "카페 이름 불일치"

    rt = g.get("require_title") or []
    if rt and not any(w in title.lower() for w in rt):
        return "block", "제목 불일치"

    return "pass", "통과"


def main():
    with open("samples.json", encoding="utf-8") as f:
        cases = json.load(f)["cases"]

    ok = fail = 0
    fails = []
    print(f"필터 회귀 테스트 — 샘플 {len(cases)}건\n" + "─" * 78)
    for c in cases:
        got, reason = judge(c)
        good = got == c["expect"]
        mark = "PASS" if good else "FAIL"
        if good:
            ok += 1
        else:
            fail += 1
            fails.append((c, got, reason))
        print(f"  [{mark}] #{c['id']:2d} {c['group']:10s} 기대={c['expect']:5s} 실제={got:5s}"
              f"  {c['title'][:34]}")

    print("─" * 78)
    print(f"  PASS {ok} · FAIL {fail}")

    if fails:
        print("\n실패한 케이스")
        for c, got, reason in fails:
            print(f"\n  #{c['id']} {c['title']}")
            print(f"    기대: {c['expect']} / 실제: {got} ({reason})")
            print(f"    이 케이스가 있는 이유: {c['why']}")
        sys.exit(1)

    print("\n  전부 통과. 필터가 과거 오탐을 그대로 막고 있고 정상 글도 살아 있습니다.")


if __name__ == "__main__":
    main()
