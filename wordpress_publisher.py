from __future__ import annotations

import mimetypes
import re
from urllib.parse import urlparse

import requests

from config import EXCLUDED_CATEGORIES, FORCED_CATEGORY, Settings
from models import RewrittenArticle, SourceArticle


class WordPressPublisher:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.api_base = f"{settings.wp_base_url}/wp-json/wp/v2"
        self.session = requests.Session()
        self.session.auth = (settings.wp_username, settings.wp_app_password)
        self.session.headers.update({"User-Agent": settings.user_agent})
        self.category_cache: dict[str, int] | None = None

    def _request(self, method: str, url: str, **kwargs) -> requests.Response:
        response = self.session.request(method, url, timeout=self.settings.request_timeout_seconds, **kwargs)
        if not response.ok:
            raise RuntimeError(f"WordPress {method} {url} -> {response.status_code}: {response.text[:500]}")
        return response

    def load_categories(self) -> dict[str, int]:
        if self.category_cache is not None:
            return self.category_cache
        mapping: dict[str, int] = {}
        page = 1
        while True:
            response = self._request(
                "GET",
                f"{self.api_base}/categories",
                params={"per_page": 100, "page": page, "hide_empty": "false"},
            )
            items = response.json()
            if not items:
                break
            for item in items:
                name = str(item.get("name", "")).strip()
                if name:
                    mapping[name.casefold()] = int(item["id"])
            total_pages = int(response.headers.get("X-WP-TotalPages", str(page)))
            if page >= total_pages:
                break
            page += 1
        self.category_cache = mapping
        return mapping

    def resolve_category_ids(self, category_names: list[str]) -> list[int]:
        mapping = self.load_categories()
        ids: list[int] = []
        for name in category_names[:2]:
            if name in EXCLUDED_CATEGORIES:
                continue
            category_id = mapping.get(name.casefold())
            if category_id is not None and category_id not in ids:
                ids.append(category_id)
            if len(ids) == 3:
                break

        forced_id = mapping.get(FORCED_CATEGORY.casefold())
        if forced_id is None:
            raise RuntimeError(f'Forced WordPress category "{FORCED_CATEGORY}" was not found. No category creation is allowed.')
        if forced_id not in ids:
            ids.append(forced_id)
        return ids

    def _existing_by_slug(self, slug: str) -> dict | None:
        response = self._request(
            "GET",
            f"{self.api_base}/posts",
            params={"slug": slug, "status": "any", "context": "edit", "per_page": 5},
        )
        items = response.json()
        return items[0] if items else None

    def upload_media(self, image_url: str, title: str) -> int:
        response = requests.get(
            image_url,
            headers={"User-Agent": self.settings.user_agent, "Referer": self.settings.wp_base_url},
            timeout=self.settings.request_timeout_seconds,
        )
        if not response.ok or not response.content:
            raise RuntimeError(f"Featured image download failed: {response.status_code}")
        content_type = response.headers.get("Content-Type", "").split(";", 1)[0].strip()
        if not content_type.startswith("image/"):
            guessed = mimetypes.guess_type(urlparse(image_url).path)[0]
            content_type = guessed or "image/jpeg"
        ext = mimetypes.guess_extension(content_type) or ".jpg"
        filename = re.sub(r"[^\w\-]+", "-", title, flags=re.UNICODE).strip("-")[:80] + ext
        result = self.session.post(
            f"{self.api_base}/media",
            files={"file": (filename, response.content, content_type)},
            data={"title": title, "alt_text": title},
            timeout=self.settings.request_timeout_seconds,
        )
        if not result.ok:
            raise RuntimeError(f"WordPress media upload failed: {result.status_code}: {result.text[:500]}")
        return int(result.json()["id"])

    def create_draft(self, source: SourceArticle, rewritten: RewrittenArticle, slug: str) -> tuple[int, bool]:
        existing = self._existing_by_slug(slug)
        if existing:
            return int(existing["id"]), False

        media_id = self.upload_media(source.image_url, rewritten.title)
        category_ids = self.resolve_category_ids(rewritten.categories)
        payload = {
            "status": "draft",
            "title": rewritten.title,
            "content": rewritten.html,
            "slug": slug,
            "featured_media": media_id,
            "categories": category_ids,
        }
        response = self._request("POST", f"{self.api_base}/posts", json=payload)
        post = response.json()
        post_id = int(post["id"])
        return post_id, True
