"""
scraper_365scores.py
====================
Robust scraper for 365Scores Magazine.

- Discovers article URLs from 365Scores.
- Uses Playwright for dynamic rendering.
- Extracts article body from:
    1) JSON-LD articleBody
    2) Next.js __NEXT_DATA__
    3) Semantic article/main containers
    4) Multiple paragraph/content selectors
    5) Direct Playwright innerText fallback
- Waits for actual article text instead of only waiting for <p>.
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
# Constants
# ============================================================================

ARABIC_DIGITS = str.maketrans(
    "٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹",
    "01234567890123456789",
)

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/128.0.0.0 Safari/537.36"
)

DEFAULT_LOGO_PATTERNS = (
    "site-logo",
    "favicon",
    "default-avatar",
)

ARTICLE_ROOT = "/ar/news/magazine"

DEFAULT_FALLBACK_IMAGE_URL = (
    "https://nabdalmalaeb.com/wp-content/uploads/default-news.jpg"
)

MIN_ARTICLE_TEXT_LENGTH = 80
MAX_WAIT_FOR_TEXT_MS = 12_000


# ============================================================================
# URL Helpers
# ============================================================================

def normalize_url(base_url: str, value: str) -> str:
    if not value:
        return ""

    return (
        urljoin(base_url, html.unescape(str(value)).strip())
        .split("#", 1)[0]
    )


def news_index_url(source_url: str) -> str:
    parsed = urlparse(source_url)

    return (
        f"{parsed.scheme}://{parsed.netloc}/ar/news/magazine/"
        if parsed.path.rstrip("/") in {"", "/ar"}
        else source_url
    )


def is_probable_article_url(url: str) -> bool:
    if not url:
        return False

    try:
        parsed = urlparse(url)

        path = unquote(parsed.path).lower().rstrip("/")

        if (
            not path.startswith(ARTICLE_ROOT)
            or parsed.scheme not in {"http", "https"}
        ):
            return False

        if path == ARTICLE_ROOT:
            return any(
                re.fullmatch(r"\d+", pid or "")
                for pid in parse_qs(parsed.query).get("p", [])
            )

        rel_path = path[len(ARTICLE_ROOT):].strip("/")

        if not rel_path:
            return False

        if re.search(
            r"^(category|tag|author|search|page/\d+)(/|$)",
            rel_path,
        ):
            return False

        slug = rel_path.split("/")[0]

        ignored_keywords = (
            "القنوات-الناقلة",
            "إحصائيات",
            "احصائيات",
            "نادي-",
            "منتخب-",
            "فريق-",
            "الأهداف-العكسية",
            "بطاقة-حمراء",
            "بطولات-",
            "أكثر-منتخب",
            "أقل-منتخب",
            "أكثر-حارس",
        )

        if any(kw in slug for kw in ignored_keywords):
            return False

        return True

    except Exception:
        return False


# ============================================================================
# Time Parsing
# ============================================================================

def parse_relative_time(
    text: str,
    now: datetime,
) -> datetime | None:

    val = re.sub(
        r"\s+",
        " ",
        text.strip().translate(ARABIC_DIGITS),
    )

    if "الآن" in val or "قبل 0 دقيقة" in val:
        return now

    units = [
        ("دقيقة|دقائق", "minutes"),
        ("ساعة|ساعات", "hours"),
        ("يوم|أيام", "days"),
    ]

    for pattern, unit in units:

        match = re.search(
            rf"(?:قبل|منذ)\s+(\d+)\s*(?:{pattern})",
            val,
        )

        if match:
            return now - timedelta(
                **{unit: int(match.group(1))}
            )

    return None


def parse_absolute_time(text: str) -> datetime | None:

    val = re.sub(
        r"\s+",
        " ",
        text.strip().translate(ARABIC_DIGITS),
    )

    match = re.search(
        r"(\d{1,2})/(\d{1,2})/(\d{4})"
        r"\s*(?:-|–|—)?\s*"
        r"(\d{1,2}):(\d{2})\s*([صم])",
        val,
    )

    if match:
        d, mth, year, hour, minute, meridiem = match.groups()

        hour = int(hour)

        if meridiem == "م" and hour < 12:
            hour += 12

        elif meridiem == "ص" and hour == 12:
            hour = 0

        try:
            dt = datetime(
                int(year),
                int(mth),
                int(d),
                hour,
                int(minute),
                tzinfo=timezone(timedelta(hours=3)),
            )

            return dt.astimezone(timezone.utc)

        except ValueError:
            pass

    match = re.search(
        r"\b(20\d{2}-\d{2}-\d{2}T[^\s<]+)",
        val,
    )

    if match:

        try:
            dt = datetime.fromisoformat(
                match.group(1).replace(
                    "Z",
                    "+00:00",
                )
            )

            return (
                dt
                if dt.tzinfo
                else dt.replace(tzinfo=timezone.utc)
            )

        except ValueError:
            pass

    return None


def parse_any_time(
    text: str,
    now: datetime,
) -> datetime | None:

    return (
        parse_absolute_time(text)
        or parse_relative_time(text, now)
    )


# ============================================================================
# Metadata & JSON
# ============================================================================

def extract_json_ld(
    soup: BeautifulSoup,
) -> list[dict]:

    items: list[dict] = []

    def collect(data):

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
            collect(json.loads(raw.strip()))
        except Exception:
            continue

    return items


def first_meta(
    soup: BeautifulSoup,
    *names: str,
) -> str:

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

        if tag and tag.get("content"):
            return tag["content"].strip()

    return ""


# ============================================================================
# Image Extraction
# ============================================================================

def _is_valid_image(url: str) -> bool:

    if not url:
        return False

    lower = url.lower().strip()

    if lower.startswith(
        (
            "data:",
            "blob:",
            "javascript:",
        )
    ):
        return False

    if any(
        pattern in lower
        for pattern in DEFAULT_LOGO_PATTERNS
    ):
        return False

    if lower.endswith(".svg"):
        return False

    return True


def extract_featured_image(
    soup: BeautifulSoup,
) -> str:

    meta = first_meta(
        soup,
        "og:image",
        "twitter:image",
        "og:image:secure_url",
        "image",
    )

    if _is_valid_image(meta):

        if meta.startswith("//"):
            meta = "https:" + meta

        return meta

    for item in extract_json_ld(soup):

        for key in (
            "image",
            "primaryImageOfPage",
            "thumbnailUrl",
        ):

            value = item.get(key)

            if isinstance(value, list):
                value = (
                    value[0]
                    if value
                    else ""
                )

            if isinstance(value, dict):
                value = value.get("url", "")

            if isinstance(value, str) and _is_valid_image(value):

                value = value.strip()

                if value.startswith("//"):
                    value = "https:" + value

                return value

    for img in soup.select(
        "article img, main img, figure img, img"
    ):

        src = (
            img.get("src")
            or img.get("data-src")
            or img.get("data-lazy-src")
            or img.get("data-original")
        )

        if not src and img.get("srcset"):

            candidates = [
                part.strip().split()[0]
                for part in img.get("srcset", "").split(",")
                if part.strip()
            ]

            if candidates:
                src = candidates[-1]

        if src and _is_valid_image(src):

            src = src.strip()

            if src.startswith("//"):
                src = "https:" + src

            return src

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
        "publish_date",
    ):

        value = first_meta(soup, key)

        if value:

            dt = parse_any_time(
                value,
                now,
            )

            if dt:
                return dt

    for time_tag in soup.select(
        "time[datetime]"
    ):

        dt = parse_any_time(
            time_tag.get("datetime", ""),
            now,
        )

        if dt:
            return dt

    for item in extract_json_ld(soup):

        value = (
            item.get("datePublished")
            or item.get("dateCreated")
        )

        if isinstance(value, str):

            dt = parse_any_time(
                value,
                now,
            )

            if dt:
                return dt

    return None


# ============================================================================
# Text Cleaning
# ============================================================================

def clean_text(text: str) -> str:

    if not text:
        return ""

    text = html.unescape(text)

    text = text.replace(
        "\u200f",
        "",
    ).replace(
        "\u200e",
        "",
    )

    text = re.sub(
        r"[ \t]+",
        " ",
        text,
    )

    text = re.sub(
        r"\n{3,}",
        "\n\n",
        text,
    )

    return text.strip()


def normalize_paragraph(
    text: str,
) -> str:

    return clean_text(
        re.sub(
            r"\s+",
            " ",
            text,
        )
    )


# ============================================================================
# Next.js Data Extraction
# ============================================================================

def extract_next_data(
    soup: BeautifulSoup,
) -> dict:

    script = soup.find(
        "script",
        id="__NEXT_DATA__",
    )

    if not script:
        return {}

    raw = script.string or script.get_text()

    if not raw:
        return {}

    try:
        data = json.loads(raw.strip())

        return (
            data
            if isinstance(data, dict)
            else {}
        )

    except Exception:
        return {}


def _find_text_values(
    value,
    results: list[str],
    depth: int = 0,
) -> None:

    if depth > 10:
        return

    if isinstance(value, dict):

        for key, child in value.items():

            key_lower = str(key).lower()

            if key_lower in {
                "articlebody",
                "article_body",
                "body",
                "content",
                "articlecontent",
                "article_content",
                "description",
            }:

                if isinstance(child, str):

                    text = normalize_paragraph(child)

                    if len(text) >= MIN_ARTICLE_TEXT_LENGTH:
                        results.append(text)

            _find_text_values(
                child,
                results,
                depth + 1,
            )

    elif isinstance(value, list):

        for child in value:
            _find_text_values(
                child,
                results,
                depth + 1,
            )


def extract_text_from_next_data(
    soup: BeautifulSoup,
) -> str:

    data = extract_next_data(soup)

    if not data:
        return ""

    candidates: list[str] = []

    _find_text_values(
        data,
        candidates,
    )

    if not candidates:
        return ""

    candidates = sorted(
        set(candidates),
        key=len,
        reverse=True,
    )

    return candidates[0]


# ============================================================================
# JSON-LD Article Text
# ============================================================================

def extract_text_from_json_ld(
    soup: BeautifulSoup,
) -> str:

    candidates: list[str] = []

    for item in extract_json_ld(soup):

        body = item.get("articleBody")

        if isinstance(body, str):

            body = clean_text(body)

            if len(body) >= MIN_ARTICLE_TEXT_LENGTH:
                candidates.append(body)

    if not candidates:
        return ""

    return max(
        candidates,
        key=len,
    )


# ============================================================================
# DOM Article Text Extraction
# ============================================================================

CONTENT_ROOT_SELECTORS = (
    "article",
    "main article",
    "main",
    "[class*='article-body']",
    "[class*='articleBody']",
    "[class*='article-content']",
    "[class*='articleContent']",
    "[class*='post-content']",
    "[class*='postContent']",
    "[class*='entry-content']",
    "[class*='entryContent']",
    "[class*='story-content']",
    "[class*='storyContent']",
    "[class*='content-body']",
    "[class*='contentBody']",
    "[class*='news-content']",
    "[class*='newsContent']",
    "[class*='magazine-content']",
    "[class*='magazineContent']",
)


PARAGRAPH_SELECTORS = (
    "article p",
    "main article p",
    "main p",
    "[class*='article-body'] p",
    "[class*='article-content'] p",
    "[class*='articleContent'] p",
    "[class*='post-content'] p",
    "[class*='postContent'] p",
    "[class*='entry-content'] p",
    "[class*='story-content'] p",
    "[class*='storyContent'] p",
    "[class*='content-body'] p",
    "[class*='contentBody'] p",
    "[class*='news-content'] p",
    "[class*='newsContent'] p",
    "[class*='magazine-content'] p",
    "[class*='magazineContent'] p",
    "div[class*='text'] p",
    "p",
)


def _remove_noise(
    root: BeautifulSoup,
) -> None:

    root.select(
        "script, style, noscript, nav, header, footer, aside"
    )

    for tag in root.select(
        "script, style, noscript, nav, header, footer, aside"
    ):
        tag.decompose()

    for tag in root.select(
        "[class*='share'], "
        "[class*='social'], "
        "[class*='related'], "
        "[class*='comment'], "
        "[class*='advert'], "
        "[class*='ads'], "
        "[id*='share'], "
        "[id*='social'], "
        "[id*='related'], "
        "[id*='comment'], "
        "[id*='advert']"
    ):
        tag.decompose()


def _extract_paragraphs_from_root(
    root,
) -> str:

    paragraphs: list[str] = []
    seen: set[str] = set()

    for selector in PARAGRAPH_SELECTORS:

        try:
            nodes = root.select(selector)
        except Exception:
            continue

        for node in nodes:

            text = normalize_paragraph(
                node.get_text(
                    " ",
                    strip=True,
                )
            )

            if len(text) <= 10:
                continue

            if text in {
                "مشاركة",
                "إعلان",
                "التعليقات",
                "تابعنا",
                "إقرأ المزيد",
                "اقرأ المزيد",
            }:
                continue

            if text in seen:
                continue

            seen.add(text)
            paragraphs.append(text)

    return "\n".join(paragraphs).strip()


def _extract_best_container_text(
    soup: BeautifulSoup,
) -> str:

    candidates: list[str] = []

    for selector in CONTENT_ROOT_SELECTORS:

        try:
            nodes = soup.select(selector)
        except Exception:
            continue

        for node in nodes:

            clone = BeautifulSoup(
                str(node),
                "html.parser",
            )

            _remove_noise(clone)

            paragraph_text = _extract_paragraphs_from_root(
                clone
            )

            if len(paragraph_text) >= MIN_ARTICLE_TEXT_LENGTH:
                candidates.append(
                    paragraph_text
                )

            raw_text = normalize_paragraph(
                clone.get_text(
                    "\n",
                    strip=True,
                )
            )

            if len(raw_text) >= MIN_ARTICLE_TEXT_LENGTH:
                candidates.append(raw_text)

    if not candidates:
        return ""

    # The article container normally produces
    # the longest coherent block of body text.
    return max(
        candidates,
        key=len,
    )


def extract_article_text(
    soup: BeautifulSoup,
) -> str:
    """
    Multi-layer article text extraction.

    Priority:
        1) JSON-LD articleBody
        2) Next.js __NEXT_DATA__
        3) semantic article/main/content containers
        4) generic paragraph extraction
    """

    # ------------------------------------------------------------------
    # 1. JSON-LD
    # ------------------------------------------------------------------

    text = extract_text_from_json_ld(soup)

    if len(text) >= MIN_ARTICLE_TEXT_LENGTH:
        return text

    # ------------------------------------------------------------------
    # 2. Next.js data
    # ------------------------------------------------------------------

    text = extract_text_from_next_data(soup)

    if len(text) >= MIN_ARTICLE_TEXT_LENGTH:
        return text

    # ------------------------------------------------------------------
    # 3. Best content container
    # ------------------------------------------------------------------

    text = _extract_best_container_text(soup)

    if len(text) >= MIN_ARTICLE_TEXT_LENGTH:
        return text

    # ------------------------------------------------------------------
    # 4. Final generic paragraph extraction
    # ------------------------------------------------------------------

    soup_copy = BeautifulSoup(
        str(soup),
        "html.parser",
    )

    _remove_noise(soup_copy)

    paragraphs = []

    seen = set()

    for p in soup_copy.select("p"):

        txt = normalize_paragraph(
            p.get_text(
                " ",
                strip=True,
            )
        )

        if len(txt) <= 10:
            continue

        if txt in seen:
            continue

        if txt in {
            "مشاركة",
            "إعلان",
            "التعليقات",
            "تابعنا",
            "إقرأ المزيد",
            "اقرأ المزيد",
        }:
            continue

        seen.add(txt)
        paragraphs.append(txt)

    return "\n".join(paragraphs).strip()


# ============================================================================
# Playwright Fallback
# ============================================================================

async def extract_playwright_fallbacks(
    page: Page,
) -> tuple[str, str]:

    try:

        data = await page.evaluate(
            """
            () => {
                const normalize = (value) => {
                    if (!value) return '';

                    return value
                        .replace(/\\s+/g, ' ')
                        .trim();
                };

                const isValidImage = (url) => {
                    if (!url) return false;

                    const lower = url.toLowerCase();

                    if (
                        lower.startsWith('data:') ||
                        lower.startsWith('blob:') ||
                        lower.startsWith('javascript:')
                    ) {
                        return false;
                    }

                    if (lower.endsWith('.svg')) {
                        return false;
                    }

                    if (
                        lower.includes('site-logo') ||
                        lower.includes('favicon') ||
                        lower.includes('default-avatar')
                    ) {
                        return false;
                    }

                    return true;
                };

                let image = '';

                const imageSelectors = [
                    'article img',
                    'main article img',
                    'main img',
                    'figure img',
                    'img'
                ];

                for (const selector of imageSelectors) {

                    const elements = document.querySelectorAll(selector);

                    for (const el of elements) {

                        let src =
                            el.currentSrc ||
                            el.src ||
                            el.getAttribute('src') ||
                            el.getAttribute('data-src') ||
                            el.getAttribute('data-lazy-src') ||
                            el.getAttribute('data-original');

                        if (isValidImage(src)) {

                            if (src.startsWith('//')) {
                                src = 'https:' + src;
                            }

                            image = src;
                            break;
                        }
                    }

                    if (image) {
                        break;
                    }
                }

                const roots = [
                    'article',
                    'main article',
                    'main',
                    '[class*="article-body"]',
                    '[class*="articleBody"]',
                    '[class*="article-content"]',
                    '[class*="articleContent"]',
                    '[class*="post-content"]',
                    '[class*="postContent"]',
                    '[class*="entry-content"]',
                    '[class*="entryContent"]',
                    '[class*="content-body"]',
                    '[class*="contentBody"]'
                ];

                const candidates = [];

                for (const selector of roots) {

                    const nodes =
                        document.querySelectorAll(selector);

                    for (const node of nodes) {

                        const paragraphs =
                            node.querySelectorAll('p');

                        const parts = [];

                        for (const p of paragraphs) {

                            const text =
                                normalize(p.innerText || '');

                            if (text.length > 10) {
                                parts.push(text);
                            }
                        }

                        if (parts.length) {

                            const combined =
                                parts.join('\\n');

                            if (combined.length >= 80) {
                                candidates.push(combined);
                            }
                        }

                        const raw =
                            normalize(node.innerText || '');

                        if (raw.length >= 80) {
                            candidates.push(raw);
                        }
                    }
                }

                // Generic paragraph fallback.
                if (!candidates.length) {

                    const parts = [];

                    for (
                        const p of document.querySelectorAll('p')
                    ) {

                        const text =
                            normalize(p.innerText || '');

                        if (text.length > 10) {
                            parts.push(text);
                        }
                    }

                    if (parts.length) {
                        candidates.push(
                            parts.join('\\n')
                        );
                    }
                }

                candidates.sort(
                    (a, b) => b.length - a.length
                );

                const text =
                    candidates.length
                        ? candidates[0]
                        : '';

                return {
                    image,
                    text
                };
            }
            """
        )

        return (
            data.get("image", ""),
            data.get("text", ""),
        )

    except Exception:
        return "", ""


# ============================================================================
# Smart Waiting
# ============================================================================

async def wait_for_article_content(
    page: Page,
    timeout_ms: int = MAX_WAIT_FOR_TEXT_MS,
) -> None:

    try:

        await page.wait_for_function(
            """
            () => {

                const selectors = [
                    'article p',
                    'main article p',
                    '[class*="article-body"] p',
                    '[class*="article-content"] p',
                    '[class*="articleContent"] p',
                    '[class*="post-content"] p',
                    '[class*="postContent"] p',
                    '[class*="entry-content"] p',
                    '[class*="content-body"] p',
                    '[class*="contentBody"] p',
                    'main p'
                ];

                for (const selector of selectors) {

                    const elements =
                        document.querySelectorAll(selector);

                    let total = 0;

                    for (const element of elements) {

                        const text =
                            (element.innerText || '').trim();

                        if (text.length > 10) {
                            total += text.length;
                        }
                    }

                    if (total >= 80) {
                        return true;
                    }
                }

                // Check Next.js JSON data.
                const next =
                    document.querySelector(
                        '#__NEXT_DATA__'
                    );

                if (next && next.textContent) {

                    const raw = next.textContent;

                    if (raw.length > 500) {
                        return true;
                    }
                }

                return false;
            }
            """,
            timeout=timeout_ms,
        )

    except Exception:
        # The caller will still attempt every extraction layer.
        pass


async def wait_for_stable_content(
    page: Page,
) -> None:

    previous_length = 0
    stable_count = 0

    for _ in range(8):

        try:

            current_length = await page.evaluate(
                """
                () => {

                    const selectors = [
                        'article',
                        'main article',
                        '[class*="article-body"]',
                        '[class*="article-content"]',
                        '[class*="articleContent"]',
                        '[class*="post-content"]',
                        '[class*="postContent"]',
                        'main'
                    ];

                    let best = 0;

                    for (const selector of selectors) {

                        const elements =
                            document.querySelectorAll(selector);

                        for (const el of elements) {

                            const value =
                                (el.innerText || '').trim().length;

                            if (value > best) {
                                best = value;
                            }
                        }
                    }

                    return best;
                }
                """
            )

            if current_length == previous_length:
                stable_count += 1
            else:
                stable_count = 0

            previous_length = current_length

            if stable_count >= 2:
                break

            await page.wait_for_timeout(350)

        except Exception:
            break


# ============================================================================
# Article Fetching
# ============================================================================

async def fetch_article(
    page: Page,
    summary: ArticleSummary,
) -> SourceArticle:

    last_error = None

    for attempt in range(2):

        try:

            await page.goto(
                summary.url,
                wait_until="domcontentloaded",
                timeout=30_000,
            )

            # Give React/Next.js a moment to mount components.
            await page.wait_for_timeout(700)

            # Wait for actual article content rather than
            # waiting only for <p>.
            await wait_for_article_content(page)

            # Allow streamed/lazy content to settle.
            await wait_for_stable_content(page)

            html_content = await page.content()

            soup = BeautifulSoup(
                html_content,
                "html.parser",
            )

            # ----------------------------------------------------------
            # Title
            # ----------------------------------------------------------

            h1 = soup.find("h1")

            title = (
                first_meta(
                    soup,
                    "og:title",
                    "twitter:title",
                )
                or (
                    h1.get_text(
                        strip=True
                    )
                    if h1
                    else summary.title
                )
            )

            # ----------------------------------------------------------
            # Image
            # ----------------------------------------------------------

            image_url = extract_featured_image(soup)

            # ----------------------------------------------------------
            # Article text
            # ----------------------------------------------------------

            article_text = extract_article_text(soup)

            # ----------------------------------------------------------
            # Playwright direct fallback
            # ----------------------------------------------------------

            pw_img, pw_text = (
                await extract_playwright_fallbacks(page)
            )

            if not image_url and pw_img:
                image_url = pw_img

            if len(pw_text) > len(article_text):
                article_text = pw_text

            article_text = clean_text(
                article_text
            )

            # ----------------------------------------------------------
            # If still too short, reload once.
            # ----------------------------------------------------------

            if len(article_text) < MIN_ARTICLE_TEXT_LENGTH:

                if attempt == 0:

                    await page.wait_for_timeout(1500)

                    try:
                        await page.reload(
                            wait_until="domcontentloaded",
                            timeout=30_000,
                        )
                    except Exception:
                        pass

                    await wait_for_article_content(
                        page,
                        timeout_ms=8_000,
                    )

                    await wait_for_stable_content(
                        page
                    )

                    continue

                raise RuntimeError(
                    "Source article text is too short or empty"
                    f" ({len(article_text)} chars)"
                )

            # ----------------------------------------------------------
            # Final image
            # ----------------------------------------------------------

            final_image_url = (
                normalize_url(
                    summary.url,
                    image_url,
                )
                if image_url
                else DEFAULT_FALLBACK_IMAGE_URL
            )

            # ----------------------------------------------------------
            # Published date
            # ----------------------------------------------------------

            published_at = (
                extract_published_at(soup)
                or summary.published_at
            )

            return SourceArticle(
                url=summary.url,
                title=title.strip(),
                text=article_text,
                image_url=final_image_url,
                published_at=published_at,
            )

        except Exception as exc:

            last_error = exc

            if attempt == 0:
                try:
                    await page.wait_for_timeout(1000)
                except Exception:
                    pass

                continue

            raise last_error


# ============================================================================
# Browser Context
# ============================================================================

async def create_browser_context(playwright):

    browser = await playwright.chromium.launch(
        headless=True,
    )

    context = await browser.new_context(
        locale="ar-SA",
        user_agent=USER_AGENT,
        timezone_id="Asia/Riyadh",
        extra_http_headers={
            "Accept-Language": (
                "ar-SA,ar;q=0.9,"
                "en-US;q=0.8,en;q=0.7"
            ),
            "Referer": "https://www.365scores.com/",
        },
    )

    page = await context.new_page()

    return browser, page


# ============================================================================
# Article Discovery
# ============================================================================

async def collect_article_cards(
    page: Page,
    source_url: str,
) -> list[ArticleSummary]:

    now = datetime.now(timezone.utc)

    results: dict[str, ArticleSummary] = {}

    async def collect():

        anchors = await page.locator(
            "a[href]"
        ).evaluate_all(
            """
            els => els.map(a => ({
                href: a.href,
                text: (a.innerText || '').trim()
            }))
            """
        )

        for item in anchors:

            url = normalize_url(
                source_url,
                item["href"],
            )

            if not is_probable_article_url(url):
                continue

            title = re.sub(
                r"\s+",
                " ",
                item["text"],
            ).strip()

            if len(title) < 12:
                continue

            results[url] = ArticleSummary(
                url=url,
                title=title[:300],
                relative_time_text=title,
                published_at=parse_any_time(
                    title,
                    now,
                ),
            )

    await collect()

    for _ in range(4):

        try:

            await page.mouse.wheel(
                0,
                3000,
            )

            await page.wait_for_timeout(
                700
            )

            await collect()

        except Exception:
            break

    return list(results.values())


# ============================================================================
# Browser Fetch
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

    index_url = news_index_url(
        source_url
    )

    async with async_playwright() as playwright:

        browser, page = (
            await create_browser_context(
                playwright
            )
        )

        try:

            await page.goto(
                index_url,
                wait_until="domcontentloaded",
                timeout=30_000,
            )

            await page.wait_for_timeout(
                1000
            )

            summaries = await collect_article_cards(
                page,
                index_url,
            )

            summaries.sort(
                key=lambda x: (
                    x.published_at
                    or datetime.min.replace(
                        tzinfo=timezone.utc
                    )
                ),
                reverse=True,
            )

            candidates = summaries[:max_articles]

            if not candidates:

                errors.append(
                    "No article candidates discovered "
                    f"from {index_url}"
                )

        except Exception as exc:

            errors.append(
                f"source discovery failed: {exc}"
            )

        finally:

            await browser.close()

    return candidates, errors


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

        browser = await playwright.chromium.launch(
            headless=True,
        )

        semaphore = asyncio.Semaphore(
            max_concurrency
        )

        async def worker(
            candidate: ArticleSummary,
        ):

            async with semaphore:

                context = await browser.new_context(
                    locale="ar-SA",
                    user_agent=USER_AGENT,
                    timezone_id="Asia/Riyadh",
                    extra_http_headers={
                        "Accept-Language": (
                            "ar-SA,ar;q=0.9,"
                            "en-US;q=0.8,en;q=0.7"
                        ),
                        "Referer": (
                            "https://www.365scores.com/"
                        ),
                    },
                )

                page = await context.new_page()

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

                    await page.close()

                    await context.close()

        try:

            await asyncio.gather(
                *(
                    worker(candidate)
                    for candidate in candidates
                )
            )

        finally:

            await browser.close()

    return articles, errors


# ============================================================================
# Public API
# ============================================================================

async def discover_and_fetch_async(
    source_url: str,
    max_articles: int,
):

    candidates, discovery_errors = (
        await discover_articles(
            source_url,
            max_articles,
        )
    )

    articles, fetch_errors = (
        await fetch_source_articles(
            news_index_url(source_url),
            candidates,
            max_concurrency=3,
        )
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
):

    return asyncio.run(
        discover_and_fetch_async(
            source_url,
            max_articles,
        )
    )
