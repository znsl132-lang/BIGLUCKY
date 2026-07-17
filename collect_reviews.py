# -*- coding: utf-8 -*-
"""
WE VAPE 지점별 리뷰 수 자동 수집
- 네이버 방문자리뷰 / 카카오맵 후기 / 당근 후기 → reviews.json
- 티맵은 웹에 리뷰 수가 공개되지 않아 tmap.json(수동 입력)을 그대로 반영
- GitHub Actions에서 매일 실행됨 (수동 실행: Actions 탭 → collect-reviews → Run workflow)
"""
import json
import re
import datetime
import pathlib

import requests
from playwright.sync_api import sync_playwright

# 지점명은 사이트(index.html)의 지점명과 반드시 동일해야 함
BRANCHES = {
    "구월 로데오점": {
        "naver":  "https://naver.me/F36TJCpJ",
        "kakao":  "https://place.map.kakao.com/1168620629",
        "daangn": "https://www.daangn.com/kr/local-profile/j8ymzr23et7m/",
    },
    "구월 길병원점": {
        "naver":  "https://naver.me/5gi1oYI5",
        "kakao":  "https://place.map.kakao.com/633298339",
        "daangn": "https://www.daangn.com/kr/local-profile/m2neojss44pe/",
    },
    "부천 상동점": {
        "naver":  "https://naver.me/GhEQIO4I",
        "kakao":  "https://place.map.kakao.com/1296224657",
        "daangn": "https://www.daangn.com/kr/local-profile/1kprrijr9775/",
    },
    "부천 신중동점": {  # 리뷰 플랫폼상 '부천중동점'
        "naver":  "https://naver.me/GmFf6N0p",
        "kakao":  "https://place.map.kakao.com/2143803668",
        "daangn": "https://www.daangn.com/kr/local-profile/7v6122quh54b/",
    },
    "인천 검단점": {
        "naver":  "https://naver.me/G8h9ie9c",
        "kakao":  "https://place.map.kakao.com/935044085",
        "daangn": "https://www.daangn.com/kr/local-profile/sqm9gmq2s5vi/",
    },
    "인천 계산점": {
        "naver":  "https://naver.me/Fn6tCOcu",
        "kakao":  "https://place.map.kakao.com/1174093631",
        "daangn": "https://www.daangn.com/kr/local-profile/k9w7mqyriumz/",
    },
    "인천 공항점": {
        "naver":  "https://naver.me/FMTgXjBC",
        "kakao":  "https://place.map.kakao.com/846947831",
        "daangn": "https://www.daangn.com/kr/local-profile/vej8jykqfqf6/",
    },
    "인천 논현점": {
        "naver":  "https://naver.me/Gye3JExC",
        "kakao":  "https://place.map.kakao.com/630219080",
        "daangn": "https://www.daangn.com/kr/local-profile/cz4f7jad4ffh/",
    },
    "인천 연수점": {
        "naver":  "https://naver.me/5JJpEkvx",
        "kakao":  "https://place.map.kakao.com/580096181",
        "daangn": "https://www.daangn.com/kr/local-profile/2f8xtnhnrrb2/",
    },
}

MOBILE_UA = ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
             "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1")

OUT = pathlib.Path("reviews.json")
TMAP = pathlib.Path("tmap.json")


def to_int(s):
    return int(str(s).replace(",", "").strip())


def daangn_count(url):
    """당근: 일반 요청으로 HTML에 '후기 N개'가 그대로 들어있음"""
    html = requests.get(url, headers={"User-Agent": MOBILE_UA}, timeout=25).text
    m = re.search(r"후기\s*([\d,]+)\s*개", html)
    return to_int(m.group(1)) if m else None


def kakao_count(page, url):
    """카카오맵: JS 렌더링 후 본문에서 '후기 N' 추출 (별점 다음, 블로그 앞)"""
    page.goto(url, wait_until="domcontentloaded", timeout=45000)
    page.wait_for_timeout(5000)
    text = page.inner_text("body")
    m = re.search(r"후기\s*\n?\s*([\d,]+)", text)
    return to_int(m.group(1)) if m else None


def naver_count(page, url):
    """네이버: naver.me → m.place 모바일 페이지에서 방문자리뷰 수 추출"""
    page.goto(url, wait_until="domcontentloaded", timeout=45000)
    page.wait_for_timeout(6000)
    html = page.content()
    m = re.search(r'"visitorReviewsTotal"\s*:\s*(\d+)', html)
    if m:
        return to_int(m.group(1))
    text = page.inner_text("body")
    m = re.search(r"방문자\s*리뷰\s*([\d,]+)", text)
    return to_int(m.group(1)) if m else None


def main():
    # 이전 결과 로드 (수집 실패 시 이전 값 유지)
    prev = {}
    if OUT.exists():
        try:
            prev = json.loads(OUT.read_text(encoding="utf-8")).get("branches", {})
        except Exception:
            prev = {}

    # 티맵 수동 입력값
    tmap = {}
    if TMAP.exists():
        try:
            tmap = json.loads(TMAP.read_text(encoding="utf-8"))
        except Exception:
            tmap = {}

    result = {}
    errors = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        desktop = browser.new_context(locale="ko-KR")
        mobile = browser.new_context(locale="ko-KR", user_agent=MOBILE_UA,
                                     viewport={"width": 390, "height": 844}, is_mobile=True)
        pg_d = desktop.new_page()
        pg_m = mobile.new_page()

        for name, urls in BRANCHES.items():
            row = {"naver": None, "kakao": None, "daangn": None, "tmap": tmap.get(name)}

            for platform, fn in (("naver", lambda u: naver_count(pg_m, u)),
                                 ("kakao", lambda u: kakao_count(pg_d, u)),
                                 ("daangn", daangn_count)):
                try:
                    row[platform] = fn(urls[platform])
                except Exception as e:
                    errors.append(f"{name}/{platform}: {e}")
                # 실패하면 이전 값 유지
                if row[platform] is None:
                    row[platform] = prev.get(name, {}).get(platform)
                    if row[platform] is None:
                        errors.append(f"{name}/{platform}: 수집값 없음")

            result[name] = row
            print(f"[OK] {name}: {row}")

        browser.close()

    kst = datetime.datetime.utcnow() + datetime.timedelta(hours=9)
    OUT.write_text(json.dumps({
        "updatedAt": kst.strftime("%Y-%m-%d %H:%M"),
        "branches": result,
        "errors": errors,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nreviews.json 저장 완료. 오류 {len(errors)}건: {errors}")


if __name__ == "__main__":
    main()
