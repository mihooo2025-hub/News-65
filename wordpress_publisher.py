"""
scraper_365scores.py
====================
Optimized, concise version for high-concurrency execution on GitHub Actions.
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

ARABIC_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹", "01234567890123456789")
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
DEFAULT_LOGO_PATTERNS = ("site-logo", "favicon", "default-avatar")
ARTICLE_ROOT = "/ar/news/magazine"


def normalize_url(base_url: str, value: str) -> str:
    if not value:
        return ""
    return urljoin(base_url, html.unescape(str(value)).strip()).split("#", 1)[0]


def news_index_url(source_url: str) -> str:
    parsed = urlparse(source_url)
    return f"{parsed.scheme}://{parsed.netloc}/ar/news/magazine/" if parsed.path.rstrip("/") in {"", "/ar"} else source_url


def is_probable_article_url(url: str) -> bool:
    if not url:
        return False
    try:
        parsed = urlparse(url)
        path = unquote(parsed.path).lower().rstrip("/")
        if not path.startswith(ARTICLE_ROOT) or parsed.scheme not in {"http", "https"}:
            return False
        if path == ARTICLE_ROOT:
            return any(re.fullmatch(r"\d+", pid or "") for pid in parse_qs(parsed.query).get("p", []))
        
        rel_path = path[len(ARTICLE_ROOT):].strip("/")
        if not rel_path or re.search(r"^(category|tag|author|search|page/\d+)(/|$)", rel_path):
            return False
        
        slug = rel_path.split("/")[0]
        # تصفية روابط الأندية والأقسام لضمان عدم معاملتها كمقالات
        if re.match(r"^(القنوات-الناقلة|إحصائيات|احصائيات|نادي-|منتخب-|فريق-)", slug) or slug in {"الأهداف-العكسية-كأس-العالم", "بطاقة-حمراء-مجموعات"}:
            return False
        return True
    except Exception:
        return False


# ============================================================================
# Time Parsing
# ============================================================================

def parse_relative_time(text: str, now: datetime) -> datetime | None:
    val = re.sub(r"\s+", " ", text.strip().translate(ARABIC_DIGITS))
    if "الآن" in val or "قبل 0 دقيقة" in val:
        return now
    units = [("دقيقة|دقائق", "minutes"), ("ساعة|ساعات", "hours"), ("يوم|أيام", "days")]
    for p, unit in units:
        if m := re.search(rf"(?:قبل|منذ)\s+(\d+)\s*(?:{p})", val):
            return now - timedelta(**{unit: int(m.group(1))})
    return None


def parse_absolute_time(text: str) -> datetime | None:
    val = re.sub(r"\s+", " ", text.strip().translate(ARABIC_DIGITS))
    if m := re.search(r"(\d{1,2})/(\d{1,2})/(\d{4})\s*(?:-|–|—)?\s*(\d{1,2}):(\d{2})\s*([صم])", val):
        d, mth, y, h, mn, mer = m.groups()
        h = int(h)
        if mer == "م" and h < 12: h += 12
        elif mer == "ص" and h == 12: h = 0
        try:
            return datetime(int(y), int(mth), int(d), h, int(mn), tzinfo=timezone(timedelta(hours=3))).astimezone(timezone.utc)
        except ValueError:
            pass
    if m := re.search(r"\b(20\d{2}-\d{2}-\d{2}T[^\s<]+)", val):
        try:
            dt = datetime.fromisoformat(m.group(1).replace("Z", "+00:00"))
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    return None


def parse_any_time(text: str, now: datetime) -> datetime | None:
    return parse_absolute_time(text) or parse_relative_time(text, now)


# ============================================================================
# Metadata & Text Extraction
# ============================================================================

def extract_json_ld(soup: BeautifulSoup) -> list[dict]:
    items = []
    def collect(data):
        if isinstance(data, dict):
            items.append(data)
            if isinstance(data.get("@graph"), list):
                for entry in data["@graph"]: collect(entry)
        elif isinstance(data, list):
            for entry in data: collect(entry)

    for script in soup.select('script[type="application/ld+json"]'):
        if script.string:
            try: collect(json.loads(script.string.strip()))
            except Exception: pass
    return items


def first_meta(soup: BeautifulSoup, *names: str) -> str:
    for name in names:
        tag = soup.find("meta", attrs={"property": name}) or soup.find("meta", attrs={"name": name})
        if tag and tag.get("content"):
            return tag["content"].strip()
    return ""


def _is_valid_image(url: str) -> bool:
    if not url or url.startswith(("data:", "blob:", "javascript:")):
        return False
    lower = url.lower()
    if any(p in lower for p in DEFAULT_LOGO_PATTERNS):
        return False
    return not lower.endswith(".svg")


def extract_featured_image(soup: BeautifulSoup) -> str:
    # 1. OpenGraph & Twitter tags
    meta = first_meta(soup, "og:image", "twitter:image", "image")
    if _is_valid_image(meta):
        return meta

    # 2. JSON-LD Extraction
    for item in extract_json_ld(soup):
        for k in ("image", "primaryImageOfPage", "thumbnailUrl"):
            val = item.get(k)
            img = val[0] if isinstance(val, list) else (val.get("url") if isinstance(val, dict) else val)
            if isinstance(img, str) and _is_valid_image(img):
                return img.strip()

    # 3. HTML5 <picture> / <source> / <img> with Lazy Loading & srcset
    for img in soup.select("article img, main img, figure img, img"):
        src = (
            img.get("src")
            or img.get("data-src")
            or img.get("data-lazy-src")
            or img.get("data-original")
            or (img.get("srcset", "").split(",")[0].split()[0] if img.get("srcset") else "")
        )
        if src and _is_valid_image(src):
            return src.strip()

    return ""


def extract_published_at(soup: BeautifulSoup) -> datetime | None:
    now = datetime.now(timezone.utc)
    for k in ("article:published_time", "datePublished", "publish_date"):
        if val := first_meta(soup, k):
            if dt := parse_any_time(val, now): return dt

    for t in soup.select("time[datetime]"):
        if dt := parse_any_time(t.get("datetime", ""), now): return dt

    return parse_any_time(soup.get_text(" ", strip=True), now)


def extract_article_text(soup: BeautifulSoup) -> str:
    for item in extract_json_ld(soup):
        body = item.get("articleBody")
        if isinstance(body, str) and len(body) >= 120:
            return re.sub(r"\s+", " ", body).strip()

    soup_copy = BeautifulSoup(str(soup), "html.parser")
    for s in soup_copy.select("script, style, nav, header, footer, aside, [class*='share'], [class*='related']"):
        s.decompose()

    paragraphs = []
    for p in soup_copy.select("article p, main p, [class*='article'] p, p"):
        txt = re.sub(r"\s+", " ", p.get_text(strip=True))
        if len(txt) > 15 and txt not in paragraphs and txt not in {"مشاركة", "إعلان", "التعليقات"}:
            paragraphs.append(txt)

    return "\n".join(paragraphs).strip()


# ============================================================================
# Playwright Automation & Concurrency
# ============================================================================

async def extract_playwright_fallbacks(page: Page) -> tuple[str, str]:
    try:
        data = await page.evaluate("""() => {
            const isVal = (u) => u && !u.startswith('data:') && !u.endsWith('.svg') && !['site-logo','favicon','default-avatar'].some(p => u.includes(p));
            let img = '';
            for (let el of document.querySelectorAll('article img, main img, img')) {
                let src = el.currentSrc || el.src || el.getAttribute('data-src') || el.getAttribute('data-lazy-src') || el.getAttribute('data-original');
                if (isVal(src)) { img = src; break; }
            }
            let text = [];
            for (let p of document.querySelectorAll('article p, main p, p')) {
                let t = p.innerText.strip ? p.innerText.strip() : p.innerText.trim();
                if (t.length > 15) text.push(t);
            }
            return { image: img, text: text.join('\\n') };
        }""")
        return data.get("image", ""), data.get("text", "")
    except Exception:
        return "", ""


async def collect_article_cards(page: Page, source_url: str) -> list[ArticleSummary]:
    now = datetime.now(timezone.utc)
    results: dict[str, ArticleSummary] = {}

    async def collect():
        anchors = await page.locator("a[href]").evaluate_all("els => els.map(a => ({href: a.href, text: a.innerText.trim()}))")
        for item in anchors:
            url = normalize_url(source_url, item["href"])
            if not is_probable_article_url(url): continue
            title = re.sub(r"\s+", " ", item["text"]).strip()
            if len(title) >= 12:
                results[url] = ArticleSummary(
                    url=url,
                    title=title[:300],
                    relative_time_text=title,
                    published_at=parse_any_time(title, now)
                )

    await collect()
    for _ in range(3):
        try:
            await page.mouse.wheel(0, 3000)
            await page.wait_for_timeout(500)
            await collect()
        except Exception:
            break

    return list(results.values())


async def fetch_article(page: Page, summary: ArticleSummary) -> SourceArticle:
    await page.goto(summary.url, wait_until="domcontentloaded", timeout=20_000)
    await page.wait_for_timeout(800)

    html_content = await page.content()
    soup = BeautifulSoup(html_content, "html.parser")

    title = first_meta(soup, "og:title", "twitter:title") or (soup.find("h1").get_text(strip=True) if soup.find("h1") else summary.title)
    image_url = extract_featured_image(soup)
    article_text = extract_article_text(soup)

    if not image_url or len(article_text) < 150:
        pw_img, pw_text = await extract_playwright_fallbacks(page)
        image_url = image_url or pw_img
        if len(pw_text) > len(article_text):
            article_text = pw_text

    return SourceArticle(
        url=summary.url,
        title=title.strip(),
        text=article_text.strip(),
        image_url=normalize_url(summary.url, image_url),
        published_at=extract_published_at(soup) or summary.published_at,
    )


async def create_browser_context(playwright):
    browser = await playwright.chromium.launch(headless=True)
    context = await browser.new_context(
        locale="ar-SA",
        user_agent=USER_AGENT,
        timezone_id="Asia/Riyadh",
        extra_http_headers={
            "Accept-Language": "ar-SA,ar;q=0.9,en-US;q=0.8,en;q=0.7",
            "Referer": "https://www.365scores.com/",
        }
    )
    page = await context.new_page()
    return browser, page


# ============================================================================
# Discovery & Concurrent Execution
# ============================================================================

async def discover_articles(source_url: str, max_articles: int) -> tuple[list[ArticleSummary], list[str]]:
    candidates, errors = [], []
    index_url = news_index_url(source_url)

    async with async_playwright() as playwright:
        browser, page = await create_browser_context(playwright)
        try:
            await page.goto(index_url, wait_until="domcontentloaded", timeout=25_000)
            summaries = await collect_article_cards(page, index_url)
            summaries.sort(key=lambda x: (x.published_at or datetime.min.replace(tzinfo=timezone.utc)), reverse=True)
            candidates = summaries[:max_articles]
            if not candidates:
                errors.append(f"No article candidates discovered from {index_url}")
        except Exception as exc:
            errors.append(f"source discovery failed: {exc}")
        finally:
            await browser.close()

    return candidates, errors


async def fetch_source_articles(source_url: str, candidates: list[ArticleSummary], max_concurrency: int = 3) -> tuple[list[SourceArticle], list[tuple[str, str]]]:
    articles, errors = [], []
    if not candidates:
        return articles, errors

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        semaphore = asyncio.Semaphore(max_concurrency)

        async def worker(candidate: ArticleSummary):
            async with semaphore:
                context = await browser.new_context(
                    locale="ar-SA",
                    user_agent=USER_AGENT,
                    timezone_id="Asia/Riyadh",
                    extra_http_headers={
                        "Accept-Language": "ar-SA,ar;q=0.9,en-US;q=0.8,en;q=0.7",
                        "Referer": "https://www.365scores.com/",
                    }
                )
                page = await context.new_page()
                try:
                    art = await fetch_article(page, candidate)
                    articles.append(art)
                except Exception as exc:
                    errors.append((candidate.url, str(exc)))
                finally:
                    await page.close()
                    await context.close()

        try:
            await asyncio.gather(*(worker(c) for c in candidates))
        finally:
            await browser.close()

    return articles, errors


async def discover_and_fetch_async(source_url: str, max_articles: int):
    candidates, discovery_errors = await discover_articles(source_url, max_articles)
    articles, fetch_errors = await fetch_source_articles(news_index_url(source_url), candidates)
    return candidates, discovery_errors, articles, fetch_errors


def discover_and_fetch(source_url: str, max_articles: int):
    return asyncio.run(discover_and_fetch_async(source_url, max_articles))
