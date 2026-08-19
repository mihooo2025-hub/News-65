from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional


@dataclass
class ArticleSummary:
    url: str
    title: str
    relative_time_text: str = ""
    published_at: Optional[datetime] = None


@dataclass
class SourceArticle:
    url: str
    title: str
    text: str
    image_url: str
    published_at: Optional[datetime] = None


@dataclass
class RewrittenArticle:
    title: str
    html: str
    categories: list[str] = field(default_factory=list)


@dataclass
class RunReport:
    created: list[tuple[str, str]] = field(default_factory=list)
    skipped_no_image: int = 0
    skipped_old: int = 0
    duplicate: int = 0
    failed: list[tuple[str, str]] = field(default_factory=list)
