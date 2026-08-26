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
from requests.utils import requote_uri
from bs4 import BeautifulSoup

from config import (
    SOURCE_NEWS_LIST_URL,
    SOURCE_BASE_URL,
    USER_AGENT,
    REQUEST_TIMEOUT_SEC,
    HOURS_WINDOW,
    BLOCKED_IMAGE_URL_SUBSTRINGS,
)

HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept-Language": "ar,en;q=0.8",
}

ARTICLE_URL_PATTERN = re.compile(r"/ar/(article|news)/[^\"'\s>]+")

# أجزاء مسار تدل أن الرابط لصفحة "قسم/تجميع" (فريق، بطولة، منتخب، لاعب...)
# وليس خبرًا مفردًا، حتى لو كان الرابط يحتوي كلمة news/article في مكان ما.
HUB_PATH_KEYWORDS = (
    "/team/",
    "/teams/",
    "/competition/",
    "/competitions/",
    "/league/",
    "/leagues/",
    "/player/",
    "/players/",
    "/standings",
    "/fixtures",
    "/tournament/",
    "/tournaments/",
    "/category/",
    "/tag/",
    "/tags/",
)


def _is_probable_single_article(url: str, title: str) -> bool:
    """
    يميّز بين رابط خبر حقيقي ورابط صفحة قسم/فريق/بطولة تجميعية.

    شروط اعتبار الرابط خبرًا حقيقيًا:
      1) الرابط يحتوي مقطع /ar/news/ أو /ar/article/.
      2) الرابط لا يحتوي أي من كلمات صفحات التجميع (فريق/بطولة/لاعب...).
      3) العنوان جملة كاملة (عدة كلمات) وليس مجرد اسم فريق أو بطولة قصير
         (عناوين الأخبار الحقيقية على 365scores طويلة ووصفية، بعكس عناوين
         صفحات الفرق والبطولات التي تكون اسم الكيان فقط).
      4) نهاية الرابط (slug) تحتوي عدة كلمات مفصولة بشرطات، لا كلمة أو كلمتين فقط.
    """
    if not ARTICLE_URL_PATTERN.search(url):
        return False

    lowered_url = url.lower()
    if any(keyword in lowered_url for keyword in HUB_PATH_KEYWORDS):
        return False

    word_count = len(title.split())
    if word_count < 4:
        return False

    slug = url.rstrip("/").split("/")[-1]
    if slug.count("-") < 2 and slug.count("%") < 6:
        # عنوان قصير بدون تفاصيل كافية في الرابط غالبًا صفحة كيان (فريق/بطولة) وليس خبرًا
        return False

    return True


def fetch_html(url: str) -> str:
    resp = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT_SEC)
    resp.raise_for_status()
    return resp.text


def _to_abs_url(url: str) -> str:
    if not url.startswith("http"):
        url = SOURCE_BASE_URL.rstrip("/") + "/" + url.lstrip("/")
    # يحوّل أي حروف غير آمنة (عربية أو رموز) في الرابط إلى ترميز URL سليم،
    # مع عدم إعادة ترميز الأجزاء المُشفّرة مسبقًا (يمنع مشاكل الترميز لاحقًا
    # عند استخدام الرابط داخل هيدرز HTTP مثل Referer).
    return requote_uri(url)


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
                abs_url = _to_abs_url(url)
                title = node["title"].strip()
                if _is_probable_single_article(abs_url, title):
                    found.append(
                        {
                            "title": title,
                            "url": abs_url,
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
            if not _is_probable_single_article(abs_url, title):
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


# كلمات تدل أن الصورة عبارة عن شعار/بطاقة مشاركة عامة وليست صورة الخبر الفعلية
GENERIC_IMAGE_HINTS = (
    "logo",
    "default",
    "placeholder",
    "share",
    "og-image",
    "og_image",
    "social",
    "cover",
    "favicon",
    "app-icon",
    "appicon",
)


def _looks_generic(image_url: str) -> bool:
    lowered = image_url.lower()
    if any(hint in lowered for hint in GENERIC_IMAGE_HINTS):
        return True
    if any(sub.lower() in lowered for sub in BLOCKED_IMAGE_URL_SUBSTRINGS):
        return True
    return False


def _extract_jsonld_image(soup: BeautifulSoup):
    """
    يبحث عن صورة الخبر داخل بيانات JSON-LD المهيكلة (schema.org/NewsArticle)،
    وهي غالبًا أدق مصدر لصورة الخبر الفعلية لأنها مخصّصة للمقال نفسه وليست بطاقة مشاركة عامة.
    """
    for script in soup.find_all("script", type="application/ld+json"):
        if not script.string:
            continue
        try:
            data = json.loads(script.string)
        except json.JSONDecodeError:
            continue

        candidates = data if isinstance(data, list) else [data]
        for item in candidates:
            if not isinstance(item, dict):
                continue
            image = item.get("image")
            if isinstance(image, str):
                return image
            if isinstance(image, dict) and isinstance(image.get("url"), str):
                return image["url"]
            if isinstance(image, list) and image:
                first = image[0]
                if isinstance(first, str):
                    return first
                if isinstance(first, dict) and isinstance(first.get("url"), str):
                    return first["url"]
    return None


def _extract_in_article_image(soup: BeautifulSoup):
    """
    خطة احتياطية أخيرة: يبحث عن أول صورة حقيقية داخل حاوية محتوى المقال
    (article/main أو أي عنصر بكلاس يحتوي article/content/story)، متجاهلًا الأيقونات الصغيرة.
    """
    containers = soup.find_all(["article", "main"])
    containers += soup.find_all(
        lambda tag: tag.name in ("div", "section")
        and tag.get("class")
        and any(
            key in " ".join(tag.get("class")).lower()
            for key in ("article", "content", "story", "post")
        )
    )

    for container in containers:
        for img in container.find_all("img"):
            src = img.get("src") or img.get("data-src") or img.get("data-lazy-src")
            if not src or src.startswith("data:"):
                continue
            if _looks_generic(src):
                continue
            # تجاهل الصور الصغيرة جدًا إن كانت الأبعاد معروفة (أيقونات/فواصل)
            try:
                width = int(img.get("width", 0))
                height = int(img.get("height", 0))
                if 0 < width < 120 or 0 < height < 120:
                    continue
            except ValueError:
                pass
            return src
    return None


def fetch_article_detail(url: str):
    """
    يجلب نص الخبر الكامل والصورة البارزة من صفحة المقال.

    ترتيب البحث عن الصورة البارزة (من الأدق إلى الأقل دقة):
      1) بيانات JSON-LD المهيكلة (schema.org) — الأدق لأنها مخصصة لهذا المقال تحديدًا.
      2) وسم og:image، بشرط ألا يكون صورة عامة معروفة (شعار/بطاقة مشاركة افتراضية).
      3) أول صورة حقيقية داخل محتوى المقال نفسه.

    يُعيد dict: {title, image_url, body_text} أو None إذا تعذر جلب المحتوى.
    """
    html = fetch_html(url)
    soup = BeautifulSoup(html, "html.parser")

    def meta(prop):
        tag = soup.find("meta", property=prop) or soup.find("meta", attrs={"name": prop})
        return tag["content"].strip() if tag and tag.get("content") else None

    title = meta("og:title") or (soup.title.string.strip() if soup.title else None)

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

    image_url = _extract_jsonld_image(soup)

    if not image_url:
        og_image = meta("og:image")
        if og_image and not _looks_generic(og_image):
            image_url = og_image

    if not image_url:
        image_url = _extract_in_article_image(soup)

    if not image_url:
        # لا توجد صورة بارزة فعلية → يُتجاهل الخبر بحسب الشرط المطلوب
        return {"title": title, "image_url": None, "body_text": body_text}

    return {"title": title, "image_url": _to_abs_url(image_url), "body_text": body_text}
