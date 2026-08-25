"""
منع تكرار نشر نفس الخبر.
يعتمد على: رابط الخبر الأصلي (مطابقة تامة) + تشابه العنوان (fuzzy) كخط دفاع ثانٍ.
لا يتم تسجيل الخبر كـ"منشور" إلا بعد نجاح النشر فعليًا في ووردبريس،
حتى تتم إعادة محاولة الأخبار الفاشلة تلقائيًا في الدورة التالية.
"""

import json
import os
import difflib
from datetime import datetime, timezone

from config import SEEN_STATE_FILE, DEDUP_TITLE_SIMILARITY_THRESHOLD


def load_seen() -> dict:
    if not os.path.exists(SEEN_STATE_FILE):
        return {"articles": []}
    try:
        with open(SEEN_STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, FileNotFoundError):
        return {"articles": []}


def save_seen(state: dict) -> None:
    os.makedirs(os.path.dirname(SEEN_STATE_FILE), exist_ok=True)
    with open(SEEN_STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def is_duplicate(state: dict, url: str, original_title: str) -> bool:
    for item in state.get("articles", []):
        if item.get("url") == url:
            return True
        existing_title = item.get("original_title", "")
        if existing_title:
            ratio = difflib.SequenceMatcher(None, existing_title, original_title).ratio()
            if ratio >= DEDUP_TITLE_SIMILARITY_THRESHOLD:
                return True
    return False


def mark_published(state: dict, url: str, original_title: str, new_title: str) -> None:
    state.setdefault("articles", []).append(
        {
            "url": url,
            "original_title": original_title,
            "published_title": new_title,
            "published_at": datetime.now(timezone.utc).isoformat(),
        }
    )
