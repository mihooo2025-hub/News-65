"""
النشر إلى ووردبريس عبر REST API (مسودة)، مع رفع الصورة البارزة وربط التصنيفات.
"""

import requests
from requests.auth import HTTPBasicAuth

from config import (
    ALWAYS_INCLUDE_CATEGORY,
    USER_AGENT,
    REQUEST_TIMEOUT_SEC,
)


class WordPressClient:
    def __init__(self, base_url: str, username: str, app_password: str):
        self.base_url = base_url.rstrip("/")
        self.auth = HTTPBasicAuth(username, app_password)
        self._category_cache = {}

    # ---------- تصنيفات ----------
    def get_category_id(self, name: str):
        if name in self._category_cache:
            return self._category_cache[name]

        resp = requests.get(
            f"{self.base_url}/wp-json/wp/v2/categories",
            params={"search": name, "per_page": 20},
            auth=self.auth,
            timeout=REQUEST_TIMEOUT_SEC,
        )
        resp.raise_for_status()
        for cat in resp.json():
            if cat["name"].strip() == name.strip():
                self._category_cache[name] = cat["id"]
                return cat["id"]
        # لا يوجد إنشاء تصنيفات جديدة أبدًا - إذا لم يوجد، يتم تجاهله فقط
        return None

    def resolve_category_ids(self, category_names):
        names = list(category_names) + [ALWAYS_INCLUDE_CATEGORY]
        ids = []
        for name in names:
            cid = self.get_category_id(name)
            if cid and cid not in ids:
                ids.append(cid)
        return ids

    # ---------- صورة بارزة ----------
    def upload_featured_image(self, image_url: str, filename: str):
        """
        يحمّل الصورة من الرابط الأصلي (مع تمرير هيدرز لتفادي حماية hotlink 403)
        ثم يرفعها إلى مكتبة وسائط ووردبريس.
        يُعيد media_id أو None إن فشل الرفع.
        """
        try:
            img_resp = requests.get(
                image_url,
                headers={"User-Agent": USER_AGENT, "Referer": image_url},
                timeout=REQUEST_TIMEOUT_SEC,
            )
            img_resp.raise_for_status()
        except requests.RequestException:
            return None

        content_type = img_resp.headers.get("Content-Type", "image/jpeg")
        safe_name = filename if filename.endswith((".jpg", ".jpeg", ".png", ".webp")) else filename + ".jpg"

        media_resp = requests.post(
            f"{self.base_url}/wp-json/wp/v2/media",
            headers={
                "Content-Disposition": f'attachment; filename="{safe_name}"',
                "Content-Type": content_type,
            },
            data=img_resp.content,
            auth=self.auth,
            timeout=REQUEST_TIMEOUT_SEC,
        )
        if media_resp.status_code not in (200, 201):
            return None
        return media_resp.json().get("id")

    # ---------- نشر المقال ----------
    def create_draft_post(self, title: str, content_html: str, category_ids, featured_media_id=None):
        payload = {
            "title": title,
            "content": content_html,
            "status": "draft",
            "categories": category_ids,
        }
        if featured_media_id:
            payload["featured_media"] = featured_media_id

        resp = requests.post(
            f"{self.base_url}/wp-json/wp/v2/posts",
            json=payload,
            auth=self.auth,
            timeout=REQUEST_TIMEOUT_SEC,
        )
        resp.raise_for_status()
        return resp.json()
