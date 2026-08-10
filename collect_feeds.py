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
# ─────────────────────────────────────────────────────────────
# 매장 명단은 stores.json 에서 읽는다. 코드를 고치지 않고 명단만 바꾸면 된다.
#   STORES_URL 환경변수를 주면 외부(허브)의 명단을 그대로 쓴다.
#   (감독관 지시 ③ — 2026-08-01)
# ─────────────────────────────────────────────────────────────
def load_stores():
    url = os.environ.get("STORES_URL", "").strip()
    if url:
        try:
            with urlopen(Request(url, headers={"User-Agent": "WeVapeMonitor/1.0"}), timeout=10) as r:
                return json.loads(r.read().decode("utf-8"))
        except Exception as e:
            print(f"[경고] STORES_URL 조회 실패 → 로컬 stores.json 사용: {e}")
    try:
        with open("stores.json", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        sys.exit(f"[중단] stores.json 을 읽을 수 없습니다: {e}")


STORES = load_stores()
STORE_LIST = STORES["stores"]
STORE_COUNT = len(STORE_LIST)

BRAND_TERMS = STORES["brand"]

# 우리 9지점 (2026-07-30 대표 확인). 정식 상호는 "위베이프 전자담배 ○○점" 형태다.
#   구월길병원점(=구월점) · 구월로데오점 · 부천상동점 · 부천 신중동점
#   인천공항점 · 인천연수점 · 인천논현점 · 인천계산점 · 인천검단점
STORE_TERMS = [s["query"] for s in STORE_LIST] + STORES.get("extra_queries", [])

# 우리 9지점을 가리키는 지역 토큰. 타 가맹점 글을 걸러내는 기준이다.
# '중동'은 신중동을 포함하려고 넣었다. '중동현대'는 아래 OTHER_STORES 에서 제외된다.
OUR_AREAS = sorted({s["area"] for s in STORE_LIST} | {"구월"})

# 타 지역·타 가맹점 표기. 위베이프 브랜드 글이지만 우리 매장이 아니다.
# 화면에서 우리 지점이 아닌 글이 보이면 여기에 지역명을 추가하면 된다.
#   '중동현대'는 우리 부천중동점이 아니다 (2026-07-30 확인)
OTHER_STORES = STORES.get("other_stores", [])

# 우리·가맹점 공식 블로그. 여기서 쓴 글은 '홍보'로 분류해 손님 글과 나눈다 (지시 ②)
OWN_MEDIA = STORES.get("own_media", [])

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
# '코일'·'누유' 를 단독으로 넣지 않는다. 자동차 정비 용어와 완전히 겹친다
# (점화코일 / 엔진오일 누유 → 제네시스·그랜저·K5 글이 대량 유입 — 2026-08-01)
VAPE_COMMUNITY = VAPE_WORDS + [
    "무화량", "연무량", "무화기", "팟 교체", "팟 누유", "쿨링", "타격감",
    "코일 저항", "코일 태움", "코일 교체 주기", "액상 누유", "액상이 새",
    "드립팁", "탱크", "저항값", "옴", "니코틴 함량",
]

# ★ 카페 이름 검증 — 커뮤니티 노이즈의 근본 해법
#   글 내용만 거르면 에어비앤비·베트남여행 카페가 계속 샌다.
#   카페 이름 자체에 전자담배 관련어가 있어야 통과시킨다.
#   (신림역 숙소 = 에어비앤비 호스트 카페 글이 들어온 것을 계기로 — 2026-08-01)
VAPE_CAFE_WORDS = [
    "전자담배", "전담", "액상", "베이프", "vape", "입호흡", "폐호흡",
    "궐련형", "일회용전자담배", "니코틴", "무화기", "베이핑", "전자연초",
]

# 카페 고정 안내문구·인사글. 본문 하단 서명에 전자담배 단어가 박혀 있어 통과한다.
CHITCHAT_WORDS = [
    "제목에 기기", "사진과 함께 일상", "소통해 보세요", "소통해보세요",
    "가족분들", "회원님들", "출석체크", "출첵", "좋은 아침", "안녕하세요 여러분",
    "건강 관리 잘하", "건강관리 잘하", "무더위", "폭염", "체고", "뒹굴",
    "인사드립니다", "가입인사", "등업", "눈팅", "잡담",
]

# 자동차 정비 글. '코일'·'누유'·'교체' 같은 말이 전자담배와 겹쳐 대량으로 딸려온다.
AUTO_NOISE = [
    "점화코일", "점화 코일", "점화플러그", "점화 플러그",
    "엔진오일", "엔진 오일", "미션오일", "기어오일", "브레이크오일",
    "냉각수", "부동액", "로커암", "실린더", "가스켓", "개스킷", "헤드커버",
    "타이어", "휠얼라인먼트", "브레이크 패드", "쇼바", "머플러", "배기량",
    "주행거리", "카센터", "정비소", "차량 인수", "중고차", "매물", "ECU", "맵핑",
    "제네시스", "그랜저", "쏘나타", "아반떼", "카니발", "스포티지", "싼타페",
    "K3", "K5", "K7", "K8", "K9", "모닝", "레이", "티볼리", "코란도",
    "벤츠", "BMW", "아우디", "폭스바겐", "테슬라", "포르쉐",
    "엔진떨림", "시동", "변속기", "클러치", "터보", "인젝터",
]

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

# 잡담·유머 게시판. 전자담배 얘기가 나와도 매장 운영에 쓸 정보가 아니고
# 10년 전 글이 그대로 검색된다 (slrclub 자유게시판 2016년 글이 잡힌 것을 계기로 — 2026-07-30)
BAD_DOMAINS = [
    "slrclub.com", "dcinside.com", "ppomppu.co.kr", "todayhumor.co.kr",
    "ruliweb.com", "inven.co.kr", "fmkorea.com", "theqoo.net", "instiz.net",
    "pann.nate.com", "bobaedream.co.kr", "mlbpark.donga.com", "82cook.com",
    "gasengi.com", "clien.net", "etoland.co.kr", "humoruniv.com",
    "cook.co.kr", "damoang.net", "arca.live", "ilbe.com",
]

# ─────────────────────────────────────────────────────────────
# 키워드 나열 스팸
#   본문 끝에 "호치민/유흥/마사지/…/전자담배/클럽/후기" 처럼 단어를 잔뜩 붙여
#   검색에 걸리게 만드는 글. 제목은 '놀라운 신개념 사유' 처럼 아무 상관 없다.
#   (베트남여행 카페 글이 커뮤니티 탭에 대량으로 들어온 것을 계기로 — 2026-07-30)
# ─────────────────────────────────────────────────────────────
SPAM_WORDS = [
    "호치민", "하노이", "다낭", "붕따우", "가라오케", "마사지", "유흥", "밤문화",
    "이발소", "황제투어", "풀빌라", "풍가이", "콜걸", "출장샵", "안마", "룸싸롱",
    "카지노", "바카라", "슬롯", "룰렛", "토토", "먹튀", "겜블", "배팅", "사설",
    "대출", "작대", "코인리딩", "성인용품", "비아그라", "환전", "렌트카", "골프투어",
]
SPAM_CAFE_WORDS = [
    "베트남여행", "호치민", "하노이", "유흥", "밤문화", "가라오케", "마사지",
    "토토", "카지노", "먹튀", "대출", "안마", "출장",
]


def is_keyword_spam(title, desc, where):
    """검색 노출용 키워드 나열 글인지 판정한다."""
    blob = f"{title} {desc}"
    # 중고거래 글은 사양을 '액상/기기/코일/팟' 식으로 나열한다. 12개는 너무 빡빡했다.
    # (판매 감시 탭이 0건이 된 원인 — 감독관 지시 ① / 2026-08-01)
    if blob.count("/") >= 22:
        return True
    if blob.count("#") >= 12:           # 해시태그 남발
        return True
    if sum(1 for w in SPAM_WORDS if w in blob) >= 3:
        return True
    if any(w in (where or "") for w in SPAM_CAFE_WORDS):
        return True
    # 의미 없는 자음 나열 (ㅂㄱㅁ, ㄲㄱ 등)이 세 번 이상
    if len(re.findall(r"[ㄱ-ㅎ]{2,}", blob)) >= 3:
        return True
    return False


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
    ["판매", "팝니다", "팜", "팔아", "택배", "거래", "구매대행", "양도", "넘겨",
     "삽니다", "구합니다", "직거래", "반택", "택포", "나눔", "처분", "정리",
     "새제품", "미개봉", "중고", "가격", "원에", "만원"],
]

# ─────────────────────────────────────────────────────────────
# ③ 출처 신뢰도 — 직원이 "이거 확실한 정보인가"를 한눈에 알아야 한다
# ─────────────────────────────────────────────────────────────
GOV_DOMAINS = ["korea.kr", ".go.kr", "mfds.go.kr", "customs.go.kr", "moef.go.kr",
               "mohw.go.kr", "ftc.go.kr", "nts.go.kr", "assembly.go.kr", "law.go.kr"]
GOV_WORDS = ["식약처", "식품의약품안전처", "관세청", "기획재정부", "기재부",
             "보건복지부", "복지부", "공정거래위원회", "공정위", "국세청",
             "행정심판위원회", "법제처", "국회", "구청", "시청", "군청", "도청",
             "지방자치단체", "지자체", "정부", "당국"]
MAJOR_PRESS = ["yna.co.kr", "news1.kr", "newsis.com", "chosun.com", "joongang.co.kr",
               "donga.com", "hani.co.kr", "khan.co.kr", "hankookilbo.com", "hankyung.com",
               "mk.co.kr", "sedaily.com", "edaily.co.kr", "mt.co.kr", "fnnews.com",
               "kbs.co.kr", "imbc.com", "sbs.co.kr", "ytn.co.kr", "jtbc.co.kr",
               "dt.co.kr", "etnews.com", "biz.chosun.com", "seoul.co.kr", "kmib.co.kr"]


def classify_voice(src, where, blob, link):
    """손님이 쓴 글인가, 매장·업체가 쓴 홍보 글인가.
    감독관 지시 ② — 우리매장 탭이 자사 홍보 블로그로 채워지는 문제."""
    w = (where or "").lower()
    if any(m.lower() in w for m in OWN_MEDIA):
        return "promo"                      # 작성자가 매장·업체 공식 채널
    if any(m.lower() in (link or "").lower() for m in ["wevape", "vape"]):
        if src == "blog":
            return "promo"
    hits = sum(1 for x in PROMO_WORDS if x in blob)
    if hits >= 2:
        return "promo"                      # 홍보 문구가 여러 개
    if src == "cafearticle":
        return "customer"                   # 카페글은 대체로 손님 목소리
    return "customer" if hits == 0 else "promo"


def source_tier(src, link, blob):
    """gov(공식) > press(주요 언론) > news(일반 언론) > user(블로그·카페)"""
    if src != "news" and src != "webkr":
        return "user"
    low = (link or "").lower()
    if any(d in low for d in GOV_DOMAINS) or any(w in blob for w in GOV_WORDS):
        return "gov"
    if any(d in low for d in MAJOR_PRESS):
        return "press"
    return "news"


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
        "desc": "법안 개정 · 단속 강화 · 시행 예정",
        # 웹문서를 추가한다. 식약처·기재부·관세청 보도자료 원문이 여기로 잡힌다.
        # 게시일 조회 기능이 있어 오래된 문서는 자동으로 걸러진다.
        "sources": ["news", "webkr"],
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
            "담배소매인 거리제한",
            "소매인 지정 취소",
            "전자담배 세금 인상",
            "액상 제세부담금",
            "전자담배 광고 제한",
            "전자담배 성분 표시",
            "무인 전자담배 자판기",
            # 세금 체계
            "액상 개별소비세", "담배소비세 전자담배", "지방교육세 담배",
            "국민건강증진부담금", "전자담배 부담금",
            # 성분·표시
            "니코틴 함량 기준", "전자담배 가향물질", "전자담배 향료 규제",
            "전자담배 경고그림", "전자담배 성분 공개", "액상 유해성 심의",
            # 판매 방식
            "전자담배 통신판매", "전자담배 자판기 규제", "전자담배 거리제한",
            # 통관·수입
            "니코틴 원료 수입", "전자담배 관세", "액상 통관",
            # 입법 절차
            "담배사업법 의안", "전자담배 입법예고", "전자담배 상임위",
            # 해외 동향 (한국 규제의 선행지표)
            "EU 전자담배 규제", "미국 FDA 전자담배", "일본 전자담배 규제",
            "해외 액상 규제",
        ],
        "drop_ads": False,
        "drop_politics": False,     # 규제 뉴스는 국회·법안 언급이 필연이다
        # (전자담배 소재) AND (규제·정책 소재) 둘 다 있어야 통과
        "require_any": [VAPE_WORDS, POLICY_WORDS + REG_WORDS],
        "require_title": VAPE_WORDS,    # 제목에 전자담배가 있어야 그 기사의 주제다
        "drop_words": LOCAL_HEALTH_NOISE + ENTERTAIN_NOISE + YOUTH_CRACKDOWN_NOISE,
        "rescue_any": ENFORCEMENT_RESCUE,   # 판매업소 단속 기사는 살린다
        "hard_words": NUISANCE_NOISE,       # 민폐·화제성 기사는 구제 없이 차단
        # 법안은 몇 주에 걸쳐 진행된다. 30일치를 봐야 '어디까지 왔나'가 보인다.
        "window_days": 30,
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
            "전자담배 배터리 폭발",
            "전자담배 화재",
            "액상 리콜",
            "니코틴 함량 초과",
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
            "전자담배 코일 누유",
            "전담 액상 누유",
            "전자담배 기기 불량",
            "입호흡 액상",
            "폐호흡 액상",
        ],
        "drop_ads": True,           # 체험단·협찬 포스팅 제거
        "drop_politics": False,
        "require_any": VAPE_COMMUNITY,
        "require_where": VAPE_CAFE_WORDS,   # 카페 이름에 전자담배 관련어가 있어야 통과
        "drop_words": PROMO_WORDS + AUTO_NOISE + CHITCHAT_WORDS,
        "alert": False,
    },
    {
        "id": "brand",
        "label": "경쟁사·브랜드",
        "desc": "액상형 중심 · 궐련형 포함",
        # 블로그를 되살렸다. 액상 브랜드 소식은 뉴스에 거의 안 나오고 카페·블로그가 주력이다.
        # 무관한 산업 기사는 '액상 브랜드' 검색어를 없애 이미 막았다.
        "sources": ["news", "cafearticle", "blog"],
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
        # ★ 홍보 차단을 껐다. 브랜드 소식은 본질이 홍보성이라, 막으면 볼 게 없어진다.
        #   대신 화면에서 '홍보/손님' 배지로 구분한다 (감독관 지시 ① — 0건 원인)
        "drop_ads": False,
        "drop_politics": False,
        "drop_words": AUTO_NOISE,
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

# 게시일을 못 찾아도 통과시키는 소스.
#   카페글·블로그는 sort=date 를 지원해 '최신순'으로 온다. 그래서 날짜가 없어도
#   상위 결과는 실제로 최신 글이다.
#   반면 웹문서(webkr)는 sort 파라미터가 아예 없어 정렬이 무작위다.
#   slrclub 2016년 글, 문화일보 2016년 기사가 들어온 건 전부 웹문서였다 (2026-07-30).
DATE_EXEMPT_SOURCES = {"cafearticle", "blog"}
NAVER_UGC_DOMAINS = ["cafe.naver.com", "blog.naver.com", "post.naver.com",
                     "m.cafe.naver.com", "m.blog.naver.com"]


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


# ─────────────────────────────────────────────────────────────
# 게시일 알아내기
#   네이버 웹문서·카페글 검색은 날짜를 안 준다. 그대로 두면 2016년 기사가
#   '신규'로 들어온다 (2026-07-30 확인). 그래서 두 단계로 직접 알아낸다.
#     1) URL 안의 날짜 패턴 (요청 0회, 즉시)
#     2) 기사 페이지의 메타태그 (article:published_time 등)
#   한 번 알아낸 날짜는 pubdates 에 캐시해 두 번 조회하지 않는다.
# ─────────────────────────────────────────────────────────────
URL_DATE_RES = [
    re.compile(r"/(20\d{2})[/\-\.](\d{1,2})[/\-\.](\d{1,2})[/\-]"),
    re.compile(r"[/_](20\d{2})(\d{2})(\d{2})"),
    re.compile(r"[?&](?:date|regDate|aid)=(20\d{2})(\d{2})(\d{2})"),
]
META_DATE_RES = [
    re.compile(rb'property=["\']article:published_time["\'][^>]*content=["\']([^"\']+)'),
    re.compile(rb'content=["\']([^"\']+)["\'][^>]*property=["\']article:published_time["\']'),
    re.compile(rb'name=["\'](?:dd:published_time|pubdate|article:published_time)["\'][^>]*content=["\']([^"\']+)'),
    re.compile(rb'"datePublished"\s*:\s*"([^"]+)"'),
    re.compile(rb'property=["\']og:regDate["\'][^>]*content=["\'](\d{14})'),
]


def _norm_date(txt):
    """여러 형식의 날짜 문자열을 YYYY-MM-DD 로 맞춘다."""
    t = re.sub(r"[^0-9]", "", txt or "")
    if len(t) >= 8:
        y, m, d = t[:4], t[4:6], t[6:8]
        if "2000" <= y <= "2099" and "01" <= m <= "12" and "01" <= d <= "31":
            return f"{y}-{m}-{d}"
    return ""


def pubdate_from_url(link):
    for rx in URL_DATE_RES:
        m = rx.search(link or "")
        if m:
            d = _norm_date("".join(m.groups()))
            if d:
                return d
    return ""


def pubdate_from_page(link, timeout=7):
    """기사 페이지 앞부분만 읽어 메타태그에서 게시일을 뽑는다."""
    try:
        req = Request(link, headers={
            "User-Agent": "Mozilla/5.0 (compatible; WeVapeMonitor/1.0)",
            "Accept-Language": "ko-KR,ko;q=0.9",
        })
        with urlopen(req, timeout=timeout) as r:
            head = r.read(120_000)          # <head> 만 있으면 충분하다
    except Exception:
        return ""
    for rx in META_DATE_RES:
        m = rx.search(head)
        if m:
            d = _norm_date(m.group(1).decode("utf-8", "ignore"))
            if d:
                return d
    return ""


def resolve_pubdate(link, cache):
    """URL → 페이지 순으로 게시일을 찾는다. 결과(실패 포함)를 캐시한다."""
    if link in cache:
        return cache[link]
    d = pubdate_from_url(link) or pubdate_from_page(link)
    cache[link] = d
    return d


# ─────────────────────────────────────────────────────────────
# 시행일 뽑아내기
#   직원이 알아야 하는 건 "법이 바뀐다"가 아니라 "언제부터 어떻게 해야 하나"다.
#   [26.08.18 시행] · 2026년 8월 18일부터 시행 · 8월 18일부터 → 날짜로 뽑는다.
# ─────────────────────────────────────────────────────────────
EFF_RES = [
    re.compile(r"\[?\s*(\d{2,4})[.\-년]\s*(\d{1,2})[.\-월]\s*(\d{1,2})\s*일?\s*"
               r"(?:부터)?\s*(?:시행|적용|발효)"),
    re.compile(r"(\d{4})년\s*(\d{1,2})월\s*(\d{1,2})일\s*(?:부터)?\s*(?:시행|적용|발효)"),
    re.compile(r"(\d{1,2})월\s*(\d{1,2})일\s*부터\s*(?:시행|적용|금지|의무)"),
]


def extract_effective(blob, now):
    """본문에서 시행일을 찾아 YYYY-MM-DD 로 돌려준다. 없으면 빈 문자열."""
    for i, rx in enumerate(EFF_RES):
        m = rx.search(blob)
        if not m:
            continue
        g = m.groups()
        if len(g) == 3:
            y, mo, d = g
            y = int(y)
            if y < 100:                      # '26' → 2026
                y += 2000
        else:
            continue
        try:
            dt = datetime(y, int(mo), int(d))
        except ValueError:
            continue
        # 너무 먼 과거·미래는 오탐으로 본다
        if abs((dt.date() - now.date()).days) > 900:
            continue
        return dt.strftime("%Y-%m-%d")
    # '8월 18일부터 시행' 처럼 연도가 없는 경우
    m = EFF_RES[2].search(blob)
    if m:
        mo, d = int(m.group(1)), int(m.group(2))
        for y in (now.year, now.year + 1):
            try:
                dt = datetime(y, mo, d)
            except ValueError:
                continue
            if (dt.date() - now.date()).days >= -60:
                return dt.strftime("%Y-%m-%d")
    return ""


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
    pubdates = prev.get("pubdates", {})         # {링크: 게시일} — 두 번 조회하지 않는다
    reg_archive = prev.get("regArchive", [])    # 규제 항목은 기간이 지나도 계속 쌓아둔다
    seen_cut = (now - timedelta(days=SEEN_DAYS)).strftime("%Y-%m-%d")
    seen = {k: v for k, v in seen.items() if v >= seen_cut}

    calls = 0
    out_groups, counts, new_alerts = [], {}, []
    # 감독관 지시 ④ — 오탐이 다시 새는지 숫자로 보이게 한다
    stats = {}
    taken = set()          # 앞선 그룹에 이미 실린 링크. 탭 간 중복을 막는다

    for g in GROUPS:
        print(f"\n[{g['label']}]")
        # 그룹별 조회 기간. 지정이 없으면 기본 WINDOW_HOURS 를 쓴다.
        wd = g.get("window_days")
        g_cutoff = (now - timedelta(days=wd)) if wd else cutoff
        g_blog_days = wd if wd else 2
        items, dup_links, dup_titles, dup_descs = [], set(), set(), set()
        # ★ 탈락 사유는 빠짐없이 세야 한다.
        #   2026-08-10 점검에서 '기간밖·날짜불명·중복' 이 선언만 되고 한 번도
        #   증가하지 않아 화면에서 늘 0 으로 찍히고 있었다. 그 결과 규제·단속 탭은
        #   3,120건을 수집해 놓고 1,897건이 왜 빠졌는지 알 수 없는 상태였다.
        #   판매감시·커뮤니티가 0건이 돼도 원인을 짚을 수 없었던 이유다.
        st = {"수집": 0, "기간밖": 0, "날짜불명": 0, "스팸": 0, "차단도메인": 0,
              "차단어": 0, "광고": 0, "정치": 0, "소재불일치": 0, "제목불일치": 0,
              "카페불일치": 0, "중복": 0, "재등장": 0, "매체상한": 0,
              "표시상한": 0, "최종": 0}
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
                    st["수집"] += 1
                    blob = title + " " + desc

                    # ── 날짜 ──
                    # 카페글·웹문서는 날짜 필드가 없다. 기간 필터를 못 쓰므로
                    # '이전 실행에 없던 링크'를 신규로 본다.
                    pub, dated = "", False
                    if it.get("pubDate"):                       # 뉴스
                        try:
                            d = parsedate_to_datetime(it["pubDate"]).astimezone(KST)
                            if d < g_cutoff:
                                st["기간밖"] += 1; continue
                            pub, dated = d.strftime("%Y-%m-%d %H:%M"), True
                        except Exception:
                            st["날짜불명"] += 1; continue
                        # ★ 네이버가 주는 pubDate 는 '기사 작성일'이 아니라
                        #   '네이버에 제공된 시간'이다. 언론사가 옛 기사를 재송고하면
                        #   2023년 기사도 오늘 날짜로 온다 (라오스 단속 기사 — 2026-08-01).
                        #   원문 게시일을 확인해 크게 다르면 원문 쪽을 믿는다.
                        real = resolve_pubdate(link, pubdates)
                        if real and abs((datetime.strptime(real, "%Y-%m-%d").date()
                                         - d.date()).days) >= 2:
                            if real < g_cutoff.strftime("%Y-%m-%d"):
                                st["기간밖"] += 1; continue   # 실제로는 기간 밖 → 제외
                            pub = real
                    elif it.get("postdate"):                    # 블로그 (YYYYMMDD)
                        try:
                            d = datetime.strptime(it["postdate"], "%Y%m%d").replace(tzinfo=KST)
                            if d.date() < (now - timedelta(days=g_blog_days)).date():
                                st["기간밖"] += 1; continue
                            pub, dated = d.strftime("%Y-%m-%d"), True
                        except Exception:
                            st["날짜불명"] += 1; continue

                    # ── 필터 ──
                    if g["drop_politics"] and any(w in blob for w in POLITICS_WORDS):
                        st["정치"] += 1; continue
                    if g["drop_ads"] and any(w in blob for w in AD_WORDS):
                        st["광고"] += 1; continue
                    # 검색 노출용 키워드 나열 스팸은 어느 그룹에서도 버린다
                    if is_keyword_spam(title, desc,
                                       it.get("cafename") or it.get("bloggername") or ""):
                        st["스팸"] += 1; continue
                    # 잡담 커뮤니티는 도메인째로 버린다
                    low_link = (link or "").lower()
                    if any(d in low_link for d in BAD_DOMAINS):
                        st["차단도메인"] += 1; continue
                    # 구제 불가 차단어 (뉴스 그룹의 민폐·화제성 기사)
                    hw = g.get("hard_words") or []
                    if hw and any(w in blob for w in hw):
                        st["차단어"] += 1; continue
                    dw = g.get("drop_words") or []
                    if dw and any(w in (blob + " " + link) for w in dw):
                        # 판매업소 단속 기사면 살린다
                        rs = g.get("rescue_any")
                        low_r = blob.lower()
                        if not (rs and all(any(w in low_r for w in grp) for grp in rs)):
                            st["차단어"] += 1
                            continue
                    # 네이버 검색의 느슨한 매칭을 후처리로 조인다.
                    # 카페명·블로그명은 검사하지 않는다 (카페 이름에 '액상'이 들어있는 경우가 많다)
                    # 리스트의 리스트면 각 묶음마다 하나 이상 맞아야 한다 (AND 조건)
                    ra = g.get("require_any") or []
                    if ra:
                        sets = ra if isinstance(ra[0], list) else [ra]
                        low = blob.lower()
                        if not all(any(w in low for w in s) for s in sets):
                            st["소재불일치"] += 1; continue
                    # 카페·블로그 이름에 소재가 있어야 통과 (무관한 카페 차단)
                    rw = g.get("require_where") or []
                    if rw:
                        w = (it.get("cafename") or it.get("bloggername") or "").lower()
                        if not any(x in w for x in rw):
                            st["카페불일치"] += 1; continue
                    # 뉴스는 '제목'에 소재가 있어야 그 기사의 주제로 본다.
                    # 본문에 한 번 스친 기사(청계천 단속, 공항 보조배터리 등)를 잘라낸다.
                    rt = g.get("require_title") or []
                    if rt and not any(w in title.lower() for w in rt):
                        st["제목불일치"] += 1; continue
                    if any(w in title for w in NOISE_WORDS):
                        st["차단어"] += 1; continue
                    # 강력범죄·사건 기사는 어느 그룹에서도 제외 (제목+요약 모두 검사)
                    if any(w in blob for w in HARD_BLOCK):
                        st["차단어"] += 1; continue

                    # ── 날짜가 안 온 소스(카페글·웹문서)는 직접 알아낸다 ──
                    #   ★ 못 찾으면 버린다. '날짜 미상'으로 남겨두면 10년 전 글이 섞인다.
                    #     (slrclub 2016년 글, 문화일보 2016년 기사가 들어온 것을 계기로 — 2026-07-30)
                    if not dated:
                        got = resolve_pubdate(link, pubdates)
                        if got:
                            if got < g_cutoff.strftime("%Y-%m-%d"):
                                st["기간밖"] += 1; continue     # 기간 밖 → 제외
                            pub, dated = got, True
                        elif (src in DATE_EXEMPT_SOURCES
                              and any(d in low_link for d in NAVER_UGC_DOMAINS)):
                            # 네이버 카페·블로그는 sort=date 라 최신순이다. 날짜 없이도 통과시킨다.
                            pub = "날짜 미상"
                        else:
                            # 그 밖(웹문서 등)은 게시일 불명이면 제외.
                            # 이 숫자가 크면 필터가 아니라 '날짜를 못 읽어서' 비는 것이다.
                            st["날짜불명"] += 1; continue

                    # ── 중복 ──
                    # 보도자료를 여러 매체가 그대로 받아쓰면 제목만 조금씩 다르다.
                    # 요약문 앞부분까지 대조해야 같은 기사로 잡힌다.
                    nt = norm_title(title)
                    nd = re.sub(r"[^\w가-힣]", "", desc)[:45]
                    if link in dup_links or (nt and nt in dup_titles) \
                       or (len(nd) >= 25 and nd in dup_descs):
                        st["중복"] += 1; continue
                    if link in taken:
                        st["중복"] += 1; continue
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
                    # 이 숫자가 크면 '새 글이 없어서' 0건인 것이지 필터 탓이 아니다.
                    if not dated and not is_new:
                        st["재등장"] += 1; continue

                    where = (it.get("cafename") or it.get("bloggername")
                             or re.sub(r"^https?://(www\.|news\.|m\.)?([^/]+).*", r"\2", link))
                    # 같은 매체가 몰아서 올린 기사는 상한까지만 (매장 언급은 예외)
                    if not g["alert"]:
                        per_source[where] = per_source.get(where, 0) + 1
                        if per_source[where] > MAX_PER_SOURCE:
                            st["매체상한"] += 1; continue

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
                        # 여기까지 온 글은 모두 게시일이 확인됐다
                        "pub": pub,
                        "kw": kw,
                        "new": is_new,
                        "tier": source_tier(src, link, blob),
                        "voice": classify_voice(src, where, blob, link),
                        "eff": extract_effective(blob, now) if g["id"] == "reg" else "",
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
        TIER_RANK = {"gov": 3, "press": 2, "news": 1, "user": 0}
        # 매장 탭은 '손님 글'을 홍보 글보다 위로 올린다 (지시 ②)
        # '최종'은 화면에 실제로 뜨는 건수여야 한다.
        # 예전에는 상한을 적용하기 전 숫자라 통계표에 52건이라 적혀 있는데
        # 화면에는 30건만 있는 상황이 생겼다. 넘친 몫은 '표시상한'으로 따로 센다.
        st["표시상한"] = max(0, len(items) - MAX_PER_GROUP)
        st["최종"] = min(len(items), MAX_PER_GROUP)
        stats[g["id"]] = st
        items.sort(key=lambda x: (x["neg"],
                                  1 if x.get("voice") == "customer" else 0,
                                  TIER_RANK.get(x.get("tier"), 0),
                                  x["new"], x["pub"]), reverse=True)
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

    # ── 규제 항목 누적 보관 ──
    #   규제는 30일이 지나도 사라지면 안 된다. "그때 뭐가 바뀌었더라"를 계속 볼 수 있어야 한다.
    reg_now = next((g["items"] for g in out_groups if g["id"] == "reg"), [])
    have = {i["link"] for i in reg_archive}
    for it in reg_now:
        if it["link"] not in have:
            reg_archive.append({k: it[k] for k in
                                ("title", "desc", "link", "src", "where", "pub", "tier", "eff")})
            have.add(it["link"])
    reg_archive.sort(key=lambda x: x.get("pub") or "", reverse=True)
    reg_archive = reg_archive[:200]

    # ── 시행 예정: 시행일이 아직 안 지난 것 (지난 것도 14일까지는 남긴다) ──
    today = now.strftime("%Y-%m-%d")
    keep_from = (now - timedelta(days=14)).strftime("%Y-%m-%d")
    upcoming = [i for i in reg_archive if i.get("eff") and i["eff"] >= keep_from]
    seen_eff = set()
    uniq = []
    for i in sorted(upcoming, key=lambda x: x["eff"]):
        key = (i["eff"], i["title"][:20])
        if key in seen_eff:
            continue
        seen_eff.add(key)
        uniq.append({**i, "dday": (datetime.strptime(i["eff"], "%Y-%m-%d").date() - now.date()).days})
    upcoming = uniq[:12]

    # ── 오늘 요약 3줄 + 꼭 볼 것 3건 ──
    summary = build_summary(out_groups, rising)
    top3 = build_top3(out_groups, now)

    history = (history + [{"date": today, "counts": counts}])[-HISTORY_DAYS:]

    payload = {
        "updatedAt": now.strftime("%Y-%m-%d %H:%M:%S KST"),
        "windowHours": WINDOW_HOURS,
        "apiCalls": calls,
        "stats": stats,
        "storeCount": STORE_COUNT,
        "summary": summary,
        "top3": top3,
        "upcoming": upcoming,
        "regArchive": reg_archive,
        "rising": rising[:8],
        "alerts": new_alerts[:30],
        "groups": out_groups,
        "trend": trend,
        "history": history,
        "seen": seen,
        "pubdates": dict(list(pubdates.items())[-4000:]),
    }
    json.dump(payload, open(OUT_PATH, "w", encoding="utf-8"), ensure_ascii=False, indent=1)

    total = sum(len(g["items"]) for g in out_groups)
    print(f"\n완료: {total}건 · 알림 {len(new_alerts)}건 · API {calls}회 → {OUT_PATH}")


def build_top3(groups, now):
    """아침에 30초만 볼 사람을 위한 '꼭 볼 것'.
    ★ 최근 48시간 안의 글만 올린다. 고객이 오늘 물어볼 사안이어야 의미가 있다."""
    limit_day = (now - timedelta(hours=48)).strftime("%Y-%m-%d")

    def fresh(items):
        out = []
        for i in items:
            p = (i.get("pub") or "").strip()
            if not p or p == "날짜 미상":      # 카페·웹문서는 날짜가 없다. 신규면 최신으로 본다
                out.append(i)
            elif p[:10] >= limit_day:
                out.append(i)
        return out

    by = {g["id"]: fresh(g.get("items", [])) for g in groups}
    picked, seen_links = [], set()

    def take(items, why, limit=3):
        for it in items:
            if len(picked) >= 3:
                return
            if it["link"] in seen_links:
                continue
            seen_links.add(it["link"])
            picked.append({**it, "why": why})
            limit -= 1
            if limit <= 0:
                return

    # 순서가 곧 우선순위다. 규제·법안이 맨 위에 온다.
    take([i for i in by.get("reg", []) if i.get("eff")], "시행일 확정", 2)
    take([i for i in by.get("reg", []) if i.get("tier") == "gov"], "공식 규제 발표", 2)
    take([i for i in by.get("reg", []) if i.get("new")], "새 법안·규제", 2)
    take([i for i in by.get("store", []) if i.get("neg")], "우리 매장 불만족", 1)
    take(by.get("reg", []), "규제·단속", 3)
    take([i for i in by.get("nonic", []) if i.get("new")], "무니코틴 이슈", 1)
    take([i for i in by.get("watch", []) if i.get("new")], "온라인 판매 의심", 1)
    take([i for i in by.get("store", []) if i.get("new")], "우리 매장 언급", 1)

    # 48시간 안에 3건이 안 되면 그 이상 된 것도 채우되 라벨로 구분한다
    if len(picked) < 3:
        # 보충분도 7일 이내로 제한한다. 그보다 오래된 건 '오늘 볼 것'이 아니다.
        week = (now - timedelta(days=7)).strftime("%Y-%m-%d")
        def recent(items):
            return [i for i in items
                    if i["link"] not in seen_links
                    and (i.get("pub") or "")[:10] >= week]
        allg = {g["id"]: g.get("items", []) for g in groups}
        take(recent(allg.get("reg", [])), "지난 규제 소식", 3)
        take(recent(allg.get("nonic", [])), "지난 무니코틴 이슈", 2)
    return picked


def cut(s, n):
    """글자 수로 뭉텅 자르면 '연간 실적 전…' 처럼 단어 중간에서 끊긴다.
    읽는 사람에게는 정보가 아니라 미끼가 된다. 띄어쓰기 경계에서 자른다."""
    s = (s or "").strip()
    if len(s) <= n:
        return s
    head = s[:n]
    sp = head.rfind(" ")
    if sp > n * 0.6:          # 너무 앞에서 끊기면 차라리 그대로 둔다
        head = head[:sp]
    return head.rstrip(" ,·-") + "…"


def build_summary(groups, rising):
    """그날 요약 3줄. 매장 언급 > 판매 감시 > 급상승 > 최다 기사 순으로 뽑는다."""
    lines = []
    by_id = {g["id"]: g for g in groups}

    # 직원이 아침에 보는 화면이다. 규제·단속을 맨 위에 둔다.
    rg = by_id.get("reg", {}).get("items", [])
    if rg:
        lines.append(f"📋 규제·단속 {len(rg)}건 — {cut(rg[0]['title'], 44)}")

    st = by_id.get("store", {}).get("items", [])
    neg_st = [i for i in st if i["neg"]]
    new_st = [i for i in st if i["new"]]
    if neg_st:
        w = ", ".join(neg_st[0]["negWords"][:3])
        lines.append(f"⚠️ 매장 불만족 신호 {len(neg_st)}건 ({w}) — {cut(neg_st[0]['title'], 34)}")
    if new_st:
        lines.append(f"우리 매장 언급 {len(new_st)}건 — {cut(new_st[0]['title'], 40)}")

    wt = [i for i in by_id.get("watch", {}).get("items", []) if i["new"]]
    if wt:
        lines.append(f"온라인 판매 의심 글 {len(wt)}건 — {wt[0]['where']}")

    if rising:
        top = ", ".join(f"{r['kw']}({r['count']})" for r in rising[:3])
        lines.append(f"검색 급상승: {top}")

    if len(lines) < 3:
        vp = by_id.get("vape", {}).get("items", [])
        if vp:
            lines.append(f"업종 뉴스 {len(vp)}건 — {cut(vp[0]['title'], 40)}")
    if not lines:
        lines.append("특별히 눈에 띄는 건이 없습니다.")
    return lines[:3]


if __name__ == "__main__":
    main()
