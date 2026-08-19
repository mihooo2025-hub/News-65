from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone

from ai_rewriter import GeminiRewriter
from config import FORCED_CATEGORY, Settings, load_settings
from models import RunReport
from scraper_365scores import discover_and_fetch
from state_store import load_state, mark_no_image, mark_processed, save_state, source_id
from telegram_reporter import TelegramReporter
from wordpress_publisher import WordPressPublisher

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)
logger = logging.getLogger(__name__)


def run(settings: Settings) -> RunReport:
    report = RunReport()
    state = load_state(settings.state_file)

    logger.info("Starting source discovery")
    candidates, discovery_errors, articles, fetch_errors = discover_and_fetch(
        settings.source_url,
        settings.max_articles_per_run,
    )
    for error in discovery_errors:
        report.failed.append((settings.source_url, error))

    cutoff = datetime.now(timezone.utc) - timedelta(hours=settings.lookback_hours)
    candidate_ids = {source_id(item.url) for item in candidates}

    filtered_articles = []
    for article in articles:
        article_id = source_id(article.url)
        if article_id in state["processed"] or article_id in state["skipped_no_image"]:
            report.duplicate += 1
            continue
        if article.published_at and article.published_at < cutoff:
            report.skipped_old += 1
            continue
        if not article.image_url:
            mark_no_image(state, article_id)
            report.skipped_no_image += 1
            continue
        if not article.text or len(article.text) < 80:
            report.failed.append((article.url, "Source article text is too short or empty"))
            continue
        filtered_articles.append(article)

    for url, error in fetch_errors:
        if source_id(url) in candidate_ids:
            report.failed.append((url, error))

    publisher = WordPressPublisher(settings)
    rewriter = GeminiRewriter(settings)

    for article in filtered_articles:
        article_id = source_id(article.url)
        slug = f"auto-365scores-{article_id}"
        try:
            logger.info("Rewriting: %s", article.title)
            rewritten = rewriter.rewrite(article)
            time.sleep(settings.rewrite_delay_seconds)

            # create_draft includes the slug-based duplicate guard.
            post_id, created = publisher.create_draft(article, rewritten, slug)
            if not created:
                mark_processed(state, article_id, post_id)
                report.duplicate += 1
                continue

            mark_processed(state, article_id, post_id)
            save_state(settings.state_file, state)
            report.created.append((rewritten.title, article.url))
            time.sleep(settings.publish_delay_seconds)
            logger.info("Draft created: %s (%s)", rewritten.title, post_id)
        except Exception as exc:
            logger.exception("Failed processing %s", article.url)
            report.failed.append((article.url, str(exc)))
            save_state(settings.state_file, state)

    save_state(settings.state_file, state)
    return report


def main() -> int:
    settings = load_settings()
    report = run(settings)

    logger.info(
        "Done | created=%s old=%s duplicate=%s no_image=%s failed=%s",
        len(report.created), report.skipped_old, report.duplicate, report.skipped_no_image, len(report.failed),
    )

    try:
        TelegramReporter(settings).send_report(report.created, report.failed)
    except Exception:
        logger.exception("Telegram report failed")
        return 2 if report.created else 0

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
