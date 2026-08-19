from __future__ import annotations

import asyncio
import json
import re
from datetime import datetime, timedelta, timezone
from typing import Iterable
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup
from playwright.async_api import Browser, Page, TimeoutError as PlaywrightTimeoutError, async_playwright

from models import ArticleSummary, SourceArticle

ARABIC_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹", "01234567890123456789")


def normalize_url(base: str, href: str) -> str:
    value = urljoin(base, href).split("#", 1)[0].strip()
    return value


def is_probable_article_url(url: str) -> bool:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        return False
    path = parsed.path.lower()
    if not parsed.netloc:
        return False
    if any(part in path for part in ("/login", "/signup", "/matches", "/teams", "/players")):
        return False
    return "/news" in path or "/article" in path


def parse_relative_time(text: str, now: datetime) -> datetime | None:
    value = text.strip().translate(ARABIC_DIGITS)
    value = re.sub(r"\s+", " ", value)
    if not value:
        return None
    if "الآن" in value or "قبل 0 دقيقة" in value:
        return now

    patterns = [
        (r"قبل\s+(\d+)\s*دقيقة", "minutes"),
        (r"قبل\s+(\d+)\s*دقائق", "minutes"),
        (r"قبل\s+(\d+)\s*دقائق", "minutes"),
        (r"قبل\s+(\d+)\s*ساعة", "hours"),
        (r"قبل\s+(\d+)\s*ساعات", "hours"),
        (r"منذ\s+(\d+)\s*دقيقة", "minutes"),
        (r"منذ\s+(\d+)\s*ساعة", "hours"),
    ]
    for pattern, unit in patterns:
        match = re.search(pattern, value)
        if match:
            amount = int(match.group(1))
            delta = timedelta(minutes=amount) if unit == "minutes" else timedelta(hours=amount)
            return now - delta
    return None


def extract_json_ld(soup: BeautifulSoup) -> list[dict]:
    items: list[dict] = []
    for script in soup.select('script[type="application/ld+json"]'):
        raw = script.string or script.get_text()
        if not raw.strip():
            continue
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict):
            items.append(data)
        elif isinstance(data, list):
            items.extend(item for item in data if isinstance(item, dict))
    return items


def first_meta(soup: BeautifulSoup, *keys: str) -> str:
    for key in keys:
        tag = soup.select_one(f'meta[property="{key}"]') or soup.select_one(f'meta[name="{key}"]')
        if tag and tag.get("content"):
            return tag["content"].strip()
    return ""


def extract_article_text(soup: BeautifulSoup) -> str:
    selectors = [
        "article",
        "[data-testid*=article]",
        "[class*=article-body]",
        "[class*=articleBody]",
        "[class*=news-body]",
        "main",
    ]
    for selector in selectors:
        container = soup.select_one(selector)
        if not container:
            continue
        paragraphs = [p.get_text(" ", strip=True) for p in container.select("p")]
        paragraphs = [p for p in paragraphs if len(p) > 25]
        if len(paragraphs) >= 2:
            return "\n\n".join(paragraphs)

    paragraphs = [p.get_text(" ", strip=True) for p in soup.select("p")]
    paragraphs = [p for p in paragraphs if len(p) > 25]
    return "\n\n".join(paragraphs[:50])


def extract_featured_image(soup: BeautifulSoup) -> str:
    for keys in (
        ("og:image",),
        ("twitter:image",),
    ):
        value = first_meta(soup, *keys)
        if value:
            return value

    for img in soup.select("article img, main img"):
        src = img.get("src") or img.get("data-src") or img.get("data-lazy-src")
        if not src:
            continue
        width = img.get("width")
        height = img.get("height")
        try:
            if width and int(width) < 250:
                continue
            if height and int(height) < 150:
                continue
        except ValueError:
            pass
        src_lower = src.lower()
        if any(token in src_lower for token in ("logo", "icon", "avatar", "favicon")):
            continue
        return src
    return ""


def extract_published_at(soup: BeautifulSoup) -> datetime | None:
    value = first_meta(soup, "article:published_time", "datePublished", "pubdate")
    if value:
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except ValueError:
            pass

    for item in extract_json_ld(soup):
        for key in ("datePublished", "dateCreated"):
            value = item.get(key)
            if isinstance(value, str):
                try:
                    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
                    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
                except ValueError:
                    continue

    time_tag = soup.find("time", datetime=True)
    if time_tag:
        raw = time_tag.get("datetime", "").strip()
        try:
            dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    return None


async def collect_article_cards(page: Page, source_url: str) -> list[ArticleSummary]:
    now = datetime.now(timezone.utc)
    results: dict[str, ArticleSummary] = {}

    async def collect_current_dom() -> None:
        anchors = await page.locator("a[href]").evaluate_all(
            """els => els.map(a => ({href: a.href, text: (a.innerText || '').trim()}))"""
        )
        for item in anchors:
            url = normalize_url(source_url, str(item.get("href", "")))
            text = re.sub(r"\s+", " ", str(item.get("text", ""))).strip()
            if not text or len(text) < 12 or not is_probable_article_url(url):
                continue
            if url.rstrip("/") == source_url.rstrip("/"):
                continue

            relative_time = ""
            for line in text.splitlines():
                if "قبل" in line or "منذ" in line:
                    relative_time = line.strip()
                    break
            parsed_time = parse_relative_time(relative_time, now)
            current = results.get(url)
            if current is None or len(text) > len(current.title):
                results[url] = ArticleSummary(
                    url=url,
                    title=text,
                    relative_time_text=relative_time,
                    published_at=parsed_time,
                )

    await collect_current_dom()

    for _ in range(7):
        try:
            await page.mouse.wheel(0, 4000)
            await page.wait_for_timeout(900)
        except Exception:
            break
        await collect_current_dom()

    return list(results.values())


async def fetch_article(page: Page, summary: ArticleSummary) -> SourceArticle:
    await page.goto(summary.url, wait_until="domcontentloaded", timeout=60_000)
    try:
        await page.wait_for_load_state("networkidle", timeout=15_000)
    except PlaywrightTimeoutError:
        pass
    html = await page.content()
    soup = BeautifulSoup(html, "html.parser")

    title = first_meta(soup, "og:title", "twitter:title") or (soup.find("h1").get_text(" ", strip=True) if soup.find("h1") else summary.title)
    text = extract_article_text(soup)
    image_url = extract_featured_image(soup)
    published_at = extract_published_at(soup) or summary.published_at
    return SourceArticle(
        url=summary.url,
        title=title.strip(),
        text=text.strip(),
        image_url=normalize_url(summary.url, image_url) if image_url else "",
        published_at=published_at,
    )


async def discover_articles(source_url: str, max_articles: int) -> tuple[list[ArticleSummary], list[str]]:
    errors: list[str] = []
    summaries: list[ArticleSummary] = []

    async with async_playwright() as playwright:
        browser: Browser = await playwright.chromium.launch(headless=True)
        try:
            page = await browser.new_page(
                locale="ar-SA",
                user_agent="Mozilla/5.0 (compatible; FootballNewsBot/1.0)",
                viewport={"width": 1440, "height": 2200},
            )
            try:
                await page.goto(source_url, wait_until="domcontentloaded", timeout=60_000)
                try:
                    await page.wait_for_load_state("networkidle", timeout=20_000)
                except PlaywrightTimeoutError:
                    pass

                for label in ("المزيد من أخبار الرياضة", "أخبار كرة قدم"):
                    locator = page.get_by_text(label, exact=True)
                    if await locator.count():
                        try:
                            await locator.first.scroll_into_view_if_needed()
                            await locator.first.click(timeout=5_000)
                            await page.wait_for_timeout(1_500)
                            break
                        except Exception:
                            pass

                summaries = await collect_article_cards(page, source_url)
                summaries.sort(key=lambda item: item.published_at or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
                summaries = summaries[:max_articles]
            except Exception as exc:
                errors.append(f"source discovery failed: {exc}")
        finally:
            await browser.close()
    return summaries, errors


async def fetch_source_articles(source_url: str, candidates: list[ArticleSummary]) -> tuple[list[SourceArticle], list[tuple[str, str]]]:
    articles: list[SourceArticle] = []
    errors: list[tuple[str, str]] = []
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        try:
            page = await browser.new_page(locale="ar-SA", user_agent="Mozilla/5.0 (compatible; FootballNewsBot/1.0)")
            for candidate in candidates:
                try:
                    article = await fetch_article(page, candidate)
                    articles.append(article)
                except Exception as exc:
                    errors.append((candidate.url, str(exc)))
        finally:
            await browser.close()
    return articles, errors


def discover_and_fetch(source_url: str, max_articles: int) -> tuple[list[ArticleSummary], list[str], list[SourceArticle], list[tuple[str, str]]]:
    candidates, discovery_errors = asyncio.run(discover_articles(source_url, max_articles))
    articles, fetch_errors = asyncio.run(fetch_source_articles(source_url, candidates))
    return candidates, discovery_errors, articles, fetch_errors
