"""
scraper_365scores.py
====================
Fetches football news from 365Scores Arabic news pages.

The source secret may point to:
https://www.365scores.com/ar

In that case the scraper automatically uses:
https://www.365scores.com/ar/news/magazine/

The scraper:
- Discovers article cards.
- Extracts article URLs and titles.
- Handles Arabic relative and absolute dates.
- Fetches article text.
- Extracts featured images.
- Keeps the original source URL for reporting.
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


ARABIC_DIGITS = str.maketrans(
    "٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹",
    "01234567890123456789",
)


def normalize_url(base_url: str, value: str) -> str:
    value = (value or "").strip()

    if not value:
        return ""

    return urljoin(base_url, value).split("#", 1)[0]


def news_index_url(source_url: str) -> str:
    """
    Use the current Arabic 365Scores news index when the secret points
    to the main Arabic homepage.
    """
    parsed = urlparse(source_url)
    path = parsed.path.rstrip("/")

    if path in {"", "/ar"}:
        return f"{parsed.scheme}://{parsed.netloc}/ar/news/magazine/"

    return source_url


def is_probable_article_url(url: str) -> bool:
    parsed = urlparse(url)

    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return False

    path = parsed.path.lower()

    if any(
        part in path
        for part in (
            "/login",
            "/signup",
            "/matches",
            "/teams",
            "/players",
        )
    ):
        return False

    return (
        "/news/magazine/" in path
        and not path.endswith("/news/magazine/")
    )


def parse_relative_time(
    text: str,
    now: datetime,
) -> datetime | None:

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
        (
            r"(?:قبل|منذ)\s+(\d+)\s*(?:دقيقة|دقائق)",
            "minutes",
        ),
        (
            r"(?:قبل|منذ)\s+(\d+)\s*(?:ساعة|ساعات)",
            "hours",
        ),
        (
            r"(?:قبل|منذ)\s+(\d+)\s*(?:يوم|أيام)",
            "days",
        ),
    ]

    for pattern, unit in patterns:
        match = re.search(pattern, value)

        if not match:
            continue

        amount = int(match.group(1))

        if unit == "minutes":
            return now - timedelta(minutes=amount)

        if unit == "hours":
            return now - timedelta(hours=amount)

        return now - timedelta(days=amount)

    return None


def parse_absolute_time(text: str) -> datetime | None:
    """
    Supports examples such as:

    19/08/2026 - 06:59 م
    19/08/2026 06:59 م
    """

    value = re.sub(
        r"\s+",
        " ",
        text.strip().translate(ARABIC_DIGITS),
    )

    match = re.search(
        r"(\d{1,2})/(\d{1,2})/(\d{4})\s*"
        r"(?:-|–|—)?\s*"
        r"(\d{1,2}):(\d{2})\s*([صم])",
        value,
    )

    if match:
        day, month, year, hour, minute = map(
            int,
            match.groups()[:5],
        )

        meridiem = match.group(6)

        if meridiem == "م" and hour < 12:
            hour += 12

        if meridiem == "ص" and hour == 12:
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

    # ISO timestamps that may appear in visible text.
    match = re.search(
        r"\b(20\d{2}-\d{2}-\d{2}T[^\s<]+)",
        value,
    )

    if match:
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


def parse_any_time(
    text: str,
    now: datetime,
) -> datetime | None:

    return (
        parse_absolute_time(text)
        or parse_relative_time(text, now)
    )


def extract_json_ld(
    soup: BeautifulSoup,
) -> list[dict]:

    items: list[dict] = []

    for script in soup.select(
        'script[type="application/ld+json"]'
    ):
        raw = script.string or script.get_text()

        if not raw:
            continue

        try:
            data = json.loads(raw)

        except Exception:
            continue

        if isinstance(data, dict):
            items.append(data)

        elif isinstance(data, list):
            items.extend(
                item
                for item in data
                if isinstance(item, dict)
            )

    return items


def first_meta(
    soup: BeautifulSoup,
    *names: str,
) -> str:

    for name in names:

        tag = soup.find(
            "meta",
            attrs={
                "property": name,
            },
        )

        if not tag:
            tag = soup.find(
                "meta",
                attrs={
                    "name": name,
                },
            )

        if tag:

            value = (
                tag.get("content")
                or ""
            ).strip()

            if value:
                return value

    return ""


def extract_featured_image(
    soup: BeautifulSoup,
) -> str:

    for key in (
        "og:image",
        "twitter:image",
    ):
        value = first_meta(
            soup,
            key,
        )

        if value:
            return value

    # JSON-LD image
    for item in extract_json_ld(soup):

        image = item.get("image")

        if isinstance(image, str) and image.strip():
            return image.strip()

        if isinstance(image, list) and image:

            for candidate in image:

                if (
                    isinstance(candidate, str)
                    and candidate.strip()
                ):
                    return candidate.strip()

                if (
                    isinstance(candidate, dict)
                    and candidate.get("url")
                ):
                    return str(
                        candidate["url"]
                    ).strip()

        if (
            isinstance(image, dict)
            and image.get("url")
        ):
            return str(
                image["url"]
            ).strip()

    # Fallback to article/main images.
    for img in soup.select(
        "article img, main img"
    ):

        src = (
            img.get("src")
            or img.get("data-src")
            or img.get("data-lazy-src")
        )

        if not src:
            continue

        src_lower = src.lower()

        if any(
            token in src_lower
            for token in (
                "logo",
                "icon",
                "avatar",
                "favicon",
            )
        ):
            continue

        return src

    return ""


def extract_published_at(
    soup: BeautifulSoup,
) -> datetime | None:

    for key in (
        "article:published_time",
        "datePublished",
        "dateCreated",
        "publish_date",
    ):

        value = first_meta(
            soup,
            key,
        )

        if not value:
            continue

        try:
            dt = datetime.fromisoformat(
                value.replace(
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

            parsed = parse_any_time(
                value,
                datetime.now(timezone.utc),
            )

            if parsed:
                return parsed

    for item in extract_json_ld(soup):

        for key in (
            "datePublished",
            "dateCreated",
        ):

            value = item.get(key)

            if not isinstance(value, str):
                continue

            try:
                dt = datetime.fromisoformat(
                    value.replace(
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

                parsed = parse_any_time(
                    value,
                    datetime.now(timezone.utc),
                )

                if parsed:
                    return parsed

    for time_tag in soup.select(
        "time[datetime]"
    ):

        raw = time_tag.get(
            "datetime",
            "",
        ).strip()

        parsed = parse_any_time(
            raw,
            datetime.now(timezone.utc),
        )

        if parsed:
            return parsed

    visible = soup.get_text(
        " ",
        strip=True,
    )

    return parse_any_time(
        visible,
        datetime.now(timezone.utc),
    )


def extract_article_text(
    soup: BeautifulSoup,
) -> str:

    candidates = []

    for selector in (
        "article",
        "main",
        "[class*='article']",
        "[class*='Article']",
        "[class*='content']",
        "[class*='Content']",
    ):

        node = soup.select_one(selector)

        if node:
            text = node.get_text(
                "\n",
                strip=True,
            )

            if text:
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

    lines = []

    for line in text.splitlines():

        line = re.sub(
            r"\s+",
            " ",
            line,
        ).strip()

        if not line:
            continue

        lines.append(line)

    return "\n".join(lines)


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
            """
            els => els.map(a => ({
                href: a.href,
                text: (a.innerText || '').trim(),
                aria: a.getAttribute('aria-label') || ''
            }))
            """
        )

        for item in anchors:

            url = normalize_url(
                source_url,
                str(item.get("href", "")),
            )

            text = re.sub(
                r"\s+",
                " ",
                str(
                    item.get(
                        "text",
                        "",
                    )
                ),
            ).strip()

            aria = re.sub(
                r"\s+",
                " ",
                str(
                    item.get(
                        "aria",
                        "",
                    )
                ),
            ).strip()

            title_text = text or aria

            if (
                not title_text
                or len(title_text) < 12
                or not is_probable_article_url(url)
            ):
                continue

            parsed_time = parse_any_time(
                title_text,
                now,
            )

            # Remove obvious date/time suffixes
            # from the card title.
            clean_title = re.sub(
                r"\s+\d{1,2}/\d{1,2}/\d{4}.*$",
                "",
                title_text,
            ).strip()

            clean_title = re.sub(
                r"\s+(?:قبل|منذ)\s+\d+\s+\S+.*$",
                "",
                clean_title,
            ).strip() or title_text

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

    # Scroll several times because the news page
    # may load more cards dynamically.
    for _ in range(10):

        try:

            await page.mouse.wheel(
                0,
                5000,
            )

            await page.wait_for_timeout(
                900
            )

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

    await page.wait_for_timeout(
        1_500
    )

    html = await page.content()

    soup = BeautifulSoup(
        html,
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

    text = extract_article_text(
        soup
    )

    image_url = extract_featured_image(
        soup
    )

    published_at = (
        extract_published_at(soup)
        or summary.published_at
    )

    return SourceArticle(
        url=summary.url,
        title=title.strip(),
        text=text.strip(),
        image_url=normalize_url(
            summary.url,
            image_url,
        ),
        published_at=published_at,
    )


async def discover_articles(
    source_url: str,
    max_articles: int,
) -> tuple[
    list[ArticleSummary],
    list[str],
]:

    candidates: list[ArticleSummary] = []
    errors: list[str] = []

    index_url = news_index_url(
        source_url
    )

    async with async_playwright() as playwright:

        browser = await playwright.chromium.launch(
            headless=True
        )

        try:

            page = await browser.new_page(
                locale="ar-SA",
                user_agent=(
                    "Mozilla/5.0 "
                    "(X11; Linux x86_64) "
                    "AppleWebKit/537.36 "
                    "Chrome/138 Safari/537.36"
                ),
                timezone_id="Asia/Riyadh",
            )

            try:

                await page.goto(
                    index_url,
                    wait_until="domcontentloaded",
                    timeout=45_000,
                )

                await page.wait_for_timeout(
                    2_000
                )

                # Try to close common consent/dialogs.
                for selector in (
                    "button:has-text('موافق')",
                    "button:has-text('السماح')",
                    "button:has-text('إغلاق')",
                    "button[aria-label='Close']",
                ):

                    try:

                        locator = page.locator(
                            selector
                        )

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
                    key=lambda item:
                        item.published_at
                        or datetime.min.replace(
                            tzinfo=timezone.utc
                        ),
                    reverse=True,
                )

                summaries = summaries[
                    :max_articles
                ]

                if not summaries:

                    errors.append(
                        "No article candidates discovered "
                        f"from {index_url}"
                    )

                else:

                    print(
                        "DISCOVERY | "
                        f"index={index_url} "
                        f"candidates={len(summaries)}"
                    )

                    for item in summaries[:15]:

                        print(
                            "DISCOVERY | "
                            f"{item.published_at} | "
                            f"{item.title[:120]} | "
                            f"{item.url}"
                        )

            except Exception as exc:

                errors.append(
                    f"source discovery failed: {exc}"
                )

        finally:

            await browser.close()

    return candidates or summaries, errors


async def fetch_source_articles(
    source_url: str,
    candidates: list[ArticleSummary],
) -> tuple[
    list[SourceArticle],
    list[tuple[str, str]],
]:

    articles: list[SourceArticle] = []
    errors: list[tuple[str, str]] = []

    if not candidates:
        return articles, errors

    async with async_playwright() as playwright:

        browser = await playwright.chromium.launch(
            headless=True
        )

        try:

            page = await browser.new_page(
                locale="ar-SA",
                user_agent=(
                    "Mozilla/5.0 "
                    "(X11; Linux x86_64) "
                    "AppleWebKit/537.36 "
                    "Chrome/138 Safari/537.36"
                ),
                timezone_id="Asia/Riyadh",
            )

            for candidate in candidates:

                try:

                    article = await fetch_article(
                        page,
                        candidate,
                    )

                    articles.append(
                        article
                    )

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
