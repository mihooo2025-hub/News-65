"""
scraper_365scores.py
====================
Scraper for 365Scores Magazine using Playwright + BeautifulSoup.
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

ARTICLE_ROOT = "/ar/news/magazine"

DEFAULT_FALLBACK_IMAGE_URL = (
    "https://nabdalmalaeb.com/wp-content/uploads/default-news.jpg"
)

LOGO_PATTERNS = (
    "site-logo",
    "favicon",
    "default-avatar",
)

MIN_TEXT = 80

CONTENT_SELECTORS = (
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
    "article",
    "main article",
    "main",
)


# ============================================================================
# Helpers
# ============================================================================

def normalize_url(base: str, value: str) -> str:
    if not value:
        return ""

    return urljoin(
        base,
        html.unescape(str(value)).strip(),
    ).split("#", 1)[0]


def news_index_url(url: str) -> str:
    parsed = urlparse(url)

    if parsed.path.rstrip("/") in {"", "/ar"}:
        return (
            f"{parsed.scheme}://{parsed.netloc}"
            "/ar/news/magazine/"
        )

    return url


def clean_text(text: str) -> str:
    if not text:
        return ""

    text = html.unescape(text)
    text = text.replace("\u200f", "").replace("\u200e", "")
    text = re.sub(r"\s+", " ", text)

    return text.strip()


def valid_image(url: str) -> bool:
    if not url:
        return False

    value = url.lower().strip()

    return not (
        value.startswith(
            ("data:", "blob:", "javascript:")
        )
        or value.endswith(".svg")
        or any(x in value for x in LOGO_PATTERNS)
    )


# ============================================================================
# URL Filtering
# ============================================================================

def is_probable_article_url(url: str) -> bool:
    if not url:
        return False

    try:
        parsed = urlparse(url)
        path = unquote(parsed.path).lower().rstrip("/")

        if (
            parsed.scheme not in {"http", "https"}
            or not path.startswith(ARTICLE_ROOT)
        ):
            return False

        if path == ARTICLE_ROOT:
            return any(
                re.fullmatch(r"\d+", x or "")
                for x in parse_qs(parsed.query).get("p", [])
            )

        rel = path[len(ARTICLE_ROOT):].strip("/")

        if not rel:
            return False

        if re.search(
            r"^(category|tag|author|search|page/\d+)(/|$)",
            rel,
        ):
            return False

        slug = rel.split("/")[0]

        ignored = (
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

        return not any(x in slug for x in ignored)

    except Exception:
        return False


# ============================================================================
# Time
# ============================================================================

def parse_time(text: str, now: datetime) -> datetime | None:
    value = re.sub(
        r"\s+",
        " ",
        text.strip().translate(ARABIC_DIGITS),
    )

    if "الآن" in value or "قبل 0 دقيقة" in value:
        return now

    for pattern, unit in (
        ("دقيقة|دقائق", "minutes"),
        ("ساعة|ساعات", "hours"),
        ("يوم|أيام", "days"),
    ):
        match = re.search(
            rf"(?:قبل|منذ)\s+(\d+)\s*(?:{pattern})",
            value,
        )

        if match:
            return now - timedelta(
                **{unit: int(match.group(1))}
            )

    match = re.search(
        r"(\d{1,2})/(\d{1,2})/(\d{4})"
        r"\s*(?:-|–|—)?\s*"
        r"(\d{1,2}):(\d{2})\s*([صم])",
        value,
    )

    if match:
        d, m, y, h, minute, meridiem = match.groups()

        h = int(h)

        if meridiem == "م" and h < 12:
            h += 12
        elif meridiem == "ص" and h == 12:
            h = 0

        try:
            return datetime(
                int(y),
                int(m),
                int(d),
                h,
                int(minute),
                tzinfo=timezone(timedelta(hours=3)),
            ).astimezone(timezone.utc)

        except ValueError:
            pass

    match = re.search(
        r"\b20\d{2}-\d{2}-\d{2}T[^\s<]+",
        value,
    )

    if match:
        try:
            dt = datetime.fromisoformat(
                match.group(0).replace("Z", "+00:00")
            )

            return (
                dt
                if dt.tzinfo
                else dt.replace(tzinfo=timezone.utc)
            )

        except ValueError:
            pass

    return None


# ============================================================================
# Metadata
# ============================================================================

def first_meta(
    soup: BeautifulSoup,
    *names: str,
) -> str:

    for name in names:
        tag = (
            soup.find("meta", property=name)
            or soup.find("meta", attrs={"name": name})
        )

        if tag and tag.get("content"):
            return tag["content"].strip()

    return ""


def json_ld(soup: BeautifulSoup) -> list[dict]:
    result = []

    def collect(value):
        if isinstance(value, dict):
            result.append(value)

            if isinstance(value.get("@graph"), list):
                for item in value["@graph"]:
                    collect(item)

        elif isinstance(value, list):
            for item in value:
                collect(item)

    for script in soup.select(
        'script[type="application/ld+json"]'
    ):
        raw = script.string or script.get_text()

        if not raw:
            continue

        try:
            collect(json.loads(raw))
        except Exception:
            pass

    return result


# ============================================================================
# Image
# ============================================================================

def extract_featured_image(
    soup: BeautifulSoup,
) -> str:

    image = first_meta(
        soup,
        "og:image",
        "twitter:image",
        "og:image:secure_url",
        "image",
    )

    if valid_image(image):
        return (
            "https:" + image
            if image.startswith("//")
            else image
        )

    for item in json_ld(soup):

        for key in (
            "image",
            "primaryImageOfPage",
            "thumbnailUrl",
        ):
            value = item.get(key)

            if isinstance(value, list):
                value = value[0] if value else ""

            if isinstance(value, dict):
                value = value.get("url", "")

            if isinstance(value, str) and valid_image(value):
                return (
                    "https:" + value
                    if value.startswith("//")
                    else value.strip()
                )

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
            src = img["srcset"].split(",")[-1].strip().split()[0]

        if valid_image(src):
            return (
                "https:" + src
                if src.startswith("//")
                else src.strip()
            )

    return ""


# ============================================================================
# Published Date
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

        if value and (dt := parse_time(value, now)):
            return dt

    for tag in soup.select("time[datetime]"):
        if dt := parse_time(
            tag.get("datetime", ""),
            now,
        ):
            return dt

    for item in json_ld(soup):
        value = (
            item.get("datePublished")
            or item.get("dateCreated")
        )

        if isinstance(value, str):
            if dt := parse_time(value, now):
                return dt

    return None


# ============================================================================
# Article Text
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

    try:
        data = json.loads(
            script.string or script.get_text()
        )

        return data if isinstance(data, dict) else {}

    except Exception:
        return {}


def find_next_text(
    value,
    results: list[str],
    depth: int = 0,
) -> None:

    if depth > 10:
        return

    if isinstance(value, dict):

        for key, child in value.items():

            if str(key).lower() in {
                "articlebody",
                "article_body",
                "body",
                "content",
                "articlecontent",
                "article_content",
            }:
                if isinstance(child, str):
                    text = clean_text(child)

                    if len(text) >= MIN_TEXT:
                        results.append(text)

            find_next_text(
                child,
                results,
                depth + 1,
            )

    elif isinstance(value, list):

        for child in value:
            find_next_text(
                child,
                results,
                depth + 1,
            )


def extract_article_text(
    soup: BeautifulSoup,
) -> str:

    # 1. JSON-LD
    candidates = [
        clean_text(item["articleBody"])
        for item in json_ld(soup)
        if isinstance(item.get("articleBody"), str)
        and len(clean_text(item["articleBody"])) >= MIN_TEXT
    ]

    if candidates:
        return max(candidates, key=len)

    # 2. Next.js
    next_candidates = []

    find_next_text(
        extract_next_data(soup),
        next_candidates,
    )

    if next_candidates:
        return max(next_candidates, key=len)

    # 3. Specific article containers
    for selector in CONTENT_SELECTORS:

        found = []

        for node in soup.select(selector):

            clone = BeautifulSoup(
                str(node),
                "html.parser",
            )

            for tag in clone.select(
                "script,style,noscript,nav,header,footer,aside,"
                "[class*='share'],[class*='social'],"
                "[class*='related'],[class*='comment'],"
                "[class*='advert'],[class*='ads']"
            ):
                tag.decompose()

            paragraphs = []
            seen = set()

            for p in clone.select("p"):

                text = clean_text(
                    p.get_text(" ", strip=True)
                )

                if (
                    len(text) > 10
                    and text not in seen
                    and text not in {
                        "مشاركة",
                        "إعلان",
                        "التعليقات",
                        "تابعنا",
                        "إقرأ المزيد",
                        "اقرأ المزيد",
                    }
                ):
                    seen.add(text)
                    paragraphs.append(text)

            text = "\n".join(paragraphs).strip()

            if len(text) >= MIN_TEXT:
                found.append(text)

        if found:
            return max(found, key=len)

    # 4. Generic paragraphs
    clone = BeautifulSoup(
        str(soup),
        "html.parser",
    )

    for tag in clone.select(
        "script,style,noscript,nav,header,footer,aside"
    ):
        tag.decompose()

    paragraphs = []
    seen = set()

    for p in clone.select("p"):

        text = clean_text(
            p.get_text(" ", strip=True)
        )

        if len(text) > 10 and text not in seen:
            seen.add(text)
            paragraphs.append(text)

    return "\n".join(paragraphs).strip()


# ============================================================================
# Playwright Fallback
# ============================================================================

async def playwright_extract(
    page: Page,
) -> tuple[str, str]:

    try:

        return await page.evaluate(
            """
            () => {

                const clean = value =>
                    (value || '')
                        .replace(/\\s+/g, ' ')
                        .trim();

                const validImage = url => {

                    if (!url) return false;

                    const x = url.toLowerCase();

                    return !(
                        x.startsWith('data:') ||
                        x.startsWith('blob:') ||
                        x.startsWith('javascript:') ||
                        x.endsWith('.svg') ||
                        x.includes('site-logo') ||
                        x.includes('favicon') ||
                        x.includes('default-avatar')
                    );
                };

                let image = '';

                for (
                    const el of document.querySelectorAll(
                        'article img, main img, figure img, img'
                    )
                ) {

                    let src =
                        el.currentSrc ||
                        el.src ||
                        el.getAttribute('src') ||
                        el.getAttribute('data-src') ||
                        el.getAttribute('data-lazy-src') ||
                        el.getAttribute('data-original');

                    if (validImage(src)) {

                        if (src.startsWith('//')) {
                            src = 'https:' + src;
                        }

                        image = src;
                        break;
                    }
                }

                let text = '';

                const roots = [
                    '[class*="article-body"]',
                    '[class*="articleBody"]',
                    '[class*="article-content"]',
                    '[class*="articleContent"]',
                    '[class*="post-content"]',
                    '[class*="postContent"]',
                    '[class*="content-body"]',
                    '[class*="contentBody"]',
                    'article',
                    'main article',
                    'main'
                ];

                for (const selector of roots) {

                    for (
                        const root of
                        document.querySelectorAll(selector)
                    ) {

                        const parts = [];

                        for (
                            const p of
                            root.querySelectorAll('p')
                        ) {

                            const value =
                                clean(p.innerText);

                            if (value.length > 10) {
                                parts.push(value);
                            }
                        }

                        const paragraphs =
                            parts.join('\\n');

                        if (paragraphs.length >= 80) {
                            text = paragraphs;
                            break;
                        }

                        const raw =
                            clean(root.innerText);

                        if (raw.length >= 80) {
                            text = raw;
                            break;
                        }
                    }

                    if (text.length >= 80) {
                        break;
                    }
                }

                if (text.length < 80) {

                    const parts = [];

                    for (
                        const p of
                        document.querySelectorAll('p')
                    ) {

                        const value =
                            clean(p.innerText);

                        if (value.length > 10) {
                            parts.push(value);
                        }
                    }

                    text = parts.join('\\n');
                }

                return {
                    image,
                    text
                };
            }
            """
        )

    except Exception:
        return "", ""


# ============================================================================
# Waiting
# ============================================================================

async def wait_for_content(
    page: Page,
    timeout: int = 12_000,
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
                    '[class*="content-body"] p',
                    '[class*="contentBody"] p',
                    'main p'
                ];

                for (const selector of selectors) {

                    let total = 0;

                    for (
                        const el of
                        document.querySelectorAll(selector)
                    ) {

                        const text =
                            (el.innerText || '').trim();

                        if (text.length > 10) {
                            total += text.length;
                        }
                    }

                    if (total >= 80) {
                        return true;
                    }
                }

                return false;
            }
            """,
            timeout=timeout,
        )

    except Exception:
        pass


async def wait_stable(page: Page) -> None:

    previous = 0
    stable = 0

    for _ in range(7):

        try:

            current = await page.evaluate(
                """
                () => {

                    let best = 0;

                    for (
                        const selector of [
                            '[class*="article-body"]',
                            '[class*="article-content"]',
                            'article',
                            'main article',
                            'main'
                        ]
                    ) {

                        for (
                            const el of
                            document.querySelectorAll(selector)
                        ) {

                            best = Math.max(
                                best,
                                (el.innerText || '').trim().length
                            );
                        }
                    }

                    return best;
                }
                """
            )

            if current == previous:
                stable += 1;
            else:
                stable = 0

            previous = current

            if stable >= 2:
                return

            await page.wait_for_timeout(350)

        except Exception:
            return


# ============================================================================
# Fetch Article
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

            await page.wait_for_timeout(700)

            await wait_for_content(page)
            await wait_stable(page)

            soup = BeautifulSoup(
                await page.content(),
                "html.parser",
            )

            title = (
                first_meta(
                    soup,
                    "og:title",
                    "twitter:title",
                )
                or (
                    soup.find("h1").get_text(strip=True)
                    if soup.find("h1")
                    else summary.title
                )
            )

            image = extract_featured_image(soup)
            text = extract_article_text(soup)

            pw_image, pw_text = (
                await playwright_extract(page)
            )

            if not image:
                image = pw_image

            if len(pw_text) > len(text):
                text = pw_text

            text = clean_text(text)

            # ----------------------------------------------------------
            # Recovery 1: lightweight scroll
            # ----------------------------------------------------------

            if len(text) < MIN_TEXT and attempt == 0:

                try:
                    await page.evaluate(
                        """
                        () => window.scrollTo(
                            0,
                            document.body.scrollHeight * 0.6
                        )
                        """
                    )
                except Exception:
                    pass

                await page.wait_for_timeout(1000)
                await wait_for_content(page, 5_000)
                await wait_stable(page)

                soup = BeautifulSoup(
                    await page.content(),
                    "html.parser",
                )

                text = extract_article_text(soup)

                pw_image, pw_text = (
                    await playwright_extract(page)
                )

                if not image:
                    image = pw_image

                if len(pw_text) > len(text):
                    text = pw_text

                text = clean_text(text)

            # ----------------------------------------------------------
            # Recovery 2: reload only if still necessary
            # ----------------------------------------------------------

            if len(text) < MIN_TEXT and attempt == 0:

                try:
                    await page.reload(
                        wait_until="domcontentloaded",
                        timeout=30_000,
                    )
                except Exception:
                    pass

                await page.wait_for_timeout(700)
                await wait_for_content(page, 8_000)
                await wait_stable(page)

                continue

            if len(text) < MIN_TEXT:

                raise RuntimeError(
                    "Source article text is too short "
                    f"or empty ({len(text)} chars)"
                )

            published_at = (
                extract_published_at(soup)
                or summary.published_at
            )

            return SourceArticle(
                url=summary.url,
                title=title.strip(),
                text=text,
                image_url=(
                    normalize_url(summary.url, image)
                    if image
                    else DEFAULT_FALLBACK_IMAGE_URL
                ),
                published_at=published_at,
            )

        except Exception as exc:

            last_error = exc

            if attempt == 0:
                await page.wait_for_timeout(1000)
                continue

            raise last_error


# ============================================================================
# Browser
# ============================================================================

async def create_browser_context(playwright):

    browser = await playwright.chromium.launch(
        headless=True
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

    return browser, await context.new_page()


# ============================================================================
# Discovery
# ============================================================================

async def collect_article_cards(
    page: Page,
    source_url: str,
) -> list[ArticleSummary]:

    now = datetime.now(timezone.utc)
    results = {}

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
                published_at=parse_time(
                    title,
                    now,
                ),
            )

    await collect()

    for _ in range(4):

        try:
            await page.mouse.wheel(0, 3000)
            await page.wait_for_timeout(700)
            await collect()
        except Exception:
            break

    return list(results.values())


async def discover_articles(
    source_url: str,
    max_articles: int,
):

    candidates = []
    errors = []

    index_url = news_index_url(source_url)

    async with async_playwright() as playwright:

        browser, page = (
            await create_browser_context(playwright)
        )

        try:

            await page.goto(
                index_url,
                wait_until="domcontentloaded",
                timeout=30_000,
            )

            await page.wait_for_timeout(1000)

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
                    f"No article candidates discovered "
                    f"from {index_url}"
                )

        except Exception as exc:

            errors.append(
                f"source discovery failed: {exc}"
            )

        finally:
            await browser.close()

    return candidates, errors


# ============================================================================
# Fetch Multiple Articles
# ============================================================================

async def fetch_source_articles(
    source_url: str,
    candidates: list[ArticleSummary],
    max_concurrency: int = 3,
):

    articles = []
    errors = []

    if not candidates:
        return articles, errors

    async with async_playwright() as playwright:

        browser = await playwright.chromium.launch(
            headless=True
        )

        semaphore = asyncio.Semaphore(
            max_concurrency
        )

        async def worker(candidate):

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

                    articles.append(
                        await fetch_article(
                            page,
                            candidate,
                        )
                    )

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
                *(worker(c) for c in candidates)
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
