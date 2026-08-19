from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

DEFAULT_STATE = {"processed": {}, "skipped_no_image": {}}


def source_id(url: str) -> str:
    canonical = url.strip()
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:24]


def load_state(path: str) -> dict[str, Any]:
    target = Path(path)
    if not target.exists():
        return json.loads(json.dumps(DEFAULT_STATE))
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return json.loads(json.dumps(DEFAULT_STATE))
    if not isinstance(data, dict):
        return json.loads(json.dumps(DEFAULT_STATE))
    data.setdefault("processed", {})
    data.setdefault("skipped_no_image", {})
    return data


def save_state(path: str, state: dict[str, Any]) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(target.suffix + ".tmp")
    tmp.write_text(
        json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    tmp.replace(target)


def mark_processed(state: dict[str, Any], article_id: str, post_id: int) -> None:
    state["processed"][article_id] = {
        "processed_at": datetime.now(timezone.utc).isoformat(),
        "wp_post_id": post_id,
    }


def mark_no_image(state: dict[str, Any], article_id: str) -> None:
    state["skipped_no_image"][article_id] = {
        "skipped_at": datetime.now(timezone.utc).isoformat(),
    }
