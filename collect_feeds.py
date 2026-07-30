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

# 우리 9지점 (2026-07-30 대표 확인). 정식 상호는 "위베이프 전자담배 ○○점" 형태다.
#   구월길병원점(=구월점) · 구월로데오점 · 부천상동점 · 부천 신중동점
#   인천공항점 · 인천연수점 · 인천논현점 · 인천계산점 · 인천검단점
STORE_TERMS = [
    "위베이프 구월길병원",
    "위베이프 길병원",
    "위베이프 구월로데오",
    "위베이프 로데오",
    "위베이프 부천상동",
    "위베이프 부천 신중동",
    "위베이프 신중동",
    "위베이프 인천공항",
    "위베이프 인천연수",
    "위베이프 인천논현",
    "위베이프 인천계산",
    "위베이프 인천검단",
]

# 우리 9지점을 가리키는 지역 토큰. 타 가맹점 글을 걸러내는 기준이다.
# '중동'은 신중동을 포함하려고 넣었다. '중동현대'는 아래 OTHER_STORES 에서 제외된다.
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

# 전자담배 소재 관련어. 뉴스·브랜드·커뮤니티는 이게 실제로 언급돼야 통과한다.
# '담배' 단독은 넣지 않는다. 너무 넓어서 무관한 기사가 대량으로 딸려온다.
# '금연구역', '담뱃세' 처럼 담배 일반에 쓰이는 말은 통과 조건에서 뺐다.
# (인제군 여름 휴가철 금연구역 운영 같은 기사가 딸려온 것을 계기로 — 2026-07-30)
# ★ '액상' 을 단독으로 넣지 않는다. 액상칼슘·액상분유·액상초크·지반 액상화·코팅 액상 소재처럼
#   일반 명사로 널리 쓰인다. 이것 하나 때문에 무관한 산업 기사가 대량 유입됐다 (2026-07-30)
VAPE_WORDS = [
    "전자담배", "전자담배액상", "전자담배 액상", "액상 전자담배", "액상형 전자담배",
    "액상담배", "전담액상", "전담 액상", "전담샵", "일회용 전담", "전담기기",
    "니코틴", "궐련형", "베이프", "vape", "베이핑", "담배사업법",
    "폐호흡", "입호흡", "카트리지", "무니코틴", "합성니코틴", "기화기",
]

# 커뮤니티 글에서만 추가로 인정하는 말. 뉴스에 쓰면 오탐이 커진다.
VAPE_COMMUNITY = VAPE_WORDS + ["코일", "누유", "무화량", "연무량", "팟 교체", "쿨링"]

# 매장 홍보·마케팅 글. 후기를 가장한 광고가 대부분이다.
PROMO_WORDS = [
    "1만원대", "최저가", "할인", "쿠폰", "이벤트", "무료배송", "무료 배송",
    "정품 보장", "시연", "입점", "문의 주세요", "문의주세요", "오픈",
    "영업시간", "찾아오시는 길", "주차 가능", "공식몰", "스토어", "구매 링크",
    "점입니다", "점 입니다", "점 입니다", "매장에 방문", "방문해 주세요",
    "카카오톡", "네이버 예약", "당첨자", "추첨",
]

# 지자체 청소년 유해환경 합동단속 기사. 지역별로 대량 복제돼 뉴스 탭을 점령한다.
YOUTH_CRACKDOWN_NOISE = [
    "청소년 유해환경", "유해환경 합동", "합동점검", "합동 점검", "민·관·경", "민관경",
    "유해약물 판매", "출입·고용", "출입 고용", "청소년 유해업소", "유해매체 배포",
    "피서지", "해수욕장", "노래연습장", "불건전 전단지",
]

# 무니코틴 표기 변형. 이게 본문에 실제로 있어야 무니코틴 탭에 들어간다.
NONIC_WORDS = ["무니코틴", "무 니코틴", "니코틴 없", "니코틴 프리", "논니코틴",
               "non-nicotine", "nicotine-free", "제로니코틴", "니코틴 미함유"]

# 이슈성 소재. 단순 제품 홍보·후기가 아니라 문제·논란·규제 글만 남긴다.
# '중독'·'청소년'·'단속' 은 뺐다. 지자체 청소년 단속 기사와 마케팅 블로그가
# 이 세 단어로 대량 통과해 실제 규제 기사를 밀어냈다 (2026-07-30)
ISSUE_WORDS = [
    "부작용", "유해", "위해", "독성", "발암", "금단", "팝콘폐",
    "논란", "우려", "경고", "위험성", "안전성", "실태조사",
    "검출", "회수", "리콜", "성분 분석", "유해성분", "연구 결과",
    "폐 손상", "호흡기", "기침", "구토", "두통", "어지럼", "니코틴 중독",
    "가향", "향료", "첨가물", "허위", "과장 광고", "오인 우려",
]

# 규제·단속·행정처분 소재. 규제·단속 탭의 두 번째 통과 조건이다.
REG_WORDS = [
    "규제", "단속", "적발", "금지", "제재", "과태료", "처분", "고발", "소송",
    "행정심판", "행정처분", "영업정지", "취소", "위반", "점검", "고시",
    "기준", "표시 의무", "인증", "허가", "신고", "의무화", "강화", "완화",
]

# 정책·시장 소재. 업종 뉴스는 이슈 아니면 이쪽이라도 걸려야 통과한다.
POLICY_WORDS = [
    "법안", "개정", "입법", "시행령", "시행규칙", "국회", "발의", "의결",
    "과세", "세율", "제세부담금", "세금", "인상", "면세",
    "소매인", "지정", "허가", "신고제", "인증", "표시 의무", "경고그림",
    "시장", "점유율", "출시", "판매량", "매출", "수입", "유통", "업계",
    "식약처", "복지부", "기재부", "관세청", "공정위", "소비자원",
]

# 지방 보건소·지자체 금연사업 기사. 전국 규제 이슈와 달리 매장 운영과 무관하다.
# (괴산군 건강지표, 화순전남대병원 금연문화 기사가 뜬 것을 계기로 — 2026-07-30)
LOCAL_HEALTH_NOISE = [
    "보건소", "보건지소", "보건의료원", "건강지표", "지역사회건강조사",
    "지역사회 건강조사", "금연클리닉", "금연 클리닉", "금연사업", "금연 사업",
    "금연 캠페인", "금연캠페인", "금연서포터즈", "금연지원", "흡연예방 교육",
    "건강증진사업", "건강생활실천", "건강도시", "보건행정", "금연문화",
]

# ★ 보건소·구청이 '전자담배 판매업소를 단속·점검'한 건은 직원이 알아야 한다.
#   LOCAL_HEALTH_NOISE 에 걸려도 아래 두 조건을 모두 만족하면 살린다.
#   (건강지표·금연클리닉 같은 통계·캠페인 기사는 이 조건에 안 걸려 계속 차단된다)
ENFORCEMENT_RESCUE = [
    ["단속", "점검", "적발", "처분", "과태료", "고발", "수거", "행정지도",
     "위반", "영업정지", "지정 취소", "고시"],
    ["판매업소", "판매점", "소매점", "판매 업소", "무인", "업소", "매장",
     "판매행위", "청소년 판매", "소매인", "유통"],
]

# 민폐·목격담·커뮤니티 화제성 기사. 규제와 무관한데 '적발' 같은 단어로 통과한다.
# (탑승구 앞 전자담배 '민폐 외국인 승객' 논란 기사를 계기로 — 2026-07-30)
NUISANCE_NOISE = [
    "민폐", "빌런", "진상", "갑질", "공분", "목격담", "황당", "충격",
    "누리꾼", "네티즌", "온라인 커뮤니티", "갈무리", "논란에 휩싸", "뭇매",
    "시끌", "발칵", "경악", "역대급",
]

# 연예·방송 기사. 유명인이 전자담배를 언급했다는 이유로 딸려온다.
ENTERTAIN_NOISE = [
    "임신", "출산", "부모 된다", "결혼", "열애", "이혼", "재혼",
    "예능", "방송인", "개그맨", "개그우먼", "아이돌", "가수", "배우",
    "드라마", "영화", "소속사", "팬미팅", "컴백", "출연", "MC",
]

# 어떤 그룹에서도 보고 싶지 않은 소재. 검색어와 무관하게 딸려오는 강력범죄·사건 기사다.
# (업종 뉴스에 아동 성범죄 기사가 뜬 것을 계기로 추가 — 2026-07-30)
HARD_BLOCK = [
    "성매매", "성착취", "성폭행", "성추행", "강제추행", "성범죄", "음란",
    "몰카", "불법촬영", "아동학대", "미성년자 성", "그루밍",
    "살인", "시신", "사체", "자살", "극단적 선택", "유서", "흉기",
    "치사", "폭행 혐의", "보이스피싱", "도박장", "납치", "감금",
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
# '액상' 단독은 건강기능식품·세제까지 끌고 오므로 전자담배 맥락을 함께 요구한다
WATCH_REQUIRE = [
    ["전자담배", "전담", "니코틴", "무니코틴", "합성니코틴", "리퀴드",
     "액상 전자담배", "전자담배 액상", "입호흡", "폐호흡", "카트리지", "베이프"],
    ["판매", "팝니다", "팔아", "택배", "거래", "구매대행", "양도", "넘겨"],
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
        "detect_neg": True,         # 불만족 판정은 이 그룹에서만 한다
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
        # 직원이 아침에 가장 먼저 봐야 할 것. 법안·단속·처분은 매장 운영에 직접 걸린다.
        "id": "reg",
        "label": "규제·단속",
        "desc": "법안 개정 · 단속 강화 · 행정처분",
        "sources": ["news"],
        "keywords": [
            "담배사업법 개정",
            "전자담배 법안",
            "전자담배 규제",
            "액상형 전자담배 규제",
            "전자담배 단속",
            "전자담배 과태료",
            "전자담배 소매인 지정",
            "무인 전자담배 판매",
            "합성니코틴 규제",
            "액상 전자담배 세금",
            "전자담배 제세부담금",
            "전자담배 표시 의무",
            "전자담배 행정처분",
            "청소년 전자담배 판매",
            "전자담배 판매업소 단속",
            "담배소매인 지정",
            "교육환경보호구역 전자담배",
            "니코틴 용액 수입통관",
            "전자담배 통관",
            "전자담배 온라인 판매 금지",
            "전자담배 표시광고",
        ],
        "drop_ads": False,
        "drop_politics": False,     # 규제 뉴스는 국회·법안 언급이 필연이다
        # (전자담배 소재) AND (규제·정책 소재) 둘 다 있어야 통과
        "require_any": [VAPE_WORDS, POLICY_WORDS + REG_WORDS],
        "require_title": VAPE_WORDS,    # 제목에 전자담배가 있어야 그 기사의 주제다
        "drop_words": LOCAL_HEALTH_NOISE + ENTERTAIN_NOISE + YOUTH_CRACKDOWN_NOISE,
        "rescue_any": ENFORCEMENT_RESCUE,   # 판매업소 단속 기사는 살린다
        "hard_words": NUISANCE_NOISE,       # 민폐·화제성 기사는 구제 없이 차단
        # 규제 뉴스는 매일 나오지 않는다. 30시간이면 화면이 빈다. 7일치를 본다.
        "window_days": 7,
        "alert": True,              # 규제 신규 건은 카톡으로도 알린다
    },
    {
        "id": "vape",
        "label": "업계·시장",
        "desc": "시장 동향 · 제품 이슈",
        "sources": ["news"],
        "keywords": [
            "전자담배",
            "액상형 전자담배",
            "궐련형 전자담배",
            "전자담배 시장",
            "전자담배 업계",
            "담뱃세",
            "전자담배 유해성",
            "전자담배 부작용",
        ],
        "drop_ads": False,
        "drop_politics": False,
        "require_any": [VAPE_WORDS, ISSUE_WORDS + POLICY_WORDS],
        "require_title": VAPE_WORDS,
        "drop_words": LOCAL_HEALTH_NOISE + ENTERTAIN_NOISE + YOUTH_CRACKDOWN_NOISE,
        "hard_words": NUISANCE_NOISE,
        "window_days": 3,
        "alert": False,
    },
    {
        "id": "nonic",
        "label": "무니코틴",
        "desc": "부작용 · 유해성 · 규제 이슈",
        # 블로그는 제외한다. '전자담배 중독성 있는 이유 레딜…' 형태의 위장 마케팅 글이
        # 하루 20건씩 도배돼 실제 이슈를 밀어냈다 (2026-07-30 확인)
        "sources": ["news", "cafearticle"],
        "keywords": [
            "무니코틴",
            "무니코틴 액상",
            "무니코틴 전자담배",
            "무니코틴 부작용",
            "무니코틴 유해성",
            "무니코틴 규제",
            "무니코틴 성분",
            "니코틴 없는 전자담배",
            "논니코틴",
        ],
        "drop_ads": True,
        "drop_politics": False,
        # 두 조건을 모두 만족해야 통과: (무니코틴 언급) AND (이슈성 소재)
        "require_any": [NONIC_WORDS, ISSUE_WORDS],
        "drop_words": PROMO_WORDS,
        "window_days": 7,
        "alert": False,
    },
    {
        "id": "community",
        "label": "커뮤니티",
        "desc": "실사용 후기 · 고장 사례",
        # 블로그는 매장 홍보글이 90%다. 카페만 본다.
        "sources": ["cafearticle"],
        "keywords": [
            "전자담배 후기",
            "전자담배 액상 추천",
            "무니코틴 액상 추천",
            "전자담배 고장",
            "코일 누유",
            "전자담배 기기 불량",
            "입호흡 액상",
            "폐호흡 액상",
        ],
        "drop_ads": True,           # 체험단·협찬 포스팅 제거
        "drop_politics": False,
        "require_any": VAPE_COMMUNITY,
        "drop_words": PROMO_WORDS,
        "alert": False,
    },
    {
        "id": "brand",
        "label": "경쟁사·브랜드",
        "desc": "액상형 중심 · 궐련형 포함",
        # 블로그 제외. '액상 브랜드' 검색에 알룰로스·분유·미녹시딜·클라이밍 초크가
        # 대량으로 딸려왔다. 브랜드명은 카페·뉴스에서 잡는다 (2026-07-30)
        "sources": ["news", "cafearticle"],
        # 액상형(입호흡·폐호흡) 브랜드를 먼저 두고 궐련형을 뒤에 둔다
        "keywords": [
            # 액상형 · 기기 브랜드 — 반드시 전자담배 맥락을 붙인다
            "오지구 전자담배", "벱티오 전자담배", "유월 액상 전자담배",
            "긱베이프", "복스미니", "아스파이어 전자담배", "보이드 전자담배",
            # 제조·유통 동향
            "전자담배 액상 신제품", "전자담배 브랜드",
            # 궐련형 · 대기업
            "쥴 전자담배", "릴 전자담배", "아이코스", "글로 전자담배",
            "KT&G 전자담배", "필립모리스 전자담배", "BAT로스만스",
        ],
        "drop_ads": True,
        "drop_politics": False,
        "drop_words": PROMO_WORDS,
        # KT&G 인삼공사·부동산 기사, 필립모리스 주가 기사 등을 걸러낸다
        "require_any": VAPE_WORDS,
        "require_title": VAPE_WORDS + ["오지구", "벱티오", "긱베이프", "복스미니",
                                       "아스파이어", "쥴", "릴 ", "아이코스", "글로 "],
        "drop_words": LOCAL_HEALTH_NOISE + ENTERTAIN_NOISE,
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
    taken = set()          # 앞선 그룹에 이미 실린 링크. 탭 간 중복을 막는다

    for g in GROUPS:
        print(f"\n[{g['label']}]")
        # 그룹별 조회 기간. 지정이 없으면 기본 WINDOW_HOURS 를 쓴다.
        wd = g.get("window_days")
        g_cutoff = (now - timedelta(days=wd)) if wd else cutoff
        g_blog_days = wd if wd else 2
        items, dup_links, dup_titles, dup_descs = [], set(), set(), set()
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
                            if d < g_cutoff:
                                continue
                            pub, dated = d.strftime("%Y-%m-%d %H:%M"), True
                        except Exception:
                            continue
                    elif it.get("postdate"):                    # 블로그 (YYYYMMDD)
                        try:
                            d = datetime.strptime(it["postdate"], "%Y%m%d").replace(tzinfo=KST)
                            if d.date() < (now - timedelta(days=g_blog_days)).date():
                                continue
                            pub, dated = d.strftime("%Y-%m-%d"), True
                        except Exception:
                            continue

                    # ── 필터 ──
                    if g["drop_politics"] and any(w in blob for w in POLITICS_WORDS):
                        continue
                    if g["drop_ads"] and any(w in blob for w in AD_WORDS):
                        continue
                    # 구제 불가 차단어 (뉴스 그룹의 민폐·화제성 기사)
                    hw = g.get("hard_words") or []
                    if hw and any(w in blob for w in hw):
                        continue
                    dw = g.get("drop_words") or []
                    if dw and any(w in (blob + " " + link) for w in dw):
                        # 판매업소 단속 기사면 살린다
                        rs = g.get("rescue_any")
                        low_r = blob.lower()
                        if not (rs and all(any(w in low_r for w in st) for st in rs)):
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
                    # 뉴스는 '제목'에 소재가 있어야 그 기사의 주제로 본다.
                    # 본문에 한 번 스친 기사(청계천 단속, 공항 보조배터리 등)를 잘라낸다.
                    rt = g.get("require_title") or []
                    if rt and not any(w in title.lower() for w in rt):
                        continue
                    if any(w in title for w in NOISE_WORDS):
                        continue
                    # 강력범죄·사건 기사는 어느 그룹에서도 제외 (제목+요약 모두 검사)
                    if any(w in blob for w in HARD_BLOCK):
                        continue

                    # ── 중복 ──
                    # 보도자료를 여러 매체가 그대로 받아쓰면 제목만 조금씩 다르다.
                    # 요약문 앞부분까지 대조해야 같은 기사로 잡힌다.
                    nt = norm_title(title)
                    nd = re.sub(r"[^\w가-힣]", "", desc)[:45]
                    if link in dup_links or (nt and nt in dup_titles) \
                       or (len(nd) >= 25 and nd in dup_descs):
                        continue
                    if link in taken:
                        continue
                    dup_links.add(link)
                    if nt:
                        dup_titles.add(nt)
                    if len(nd) >= 25:
                        dup_descs.add(nd)

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

                    # 불만족 신호. '우리 매장' 그룹에서만 판정한다.
                    # 커뮤니티·업종 뉴스의 일반적인 기기 불만은 우리 매장 컴플레인이 아니다.
                    hits = ([w for w in NEGATIVE_WORDS if w in blob]
                            if g.get("detect_neg") else [])

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
        for i in items[:MAX_PER_GROUP]:
            taken.add(i["link"])
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

    # 직원이 아침에 보는 화면이다. 규제·단속을 맨 위에 둔다.
    rg = by_id.get("reg", {}).get("items", [])
    if rg:
        lines.append(f"📋 규제·단속 {len(rg)}건 — {rg[0]['title'][:44]}")

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
