# -*- coding: utf-8 -*-
"""
법령·법안 수집기 — laws.json 을 만든다.

뉴스와 다른 점: 기사는 기자가 해석한 것이고 이건 원문이다.
"법안이 개정중이다", "언제부터 단속이 강화된다" 를 정확히 알려면 두 갈래가 필요하다.

  1) 법제처 국가법령정보 API  → 이미 공포된 법. 공포일·시행일·개정이유
  2) 열린국회정보 API         → 아직 국회에 계류 중인 발의 법안

이 둘을 섞으면 안 된다. 통과된 법과 발의만 된 법안은 완전히 다르다.
뉴스 기사가 부정확했던 이유가 정확히 이것이다.

    python3 collect_laws.py

환경변수
    LAW_OC        법제처 OPEN API 신청 시 받는 ID (이메일 앞부분). 없으면 test 로 동작
    ASSEMBLY_KEY  열린국회정보 인증키. 없으면 국회 법안 부분만 건너뛴다

둘 다 없어도 죽지 않는다. 못 가져온 부분은 화면에 "키 미등록"으로 표시된다.
"""

import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode
from urllib.request import Request, urlopen

KST = timezone(timedelta(hours=9))
OUT_PATH = "laws.json"
UA = {"User-Agent": "WeVapeLawMonitor/1.0"}

LAW_BASE = "https://www.law.go.kr/DRF"
ASSEMBLY_BASE = "https://open.assembly.go.kr/portal/openapi"

# 추적할 법령. 우리 매장 운영에 실제로 처분이 나오는 것만.
# 이름은 법제처 '법령명한글' 과 정확히 일치해야 한다.
TRACK_LAWS = [
    ("담배사업법",            "매장 운영의 뿌리. 소매인 지정, 온라인 판매, 광고 제한"),
    ("담배사업법 시행령",      "법에서 위임한 세부 기준. 실제 단속 기준이 여기 있다"),
    ("담배사업법 시행규칙",    "서식과 절차. 소매인 지정 신청·변경이 여기"),
    ("국민건강증진법",         "금연구역, 경고그림·문구, 광고 제한"),
    ("청소년 보호법",          "청소년 판매 금지. 과징금·영업정지가 여기서 나온다"),
]

# 국회 발의 법안에서 찾을 말. 너무 넓히면 무관한 법안이 쏟아진다.
BILL_WORDS = ["담배", "니코틴", "전자담배", "흡연", "금연"]

# 법안 제목에 이게 있으면 우리와 무관 — 농업·재배 쪽 법안이 자주 걸린다
BILL_NOISE = ["엽연초", "연초경작", "재배농가", "경작자", "농업소득"]


def get(url, timeout=15):
    with urlopen(Request(url, headers=UA), timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def jget(url, timeout=15):
    return json.loads(get(url, timeout))


def ymd(s):
    """20260424 → 2026-04-24"""
    s = re.sub(r"[^0-9]", "", str(s or ""))
    return f"{s[0:4]}-{s[4:6]}-{s[6:8]}" if len(s) >= 8 else ""


def flat(v):
    """법제처는 문자열을 [[...]] 로 겹쳐 주기도 한다. 평평하게 편다."""
    if v is None:
        return []
    if isinstance(v, str):
        return [v]
    out = []
    for x in v:
        out.extend(flat(x))
    return out


def clean_reason(raw):
    """제개정이유에서 사람이 읽을 부분만 남긴다."""
    lines = [re.sub(r"\s+", " ", x).strip() for x in flat(raw)]
    drop = ("<법제처 제공>", "[일부개정]", "[전부개정]", "[제정]", "[타법개정]")
    lines = [x for x in lines if x and x not in drop and not x.startswith("◇")]
    return " ".join(lines)[:600]


# ────────────────────────────────────────────────────────────
# 1) 법제처 — 공포된 법
# ─────────────────────────────────────────────────────────────

def fetch_law(name, why, oc):
    """법령 하나의 현재 상태와 개정 이유를 가져온다."""
    q = urlencode({"OC": oc, "target": "law", "type": "JSON",
                   "query": name, "display": "5"})
    try:
        js = jget(f"{LAW_BASE}/lawSearch.do?{q}")
    except Exception as e:
        print(f"  [실패] {name} 목록 조회: {e}")
        return None

    items = js.get("LawSearch", {}).get("law") or []
    if isinstance(items, dict):
        items = [items]
    # 이름이 정확히 같은 것만. '담배사업법' 검색에 시행령·시행규칙이 섞여 온다.
    hit = next((x for x in items if (x.get("법령명한글") or "").strip() == name), None)
    if not hit:
        print(f"  [없음] {name} — 법령명이 정확히 일치하는 항목이 없습니다")
        return None

    law_id = hit.get("법령ID", "")
    rec = {
        "name": name,
        "why": why,
        "id": law_id,
        "kind": hit.get("법령구분명", ""),
        "dept": hit.get("소관부처명", ""),
        "no": hit.get("공포번호", ""),
        "promulgated": ymd(hit.get("공포일자")),
        "effective": ymd(hit.get("시행일자")),
        "revision": hit.get("제개정구분명", ""),
        "link": f"https://www.law.go.kr/법령/{name}",
        "reason": "",
    }

    # 개정 이유는 본문 조회에만 있다. 이게 직원이 실제로 읽을 부분이다.
    try:
        q2 = urlencode({"OC": oc, "target": "law", "type": "JSON", "ID": law_id})
        body = jget(f"{LAW_BASE}/lawService.do?{q2}", timeout=25)
        rec["reason"] = clean_reason(
            (body.get("법령", {}).get("제개정이유") or {}).get("제개정이유내용"))
    except Exception as e:
        print(f"  [주의] {name} 개정이유 조회 실패: {e}")

    return rec


def fetch_upcoming(name, oc, today):
    """아직 시행 안 된 개정분. 'D-며칠 뒤부터 바뀐다' 를 잡는 부분."""
    q = urlencode({"OC": oc, "target": "eflaw", "type": "JSON",
                   "query": name, "display": "20"})
    try:
        js = jget(f"{LAW_BASE}/lawSearch.do?{q}")
    except Exception:
        return []
    items = js.get("LawSearch", {}).get("law") or []
    if isinstance(items, dict):
        items = [items]
    out = []
    for x in items:
        if (x.get("법령명한글") or "").strip() != name:
            continue
        eff = ymd(x.get("시행일자"))
        if eff and eff > today:
            out.append({
                "name": name,
                "effective": eff,
                "promulgated": ymd(x.get("공포일자")),
                "revision": x.get("제개정구분명", ""),
                "no": x.get("공포번호", ""),
                "link": f"https://www.law.go.kr/법령/{name}",
            })
    return out


# ────────────────────────────────────────────────────────────
# 2) 열린국회정보 — 아직 통과 안 된 법안
# ─────────────────────────────────────────────────────────────

def fetch_bills(key, age="22"):
    """국회에 발의된 담배 관련 법률안. 통과된 게 아니라 '논의 중'인 것."""
    if not key:
        return [], "국회 인증키(ASSEMBLY_KEY)가 등록되지 않아 건너뛰었습니다"

    seen, bills = set(), []
    for word in BILL_WORDS:
        q = urlencode({"KEY": key, "Type": "json", "pIndex": "1", "pSize": "100",
                       "AGE": age, "BILL_NAME": word})
        try:
            js = jget(f"{ASSEMBLY_BASE}/nzmimeepazxkubdpn?{q}", timeout=20)
        except Exception as e:
            print(f"  [실패] 국회 '{word}': {e}")
            continue

        rows = []
        for v in js.values():
            if isinstance(v, list):
                for blk in v:
                    if isinstance(blk, dict) and isinstance(blk.get("row"), list):
                        rows = blk["row"]
        if not rows:
            continue

        for r in rows:
            title = (r.get("BILL_NAME") or "").strip()
            bid = r.get("BILL_ID") or title
            if not title or bid in seen:
                continue
            if any(n in title for n in BILL_NOISE):
                continue
            seen.add(bid)
            result = (r.get("PROC_RESULT") or "").strip()
            bills.append({
                "title": title,
                "no": r.get("BILL_NO", ""),
                "proposer": (r.get("PROPOSER") or "").strip(),
                "date": (r.get("PROPOSE_DT") or "").strip(),
                "committee": (r.get("COMMITTEE") or "").strip(),
                "result": result or "계류 중",
                "done": bool(result),
                "link": r.get("DETAIL_LINK", ""),
                "kw": word,
            })

    bills.sort(key=lambda x: x["date"], reverse=True)
    return bills[:40], ""


# ─────────────────────────────────────────────────────────────

def main():
    now = datetime.now(KST)
    today = now.strftime("%Y-%m-%d")

    oc = os.environ.get("LAW_OC", "").strip()
    oc_is_test = not oc
    if oc_is_test:
        oc = "test"
        print("[주의] LAW_OC 가 없어 test 계정으로 조회합니다. 운영용 ID를 등록하세요.")

    print(f"법령 수집 시작 — {today}")

    laws, upcoming = [], []
    for name, why in TRACK_LAWS:
        rec = fetch_law(name, why, oc)
        if rec:
            laws.append(rec)
            print(f"  {name}: 시행 {rec['effective']} · {rec['revision']}")
        upcoming.extend(fetch_upcoming(name, oc, today))

    # 시행 예정에 D-day 를 붙인다
    for u in upcoming:
        u["dday"] = (datetime.strptime(u["effective"], "%Y-%m-%d").date() - now.date()).days
    upcoming.sort(key=lambda x: x["effective"])

    # 최근 시행분(지난 180일)도 따로 모은다 — "이거 언제부터 바뀐 거죠?" 대비
    since = (now - timedelta(days=180)).strftime("%Y-%m-%d")
    recent = sorted([l for l in laws if l["effective"] >= since],
                    key=lambda x: x["effective"], reverse=True)

    bills, bills_note = fetch_bills(os.environ.get("ASSEMBLY_KEY", "").strip())
    pending = [b for b in bills if not b["done"]]
    print(f"  국회 법안 {len(bills)}건 (계류 {len(pending)}건) {bills_note}")

    payload = {
        "updatedAt": now.strftime("%Y-%m-%d %H:%M:%S KST"),
        "ocIsTest": oc_is_test,
        "billsNote": bills_note,
        "laws": laws,
        "upcoming": upcoming[:12],
        "recent": recent,
        "bills": bills,
        "pendingCount": len(pending),
        "sources": [
            {"name": "법제처 국가법령정보 OPEN API",
             "url": "https://open.law.go.kr/LSO/openApi/guideList.do",
             "note": "공포·시행일자와 개정이유의 원출처. 법령 원문 그대로"},
            {"name": "열린국회정보 OPEN API",
             "url": "https://open.assembly.go.kr/portal/openapi/main.do",
             "note": "국회에 발의된 법률안. 아직 통과된 것이 아님"},
        ],
    }
    json.dump(payload, open(OUT_PATH, "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    print(f"\n완료: 법령 {len(laws)}건 · 시행예정 {len(upcoming)}건 · 법안 {len(bills)}건 → {OUT_PATH}")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        # 법령 수집이 실패해도 뉴스 수집까지 죽이면 안 된다
        print(f"[중단] {e}")
        sys.exit(0)
