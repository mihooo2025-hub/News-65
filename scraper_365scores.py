"""
scraper_365scores.py
====================
Fetches football news from 365Scores Arabic news pages.
Handles article discovery, URL normalization, metadata extraction,
Arabic date parsing, and robust content collection via Playwright & BeautifulSoup.
"""

from __future__ import annotations

import asyncio
import json
import re
from datetime import datetime, timedelta, timezone
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup
from playwright.async_api import Page, async_playwright

from models import ArticleSummary, SourceArticle

# Constants & Helpers
ARABIC_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹", "01234567890123456789")
USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36"
DEFAULT_LOGO_PATTERNS = ("365scores", "logo", "icon", "avatar", "favicon", "placeholder", "default")


def normalize_url(base_url: str, value: str) -> str:
    return urljoin(base_url, value.strip()).split("#", 1)[0] if value and value.strip() else ""


def news_index_url(source_url: str) -> str:
    parsed = urlparse(source_url)
    return f"{parsed.scheme}://{parsed.netloc}/ar/news/magazine/" if parsed.path.rstrip("/") in {"", "/ar"} else source_url


def is_probable_article_url(url: str) -> False | bool:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return False
    path = parsed.path.lower()
    if any(p in path for p in ("/login", "/signup", "/matches", "/teams", "/players")):
        return False
    return "/news/magazine/" in path and not path.endswith("/news/magazine/")


# Time Parsing Utilities
def parse_relative_time(text: str, now: datetime) -> datetime | None:
    value = re.sub(r"\s+", " ", text.strip().translate(ARABIC_DIGITS))
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
            delta = timedelta(minutes=amount) if unit == "minutes" else timedelta(hours=amount) if unit == "hours" else timedelta(days=amount)
            return now - delta
    return None


def parse_absolute_time(text: str) -> datetime | None:
    value = re.sub(r"\s+", " ", text.strip().translate(ARABIC_DIGITS))
    if match := re.search(r"(\d{1,2})/(\d{1,2})/(\d{4})\s*(?:-|–|—)?\s*(\d{1,2}):(\d{2})\s*([صم])", value):
        day, month, year, hour, minute = map(int, match.groups()[:5])
        meridiem = match.group(6)
        if meridiem == "م" and hour < 12:
            hour += 12
        elif meridiem == "ص" and hour == 12:
            hour = 0
        try:
            return datetime(year, month, day, hour, minute, tzinfo=timezone(timedelta(hours=3))).astimezone(timezone.utc)
        except ValueError:
            return None

    if match := re.search(r"\b(20\d{2}-\d{2}-\d{2}T[^\s<]+)", value):
        try:
            dt = datetime.fromisoformat(match.group(1).replace("Z", "+00:00"))
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    return None


def parse_any_time(text: str, now: datetime) -> datetime | None:
    return parse_absolute_time(text) or parse_relative_time(text, now)


# DOM / Scraping Extraction Functions
def extract_json_ld(soup: BeautifulSoup) -> list[dict]:
    items = []
    for script in soup.select('script[type="application/ld+json"]'):
        raw = script.string or script.get_text()
        if not raw:
            continue
        try:
            data = json.loads(raw)
            if isinstance(data, dict):
                items.append(data)
            elif isinstance(data, list):
                items.extend(item for item in data if isinstance(item, dict))
        except Exception:
            continue
    return items


def first_meta(soup: BeautifulSoup, *names: str) -> str:
    for name in names:
        tag = soup.find("meta", attrs={"property": name}) or soup.find("meta", attrs={"name": name})
        if tag and (val := (tag.get("content") or "").strip()):
            return val
    return ""


def _is_valid_article_image(url_str: str) -> bool:
    """تتأكد من أن رابط الصورة ليس شعار الموقع الافتراضي أو صورة عامة."""
    if not url_str or not isinstance(url_str, str):
        return False
    lower_url = url_str.lower().strip()
    return not any(pattern in lower_url for pattern in DEFAULT_LOGO_PATTERNS)


def extract_featured_image(soup: BeautifulSoup) -> str:
    # 1. البحث في صور المقال الحقيقية عبر وسوم img و source و picture والـ Lazy Loading
    for selector in ("article", "main", "[class*='article']", "[class*='Article']", "[class*='news']"):
        container = soup.select_one(selector)
        if not container:
            continue
            
        for img in container.select("img, source"):
            srcset = img.get("srcset") or img.get("data-srcset")
            if srcset:
                first_src = srcset.split(",")[0].strip().split(" ")[0]
                if _is_valid_article_image(first_src):
                    return first_src.strip()
                    
            src = img.get("src") or img.get("data-src") or img.get("data-lazy-src")
            if src and _is_valid_article_image(src):
                return src.strip()

        for tag in container.find_all(True, style=True):
            style = tag.get("style", "")
            if "background-image" in style and "url(" in style:
                match = re.search(r'url\(([\'"]?)(.*?)\1\)', style)
                if match and _is_valid_article_image(match.group(2)):
                    return match.group(2).strip()

    # 2. البحث في JSON-LD Schema
    for item in extract_json_ld(soup):
        img = item.get("image")
        if isinstance(img, str) and _is_valid_article_image(img):
            return img.strip()
        if isinstance(img, list):
            for cand in img:
                cand_url = cand if isinstance(cand, str) else cand.get("url") if isinstance(cand, dict) else ""
                if cand_url and _is_valid_article_image(str(cand_url)):
                    return str(cand_url).strip()
        if isinstance(img, dict) and img.get("url") and _is_valid_article_image(str(img["url"])):
            return str(img["url"]).strip()

    # 3. الاعتماد على Meta Tags بشرط ألا تكون الصورة العامة للموقع
    if val := first_meta(soup, "og:image", "twitter:image"):
        if _is_valid_article_image(val):
            return val

    return ""


def extract_published_at(soup: BeautifulSoup) -> datetime | None:
    now = datetime.now(timezone.utc)
    for key in ("article:published_time", "datePublished", "dateCreated", "publish_date"):
        if val := first_meta(soup, key):
            try:
                dt = datetime.fromisoformat(val.replace("Z", "+00:00"))
                return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
            except ValueError:
                if parsed := parse_any_time(val, now):
                    return parsed

    for item in extract_json_ld(soup):
        for key in ("datePublished", "dateCreated"):
            if val := item.get(key):
                if isinstance(val, str):
                    try:
                        dt = datetime.fromisoformat(val.replace("Z", "+00:00"))
                        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
                    except ValueError:
                        if parsed := parse_any_time(val, now):
                            return parsed

    for time_tag in soup.select("time[datetime]"):
        if parsed := parse_any_time(time_tag.get("datetime", "").strip(), now):
            return parsed

    return parse_any_time(soup.get_text(" ", strip=True), now)


def extract_article_text(soup: BeautifulSoup) -> str:
    # Fallback 1: JSON-LD Schema (Best quality for 365Scores)
    for item in extract_json_ld(soup):
        body = item.get("articleBody") or item.get("description")
        if isinstance(body, str) and len(body.strip()) > 80:
            return body.strip()

    # Fallback 2: Direct Paragraph Extraction
    paragraphs = []
    for p in soup.select("article p, main p, [class*='article'] p, [class*='News'] p, [class*='text'] p, p"):
        text = p.get_text(" ", strip=True)
        if text and len(text) > 15:
            paragraphs.append(text)
    if paragraphs and len(full_p := "\n".join(paragraphs)) >= 80:
        return full_p

    # Fallback 3: Container Text Extraction
    candidates = []
    selectors = ("article", "main", "[class*='article']", "[class*='Article']", "[class*='news']", "[class*='News']", "[class*='content']", "[class*='Content']")
    for selector in selectors:
        if node := soup.select_one(selector):
            if text := node.get_text("\n", strip=True):
                candidates.append(text)

    if not candidates:
        candidates.append(soup.get_text("\n", strip=True))

    text = max(candidates, key=len, default="")
    lines = [re.sub(r"\s+", " ", line).strip() for line in text.splitlines() if line.strip()]
    return "\n".join(lines)


# Playwright Automation Tasks
async def collect_article_cards(page: Page, source_url: str) -> list[ArticleSummary]:
    now = datetime.now(timezone.utc)
    results: dict[str, ArticleSummary] = {}

    async def collect_current_dom() -> None:
        anchors = await page.locator("a[href]").evaluate_all(
            "els => els.map(a => ({ href: a.href, text: (a.innerText || '').trim(), aria: a.getAttribute('aria-label') || '' }))"
        )
        for item in anchors:
            url = normalize_url(source_url, str(item.get("href", "")))
            title_text = re.sub(r"\s+", " ", str(item.get("text", "") or item.get("aria", ""))).strip()

            if not title_text or len(title_text) < 12 or not is_probable_article_url(url):
                continue

            parsed_time = parse_any_time(title_text, now)
            clean_title = re.sub(r"\s+\d{1,2}/\d{1,2}/\d{4}.*$", "", title_text).strip()
            clean_title = re.sub(r"\s+(?:قبل|منذ)\s+\d+\s+\S+.*$", "", clean_title).strip() or title_text

            current = results.get(url)
            candidate = ArticleSummary(
                url=url,
                title=clean_title[:300],
                relative_time_text=title_text,
                published_at=parsed_time,
            )
            if current is None or len(candidate.title) > len(current.title):
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


async def fetch_article(page: Page, summary: ArticleSummary) -> SourceArticle:
    await page.goto(summary.url, wait_until="domcontentloaded", timeout=45_000)
    await page.wait_for_timeout(2_000)

    html = await page.content()
    soup = BeautifulSoup(html, "html.parser")

    title = first_meta(soup, "og:title", "twitter:title") or (
        soup.find("h1").get_text(" ", strip=True) if soup.find("h1") else summary.title
    )

    return SourceArticle(
        url=summary.url,
        title=title.strip(),
        text=extract_article_text(soup).strip(),
        image_url=normalize_url(summary.url, extract_featured_image(soup)),
        published_at=extract_published_at(soup) or summary.published_at,
    )


async def create_browser_context(playwright):
    browser = await playwright.chromium.launch(headless=True)
    page = await browser.new_page(
        locale="ar-SA",
        user_agent=USER_AGENT,
        timezone_id="Asia/Riyadh",
    )
    return browser, page


async def discover_articles(source_url: str, max_articles: int) -> tuple[list[ArticleSummary], list[str]]:
    candidates, errors = [], []
    index_url = news_index_url(source_url)

    async with async_playwright() as playwright:
        browser, page = await create_browser_context(playwright)
        try:
            await page.goto(index_url, wait_until="domcontentloaded", timeout=45_000)
            await page.wait_for_timeout(2_000)

            for selector in ("button:has-text('موافق')", "button:has-text('السماح')", "button:has-text('إغلاق')", "button[aria-label='Close']"):
                try:
                    locator = page.locator(selector)
                    if await locator.count():
                        await locator.first.scroll_into_view_if_needed()
                        await locator.first.click(timeout=5_000)
                except Exception:
                    pass

            summaries = await collect_article_cards(page, index_url)
            summaries.sort(key=lambda item: item.published_at or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
            summaries = summaries[:max_articles]

            if not summaries:
                errors.append(f"No article candidates discovered from {index_url}")
            else:
                print(f"DISCOVERY | index={index_url} candidates={len(summaries)}")
                for item in summaries[:15]:
                    print(f"DISCOVERY | {item.published_at} | {item.title[:120]} | {item.url}")

            candidates = summaries
        except Exception as exc:
            errors.append(f"source discovery failed: {exc}")
        finally:
            await browser.close()

    return candidates, errors


async def fetch_source_articles(source_url: str, candidates: list[ArticleSummary]) -> tuple[list[SourceArticle], list[tuple[str, str]]]:
    articles, errors = [], []
    if not candidates:
        return articles, errors

    async with async_playwright() as playwright:
        browser, page = await create_browser_context(playwright)
        try:
            for candidate in candidates:
                try:
                    article = await fetch_article(page, candidate)
                    articles.append(article)
                except Exception as exc:
                    errors.append((candidate.url, str(exc)))
        finally:
            await browser.close()

    return articles, errors


# Main Entry Point
def discover_and_fetch(source_url: str, max_articles: int) -> tuple[list[ArticleSummary], list[str], list[SourceArticle], list[tuple[str, str]]]:
    candidates, discovery_errors = asyncio.run(discover_articles(source_url, max_articles))
    articles, fetch_errors = asyncio.run(fetch_source_articles(news_index_url(source_url), candidates))
    return candidates, discovery_errors, articles, fetch_errors
