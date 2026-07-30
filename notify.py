# -*- coding: utf-8 -*-
"""
feeds.json 을 읽어 '봐야 할 것만' 카카오톡으로 보낸다.

보내는 조건 (하나라도 있으면 발송):
  · 우리 매장·지점 언급 신규 글
  · 온라인 판매 의심 신규 글
  · 검색 급상승 키워드

환경변수 (없으면 조용히 건너뛴다 — 수집은 그대로 돌아간다):
  KAKAO_REST_API_KEY   : 카카오 개발자 앱의 REST API 키
  KAKAO_REFRESH_TOKEN  : 최초 1회 발급한 리프레시 토큰
  DASHBOARD_URL        : 대시보드 주소 (메시지 버튼에 붙는다)

카카오 '나에게 보내기'는 본인 계정으로만 발송된다. 고객 발송이 아니므로
정보통신망법상 광고성 정보 규정과 무관하다.
"""

import json
import os
import sys
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from urllib.error import HTTPError

FEEDS = os.environ.get("OUT_PATH", "feeds.json")
REST_KEY = os.environ.get("KAKAO_REST_API_KEY", "").strip()
REFRESH = os.environ.get("KAKAO_REFRESH_TOKEN", "").strip()
DASH_URL = os.environ.get("DASHBOARD_URL", "").strip()

TOKEN_URL = "https://kauth.kakao.com/oauth/token"
SEND_URL = "https://kapi.kakao.com/v2/api/talk/memo/default/send"


def post(url, data, headers):
    req = Request(url, data=urlencode(data).encode("utf-8"), headers=headers)
    with urlopen(req, timeout=15) as r:
        return json.loads(r.read().decode("utf-8"))


def access_token():
    res = post(TOKEN_URL, {
        "grant_type": "refresh_token",
        "client_id": REST_KEY,
        "refresh_token": REFRESH,
    }, {"Content-Type": "application/x-www-form-urlencoded;charset=utf-8"})
    return res["access_token"]


def build_message(d):
    """보낼 내용이 없으면 None 을 반환한다."""
    groups = {g["id"]: g for g in d.get("groups", [])}
    store = [i for i in groups.get("store", {}).get("items", []) if i.get("new")]
    watch = [i for i in groups.get("watch", {}).get("items", []) if i.get("new")]
    rising = d.get("rising", [])

    if not (store or watch or rising):
        return None

    lines = [f"📌 We Vape 이슈 ({d.get('updatedAt','')[:16]})"]

    if store:
        lines.append(f"\n🏪 매장 언급 {len(store)}건")
        for i in store[:4]:
            lines.append(f"· [{i['src']}/{i['where']}] {i['title'][:45]}")

    if watch:
        lines.append(f"\n🚨 온라인 판매 의심 {len(watch)}건")
        for i in watch[:4]:
            lines.append(f"· [{i['where']}] {i['title'][:45]}")

    if rising:
        top = ", ".join(f"{r['kw']} {r['count']}건" for r in rising[:4])
        lines.append(f"\n📈 검색 급상승\n{top}")

    text = "\n".join(lines)
    return text[:990]                      # 카카오 텍스트 템플릿 한도 1,000자


def main():
    if not os.path.exists(FEEDS):
        print(f"{FEEDS} 없음. 건너뜁니다.")
        return
    d = json.load(open(FEEDS, encoding="utf-8"))

    text = build_message(d)
    if not text:
        print("알릴 내용 없음. 발송하지 않습니다.")
        return

    if not (REST_KEY and REFRESH):
        print("카카오 설정이 없어 발송을 건너뜁니다. 아래 내용이 발송될 예정이었습니다.\n")
        print(text)
        return

    try:
        token = access_token()
        template = {"object_type": "text", "text": text,
                    "link": {"web_url": DASH_URL, "mobile_web_url": DASH_URL} if DASH_URL else {}}
        if DASH_URL:
            template["button_title"] = "대시보드 열기"
        post(SEND_URL, {"template_object": json.dumps(template, ensure_ascii=False)},
             {"Authorization": f"Bearer {token}",
              "Content-Type": "application/x-www-form-urlencoded;charset=utf-8"})
        print("카톡 발송 완료")
    except HTTPError as e:
        body = e.read().decode("utf-8", "ignore")[:300]
        # 알림 실패로 워크플로 전체를 실패시키지 않는다. 수집 결과는 이미 저장됐다.
        print(f"[경고] 카톡 발송 실패 HTTP {e.code}: {body}")
        print("리프레시 토큰이 만료됐을 수 있습니다. 재발급이 필요합니다.")
    except Exception as e:
        print(f"[경고] 카톡 발송 실패: {e}")


if __name__ == "__main__":
    main()
