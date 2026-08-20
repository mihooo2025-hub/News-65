"""
scraper_365scores.py
====================
Fetches football news from 365Scores Arabic news pages.
Handles article discovery, URL normalization, metadata extraction,
Arabic date parsing, robust content collection, and featured image
extraction via BeautifulSoup + Playwright fallback.
Optimized for high-concurrency execution on CI/CD platforms (GitHub Actions).
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
    decoded = path or ""

    for _ in range(3):
        new_value = unquote(decoded)

        if new_value == decoded:
            break

        decoded = new_value

    return decoded


def is_probable_article_url(url: str) -> bool:
    if not url:
        return False

    try:
        parsed = urlparse(url)
    except Exception:
        return False

    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return False

    path = _decode_path(parsed.path).lower().rstrip("/")

    if not path.startswith(ARTICLE_ROOT):
        return False

    if path == ARTICLE_ROOT:
        query = parse_qs(parsed.query)
        post_ids = query.get("p", [])

        return any(
            re.fullmatch(r"\d+", post_id or "")
            for post_id in post_ids
        )

    if not path.startswith(ARTICLE_ROOT + "/"):
        return False

    relative_path = path[len(ARTICLE_ROOT):].strip("/")

    if not relative_path:
        return False

    if re.fullmatch(r"page/\d+", relative_path):
        return False

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

    slug = relative_path.split("/")[0].strip()

    if not slug:
        return False

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

    if re.match(
        r"^(?:إحصائيات|احصائيات)-كأس-العالم(?:-|$)",
        slug,
    ):
        return False

    exact_non_articles = {
        "الأهداف-العكسية-كأس-العالم",
        "بطاقة-حمراء-مجموعات",
    }

    if slug in exact_non_articles:
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

    match = re.fullmatch(
        r"url\(\s*['\"]?(.*?)['\"]?\s*\)",
        value,
        re.IGNORECASE,
    )

    if match:
        value = match.group(1).strip()

    return value.strip(" '\"")


def _is_valid_article_image(url_str: str) -> bool:
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

    if path.endswith(".svg"):
        return False

    if any(
        pattern in lower_url
        for pattern in DEFAULT_LOGO_PATTERNS
    ):
        return False

    return True


def _pick_from_srcset(srcset: str | None) -> str:
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

    try:
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
                    setTimeout(resolve, 500)
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

                        if (element.currentSrc) {
                            candidates.push(
                                element.currentSrc
                            );
                        }

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
# Article Text Helpers
# ============================================================================

def _normalize_article_line(text: str) -> str:
    if not text:
        return ""

    text = html.unescape(text)

    text = re.sub(
        r"\s+",
        " ",
        text,
    ).strip()

    return text


def _is_noise_article_text(text: str) -> bool:
    if not text:
        return True

    normalized = re.sub(
        r"\s+",
        " ",
        text,
    ).strip()

    if len(normalized) < 3:
        return True

    noise_exact = {
        "مشاركة",
        "شارك",
        "التعليقات",
        "اقرأ أيضًا",
        "اقرأ ايضا",
        "المزيد",
        "إعلان",
        "اعلان",
        "التالي",
        "السابق",
        "الرئيسية",
        "الرئيسيه",
    }

    if normalized in noise_exact:
        return True

    return False


def _collect_article_paragraphs(
    container,
) -> list[str]:

    paragraphs: list[str] = []
    seen: set[str] = set()

    nodes = container.select(
        "p, "
        "li, "
        "[data-testid*='paragraph'], "
        "[data-test*='paragraph'], "
        "[class*='paragraph'], "
        "[class*='Paragraph']"
    )

    for node in nodes:
        text = _normalize_article_line(
            node.get_text(
                " ",
                strip=True,
            )
        )

        if _is_noise_article_text(text):
            continue

        key = re.sub(
            r"\s+",
            " ",
            text,
        ).strip()

        if key in seen:
            continue

        seen.add(key)
        paragraphs.append(text)

    return paragraphs


def _clean_article_container(
    container,
) -> None:

    for selector in (
        "script",
        "style",
        "noscript",
        "svg",
        "nav",
        "header",
        "footer",
        "aside",
        "form",
        "button",
        "[role='navigation']",
        "[role='button']",
        "[aria-label*='share']",
        "[class*='share']",
        "[class*='Share']",
        "[class*='comment']",
        "[class*='Comment']",
        "[class*='related']",
        "[class*='Related']",
        "[class*='recommend']",
        "[class*='Recommend']",
        "[class*='advert']",
        "[class*='Advert']",
        "[class*='banner']",
        "[class*='Banner']",
    ):
        for node in container.select(selector):
            node.decompose()


def extract_article_text(
    soup: BeautifulSoup,
) -> str:

    json_ld_bodies: list[str] = []

    for item in extract_json_ld(soup):
        body = item.get("articleBody")

        if (
            isinstance(body, str)
            and len(body.strip()) >= 120
        ):
            cleaned = _normalize_article_line(body)

            if cleaned:
                json_ld_bodies.append(cleaned)

    if json_ld_bodies:
        best_body = max(
            json_ld_bodies,
            key=len,
        )

        if len(best_body) >= 200:
            return best_body

    selectors = (
        "article",
        "[data-testid*='article']",
        "[data-test*='article']",
        "[class*='article-body']",
        "[class*='ArticleBody']",
        "[class*='article-content']",
        "[class*='ArticleContent']",
        "[class*='article']",
        "[class*='Article']",
        "main",
        "[class*='news-content']",
        "[class*='NewsContent']",
        "[class*='content']",
        "[class*='Content']",
    )

    containers = []
    seen_containers = set()

    for selector in selectors:
        for node in soup.select(selector):
            node_id = id(node)

            if node_id in seen_containers:
                continue

            seen_containers.add(node_id)
            containers.append(node)

    best_paragraphs: list[str] = []

    for container in containers:
        container_copy = BeautifulSoup(
            str(container),
            "html.parser",
        )

        _clean_article_container(
            container_copy
        )

        paragraphs = _collect_article_paragraphs(
            container_copy
        )

        combined_length = sum(
            len(item)
            for item in paragraphs
        )

        best_length = sum(
            len(item)
            for item in best_paragraphs
        )

        if combined_length > best_length:
            best_paragraphs = paragraphs

    if best_paragraphs:
        full_text = "\n".join(
            best_paragraphs
        ).strip()

        if len(full_text) >= 80:
            return full_text

    paragraphs = []

    for p in soup.select("p"):
        text = _normalize_article_line(
            p.get_text(
                " ",
                strip=True,
            )
        )

        if _is_noise_article_text(text):
            continue

        if len(text) < 15:
            continue

        if text not in paragraphs:
            paragraphs.append(text)

    if paragraphs:
        return "\n".join(paragraphs).strip()

    text = soup.get_text(
        "\n",
        strip=True,
    )

    lines = []

    for line in text.splitlines():
        normalized = _normalize_article_line(line)

        if not _is_noise_article_text(normalized):
            lines.append(normalized)

    return "\n".join(lines).strip()


# ============================================================================
# Playwright Article Text Fallback
# ============================================================================

async def extract_article_text_with_playwright(
    page: Page,
) -> str:

    try:
        await page.evaluate(
            """async () => {
                const article = document.querySelector(
                    [
                        'article',
                        '[data-testid*="article"]',
                        '[data-test*="article"]',
                        '[class*="article"]',
                        '[class*="Article"]',
                        'main'
                    ].join(',')
                );

                if (article) {
                    try {
                        article.scrollIntoView({
                            block: 'center',
                            behavior: 'instant'
                        });
                    } catch (_) {}
                }

                await new Promise(resolve =>
                    setTimeout(resolve, 500)
                );
            }"""
        )

        result = await page.evaluate(
            """() => {
                const selectors = [
                    'article',
                    '[data-testid*="article"]',
                    '[data-test*="article"]',
                    '[class*="article-body"]',
                    '[class*="ArticleBody"]',
                    '[class*="article-content"]',
                    '[class*="ArticleContent"]',
                    '[class*="article"]',
                    '[class*="Article"]',
                    'main',
                    '[class*="news-content"]',
                    '[class*="NewsContent"]',
                    '[class*="content"]',
                    '[class*="Content"]'
                ];

                const noiseExact = new Set([
                    'مشاركة',
                    'شارك',
                    'التعليقات',
                    'اقرأ أيضًا',
                    'اقرأ ايضا',
                    'المزيد',
                    'إعلان',
                    'اعلان',
                    'التالي',
                    'السابق',
                    'الرئيسية',
                    'الرئيسيه'
                ]);

                const normalize = (value) => {
                    return String(value || '')
                        .replace(/\\s+/g, ' ')
                        .trim();
                };

                const isNoise = (value) => {
                    const text = normalize(value);

                    if (!text || text.length < 3) {
                        return true;
                    }

                    if (noiseExact.has(text)) {
                        return true;
                    }

                    return false;
                };

                const containers = [];

                for (const selector of selectors) {
                    document
                        .querySelectorAll(selector)
                        .forEach(node => {
                            if (!containers.includes(node)) {
                                containers.push(node);
                            }
                        });
                }

                let best = [];

                for (const container of containers) {
                    const nodes = container.querySelectorAll(
                        [
                            'p',
                            'li',
                            '[data-testid*="paragraph"]',
                            '[data-test*="paragraph"]',
                            '[class*="paragraph"]',
                            '[class*="Paragraph"]'
                        ].join(',')
                    );

                    const paragraphs = [];
                    const seen = new Set();

                    for (const node of nodes) {
                        let text = normalize(
                            node.innerText ||
                            node.textContent ||
                            ''
                        );

                        if (isNoise(text)) {
                            continue;
                        }

                        if (text.length < 15) {
                            continue;
                        }

                        if (seen.has(text)) {
                            continue;
                        }

                        seen.add(text);
                        paragraphs.append(text);
                    }

                    const currentLength =
                        paragraphs.reduce(
                            (sum, value) =>
                                sum + value.length,
                            0
                        );

                    const bestLength =
                        best.reduce(
                            (sum, value) =>
                                sum + value.length,
                            0
                        );

                    if (
                        currentLength > bestLength
                        && paragraphs.length > 0
                    ) {
                        best = paragraphs;
                    }
                }

                if (best.length > 0) {
                    return best.join('\\n');
                }

                return '';
            }"""
        )

        if isinstance(result, str):
            result = result.strip()

            if len(result) >= 80:
                return result

    except Exception:
        pass

    return ""


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

    # تقليل التكرار للتصفح السريع بدلاً من 10 مرات
    for _ in range(3):
        try:
            await page.mouse.wheel(0, 3000)
            await page.wait_for_timeout(500)
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
        timeout=20_000,
    )

    await page.wait_for_timeout(800)

    try:
        await page.wait_for_load_state(
            "networkidle",
            timeout=3_000,
        )
    except Exception:
        pass

    try:
        await page.evaluate(
            """async () => {
                const article = document.querySelector(
                    [
                        'article',
                        '[data-testid*="article"]',
                        '[data-test*="article"]',
                        '[class*="article"]',
                        '[class*="Article"]',
                        'main'
                    ].join(',')
                );

                if (article) {
                    try {
                        article.scrollIntoView({
                            block: 'center',
                            behavior: 'instant'
                        });
                    } catch (_) {}
                }

                await new Promise(resolve =>
                    setTimeout(resolve, 500)
                );
            }"""
        )
    except Exception:
        pass

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
    # IMAGE
    # ==================================================================

    image_url = extract_featured_image(soup)

    if not image_url:
        image_url = await extract_featured_image_with_playwright(
            page
        )

    image_url = normalize_url(
        summary.url,
        image_url,
    )

    # ==================================================================
    # ARTICLE TEXT
    # ==================================================================

    article_text = extract_article_text(soup)

    if len(article_text) < 200:
        playwright_text = await extract_article_text_with_playwright(
            page
        )

        if len(playwright_text) > len(article_text):
            article_text = playwright_text

    return SourceArticle(
        url=summary.url,
        title=title.strip(),
        text=article_text.strip(),
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
                timeout=25_000,
            )

            await page.wait_for_timeout(1_000)

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
                            timeout=2_000
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
# Fetch Articles (Concurrent Execution)
# ============================================================================

async def fetch_source_articles(
    source_url: str,
    candidates: list[ArticleSummary],
    max_concurrency: int = 3,
) -> tuple[
    list[SourceArticle],
    list[tuple[str, str]],
]:

    articles = []
    errors = []

    if not candidates:
        return articles, errors

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        semaphore = asyncio.Semaphore(max_concurrency)

        async def worker(candidate: ArticleSummary):
            async with semaphore:
                page = await browser.new_page(
                    locale="ar-SA",
                    user_agent=USER_AGENT,
                    timezone_id="Asia/Riyadh",
                )
                try:
                    article = await fetch_article(page, candidate)
                    articles.append(article)
                except Exception as exc:
                    errors.append((candidate.url, str(exc)))
                finally:
                    await page.close()

        try:
            await asyncio.gather(*(worker(c) for c in candidates))
        finally:
            await browser.close()

    return articles, errors


# ============================================================================
# Main Entry Point (Optimized Async Execution)
# ============================================================================

async def discover_and_fetch_async(
    source_url: str,
    max_articles: int,
) -> tuple[
    list[ArticleSummary],
    list[str],
    list[SourceArticle],
    list[tuple[str, str]],
]:

    candidates, discovery_errors = await discover_articles(
        source_url,
        max_articles,
    )

    articles, fetch_errors = await fetch_source_articles(
        news_index_url(source_url),
        candidates,
    )

    return (
        candidates,
        discovery_errors,
        articles,
        fetch_errors,
    )


def discover_and_fetch(
    source_url: str,
    max_articles: int,
) -> tuple[
    list[ArticleSummary],
    list[str],
    list[SourceArticle],
    list[tuple[str, str]],
]:

    return asyncio.run(
        discover_and_fetch_async(
            source_url,
            max_articles,
        )
    )
