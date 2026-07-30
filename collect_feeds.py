# -*- coding: utf-8 -*-
"""
We Vape 이슈 수집기 — 네이버 뉴스·블로그·카페글·웹문서 + 검색어 트렌드

환경변수 (GitHub Secrets):
  NAVER_CLIENT_ID     : NCP NAVER API HUB Client ID
  NAVER_CLIENT_SECRET : NCP NAVER API HUB Client Secret

결과: feeds.json  (대시보드와 알림 스크립트가 함께 읽는다)

▼ 고칠 곳은 딱 두 군데다
  1. BRAND_TERMS / STORE_TERMS — 실제 간판·네이버플레이스 표기에 맞게
  2. GROUPS 의 keywords
"""

import hashlib
import html as htmllib
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

KST = timezone(timedelta(hours=9))
OUT_PATH = os.environ.get("OUT_PATH", "feeds.json")

SEARCH_HOST = "https://naverapihub.apigw.ntruss.com"
# 검색어 트렌드는 호스트가 이관 중이라 두 곳을 순서대로 시도한다
TREND_HOSTS = [
    "https://naverapihub.apigw.ntruss.com",
    "https://naveropenapi.apigw.ntruss.com",
]

WINDOW_HOURS = int(os.environ.get("WINDOW_HOURS", "30"))   # 뉴스·블로그 기간 필터
DISPLAY = 30                                              # 키워드당 조회 건수
MAX_PER_GROUP = 30                                        # 그룹당 노출 상한 (60→30, 다 안 읽는다)
MAX_PER_SOURCE = 3                                        # 같은 언론사·카페 최대 건수
HISTORY_DAYS = 90                                         # 추이 보관 기간
SEEN_DAYS = 30                                            # 중복 판정용 링크 보관 기간

# ─────────────────────────────────────────────────────────────
# ① 우리 매장 — 여기가 가장 중요하다. 실제 표기로 맞출 것
# ─────────────────────────────────────────────────────────────
BRAND_TERMS = ["위베이프", "WEVAPE", "위베이프 전자담배"]

# 우리 9지점 (2026-07-30 대표 확인)
#   구월길병원점(=구월점) · 구월로데오점 · 부천상동점 · 부천중동점
#   인천공항점 · 인천연수점 · 인천논현점 · 인천계산점 · 인천검단점
STORE_TERMS = [
    "위베이프 구월길병원",
    "위베이프 길병원",
    "위베이프 구월로데오",
    "위베이프 로데오",
    "위베이프 부천상동",
    "위베이프 부천중동",
    "위베이프 인천공항",
    "위베이프 인천연수",
    "위베이프 인천논현",
    "위베이프 인천계산",
    "위베이프 인천검단",
]

# 우리 9지점을 가리키는 지역 토큰. 타 가맹점 글을 걸러내는 기준이다.
OUR_AREAS = ["길병원", "로데오", "구월", "상동", "중동", "공항", "연수", "논현", "계산", "검단"]

# 타 지역·타 가맹점 표기. 위베이프 브랜드 글이지만 우리 매장이 아니다.
# 화면에서 우리 지점이 아닌 글이 보이면 여기에 지역명을 추가하면 된다.
#   '중동현대'는 우리 부천중동점이 아니다 (2026-07-30 확인)
OTHER_STORES = ["중동현대", "강남역", "신논현", "역삼", "압구정", "대구", "호산동",
                "부평", "굴포천", "부산", "대전", "광주", "천안", "수원", "청라", "송도"]

# 불만족·컴플레인 신호. 매장 언급 글에서 이게 잡히면 먼저 보여주고 카톡으로도 알린다.
NEGATIVE_WORDS = [
    "불친절", "무례", "퉁명", "짜증", "성의없", "성의 없", "응대가", "태도가",
    "별로", "실망", "최악", "비추", "다시 안", "안 갈", "안갈", "후회",
    "환불", "교환 거부", "거부당", "항의", "컴플레인", "불만", "사과",
    "불량", "고장", "누유", "새서", "새요", "터짐", "터졌", "먹통", "안 켜",
    "비싸", "바가지", "가격이", "헛걸음", "품절", "재고 없", "재고없",
    "오래 기다", "한참 기다", "설명도 안", "모른다고",
]

# 담배·전자담배 소재 관련어. 업종·브랜드 뉴스는 이게 실제로 언급돼야 통과한다.
TOBACCO_WORDS = [
    "전자담배", "담배", "니코틴", "액상", "흡연", "금연", "담뱃세", "궐련", "연초",
    "베이프", "vape", "담배사업법", "유해성",
]

# 매장 운영에 실제로 걸리는 경제·사회 소재
LOCAL_BIZ_WORDS = [
    "소상공인", "자영업", "상권", "임대료", "카드 수수료", "카드수수료", "최저임금",
    "폐업", "창업", "매출", "소비", "내수", "골목상권", "전통시장",
    "배달", "인건비", "대출", "지원금", "부가세", "종합소득세", "생활물가",
]
# '위베이프'로 검색하면 동명의 온라인 쇼핑몰 글이 섞인다. 우리 매장 언급이 아니므로 버린다.
STORE_NOISE = ["wevape.co.kr", "우주베이프", "쿠팡", "네이버쇼핑", "스마트스토어", "무료배송", "택배발송"]

# 네이버 검색은 '위베이프 구월'을 "베이프" + "구월" 로 느슨하게 매칭한다.
# 그래서 베이프스킨·Bape 의류·메타베이프 카페 글이 대량으로 섞인다.
# 제목이나 요약에 아래 표기가 실제로 있는 글만 남긴다.
# 두 조건을 모두 만족해야 통과: (브랜드 표기) AND (우리 지점 지역명)
# 이 두 번째 조건이 강남역점·대구·부평 같은 타 가맹점 글을 걸러낸다.
STORE_REQUIRE = [
    ["위베이프", "wevape", "we vape"],
    OUR_AREAS,
]

# 담배사업법상 '담배'는 액상·연초다. 기기(디바이스) 중고거래는 온라인 판매 금지 대상이 아니다.
# 두 조건을 모두 만족해야 통과한다: (액상·니코틴 언급) AND (판매·거래 의도)
WATCH_REQUIRE = [
    ["액상", "니코틴", "리퀴드", "무니코틴", "합성니코틴", "원액"],
    ["판매", "팝니다", "팔아", "택배", "거래", "구매대행", "양도", "넘겨", "처분"],
]

# ─────────────────────────────────────────────────────────────
# ② 그룹 정의 — 위에 있는 그룹이 대시보드에서 먼저 보인다
# ─────────────────────────────────────────────────────────────
GROUPS = [
    {
        "id": "store",
        "label": "우리 매장",
        "desc": "매장·지점 언급 감시",
        "sources": ["cafearticle", "blog"],
        # 우리 9지점만 본다. 브랜드 전체 언급을 보려면 BRAND_TERMS + STORE_TERMS 로 바꾸고
        # STORE_REQUIRE 의 두 번째 조건(OUR_AREAS)을 지우면 된다.
        "keywords": STORE_TERMS,
        "drop_ads": False,          # 매장 언급은 광고성이라도 봐야 한다
        "drop_politics": False,
        "drop_words": STORE_NOISE + OTHER_STORES,   # 동명 쇼핑몰 + 타 지역 가맹점 제거
        "require_any": STORE_REQUIRE,   # 브랜드 표기가 본문에 실제로 있어야 통과
        "alert": True,              # 새 글이 잡히면 카톡 알림
    },
    {
        "id": "watch",
        "label": "판매 감시",
        "desc": "온라인·택배 판매 (26.4.24 이후 금지)",
        "sources": ["cafearticle", "webkr"],
        "keywords": [
            "액상 택배",
            "액상 판매합니다",
            "니코틴 원액 판매",
            "무니코틴 액상 판매",
            "전자담배 액상 구매대행",
            "액상 대량 판매",
            "폐업 액상 정리",
        ],
        "drop_ads": False,
        "drop_politics": False,
        "require_any": WATCH_REQUIRE,   # 기기만 거래하는 글은 제외
        "alert": True,
    },
    {
        "id": "vape",
        "label": "업종 뉴스",
        "desc": "규제 · 시장",
        "sources": ["news"],
        "keywords": [
            "전자담배",
            "액상형 전자담배",
            "궐련형 전자담배",
            "합성니코틴",
            "담배사업법",
            "담뱃세",
            "금연정책",
            "청소년 담배",
        ],
        "drop_ads": False,
        "drop_politics": False,     # 규제 뉴스는 국회·법안 언급이 필연이다
        "require_any": TOBACCO_WORDS,   # 소재가 담배가 아닌 기사 제거
        "alert": False,
    },
    {
        "id": "community",
        "label": "커뮤니티",
        "desc": "후기 · 고장 사례",
        "sources": ["cafearticle", "blog"],
        "keywords": [
            "전자담배 후기",
            "액상 추천",
            "전자담배 고장",
            "코일 누유",
            "기기 불량",
            "입호흡 액상",
            "폐호흡 액상",
        ],
        "drop_ads": True,           # 체험단·협찬 포스팅 제거
        "drop_politics": False,
        "require_any": ["전자담배", "액상", "코일", "누유", "입호흡", "폐호흡",
                        "베이프", "팟", "기기", "니코틴", "카트리지"],
        "alert": False,
    },
    {
        "id": "brand",
        "label": "경쟁사·브랜드",
        "desc": "브랜드 동향",
        "sources": ["news", "cafearticle"],
        "keywords": ["쥴 전자담배", "릴 전자담배", "아이코스", "글로 전자담배",
                     "KT&G", "필립모리스", "BAT로스만스"],
        "drop_ads": True,
        "drop_politics": False,
        # KT&G 인삼공사·부동산 기사, 필립모리스 주가 기사 등을 걸러낸다
        "require_any": TOBACCO_WORDS,
        "alert": False,
    },
    {
        "id": "econ",
        "label": "경제·상권",
        "desc": "자영업·소상공인 관점 · 정치 제외",
        "sources": ["news"],
        "keywords": ["소상공인", "자영업", "상권", "카드 수수료", "최저임금",
                     "임대료", "내수 소비", "생활물가"],
        "drop_ads": False,
        "drop_politics": True,
        # 거시 시황 기사가 아니라 매장 운영에 걸리는 기사만 남긴다
        "require_any": LOCAL_BIZ_WORDS,
        "alert": False,
    },
]

# ─────────────────────────────────────────────────────────────
# ③ 검색어 트렌드 — 최대 5그룹, 그룹당 5키워드
# ─────────────────────────────────────────────────────────────
TREND_GROUPS = [
    {"groupName": "전자담배",   "keywords": ["전자담배", "액상 전자담배", "전자담배 액상"]},
    {"groupName": "브랜드",     "keywords": ["쥴", "릴", "아이코스", "글로"]},
    {"groupName": "액상",       "keywords": ["입호흡 액상", "폐호흡 액상", "무니코틴 액상"]},
    {"groupName": "지역",       "keywords": ["인천 전자담배", "부천 전자담배"]},
    {"groupName": "금연",       "keywords": ["금연", "금연보조제"]},
]
TREND_DAYS = 90

# ─────────────────────────────────────────────────────────────
# 필터 단어
# ─────────────────────────────────────────────────────────────
POLITICS_WORDS = [
    "대통령", "대선", "총선", "지방선거", "선거", "공천", "탄핵", "개각",
    "국민의힘", "더불어민주당", "민주당", "조국혁신당", "개혁신당",
    "여당", "야당", "여야", "정당", "당대표", "원내대표", "최고위",
    "국회의원", "의원총회", "청문회", "국정감사", "대정부질문",
    "청와대", "대통령실", "정치권", "출마", "당론", "정계",
]
AD_WORDS = [
    "협찬", "체험단", "원고료", "제공받아", "제공 받아", "소정의", "무상으로 제공",
    "파트너스", "제휴", "광고 포함", "유료광고", "서포터즈", "리뷰단",
]
# 소재와 무관한 기사 유형. 뉴스가 중구난방해지는 주범이다.
NOISE_WORDS = [
    "부고", "인사발령", "인사]", "포토", "화보", "오늘의 운세", "주간 운세", "신간",
    "코스피", "코스닥", "증시", "마감시황", "개장", "장마감", "특징주", "상한가", "하한가",
    "주가", "목표주가", "매수의견", "공시", "유상증자", "배당", "실적발표", "컨센서스",
    "오늘의 날씨", "주간 날씨", "미세먼지 농도", "로또", "부동산 시황", "분양", "청약",
    "인사·부고", "동정", "만평", "사설", "오늘의 사진",
]


SOURCE_PATH = {
    "news": "/search/v1/news",
    "blog": "/search/v1/blog",
    "cafearticle": "/search/v1/cafearticle",
    "webkr": "/search/v1/webkr",
}
SOURCE_LABEL = {"news": "뉴스", "blog": "블로그", "cafearticle": "카페", "webkr": "웹"}


# ─────────────────────────────────────────────────────────────
# 유틸
# ─────────────────────────────────────────────────────────────

def env(name):
    v = os.environ.get(name, "").strip()
    if not v:
        sys.exit(f"[중단] 환경변수 {name} 가 비어 있습니다. GitHub Secrets 를 확인하세요.")
    return v


def clean(text):
    return htmllib.unescape(re.sub(r"<[^>]+>", "", text or "")).strip()


def h10(s):
    return hashlib.sha1(s.encode("utf-8")).hexdigest()[:10]


def norm_title(t):
    return re.sub(r"[^\w가-힣]", "", t)[:28]


def call(path, params, cid, csec, retries=3):
    url = f"{SEARCH_HOST}{path}?{urlencode(params)}"
    req = Request(url, headers={"X-NCP-APIGW-API-KEY-ID": cid,
                                "X-NCP-APIGW-API-KEY": csec})
    for i in range(retries):
        try:
            with urlopen(req, timeout=15) as r:
                return json.loads(r.read().decode("utf-8"))
        except HTTPError as e:
            msg = e.read().decode("utf-8", "ignore")[:180]
            if e.code == 429:
                time.sleep(2 * (i + 1)); continue
            if e.code == 401:
                sys.exit(f"[중단] 인증 실패(401). Client ID/Secret 또는 API 권한 확인. {msg}")
            if e.code == 403:
                print(f"  ! 403 — Application 에서 해당 API 를 선택했는지 확인하세요. {msg}")
                return None
            print(f"  ! HTTP {e.code}: {msg}")
            return None
        except (URLError, TimeoutError) as e:
            print(f"  ! 네트워크 오류: {e}")
            time.sleep(1.5 * (i + 1))
    return None


def fetch_trend(cid, csec):
    """검색어 트렌드. 호스트가 이관 중이라 두 곳을 시도한다."""
    end = datetime.now(KST).date()
    body = json.dumps({
        "startDate": (end - timedelta(days=TREND_DAYS)).isoformat(),
        "endDate": end.isoformat(),
        "timeUnit": "week",
        "keywordGroups": TREND_GROUPS,
    }, ensure_ascii=False).encode("utf-8")

    for host in TREND_HOSTS:
        req = Request(f"{host}/datalab/v1/search", data=body, method="POST",
                      headers={"X-NCP-APIGW-API-KEY-ID": cid,
                               "X-NCP-APIGW-API-KEY": csec,
                               "Content-Type": "application/json"})
        try:
            with urlopen(req, timeout=20) as r:
                data = json.loads(r.read().decode("utf-8"))
                print(f"  트렌드 OK ({host})")
                return data
        except HTTPError as e:
            print(f"  ! 트렌드 {host} → HTTP {e.code}: {e.read().decode('utf-8','ignore')[:120]}")
        except Exception as e:
            # 트렌드 실패로 수집 결과 전체를 잃으면 안 된다. 로그만 남기고 넘어간다.
            print(f"  ! 트렌드 {host} → {e}")
    print("  ! 트렌드 수집 실패. Application 에서 '검색어 트렌드'를 선택했는지 확인하세요.")
    return None


# ─────────────────────────────────────────────────────────────
# 수집
# ─────────────────────────────────────────────────────────────

def main():
    cid, csec = env("NAVER_CLIENT_ID"), env("NAVER_CLIENT_SECRET")
    now = datetime.now(KST)
    cutoff = now - timedelta(hours=WINDOW_HOURS)
    today = now.strftime("%Y-%m-%d")

    # 이전 상태 이어받기
    prev = {}
    if os.path.exists(OUT_PATH):
        try:
            prev = json.load(open(OUT_PATH, encoding="utf-8"))
        except Exception as e:
            print(f"기존 {OUT_PATH} 읽기 실패, 초기화: {e}")
    history = [h for h in prev.get("history", []) if h.get("date") != today]
    seen = prev.get("seen", {})                 # {링크해시: 최초발견일}
    seen_cut = (now - timedelta(days=SEEN_DAYS)).strftime("%Y-%m-%d")
    seen = {k: v for k, v in seen.items() if v >= seen_cut}

    calls = 0
    out_groups, counts, new_alerts = [], {}, []

    for g in GROUPS:
        print(f"\n[{g['label']}]")
        items, dup_links, dup_titles = [], set(), set()
        per_source = {}          # 같은 언론사·카페가 화면을 점령하는 것을 막는다

        for src in g["sources"]:
            path = SOURCE_PATH[src]
            for kw in g["keywords"]:
                params = {"query": kw, "display": DISPLAY, "start": 1, "format": "json"}
                if src != "webkr":                # 웹문서는 sort 파라미터가 없다
                    params["sort"] = "date"
                data = call(path, params, cid, csec)
                calls += 1
                time.sleep(0.12)
                if not data:
                    continue

                kept = 0
                for it in data.get("items", []):
                    title = clean(it.get("title"))
                    desc = clean(it.get("description"))
                    link = it.get("originallink") or it.get("link") or ""
                    if not link:
                        continue
                    blob = title + " " + desc

                    # ── 날짜 ──
                    # 카페글·웹문서는 날짜 필드가 없다. 기간 필터를 못 쓰므로
                    # '이전 실행에 없던 링크'를 신규로 본다.
                    pub, dated = "", False
                    if it.get("pubDate"):                       # 뉴스
                        try:
                            d = parsedate_to_datetime(it["pubDate"]).astimezone(KST)
                            if d < cutoff:
                                continue
                            pub, dated = d.strftime("%Y-%m-%d %H:%M"), True
                        except Exception:
                            continue
                    elif it.get("postdate"):                    # 블로그 (YYYYMMDD)
                        try:
                            d = datetime.strptime(it["postdate"], "%Y%m%d").replace(tzinfo=KST)
                            if d.date() < (now - timedelta(days=2)).date():
                                continue
                            pub, dated = d.strftime("%Y-%m-%d"), True
                        except Exception:
                            continue

                    # ── 필터 ──
                    if g["drop_politics"] and any(w in blob for w in POLITICS_WORDS):
                        continue
                    if g["drop_ads"] and any(w in blob for w in AD_WORDS):
                        continue
                    dw = g.get("drop_words") or []
                    if dw and any(w in (blob + " " + link) for w in dw):
                        continue
                    # 네이버 검색의 느슨한 매칭을 후처리로 조인다.
                    # 카페명·블로그명은 검사하지 않는다 (카페 이름에 '액상'이 들어있는 경우가 많다)
                    # 리스트의 리스트면 각 묶음마다 하나 이상 맞아야 한다 (AND 조건)
                    ra = g.get("require_any") or []
                    if ra:
                        sets = ra if isinstance(ra[0], list) else [ra]
                        low = blob.lower()
                        if not all(any(w in low for w in s) for s in sets):
                            continue
                    if any(w in title for w in NOISE_WORDS):
                        continue

                    # ── 중복 ──
                    nt = norm_title(title)
                    if link in dup_links or (nt and nt in dup_titles):
                        continue
                    dup_links.add(link)
                    if nt:
                        dup_titles.add(nt)

                    lh = h10(link)
                    is_new = lh not in seen
                    if is_new:
                        seen[lh] = today
                    # 날짜가 없는 소스는 신규가 아니면 버린다 (과거 글이 계속 쌓이는 것 방지)
                    if not dated and not is_new:
                        continue

                    where = (it.get("cafename") or it.get("bloggername")
                             or re.sub(r"^https?://(www\.|news\.|m\.)?([^/]+).*", r"\2", link))
                    # 같은 매체가 몰아서 올린 기사는 상한까지만 (매장 언급은 예외)
                    if not g["alert"]:
                        per_source[where] = per_source.get(where, 0) + 1
                        if per_source[where] > MAX_PER_SOURCE:
                            continue

                    # 불만족 신호 (어떤 단어에 걸렸는지도 남긴다)
                    hits = [w for w in NEGATIVE_WORDS if w in blob]

                    row = {
                        "title": title,
                        "desc": desc[:180],
                        "link": it.get("link") or link,
                        "src": SOURCE_LABEL[src],
                        "where": where,
                        "pub": pub or ("신규" if is_new else ""),
                        "kw": kw,
                        "new": is_new,
                        "neg": bool(hits),
                        "negWords": hits[:4],
                    }
                    items.append(row)
                    if g["alert"] and is_new:
                        new_alerts.append({"group": g["label"], **row})
                    kept += 1

                counts[kw] = counts.get(kw, 0) + kept
                if kept:
                    print(f"  [{SOURCE_LABEL[src]}] {kw}: {kept}건")

        # 불만족 글을 맨 위로, 그다음 신규, 그다음 최신순
        items.sort(key=lambda x: (x["neg"], x["new"], x["pub"]), reverse=True)
        out_groups.append({k: g[k] for k in ("id", "label", "desc")} |
                          {"keywords": g["keywords"], "items": items[:MAX_PER_GROUP]})

    # ── 검색어 트렌드 ──
    print("\n[검색어 트렌드]")
    trend = fetch_trend(cid, csec)
    calls += 1

    # ── 급상승 키워드 (최근 7일 평균 대비) ──
    base_days = history[-7:]
    rising = []
    for kw, c in counts.items():
        past = [h["counts"].get(kw, 0) for h in base_days if kw in h.get("counts", {})]
        b = sum(past) / len(past) if past else 0
        if c >= 3 and (b == 0 or c / b >= 1.8):
            rising.append({"kw": kw, "count": c, "base": round(b, 1),
                           "ratio": round(c / b, 1) if b else None})
    rising.sort(key=lambda x: (x["ratio"] or 99, x["count"]), reverse=True)

    # ── 오늘 요약 3줄 (외부 AI 없이, 기사 밀집도 기준) ──
    summary = build_summary(out_groups, rising)

    history = (history + [{"date": today, "counts": counts}])[-HISTORY_DAYS:]

    payload = {
        "updatedAt": now.strftime("%Y-%m-%d %H:%M:%S KST"),
        "windowHours": WINDOW_HOURS,
        "apiCalls": calls,
        "summary": summary,
        "rising": rising[:8],
        "alerts": new_alerts[:30],
        "groups": out_groups,
        "trend": trend,
        "history": history,
        "seen": seen,
    }
    json.dump(payload, open(OUT_PATH, "w", encoding="utf-8"), ensure_ascii=False, indent=1)

    total = sum(len(g["items"]) for g in out_groups)
    print(f"\n완료: {total}건 · 알림 {len(new_alerts)}건 · API {calls}회 → {OUT_PATH}")


def build_summary(groups, rising):
    """그날 요약 3줄. 매장 언급 > 판매 감시 > 급상승 > 최다 기사 순으로 뽑는다."""
    lines = []
    by_id = {g["id"]: g for g in groups}

    st = by_id.get("store", {}).get("items", [])
    neg_st = [i for i in st if i["neg"]]
    new_st = [i for i in st if i["new"]]
    if neg_st:
        w = ", ".join(neg_st[0]["negWords"][:3])
        lines.append(f"⚠️ 매장 불만족 신호 {len(neg_st)}건 ({w}) — {neg_st[0]['title'][:34]}")
    if new_st:
        lines.append(f"우리 매장 언급 {len(new_st)}건 — {new_st[0]['title'][:40]}")

    wt = [i for i in by_id.get("watch", {}).get("items", []) if i["new"]]
    if wt:
        lines.append(f"온라인 판매 의심 글 {len(wt)}건 — {wt[0]['where']}")

    if rising:
        top = ", ".join(f"{r['kw']}({r['count']})" for r in rising[:3])
        lines.append(f"검색 급상승: {top}")

    if len(lines) < 3:
        vp = by_id.get("vape", {}).get("items", [])
        if vp:
            lines.append(f"업종 뉴스 {len(vp)}건 — {vp[0]['title'][:40]}")
    if not lines:
        lines.append("특별히 눈에 띄는 건이 없습니다.")
    return lines[:3]


if __name__ == "__main__":
    main()
