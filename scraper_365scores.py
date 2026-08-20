"""
scraper_365scores.py
====================
Fetches football news from 365Scores Arabic news pages.
Handles article discovery, URL normalization, metadata extraction,
Arabic date parsing, and robust content collection via Playwright & BeautifulSoup.
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

# IMPORTANT:
# Do NOT include "365scores" here.
# Legitimate article images can be hosted on a 365Scores CDN/domain.
DEFAULT_LOGO_PATTERNS = (
    "logo",
    "icon",
    "avatar",
    "favicon",
    "placeholder",
    "default-image",
    "default_image",
)

ARTICLE_ROOT = "/ar/news/magazine"

# Paths that are definitely not individual articles.
BLOCKED_PATH_PREFIXES = (
    "/login",
    "/signup",
    "/matches",
    "/teams",
    "/players",
    "/category/",
    "/content-tag/",
    "/tag/",
    "/magazine/category/",
)

# Exact standalone sections that can appear below /news/magazine/
BLOCKED_SECTION_SLUGS = {
    "كرة-القدم",
    "كرة-القدم-الإنجليزية",
    "كرة-القدم-الاسبانية",
    "كرة-القدم-الإسبانية",
    "كرة-القدم-الإيطالية",
    "كرة-القدم-الألمانية",
    "كرة-القدم-الفرنسية",
    "الكرة-العربية",
    "كرة-القدم-السعودية",
    "كرة-القدم-الإماراتية",
    "كرة-القدم-المصرية",
    "كرة-القدم-المغربية",
    "أخبار",
    "أخبار-عامة",
    "تقديم-المباريات",
    "انتقالات-وشائعات",
    "مقابلات-حصرية",
    "تقارير-خاصة",
    "خاص-365scores",
    "لايت-365scores",
    "بطولات-ودوريات",
    "الدوري-الإنجليزي",
    "الدوري-الإسباني",
    "الدوري-السعودي",
    "الدوري-المصري-الممتاز",
    "الدوري-المغربي",
    "الدوري-العراقي",
    "الدوري-الإماراتي",
    "دوري-روشن-السعودي",
    "دوري-أبطال-أوروبا",
    "دوري-أبطال-آسيا-للنخبة",
    "دوري-أبطال-إفريقيا",
    "المنتخبات-العربية",
    "مباريات-اليوم",
    "أرشيف-كرة-القدم",
    "رياضات-أخرى",
}

# Sections for clubs / national teams / competitions that commonly have
# their own direct slug.
BLOCKED_SLUG_PREFIXES = (
    "النادي-",
    "نادي-",
    "منتخب-",
    "دوري-",
    "الدوري-",
    "كأس-",
    "بطولات-",
)

# Daily broadcast articles.
BROADCAST_ARTICLE_RE = re.compile(
    r"^القنوات-الناقلة(?:-لمباريات)?(?:-مباراة)?(?:-|$)",
    re.IGNORECASE,
)

# Statistics / archive pages that are not normal news.
STATISTICS_RE = re.compile(
    r"^(?:إحصائيات|احصائيات)(?:-|$)",
    re.IGNORECASE,
)

WORLD_CUP_STATS_RE = re.compile(
    r"(?:إحصائيات|احصائيات).*(?:كأس-العالم|المونديال)",
    re.IGNORECASE,
)

SPECIAL_NON_ARTICLE_PATTERNS = (
    "الأهداف-العكسية-كأس-العالم",
    "بطاقة-حمراء-مجموعات",
)

# Common direct section pages.
# These are exact slugs rather than broad substring matching so that a real
# article such as "الأهلي يعلن التعاقد..." is NOT accidentally rejected.
COMMON_TEAM_SECTION_SLUGS = {
    "الهلال",
    "النصر",
    "الأهلي",
    "الاتحاد",
    "الشباب",
    "القادسية",
    "الفتح",
    "التعاون",
    "الاتفاق",
    "الرائد",
    "الوحدة",
    "الفيحاء",
    "الخلود",
    "ضمك",
    "الرياض",
    "الأخدود",
    "مانشستر-سيتي",
    "مانشستر-يونايتد",
    "ليفربول",
    "أرسنال",
    "تشيلسي",
    "توتنهام",
    "ريال-مدريد",
    "برشلونة",
    "أتلتيكو-مدريد",
    "يوفنتوس",
    "ميلان",
    "إنتر",
    "بايرن-ميونخ",
    "باريس-سان-جيرمان",
}


def normalize_url(base_url: str, value: str) -> str:
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
    Decode percent-encoded Arabic paths several times.
    This handles links such as:
    %d9%83%d8%b1%d8%a9
    """
    decoded = path or ""

    for _ in range(3):
        new_value = unquote(decoded)
        if new_value == decoded:
            break
        decoded = new_value

    return decoded


def _clean_slug(value: str) -> str:
    value = unquote(value or "")
    value = value.strip().strip("/")
    value = re.sub(r"\s+", "-", value)
    return value.lower()


def is_probable_article_url(url: str) -> bool:
    """
    Strict pre-discovery filtering.

    Rejects:
    - team / club sections
    - national-team sections
    - league / category sections
    - daily broadcast pages
    - statistics pages
    - pagination
    - malformed/truncated URLs
    - obvious non-article sections

    Accepts:
    - normal /ar/news/magazine/<slug> article URLs
    - article URLs using ?p=<numeric_id>
    """

    if not url:
        return False

    try:
        parsed = urlparse(url)
    except Exception:
        return False

    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return False

    if parsed.netloc.lower() not in {
        "365scores.com",
        "www.365scores.com",
    }:
        return False

    path = _decode_path(parsed.path).rstrip("/")
    path_lower = path.lower()

    # ------------------------------------------------------------------
    # 1. Query-based WordPress-style article:
    #    /ar/news/magazine/?p=462192
    # ------------------------------------------------------------------
    if path_lower == ARTICLE_ROOT:
        query = parse_qs(parsed.query)

        post_ids = query.get("p", [])
        if post_ids and any(re.fullmatch(r"\d+", x or "") for x in post_ids):
            return True

        # Bare magazine index is NOT an article.
        return False

    # ------------------------------------------------------------------
    # 2. Only pages inside the Arabic news magazine are considered.
    # ------------------------------------------------------------------
    if not path_lower.startswith(ARTICLE_ROOT + "/"):
        return False

    relative_path = path[len(ARTICLE_ROOT):].strip("/")

    if not relative_path:
        return False

    # ------------------------------------------------------------------
    # 3. Reject pagination.
    # ------------------------------------------------------------------
    if re.fullmatch(r"page/\d+", relative_path, re.IGNORECASE):
        return False

    # ------------------------------------------------------------------
    # 4. Reject obvious blocked path structures.
    # ------------------------------------------------------------------
    for prefix in BLOCKED_PATH_PREFIXES:
        if path_lower.startswith(prefix):
            return False

    # ------------------------------------------------------------------
    # 5. Reject multi-level category / taxonomy pages.
    #
    # Normal article URLs are generally a single slug after /magazine/.
    # ------------------------------------------------------------------
    segments = [
        segment.strip()
        for segment in relative_path.split("/")
        if segment.strip()
    ]

    if len(segments) != 1:
        return False

    slug = _clean_slug(segments[0])

    if not slug:
        return False

    # ------------------------------------------------------------------
    # 6. Reject malformed / truncated slugs.
    # ------------------------------------------------------------------
    if slug.endswith("-"):
        return False

    if slug in {"-", "--", "..."}:
        return False

    if re.fullmatch(r"\d+", slug):
        return False

    # Unusual URL encoding left behind after decoding.
    if "%" in slug:
        return False

    # ------------------------------------------------------------------
    # 7. Exact section pages.
    # ------------------------------------------------------------------
    if slug in {_clean_slug(x) for x in BLOCKED_SECTION_SLUGS}:
        return False

    if slug in {_clean_slug(x) for x in COMMON_TEAM_SECTION_SLUGS}:
        return False

    # ------------------------------------------------------------------
    # 8. Club / national-team / league style section slugs.
    #
    # Prefix matching is restricted to the beginning of the WHOLE slug.
    # We do NOT search the complete URL with "in" because an article title
    # can legitimately contain these words.
    # ------------------------------------------------------------------
    for prefix in BLOCKED_SLUG_PREFIXES:
        normalized_prefix = _clean_slug(prefix)
        if slug.startswith(normalized_prefix):
            # Keep genuine longer article headlines such as:
            # "الدوري-السعودي-يترقب-صفقة-..."
            #
            # Short section-like URLs are rejected.
            remainder = slug[len(normalized_prefix):].strip("-")

            if not remainder:
                return False

            # Examples of direct taxonomy/section URLs:
            # /نادي-الزمالك/
            # /منتخب-السعودية/
            # /الدوري-السعودي/
            #
            # A longer multi-part slug is treated as an article.
            if remainder.count("-") <= 1:
                return False

    # ------------------------------------------------------------------
    # 9. Daily "where to watch" / channels pages.
    # ------------------------------------------------------------------
    if BROADCAST_ARTICLE_RE.match(slug):
        return False

    # ------------------------------------------------------------------
    # 10. Statistics pages.
    # ------------------------------------------------------------------
    if STATISTICS_RE.match(slug):
        return False

    if WORLD_CUP_STATS_RE.search(slug):
        return False

    # ------------------------------------------------------------------
    # 11. Known special non-news pages.
    # ------------------------------------------------------------------
    if any(_clean_slug(pattern) in slug for pattern in SPECIAL_NON_ARTICLE_PATTERNS):
        return False

    # ------------------------------------------------------------------
    # 12. Very short/truncated slugs.
    # ------------------------------------------------------------------
    # Allow normal short titles, but reject obvious fragments.
    if len(slug.replace("-", "")) < 5:
        return False

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

            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except ValueError:
            pass

    return None


def parse_any_time(text: str, now: datetime) -> datetime | None:
    return parse_absolute_time(text) or parse_relative_time(text, now)


# ============================================================================
# JSON-LD
# ============================================================================

def extract_json_ld(soup: BeautifulSoup) -> list[dict]:
    """
    Extract all JSON-LD objects, including objects nested inside @graph.
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

    for script in soup.select('script[type="application/ld+json"]'):
        raw = script.string or script.get_text()

        if not raw:
            continue

        raw = raw.strip()

        try:
            data = json.loads(raw)
            collect(data)
        except Exception:
            continue

    return items


def first_meta(soup: BeautifulSoup, *names: str) -> str:
    for name in names:
        tag = (
            soup.find("meta", attrs={"property": name})
            or soup.find("meta", attrs={"name": name})
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
# Image Extraction Helpers
# ============================================================================

def _clean_image_candidate(value: str | None) -> str:
    if not value:
        return ""

    value = html.unescape(str(value)).strip()

    if value.startswith(("data:", "blob:", "javascript:")):
        return ""

    # CSS-style url(...)
    css_match = re.fullmatch(
        r"url\(\s*['\"]?(.*?)['\"]?\s*\)",
        value,
        re.IGNORECASE,
    )

    if css_match:
        value = css_match.group(1).strip()

    # Remove surrounding quotes.
    value = value.strip(" '\"")

    return value


def _pick_from_srcset(srcset: str | None) -> str:
    """
    Pick the largest candidate from a srcset.
    Supports:
      image.jpg 640w, image.jpg 1280w
      image.webp 1x, image.webp 2x
    """

    if not srcset:
        return ""

    candidates = []

    for raw_candidate in srcset.split(","):
        candidate = raw_candidate.strip()

        if not candidate:
            continue

        parts = candidate.split()

        if not parts:
            continue

        url = _clean_image_candidate(parts[0])

        if not url:
            continue

        score = 0.0

        if len(parts) > 1:
            descriptor = parts[1].lower()

            width_match = re.fullmatch(r"(\d+(?:\.\d+)?)w", descriptor)
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

    candidates.sort(key=lambda item: item[0], reverse=True)

    return candidates[0][1]


def _is_valid_article_image(url_str: str) -> bool:
    """
    Validate an image candidate without rejecting valid 365Scores CDN URLs.
    """

    value = _clean_image_candidate(url_str)

    if not value:
        return False

    lower_url = value.lower()

    # Never reject merely because "365scores" exists in the URL.
    if lower_url.startswith(("data:", "blob:", "javascript:")):
        return False

    parsed = urlparse(value)
    path = parsed.path.lower()

    # SVGs are generally icons/logos, not featured article photos.
    if path.endswith(".svg"):
        return False

    # Obvious logo/avatar/placeholder patterns.
    if any(pattern in lower_url for pattern in DEFAULT_LOGO_PATTERNS):
        return False

    # Tiny tracking / spacer GIFs.
    if path.endswith(
        (
            ".gif",
            ".ico",
        )
    ):
        if any(
            marker in lower_url
            for marker in (
                "pixel",
                "tracking",
                "spacer",
                "transparent",
            )
        ):
            return False

    # Need at least a plausible URL-like value.
    if not parsed.scheme and not value.startswith(("/", "./", "../")):
        return False

    return True


def _extract_image_from_json_value(value) -> list[str]:
    """
    Recursively collect image URLs from JSON-LD values.
    """

    results: list[str] = []

    if isinstance(value, str):
        if _is_valid_article_image(value):
            results.append(value.strip())

    elif isinstance(value, list):
        for item in value:
            results.extend(_extract_image_from_json_value(item))

    elif isinstance(value, dict):
        for key in (
            "url",
            "contentUrl",
            "contentURL",
            "thumbnailUrl",
            "thumbnailURL",
        ):
            candidate = value.get(key)

            if isinstance(candidate, str):
                if _is_valid_article_image(candidate):
                    results.append(candidate.strip())

        # Handle nested image objects.
        for key in (
            "image",
            "content",
            "thumbnail",
            "logo",
        ):
            nested = value.get(key)

            if key != "logo":
                results.extend(_extract_image_from_json_value(nested))

    return results


def _extract_dom_image_candidates(container) -> list[str]:
    """
    Collect image URLs from img/source/picture/noscript/background images.
    """

    candidates: list[str] = []

    image_attributes = (
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
        "data-src-webp",
        "data-src-webp",
    )

    # ------------------------------------------------------------------
    # img + source
    # ------------------------------------------------------------------
    for element in container.select(
        "picture img, picture source, img, source"
    ):
        for attr in srcset_attributes:
            value = element.get(attr)

            if value:
                candidate = _pick_from_srcset(value)

                if candidate and _is_valid_article_image(candidate):
                    candidates.append(candidate)

        for attr in image_attributes:
            value = element.get(attr)

            if value:
                candidate = _clean_image_candidate(value)

                if candidate and _is_valid_article_image(candidate):
                    candidates.append(candidate)

    # ------------------------------------------------------------------
    # Lazy-loaded images represented in noscript.
    # ------------------------------------------------------------------
    for noscript in container.find_all("noscript"):
        raw = noscript.get_text(" ", strip=True)

        if not raw:
            continue

        try:
            nested = BeautifulSoup(raw, "html.parser")

            for element in nested.select("img, source"):
                for attr in srcset_attributes:
                    value = element.get(attr)

                    if value:
                        candidate = _pick_from_srcset(value)

                        if candidate and _is_valid_article_image(candidate):
                            candidates.append(candidate)

                for attr in image_attributes:
                    value = element.get(attr)

                    if value:
                        candidate = _clean_image_candidate(value)

                        if candidate and _is_valid_article_image(candidate):
                            candidates.append(candidate)

        except Exception:
            continue

    # ------------------------------------------------------------------
    # CSS background-image
    # ------------------------------------------------------------------
    for tag in container.find_all(True, style=True):
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

            if candidate and _is_valid_article_image(candidate):
                candidates.append(candidate)

    return candidates


def extract_featured_image(soup: BeautifulSoup) -> str:
    """
    Robust featured-image extraction.

    Priority:
      1. JSON-LD / Schema.org
      2. OpenGraph / Twitter meta
      3. article/main DOM
      4. picture/source
      5. lazy-loading attributes
      6. noscript
      7. background-image
    """

    # ------------------------------------------------------------------
    # 1. JSON-LD
    # ------------------------------------------------------------------
    for item in extract_json_ld(soup):
        # Standard "image"
        for candidate in _extract_image_from_json_value(item.get("image")):
            if _is_valid_article_image(candidate):
                return candidate

        # Schema.org primaryImageOfPage
        for candidate in _extract_image_from_json_value(
            item.get("primaryImageOfPage")
        ):
            if _is_valid_article_image(candidate):
                return candidate

        # Thumbnail
        for candidate in _extract_image_from_json_value(
            item.get("thumbnailUrl")
        ):
            if _is_valid_article_image(candidate):
                return candidate

    # ------------------------------------------------------------------
    # 2. Meta tags
    # ------------------------------------------------------------------
    meta_image = first_meta(
        soup,
        "og:image",
        "og:image:url",
        "og:image:secure_url",
        "twitter:image",
        "twitter:image:src",
    )

    if meta_image and _is_valid_article_image(meta_image):
        return meta_image

    # ------------------------------------------------------------------
    # 3. <link rel="image_src">
    # ------------------------------------------------------------------
    link_image = soup.find(
        "link",
        attrs={"rel": lambda value: value and "image_src" in value},
    )

    if link_image:
        href = link_image.get("href", "")

        if href and _is_valid_article_image(href):
            return href.strip()

    # ------------------------------------------------------------------
    # 4. Locate article containers.
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

    containers = []

    for selector in selectors:
        for container in soup.select(selector):
            containers.append(container)

    # Remove duplicates while preserving order.
    unique_containers = []
    seen_ids = set()

    for container in containers:
        object_id = id(container)

        if object_id not in seen_ids:
            seen_ids.add(object_id)
            unique_containers.append(container)

    # ------------------------------------------------------------------
    # 5. DOM extraction.
    # ------------------------------------------------------------------
    for container in unique_containers:
        candidates = _extract_dom_image_candidates(container)

        for candidate in candidates:
            if _is_valid_article_image(candidate):
                return candidate.strip()

    return ""


# ============================================================================
# Playwright Image Fallback
# ============================================================================

async def extract_featured_image_with_playwright(page: Page) -> str:
    """
    Fast browser-side fallback.

    It checks the first usable image inside the article/main area and reads:
      - currentSrc
      - src
      - data-src
      - data-lazy-src
      - data-original
      - srcset
      - data-srcset
      - picture/source
      - background-image
    """

    try:
        value = await page.evaluate(
            """() => {
                const badPatterns =
                    /logo|icon|avatar|favicon|placeholder|default-image|default_image/i;

                const isValid = (value) => {
                    if (!value || typeof value !== "string") return false;

                    const url = value.trim();

                    if (!url) return false;
                    if (/^(data:|blob:|javascript:)/i.test(url)) return false;
                    if (/\\.svg(?:$|\\?)/i.test(url)) return false;
                    if (badPatterns.test(url)) return false;

                    return true;
                };

                const attrs = [
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
                    "data-original-src"
                ];

                const srcsetAttrs = [
                    "srcset",
                    "data-srcset",
                    "data-lazy-srcset",
                    "data-original-srcset"
                ];

                const getSrcsetCandidate = (value) => {
                    if (!value) return "";

                    const items = value
                        .split(",")
                        .map(x => x.trim())
                        .filter(Boolean);

                    if (!items.length) return "";

                    let best = "";
                    let bestScore = -1;

                    for (const item of items) {
                        const parts = item.split(/\\s+/);
                        const url = parts[0];

                        if (!isValid(url)) continue;

                        let score = 0;

                        if (parts[1]) {
                            const descriptor = parts[1];

                            const width = descriptor.match(/(\\d+(?:\\.\\d+)?)w/);
                            const density = descriptor.match(/(\\d+(?:\\.\\d+)?)x/);

                            if (width) {
                                score = Number(width[1]);
                            } else if (density) {
                                score = Number(density[1]) * 1000;
                            }
                        }

                        if (score > bestScore) {
                            bestScore = score;
                            best = url;
                        }
                    }

                    return best;
                };

                const roots = [
                    ...document.querySelectorAll(
                        "article, main, [data-testid*='article'], " +
                        "[data-test*='article'], [class*='article'], " +
                        "[class*='Article'], [class*='news'], [class*='News']"
                    )
                ];

                const seen = new Set();

                for (const root of roots) {
                    const elements = root.querySelectorAll(
                        "picture source, picture img, img, [style*='background-image']"
                    );

                    for (const element of elements) {
                        const candidates = [];

                        // Browser's resolved lazy-loaded image.
                        if (element.currentSrc) {
                            candidates.push(element.currentSrc);
                        }

                        // Normal attributes.
                        for (const attr of attrs) {
                            const value = element.getAttribute(attr);
                            if (value) candidates.push(value);
                        }

                        // srcset / lazy srcset.
                        for (const attr of srcsetAttrs) {
                            const value = element.getAttribute(attr);

                            if (value) {
                                const selected = getSrcsetCandidate(value);

                                if (selected) {
                                    candidates.push(selected);
                                }
                            }
                        }

                        // CSS background-image.
                        const style = element.getAttribute("style") || "";

                        if (/background-image/i.test(style)) {
                            const match = style.match(
                                /url\\(\\s*['"]?(.*?)['"]?\\s*\\)/i
                            );

                            if (match && match[1]) {
                                candidates.push(match[1]);
                            }
                        }

                        for (const candidate of candidates) {
                            if (!candidate) continue;

                            const clean = String(candidate).trim();

                            if (!isValid(clean)) continue;

                            if (seen.has(clean)) continue;
                            seen.add(clean);

                            // Avoid tiny images/icons when dimensions are known.
                            if (element.tagName === "IMG") {
                                const width =
                                    element.naturalWidth ||
                                    element.width ||
                                    0;

                                const height =
                                    element.naturalHeight ||
                                    element.height ||
                                    0;

                                if (
                                    (width > 0 && width < 180) ||
                                    (height > 0 && height < 120)
                                ) {
                                    continue;
                                }
                            }

                            return clean;
                        }
                    }
                }

                return "";
            }"""
        )

        value = _clean_image_candidate(value)

        if _is_valid_article_image(value):
            return value

    except Exception:
        pass

    return ""


# ============================================================================
# Published Time
# ============================================================================

def extract_published_at(soup: BeautifulSoup) -> datetime | None:
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
                    else dt.replace(tzinfo=timezone.utc)
                )

            except ValueError:
                if parsed := parse_any_time(val, now):
                    return parsed

    for item in extract_json_ld(soup):
        for key in ("datePublished", "dateCreated"):
            if val := item.get(key):
                if isinstance(val, str):
                    try:
                        dt = datetime.fromisoformat(
                            val.replace("Z", "+00:00")
                        )

                        return (
                            dt
                            if dt.tzinfo
                            else dt.replace(tzinfo=timezone.utc)
                        )

                    except ValueError:
                        if parsed := parse_any_time(val, now):
                            return parsed

    for time_tag in soup.select("time[datetime]"):
        if parsed := parse_any_time(
            time_tag.get("datetime", "").strip(),
            now,
        ):
            return parsed

    return parse_any_time(
        soup.get_text(" ", strip=True),
        now,
    )


# ============================================================================
# Article Text
# ============================================================================

def extract_article_text(soup: BeautifulSoup) -> str:
    for item in extract_json_ld(soup):
        body = item.get("articleBody") or item.get("description")

        if isinstance(body, str) and len(body.strip()) > 80:
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
        text = p.get_text(" ", strip=True)

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
            if text := node.get_text("\n", strip=True):
                candidates.append(text)

    if not candidates:
        candidates.append(
            soup.get_text("\n", strip=True)
        )

    text = max(
        candidates,
        key=len,
        default="",
    )

    lines = [
        re.sub(r"\s+", " ", line).strip()
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

            # Strict filtering occurs BEFORE adding candidate.
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

            if not title_text or len(title_text) < 12:
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
                or len(candidate.title) > len(current.title)
            ):
                results[url] = candidate

    await collect_current_dom()

    for _ in range(10):
        try:
            await page.mouse.wheel(0, 5000)

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

    await page.wait_for_timeout(2_000)

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

    # ---------------------------------------------------------------
    # Primary image extraction: BeautifulSoup.
    # ---------------------------------------------------------------
    image_url = extract_featured_image(soup)

    # ---------------------------------------------------------------
    # Fallback: browser-side extraction.
    #
    # This is intentionally executed BEFORE the article is considered
    # to have no image, so lazy-loaded images have one more opportunity.
    # ---------------------------------------------------------------
    if not image_url:
        image_url = await extract_featured_image_with_playwright(
            page
        )

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
) -> tuple[list[ArticleSummary], list[str]]:

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

            # Handle common consent / close buttons.
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
                    f"No article candidates discovered from {index_url}"
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
