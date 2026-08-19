from __future__ import annotations

import json
import os
from dataclasses import dataclass

DEFAULT_CATEGORIES = [
    "مقالات وتحليلات",
    "اهم الاخبار",
    "سوق الانتقالات",
    "ريال مدريد",
    "برشلونة",
    "ليفربول",
    "مانشستر يونايتد",
    "مانشستر سيتي",
    "تشيلسي",
    "ارسنال",
    "بايرن ميونخ",
    "باريس سان جيرمان",
    "ميلان",
    "يوفنتوس",
    "انتر ميلان",
    "بوروسيا دورتموند",
    "اتليتكو مدريد",
    "Uncategorized",
]

EXCLUDED_CATEGORIES = {"اهم الاخبار", "مقالات وتحليلات"}
FORCED_CATEGORY = "Uncategorized"


@dataclass(frozen=True)
class Settings:
    source_url: str
    gemini_api_key: str
    gemini_model: str
    wp_base_url: str
    wp_username: str
    wp_app_password: str
    telegram_bot_token: str
    telegram_chat_id: str
    lookback_hours: int = 6
    rewrite_delay_seconds: float = 5.0
    publish_delay_seconds: float = 5.0
    request_timeout_seconds: int = 35
    max_articles_per_run: int = 100
    state_file: str = "data/state.json"
    user_agent: str = "Mozilla/5.0 (compatible; FootballNewsBot/1.0)"
    categories: tuple[str, ...] = tuple(DEFAULT_CATEGORIES)


def _required(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def load_settings() -> Settings:
    raw_categories = os.getenv("ALLOWED_CATEGORIES_JSON", "").strip()
    if raw_categories:
        try:
            categories = tuple(json.loads(raw_categories))
            if not all(isinstance(item, str) and item.strip() for item in categories):
                raise ValueError
        except (json.JSONDecodeError, ValueError) as exc:
            raise RuntimeError("ALLOWED_CATEGORIES_JSON must be a JSON array of strings") from exc
    else:
        categories = tuple(DEFAULT_CATEGORIES)

    if FORCED_CATEGORY not in categories:
        categories = (*categories, FORCED_CATEGORY)

    return Settings(
        source_url=_required("SOURCE_URL"),
        gemini_api_key=_required("GEMINI_API_KEY"),
        gemini_model=os.getenv("GEMINI_MODEL", "gemini-3.1-flash-lite").strip(),
        wp_base_url=_required("WP_BASE_URL").rstrip("/"),
        wp_username=_required("WP_USERNAME"),
        wp_app_password=_required("WP_APP_PASSWORD"),
        telegram_bot_token=_required("TELEGRAM_BOT_TOKEN"),
        telegram_chat_id=_required("TELEGRAM_CHAT_ID"),
        lookback_hours=int(os.getenv("LOOKBACK_HOURS", "6")),
        rewrite_delay_seconds=float(os.getenv("REWRITE_DELAY_SECONDS", "5")),
        publish_delay_seconds=float(os.getenv("PUBLISH_DELAY_SECONDS", "5")),
        request_timeout_seconds=int(os.getenv("REQUEST_TIMEOUT_SECONDS", "35")),
        max_articles_per_run=int(os.getenv("MAX_ARTICLES_PER_RUN", "100")),
        state_file=os.getenv("STATE_FILE", "data/state.json").strip(),
        categories=categories,
    )
