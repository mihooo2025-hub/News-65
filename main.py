"""
main.py
=======
Main publishing workflow.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone

from ai_rewriter import GeminiRewriter
from config import Settings, load_settings
from models import RunReport
from scraper_365scores import discover_and_fetch
from state_store import (
    load_state,
    mark_processed,
    save_state,
    source_id,
)
from telegram_reporter import TelegramReporter
from wordpress_publisher import WordPressPublisher


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)

logger = logging.getLogger(__name__)


def run(settings: Settings) -> int:
    state = load_state(settings.state_file)
    report = RunReport()

    logger.info("Starting source discovery")

    candidates, discovery_errors, articles, fetch_errors = discover_and_fetch(
        settings.source_url,
        settings.max_articles_per_run,
    )

    for error in discovery_errors:
        report.failed.append((settings.source_url, error))

    for url, error in fetch_errors:
        report.failed.append((url, error))

    cutoff = datetime.now(timezone.utc) - timedelta(hours=settings.lookback_hours)

    logger.info(
        "Discovery returned candidates=%s fetched_articles=%s",
        len(candidates),
        len(articles),
    )

    filtered_articles = []

    for article in articles:
        article_id = source_id(article.url)

        # 1. Check if already processed
        if article_id in state["processed"]:
            report.duplicate += 1
            continue

        # 2. Check for featured image
        if not article.image_url:
            report.skipped_no_image += 1
            logger.info("Skipping article without featured image: %s", article.url)
            continue

        # 3. Check for article text length
        if not article.text or len(article.text) < 80:
            report.failed.append(
                (article.url, "Source article text is too short or empty")
            )
            continue

        # 4. Check lookback window cutoff
        if article.published_at and article.published_at < cutoff:
            report.skipped_old += 1
            continue

        filtered_articles.append(article)

    rewriter = GeminiRewriter(settings)
    publisher = WordPressPublisher(settings)

    for article in filtered_articles:
        article_id = source_id(article.url)

        try:
            logger.info("Processing: %s", article.title)

            rewritten = rewriter.rewrite(article)

            # Delay after AI rewriting
            time.sleep(settings.rewrite_delay_seconds)

            slug = publisher.build_slug(article.url)
            post_id, created = publisher.create_draft(article, rewritten, slug)

            if not created:
                mark_processed(state, article_id, post_id)
                save_state(settings.state_file, state)
                report.duplicate += 1
                continue

            mark_processed(state, article_id, post_id)
            save_state(settings.state_file, state)

            report.created.append((rewritten.title, article.url))
            logger.info("Draft created: %s (%s)", rewritten.title, post_id)

            # Delay after creating WordPress draft
            time.sleep(settings.publish_delay_seconds)

        except Exception as exc:
            logger.exception("Failed processing %s", article.url)
            report.failed.append((article.url, str(exc)))

    logger.info(
        "Done | created=%s old=%s duplicate=%s no_image=%s failed=%s",
        len(report.created),
        report.skipped_old,
        report.duplicate,
        report.skipped_no_image,
        len(report.failed),
    )

    # Always send Telegram report
    try:
        TelegramReporter(settings).send_report(
            report.created,
            report.failed,
            skipped_old=report.skipped_old,
            duplicate=report.duplicate,
            no_image=report.skipped_no_image,
        )
    except Exception:
        logger.exception("Telegram report failed")
        return 2 if report.failed else 0

    return 1 if report.failed and not report.created else 0


if __name__ == "__main__":
    raise SystemExit(run(load_settings()))
