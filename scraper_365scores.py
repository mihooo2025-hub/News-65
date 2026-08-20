"""
scraper_365scores.py
====================
Fetches football news from 365Scores Arabic news pages.
Handles article discovery, URL normalization, metadata extraction,
Arabic date parsing, robust content collection, and featured image
extraction via BeautifulSoup + Playwright fallback.
"""

from __future__ import annotations

import asyncio
import html
import json
import re
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, unquote, urljoin, urlparse

from bs4 import BeautifulSoup
from playwright.async_api import Page, async_playwright

from models import ArticleSummary, SourceArticle


# ============================================================================
# Constants & Helpers
# ============================================================================

ARABIC_DIGITS = str.maketrans(
    "٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹",
    "01234567890123456789",
)

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/138.0.0.0 Safari/537.36"
)

# لا نضع "365scores" هنا، لأن صور المقالات الحقيقية قد تكون مستضافة
# على نطاقات أو مسارات تابعة لـ 365Scores.
DEFAULT_LOGO_PATTERNS = (
    "logo",
    "icon",
    "avatar",
    "favicon",
    "placeholder",
    "default-image",
    "default_image",
    "no-image",
    "no_image",
)

ARTICLE_ROOT = "/ar/news/magazine"


def normalize_url(base_url: str, value: str) -> str:
    """Convert relative URLs to absolute URLs and remove fragments."""
    if not value:
        return ""

    value = html.unescape(str(value)).strip()

    if not value:
        return ""

    return urljoin(base_url, value).split("#", 1)[0]


def news_index_url(source_url: str) -> str:
    parsed = urlparse(source_url)

    if parsed.path.rstrip("/") in {"", "/ar"}:
        return f"{parsed.scheme}://{parsed.netloc}/ar/news/magazine/"

    return source_url


def _decode_path(path: str) -> str:
    """
    Decode percent-encoded Arabic URLs safely.
    Handles paths containing values such as:
    %d9%83%d8%b1%d8%a9
    """
    decoded = path or ""

    for _ in range(3):
        new_value = unquote(decoded)

        if new_value == decoded:
            break

        decoded = new_value

    return decoded


def is_probable_article_url(url: str) -> bool:
    """
    Conservative article URL filter.

    IMPORTANT:
    The goal is NOT to aggressively reject possible articles.
    A genuine article is preferred over accidentally losing news.

    Only reject URLs that are clearly not individual news articles.
    """

    if not url:
        return False

    try:
        parsed = urlparse(url)
    except Exception:
        return False

    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return False

    path = _decode_path(parsed.path).lower().rstrip("/")

    # يجب أن يكون الرابط داخل قسم الأخبار العربية للمجلة.
    if not path.startswith(ARTICLE_ROOT):
        return False

    # ------------------------------------------------------------------
    # دعم المقالات التي تستخدم:
    # /ar/news/magazine/?p=123456
    # ------------------------------------------------------------------
    if path == ARTICLE_ROOT:
        query = parse_qs(parsed.query)
        post_ids = query.get("p", [])

        return any(
            re.fullmatch(r"\d+", post_id or "")
            for post_id in post_ids
        )

    # ------------------------------------------------------------------
    # يجب أن يكون هناك شيء بعد /ar/news/magazine/
    # ------------------------------------------------------------------
    if not path.startswith(ARTICLE_ROOT + "/"):
        return False

    relative_path = path[len(ARTICLE_ROOT):].strip("/")

    if not relative_path:
        return False

    # ------------------------------------------------------------------
    # استبعاد صفحات الترقيم فقط:
    # /page/2
    # /page/3
    # ------------------------------------------------------------------
    if re.fullmatch(r"page/\d+", relative_path):
        return False

    # ------------------------------------------------------------------
    # استبعاد المسارات المؤكدة غير المقالية فقط.
    # لا نستبعد كلمات عامة مثل الأهلي أو الدوري أو الهلال،
    # لأنها قد تكون جزءًا من عنوان خبر حقيقي.
    # ------------------------------------------------------------------
    clearly_non_article_patterns = (
        r"^category(?:/|$)",
        r"^tag(?:/|$)",
        r"^content-tag(?:/|$)",
        r"^author(?:/|$)",
        r"^search(?:/|$)",
        r"^page/\d+$",
    )

    if any(
        re.search(pattern, relative_path)
        for pattern in clearly_non_article_patterns
    ):
        return False

    # نأخذ أول slug للفحص المحافظ.
    slug = relative_path.split("/")[0].strip()

    if not slug:
        return False

    # ------------------------------------------------------------------
    # روابط القنوات الناقلة اليومية.
    # نستبعد النمط المؤكد فقط.
    # ------------------------------------------------------------------
    if re.match(
        r"^القنوات-الناقلة-لمباريات-اليوم(?:-|$)",
        slug,
    ):
        return False

    if re.match(
        r"^القنوات-الناقلة-لمباراة-اليوم(?:-|$)",
        slug,
    ):
        return False

    # ------------------------------------------------------------------
    # صفحات الإحصائيات الواضحة الخاصة بكأس العالم.
    # ------------------------------------------------------------------
    if re.match(
        r"^(?:إحصائيات|احصائيات)-كأس-العالم(?:-|$)",
        slug,
    ):
        return False

    # ------------------------------------------------------------------
    # صفحات تجميعية مؤكدة وغير مقالية.
    # ------------------------------------------------------------------
    exact_non_articles = {
        "الأهداف-العكسية-كأس-العالم",
        "بطاقة-حمراء-مجموعات",
    }

    if slug in exact_non_articles:
        return False

    # ------------------------------------------------------------------
    # لا نقوم هنا باستبعاد:
    #
    # الأهلي
    # الهلال
    # النصر
    # القادسية
    # صلاح
    # أستون فيلا
    # الدوري
    # كأس
    # منتخب
    #
    # لأن هذه الكلمات قد تكون جزءًا من عنوان خبر حقيقي.
    # ------------------------------------------------------------------

    # إذا لم يكن الرابط مؤكدًا أنه غير مقالي، نسمح له بالمرور.
    return True


# ============================================================================
# Time Parsing Utilities
# ============================================================================

def parse_relative_time(text: str, now: datetime) -> datetime | None:
    value = re.sub(
        r"\s+",
        " ",
        text.strip().translate(ARABIC_DIGITS),
    )

    if not value:
        return None

    if "الآن" in value or "قبل 0 دقيقة" in value:
        return now

    patterns = [
        (r"(?:قبل|منذ)\s+(\d+)\s*(?:دقيقة|دقائق)", "minutes"),
        (r"(?:قبل|منذ)\s+(\d+)\s*(?:ساعة|ساعات)", "hours"),
        (r"(?:قبل|منذ)\s+(\d+)\s*(?:يوم|أيام)", "days"),
    ]

    for pattern, unit in patterns:
        if match := re.search(pattern, value):
            amount = int(match.group(1))

            if unit == "minutes":
                delta = timedelta(minutes=amount)
            elif unit == "hours":
                delta = timedelta(hours=amount)
            else:
                delta = timedelta(days=amount)

            return now - delta

    return None


def parse_absolute_time(text: str) -> datetime | None:
    value = re.sub(
        r"\s+",
        " ",
        text.strip().translate(ARABIC_DIGITS),
    )

    if match := re.search(
        r"(\d{1,2})/(\d{1,2})/(\d{4})"
        r"\s*(?:-|–|—)?\s*"
        r"(\d{1,2}):(\d{2})\s*([صم])",
        value,
    ):
        day, month, year, hour, minute = map(
            int,
            match.groups()[:5],
        )

        meridiem = match.group(6)

        if meridiem == "م" and hour < 12:
            hour += 12
        elif meridiem == "ص" and hour == 12:
            hour = 0

        try:
            return datetime(
                year,
                month,
                day,
                hour,
                minute,
                tzinfo=timezone(timedelta(hours=3)),
            ).astimezone(timezone.utc)

        except ValueError:
            return None

    if match := re.search(
        r"\b(20\d{2}-\d{2}-\d{2}T[^\s<]+)",
        value,
    ):
        try:
            dt = datetime.fromisoformat(
                match.group(1).replace("Z", "+00:00")
            )

            return (
                dt
                if dt.tzinfo
                else dt.replace(tzinfo=timezone.utc)
            )

        except ValueError:
            pass

    return None


def parse_any_time(text: str, now: datetime) -> datetime | None:
    return (
        parse_absolute_time(text)
        or parse_relative_time(text, now)
    )


# ============================================================================
# JSON-LD Extraction
# ============================================================================

def extract_json_ld(soup: BeautifulSoup) -> list[dict]:
    """
    Extract JSON-LD objects and nested @graph objects.
    """
    items: list[dict] = []

    def collect(data) -> None:
        if isinstance(data, dict):
            items.append(data)

            graph = data.get("@graph")

            if isinstance(graph, list):
                for entry in graph:
                    collect(entry)

        elif isinstance(data, list):
            for entry in data:
                collect(entry)

    for script in soup.select(
        'script[type="application/ld+json"]'
    ):
        raw = script.string or script.get_text()

        if not raw:
            continue

        try:
            data = json.loads(raw.strip())
            collect(data)

        except Exception:
            continue

    return items


def first_meta(soup: BeautifulSoup, *names: str) -> str:
    for name in names:
        tag = (
            soup.find(
                "meta",
                attrs={"property": name},
            )
            or soup.find(
                "meta",
                attrs={"name": name},
            )
        )

        if tag:
            value = (
                tag.get("content")
                or tag.get("value")
                or ""
            ).strip()

            if value:
                return value

    return ""


# ============================================================================
# Featured Image Helpers
# ============================================================================

def _clean_image_candidate(value: str | None) -> str:
    """Clean and normalize a possible image URL."""
    if not value:
        return ""

    value = html.unescape(str(value)).strip()

    if not value:
        return ""

    if value.startswith(
        (
            "data:",
            "blob:",
            "javascript:",
        )
    ):
        return ""

    # دعم CSS: url(...)
    match = re.fullmatch(
        r"url\(\s*['\"]?(.*?)['\"]?\s*\)",
        value,
        re.IGNORECASE,
    )

    if match:
        value = match.group(1).strip()

    return value.strip(" '\"")


def _is_valid_article_image(url_str: str) -> bool:
    """
    Check if the URL looks like a genuine article image.

    IMPORTANT:
    Do not reject URLs merely because they contain "365scores".
    """

    value = _clean_image_candidate(url_str)

    if not value:
        return False

    lower_url = value.lower()

    if lower_url.startswith(
        (
            "data:",
            "blob:",
            "javascript:",
        )
    ):
        return False

    parsed = urlparse(value)
    path = parsed.path.lower()

    # غالبًا SVG تكون أيقونات أو شعارات.
    if path.endswith(".svg"):
        return False

    # استبعاد الصور الواضح أنها شعار أو placeholder.
    if any(
        pattern in lower_url
        for pattern in DEFAULT_LOGO_PATTERNS
    ):
        return False

    return True


def _pick_from_srcset(srcset: str | None) -> str:
    """
    Pick the largest/best candidate from srcset.
    """

    if not srcset:
        return ""

    candidates = []

    for raw_item in srcset.split(","):
        item = raw_item.strip()

        if not item:
            continue

        parts = item.split()

        if not parts:
            continue

        url = _clean_image_candidate(parts[0])

        if not url:
            continue

        score = 0.0

        if len(parts) > 1:
            descriptor = parts[1].lower()

            width_match = re.fullmatch(
                r"(\d+(?:\.\d+)?)w",
                descriptor,
            )

            density_match = re.fullmatch(
                r"(\d+(?:\.\d+)?)x",
                descriptor,
            )

            if width_match:
                score = float(width_match.group(1))

            elif density_match:
                score = float(density_match.group(1)) * 1000

        candidates.append((score, url))

    if not candidates:
        return ""

    candidates.sort(
        key=lambda item: item[0],
        reverse=True,
    )

    return candidates[0][1]


def _extract_image_urls_from_json(value) -> list[str]:
    """
    Recursively extract image URLs from JSON-LD structures.
    """

    results: list[str] = []

    if isinstance(value, str):
        if _is_valid_article_image(value):
            results.append(value.strip())

    elif isinstance(value, list):
        for item in value:
            results.extend(
                _extract_image_urls_from_json(item)
            )

    elif isinstance(value, dict):
        for key in (
            "url",
            "contentUrl",
            "contentURL",
            "thumbnailUrl",
            "thumbnailURL",
        ):
            candidate = value.get(key)

            if (
                isinstance(candidate, str)
                and _is_valid_article_image(candidate)
            ):
                results.append(candidate.strip())

        for key in (
            "image",
            "primaryImageOfPage",
            "thumbnail",
        ):
            if key in value:
                results.extend(
                    _extract_image_urls_from_json(
                        value[key]
                    )
                )

    return results


def _extract_dom_image_candidates(
    container,
) -> list[str]:
    """
    Extract all possible image URLs from a DOM container.
    Supports:
    - picture
    - source
    - img
    - lazy loading
    - srcset
    - noscript
    - CSS background-image
    """

    candidates: list[str] = []

    direct_attributes = (
        "src",
        "data-src",
        "data-lazy-src",
        "data-original",
        "data-image",
        "data-image-src",
        "data-img-src",
        "data-url",
        "data-fallback-src",
        "data-src-webp",
        "data-webp",
        "data-original-src",
    )

    srcset_attributes = (
        "srcset",
        "data-srcset",
        "data-lazy-srcset",
        "data-original-srcset",
    )

    # ------------------------------------------------------------------
    # img / source / picture
    # ------------------------------------------------------------------
    for element in container.select(
        "picture img, picture source, img, source"
    ):
        for attr in srcset_attributes:
            value = element.get(attr)

            if value:
                candidate = _pick_from_srcset(value)

                if (
                    candidate
                    and _is_valid_article_image(candidate)
                ):
                    candidates.append(candidate)

        for attr in direct_attributes:
            value = element.get(attr)

            if value:
                candidate = _clean_image_candidate(value)

                if (
                    candidate
                    and _is_valid_article_image(candidate)
                ):
                    candidates.append(candidate)

    # ------------------------------------------------------------------
    # noscript
    # ------------------------------------------------------------------
    for noscript in container.find_all("noscript"):
        raw = noscript.get_text(
            " ",
            strip=True,
        )

        if not raw:
            continue

        try:
            nested_soup = BeautifulSoup(
                raw,
                "html.parser",
            )

            for element in nested_soup.select(
                "img, source"
            ):
                for attr in srcset_attributes:
                    value = element.get(attr)

                    if value:
                        candidate = _pick_from_srcset(value)

                        if (
                            candidate
                            and _is_valid_article_image(candidate)
                        ):
                            candidates.append(candidate)

                for attr in direct_attributes:
                    value = element.get(attr)

                    if value:
                        candidate = _clean_image_candidate(value)

                        if (
                            candidate
                            and _is_valid_article_image(candidate)
                        ):
                            candidates.append(candidate)

        except Exception:
            continue

    # ------------------------------------------------------------------
    # background-image
    # ------------------------------------------------------------------
    for tag in container.find_all(
        True,
        style=True,
    ):
        style = tag.get("style", "")

        if "background-image" not in style.lower():
            continue

        matches = re.findall(
            r"url\(\s*(['\"]?)(.*?)\1\s*\)",
            style,
            re.IGNORECASE,
        )

        for _, value in matches:
            candidate = _clean_image_candidate(value)

            if (
                candidate
                and _is_valid_article_image(candidate)
            ):
                candidates.append(candidate)

    return candidates


def extract_featured_image(soup: BeautifulSoup) -> str:
    """
    Extract featured article image using BeautifulSoup.

    Search order:
    1. JSON-LD
    2. OpenGraph / Twitter meta
    3. link rel=image_src
    4. article containers
    5. picture / source / img
    6. Lazy Loading attributes
    7. noscript
    8. CSS background-image
    """

    # ------------------------------------------------------------------
    # 1. JSON-LD
    # ------------------------------------------------------------------
    for item in extract_json_ld(soup):
        for key in (
            "image",
            "primaryImageOfPage",
            "thumbnailUrl",
        ):
            if key not in item:
                continue

            candidates = _extract_image_urls_from_json(
                item.get(key)
            )

            for candidate in candidates:
                if _is_valid_article_image(candidate):
                    return candidate.strip()

    # ------------------------------------------------------------------
    # 2. OpenGraph / Twitter
    # ------------------------------------------------------------------
    meta_image = first_meta(
        soup,
        "og:image",
        "og:image:url",
        "og:image:secure_url",
        "twitter:image",
        "twitter:image:src",
    )

    if (
        meta_image
        and _is_valid_article_image(meta_image)
    ):
        return meta_image.strip()

    # ------------------------------------------------------------------
    # 3. <link rel="image_src">
    # ------------------------------------------------------------------
    for link_tag in soup.find_all("link"):
        rel = link_tag.get("rel")

        if not rel:
            continue

        rel_text = (
            " ".join(rel)
            if isinstance(rel, list)
            else str(rel)
        ).lower()

        if "image_src" not in rel_text:
            continue

        href = link_tag.get("href", "")

        if (
            href
            and _is_valid_article_image(href)
        ):
            return href.strip()

    # ------------------------------------------------------------------
    # 4. Article containers
    # ------------------------------------------------------------------
    selectors = (
        "article",
        "main",
        "[data-testid*='article']",
        "[data-test*='article']",
        "[class*='article']",
        "[class*='Article']",
        "[class*='news']",
        "[class*='News']",
        "[class*='content']",
        "[class*='Content']",
    )

    seen = set()

    for selector in selectors:
        for container in soup.select(selector):
            container_id = id(container)

            if container_id in seen:
                continue

            seen.add(container_id)

            candidates = _extract_dom_image_candidates(
                container
            )

            for candidate in candidates:
                if _is_valid_article_image(candidate):
                    return candidate.strip()

    return ""


# ============================================================================
# Playwright Featured Image Fallback
# ============================================================================

async def extract_featured_image_with_playwright(
    page: Page,
) -> str:
    """
    Final fallback when BeautifulSoup cannot find the article image.

    The browser:
    1. Scrolls article containers into view.
    2. Waits for lazy loading.
    3. Checks currentSrc.
    4. Checks all lazy-loading attributes.
    5. Checks srcset.
    6. Checks picture/source/img.
    7. Rejects tiny images and obvious icons/logos.
    """

    try:
        # محاولة تحفيز Lazy Loading.
        await page.evaluate(
            """async () => {
                const containers = document.querySelectorAll(
                    [
                        'article',
                        'main',
                        '[class*="article"]',
                        '[class*="Article"]',
                        '[class*="news"]',
                        '[class*="News"]',
                        '[class*="content"]',
                        '[class*="Content"]'
                    ].join(',')
                );

                containers.forEach(container => {
                    try {
                        container.scrollIntoView({
                            block: 'center',
                            behavior: 'instant'
                        });
                    } catch (_) {}
                });

                await new Promise(resolve =>
                    setTimeout(resolve, 1500)
                );
            }"""
        )

        image_url = await page.evaluate(
            """() => {
                const invalidPatterns = [
                    'logo',
                    'icon',
                    'avatar',
                    'favicon',
                    'placeholder',
                    'default-image',
                    'default_image',
                    'no-image',
                    'no_image'
                ];

                const isValid = (value) => {
                    if (!value || typeof value !== 'string') {
                        return false;
                    }

                    const url = value.trim();

                    if (!url) {
                        return false;
                    }

                    if (
                        /^(data:|blob:|javascript:)/i.test(url)
                    ) {
                        return false;
                    }

                    const lower = url.toLowerCase();

                    if (/\\.svg(?:$|\\?)/i.test(lower)) {
                        return false;
                    }

                    if (
                        invalidPatterns.some(pattern =>
                            lower.includes(pattern)
                        )
                    ) {
                        return false;
                    }

                    return true;
                };

                const getBestSrcset = (srcset) => {
                    if (!srcset) {
                        return '';
                    }

                    const items = srcset
                        .split(',')
                        .map(item => item.trim())
                        .filter(Boolean);

                    let bestUrl = '';
                    let bestScore = -1;

                    for (const item of items) {
                        const parts =
                            item.split(/\\s+/);

                        const url = parts[0];

                        if (!isValid(url)) {
                            continue;
                        }

                        let score = 0;

                        if (parts[1]) {
                            const descriptor = parts[1];

                            const width =
                                descriptor.match(
                                    /(\\d+(?:\\.\\d+)?)w/
                                );

                            const density =
                                descriptor.match(
                                    /(\\d+(?:\\.\\d+)?)x/
                                );

                            if (width) {
                                score =
                                    Number(width[1]);
                            } else if (density) {
                                score =
                                    Number(density[1])
                                    * 1000;
                            }
                        }

                        if (score >= bestScore) {
                            bestScore = score;
                            bestUrl = url;
                        }
                    }

                    return bestUrl;
                };

                const roots = [
                    ...document.querySelectorAll(
                        [
                            'article',
                            'main',
                            '[data-testid*="article"]',
                            '[data-test*="article"]',
                            '[class*="article"]',
                            '[class*="Article"]',
                            '[class*="news"]',
                            '[class*="News"]',
                            '[class*="content"]',
                            '[class*="Content"]'
                        ].join(',')
                    )
                ];

                const seenUrls = new Set();

                for (const root of roots) {
                    const elements =
                        root.querySelectorAll(
                            'picture, picture source, picture img, img, source'
                        );

                    for (const element of elements) {
                        const candidates = [];

                        // الصورة التي اختارها المتصفح فعليًا.
                        if (element.currentSrc) {
                            candidates.push(
                                element.currentSrc
                            );
                        }

                        // srcset
                        const srcsetAttributes = [
                            'srcset',
                            'data-srcset',
                            'data-lazy-srcset',
                            'data-original-srcset'
                        ];

                        for (
                            const attribute
                            of srcsetAttributes
                        ) {
                            const value =
                                element.getAttribute(
                                    attribute
                                );

                            if (value) {
                                const best =
                                    getBestSrcset(value);

                                if (best) {
                                    candidates.push(best);
                                }
                            }
                        }

                        // جميع خصائص Lazy Loading المحتملة.
                        const directAttributes = [
                            'src',
                            'data-src',
                            'data-lazy-src',
                            'data-original',
                            'data-image',
                            'data-image-src',
                            'data-img-src',
                            'data-url',
                            'data-fallback-src',
                            'data-src-webp',
                            'data-webp',
                            'data-original-src'
                        ];

                        for (
                            const attribute
                            of directAttributes
                        ) {
                            const value =
                                element.getAttribute(
                                    attribute
                                );

                            if (value) {
                                candidates.push(value);
                            }
                        }

                        for (
                            const candidate
                            of candidates
                        ) {
                            if (!isValid(candidate)) {
                                continue;
                            }

                            const url =
                                String(candidate).trim();

                            if (seenUrls.has(url)) {
                                continue;
                            }

                            seenUrls.add(url);

                            // نستبعد الصور الصغيرة فقط
                            // عندما تكون أبعادها معروفة.
                            if (
                                element.tagName === 'IMG'
                            ) {
                                const width =
                                    element.naturalWidth
                                    || 0;

                                const height =
                                    element.naturalHeight
                                    || 0;

                                if (
                                    width > 0 &&
                                    height > 0 &&
                                    (
                                        width < 180 ||
                                        height < 120
                                    )
                                ) {
                                    continue;
                                }
                            }

                            return url;
                        }
                    }
                }

                return '';
            }"""
        )

        return (
            image_url.strip()
            if image_url
            else ""
        )

    except Exception:
        return ""


# ============================================================================
# Published Time
# ============================================================================

def extract_published_at(
    soup: BeautifulSoup,
) -> datetime | None:

    now = datetime.now(timezone.utc)

    for key in (
        "article:published_time",
        "datePublished",
        "dateCreated",
        "publish_date",
    ):
        if val := first_meta(soup, key):
            try:
                dt = datetime.fromisoformat(
                    val.replace("Z", "+00:00")
                )

                return (
                    dt
                    if dt.tzinfo
                    else dt.replace(
                        tzinfo=timezone.utc
                    )
                )

            except ValueError:
                if parsed := parse_any_time(
                    val,
                    now,
                ):
                    return parsed

    for item in extract_json_ld(soup):
        for key in (
            "datePublished",
            "dateCreated",
        ):
            if val := item.get(key):
                if isinstance(val, str):
                    try:
                        dt = datetime.fromisoformat(
                            val.replace(
                                "Z",
                                "+00:00",
                            )
                        )

                        return (
                            dt
                            if dt.tzinfo
                            else dt.replace(
                                tzinfo=timezone.utc
                            )
                        )

                    except ValueError:
                        if parsed := parse_any_time(
                            val,
                            now,
                        ):
                            return parsed

    for time_tag in soup.select(
        "time[datetime]"
    ):
        if parsed := parse_any_time(
            time_tag.get(
                "datetime",
                "",
            ).strip(),
            now,
        ):
            return parsed

    return parse_any_time(
        soup.get_text(
            " ",
            strip=True,
        ),
        now,
    )


# ============================================================================
# Article Text
# ============================================================================

def extract_article_text(
    soup: BeautifulSoup,
) -> str:

    for item in extract_json_ld(soup):
        body = (
            item.get("articleBody")
            or item.get("description")
        )

        if (
            isinstance(body, str)
            and len(body.strip()) > 80
        ):
            return body.strip()

    paragraphs = []

    for p in soup.select(
        "article p, "
        "main p, "
        "[class*='article'] p, "
        "[class*='News'] p, "
        "[class*='text'] p, "
        "p"
    ):
        text = p.get_text(
            " ",
            strip=True,
        )

        if text and len(text) > 15:
            paragraphs.append(text)

    if paragraphs:
        full_p = "\n".join(paragraphs)

        if len(full_p) >= 80:
            return full_p

    candidates = []

    selectors = (
        "article",
        "main",
        "[class*='article']",
        "[class*='Article']",
        "[class*='news']",
        "[class*='News']",
        "[class*='content']",
        "[class*='Content']",
    )

    for selector in selectors:
        if node := soup.select_one(selector):
            if text := node.get_text(
                "\n",
                strip=True,
            ):
                candidates.append(text)

    if not candidates:
        candidates.append(
            soup.get_text(
                "\n",
                strip=True,
            )
        )

    text = max(
        candidates,
        key=len,
        default="",
    )

    lines = [
        re.sub(
            r"\s+",
            " ",
            line,
        ).strip()
        for line in text.splitlines()
        if line.strip()
    ]

    return "\n".join(lines)


# ============================================================================
# Playwright Automation Tasks
# ============================================================================

async def collect_article_cards(
    page: Page,
    source_url: str,
) -> list[ArticleSummary]:

    now = datetime.now(timezone.utc)

    results: dict[str, ArticleSummary] = {}

    async def collect_current_dom() -> None:
        anchors = await page.locator(
            "a[href]"
        ).evaluate_all(
            """els => els.map(a => ({
                href: a.href,
                text: (a.innerText || '').trim(),
                aria: a.getAttribute('aria-label') || ''
            }))"""
        )

        for item in anchors:
            url = normalize_url(
                source_url,
                str(item.get("href", "")),
            )

            # فلترة محافظة جدًا قبل مرحلة الجلب.
            if not is_probable_article_url(url):
                continue

            title_text = re.sub(
                r"\s+",
                " ",
                str(
                    item.get("text", "")
                    or item.get("aria", "")
                ),
            ).strip()

            if (
                not title_text
                or len(title_text) < 12
            ):
                continue

            parsed_time = parse_any_time(
                title_text,
                now,
            )

            clean_title = re.sub(
                r"\s+\d{1,2}/\d{1,2}/\d{4}.*$",
                "",
                title_text,
            ).strip()

            clean_title = re.sub(
                r"\s+(?:قبل|منذ)\s+\d+\s+\S+.*$",
                "",
                clean_title,
            ).strip()

            clean_title = clean_title or title_text

            current = results.get(url)

            candidate = ArticleSummary(
                url=url,
                title=clean_title[:300],
                relative_time_text=title_text,
                published_at=parsed_time,
            )

            if (
                current is None
                or len(candidate.title)
                > len(current.title)
            ):
                results[url] = candidate

    await collect_current_dom()

    for _ in range(10):
        try:
            await page.mouse.wheel(
                0,
                5000,
            )

            await page.wait_for_timeout(900)

            await collect_current_dom()

        except Exception:
            break

    return list(results.values())


async def fetch_article(
    page: Page,
    summary: ArticleSummary,
) -> SourceArticle:

    await page.goto(
        summary.url,
        wait_until="domcontentloaded",
        timeout=45_000,
    )

    # انتظار أولي لتحميل الصفحة.
    await page.wait_for_timeout(1_500)

    html_content = await page.content()

    soup = BeautifulSoup(
        html_content,
        "html.parser",
    )

    title = (
        first_meta(
            soup,
            "og:title",
            "twitter:title",
        )
        or (
            soup.find("h1").get_text(
                " ",
                strip=True,
            )
            if soup.find("h1")
            else summary.title
        )
    )

    # ==================================================================
    # المرحلة الأولى:
    # استخراج الصورة من HTML / JSON-LD / Meta / Lazy attributes.
    # ==================================================================
    image_url = extract_featured_image(soup)

    # ==================================================================
    # المرحلة الثانية:
    # إذا لم يجد BeautifulSoup الصورة، نستخدم المتصفح نفسه.
    #
    # هذا يسمح لـ Lazy Loading بالعمل وقراءة currentSrc.
    # ==================================================================
    if not image_url:
        image_url = await extract_featured_image_with_playwright(
            page
        )

    # ==================================================================
    # توحيد الرابط بعد استخراج الصورة.
    # ==================================================================
    image_url = normalize_url(
        summary.url,
        image_url,
    )

    return SourceArticle(
        url=summary.url,
        title=title.strip(),
        text=extract_article_text(soup).strip(),
        image_url=image_url,
        published_at=(
            extract_published_at(soup)
            or summary.published_at
        ),
    )


async def create_browser_context(playwright):
    browser = await playwright.chromium.launch(
        headless=True
    )

    page = await browser.new_page(
        locale="ar-SA",
        user_agent=USER_AGENT,
        timezone_id="Asia/Riyadh",
    )

    return browser, page


# ============================================================================
# Discovery
# ============================================================================

async def discover_articles(
    source_url: str,
    max_articles: int,
) -> tuple[
    list[ArticleSummary],
    list[str],
]:

    candidates = []
    errors = []

    index_url = news_index_url(source_url)

    async with async_playwright() as playwright:
        browser, page = await create_browser_context(
            playwright
        )

        try:
            await page.goto(
                index_url,
                wait_until="domcontentloaded",
                timeout=45_000,
            )

            await page.wait_for_timeout(2_000)

            for selector in (
                "button:has-text('موافق')",
                "button:has-text('السماح')",
                "button:has-text('إغلاق')",
                "button[aria-label='Close']",
            ):
                try:
                    locator = page.locator(selector)

                    if await locator.count():
                        await locator.first.scroll_into_view_if_needed()

                        await locator.first.click(
                            timeout=5_000
                        )

                except Exception:
                    pass

            summaries = await collect_article_cards(
                page,
                index_url,
            )

            summaries.sort(
                key=lambda item: (
                    item.published_at
                    or datetime.min.replace(
                        tzinfo=timezone.utc
                    )
                ),
                reverse=True,
            )

            summaries = summaries[:max_articles]

            if not summaries:
                errors.append(
                    f"No article candidates discovered "
                    f"from {index_url}"
                )

            else:
                print(
                    f"DISCOVERY | "
                    f"index={index_url} "
                    f"candidates={len(summaries)}"
                )

            candidates = summaries

        except Exception as exc:
            errors.append(
                f"source discovery failed: {exc}"
            )

        finally:
            await browser.close()

    return candidates, errors


# ============================================================================
# Fetch Articles
# ============================================================================

async def fetch_source_articles(
    source_url: str,
    candidates: list[ArticleSummary],
) -> tuple[
    list[SourceArticle],
    list[tuple[str, str]],
]:

    articles = []
    errors = []

    if not candidates:
        return articles, errors

    async with async_playwright() as playwright:
        browser, page = await create_browser_context(
            playwright
        )

        try:
            for candidate in candidates:
                try:
                    article = await fetch_article(
                        page,
                        candidate,
                    )

                    # لا نستبعد هنا مباشرة.
                    # مرحلة أخرى في المشروع يمكنها اتخاذ قرار
                    # الاستبعاد النهائي إذا بقيت الصورة فارغة.
                    articles.append(article)

                except Exception as exc:
                    errors.append(
                        (
                            candidate.url,
                            str(exc),
                        )
                    )

        finally:
            await browser.close()

    return articles, errors


# ============================================================================
# Main Entry Point
# ============================================================================

def discover_and_fetch(
    source_url: str,
    max_articles: int,
) -> tuple[
    list[ArticleSummary],
    list[str],
    list[SourceArticle],
    list[tuple[str, str]],
]:

    candidates, discovery_errors = asyncio.run(
        discover_articles(
            source_url,
            max_articles,
        )
    )

    articles, fetch_errors = asyncio.run(
        fetch_source_articles(
            news_index_url(source_url),
            candidates,
        )
    )

    return (
        candidates,
        discovery_errors,
        articles,
        fetch_errors,
    )
