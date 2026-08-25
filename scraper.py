"""
سحب الأخبار من 365scores.com/ar.

ملاحظة مهمة:
365scores موقع يعتمد على JavaScript/Next.js، وبنية الصفحة قد تتغير مع الوقت.
لهذا تم بناء السحب بطريقتين متتاليتين (الأولى ثم احتياطية عند فشلها):
  1) استخراج بيانات JSON المضمّنة في الصفحة (__NEXT_DATA__ أو ما شابهها).
  2) البحث المباشر عن روابط المقالات في HTML عبر نمط الرابط.
إذا تغيّرت بنية الموقع مستقبلًا، عدّل فقط الدوال داخل هذا الملف
(extract_articles_from_html / parse_next_data) دون المساس ببقية المشروع.
"""

import json
import re
from datetime import datetime, timedelta, timezone

import requests
from bs4 import BeautifulSoup

from config import (
    SOURCE_NEWS_LIST_URL,
    SOURCE_BASE_URL,
    USER_AGENT,
    REQUEST_TIMEOUT_SEC,
    HOURS_WINDOW,
)

HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept-Language": "ar,en;q=0.8",
}

ARTICLE_URL_PATTERN = re.compile(r"/ar/(article|news)/[^\"'\s>]+")


def fetch_html(url: str) -> str:
    resp = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT_SEC)
    resp.raise_for_status()
    return resp.text


def _to_abs_url(url: str) -> str:
    if url.startswith("http"):
        return url
    return SOURCE_BASE_URL.rstrip("/") + "/" + url.lstrip("/")


def parse_next_data(html: str):
    """
    يحاول استخراج قائمة الأخبار من كتلة JSON المضمّنة في الصفحة (__NEXT_DATA__).
    يُعيد قائمة من dict فيها على الأقل: url, title, published_at (قد تكون None إن لم تتوفر).
    """
    soup = BeautifulSoup(html, "html.parser")
    script_tag = soup.find("script", id="__NEXT_DATA__")
    if not script_tag or not script_tag.string:
        return None

    try:
        data = json.loads(script_tag.string)
    except json.JSONDecodeError:
        return None

    # بنية Next.js تختلف من موقع لآخر، لذلك نبحث بشكل عام عن أي عناصر
    # تحتوي على مفاتيح تشبه مقالات خبرية (title/url أو title/slug).
    found = []

    def walk(node):
        if isinstance(node, dict):
            has_title = isinstance(node.get("title"), str)
            has_link = isinstance(node.get("url"), str) or isinstance(node.get("slug"), str)
            if has_title and has_link:
                url = node.get("url") or node.get("slug")
                found.append(
                    {
                        "title": node["title"].strip(),
                        "url": _to_abs_url(url),
                        "published_at": node.get("date") or node.get("publishDate"),
                    }
                )
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(data)
    return found if found else None


def extract_articles_from_html(html: str):
    """
    خطة احتياطية: البحث المباشر عن روابط المقالات ونصوصها في HTML.
    """
    soup = BeautifulSoup(html, "html.parser")
    results = []
    seen_urls = set()

    for a in soup.find_all("a", href=True):
        href = a["href"]
        if ARTICLE_URL_PATTERN.search(href):
            abs_url = _to_abs_url(href)
            if abs_url in seen_urls:
                continue
            title = a.get_text(strip=True)
            if not title:
                continue
            seen_urls.add(abs_url)
            results.append({"title": title, "url": abs_url, "published_at": None})

    return results


def get_recent_articles():
    """
    يُعيد قائمة الأخبار المتاحة في آخر HOURS_WINDOW ساعات.
    إذا تعذر تحديد وقت النشر لخبر ما، يتم تضمينه احتياطًا (أفضل من تفويته)
    وتُترك عملية الفلترة النهائية لمرحلة إزالة التكرار (dedup) لضمان عدم إعادة نشره لاحقًا.
    """
    html = fetch_html(SOURCE_NEWS_LIST_URL)

    articles = parse_next_data(html)
    if not articles:
        articles = extract_articles_from_html(html)

    cutoff = datetime.now(timezone.utc) - timedelta(hours=HOURS_WINDOW)
    recent = []
    for art in articles:
        pub = art.get("published_at")
        if pub:
            try:
                pub_dt = datetime.fromisoformat(pub.replace("Z", "+00:00"))
                if pub_dt < cutoff:
                    continue
            except ValueError:
                pass
        recent.append(art)

    return recent


def fetch_article_detail(url: str):
    """
    يجلب نص الخبر الكامل والصورة البارزة من صفحة المقال.
    يعتمد على meta تاغز (og:title / og:image / og:description) لأنها الأكثر ثباتًا
    بصرف النظر عن تغييرات تصميم الموقع، مع خطة احتياطية لاستخراج الفقرات من الجسم.

    يُعيد dict: {title, image_url, body_text} أو None إذا تعذر جلب المحتوى أو الصورة.
    """
    html = fetch_html(url)
    soup = BeautifulSoup(html, "html.parser")

    def meta(prop):
        tag = soup.find("meta", property=prop) or soup.find("meta", attrs={"name": prop})
        return tag["content"].strip() if tag and tag.get("content") else None

    title = meta("og:title") or (soup.title.string.strip() if soup.title else None)
    image_url = meta("og:image")

    # محاولة استخراج نص المقال الكامل من جسم الصفحة
    body_candidates = soup.find_all(["p"])
    body_text = "\n".join(
        p.get_text(strip=True) for p in body_candidates if len(p.get_text(strip=True)) > 40
    )

    if not body_text:
        # احتياط: استخدم وصف meta إن لم نجد فقرات كافية
        body_text = meta("og:description") or ""

    if not title or not body_text:
        return None

    if not image_url:
        # لا توجد صورة بارزة → يُتجاهل الخبر بحسب الشرط المطلوب
        return {"title": title, "image_url": None, "body_text": body_text}

    return {"title": title, "image_url": _to_abs_url(image_url), "body_text": body_text}
