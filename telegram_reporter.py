from __future__ import annotations

import requests

from config import Settings


def _escape(value: str) -> str:
    return value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


class TelegramReporter:
    def __init__(self, settings: Settings):
        self.base_url = f"https://api.telegram.org/bot{settings.telegram_bot_token}"
        self.chat_id = settings.telegram_chat_id
        self.timeout = settings.request_timeout_seconds

    def send_report(self, created: list[tuple[str, str]], failures: list[tuple[str, str]]) -> None:
        if not created and not failures:
            return
        lines = ["<b>تقرير نشر الأخبار</b>", ""]
        if created:
            lines.append(f"<b>الأخبار الجديدة: {len(created)}</b>")
            for title, source_url in created:
                lines.append(f"• {_escape(title)}")
                lines.append(f"<a href=\"{source_url}\">رابط الخبر الأصلي</a>")
                lines.append("")
        else:
            lines.append("لم تتم إضافة أخبار جديدة في هذه الدورة.")

        if failures:
            lines.append(f"<b>الأخطاء القابلة لإعادة المحاولة: {len(failures)}</b>")
            for url, error in failures[:10]:
                lines.append(f"• {_escape(url)} — {_escape(error[:180])}")

        message = "\n".join(lines)
        response = requests.post(
            f"{self.base_url}/sendMessage",
            json={"chat_id": self.chat_id, "text": message, "parse_mode": "HTML", "disable_web_page_preview": True},
            timeout=self.timeout,
        )
        response.raise_for_status()
