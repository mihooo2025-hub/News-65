"""
telegram_reporter.py
====================
Sends a report to Telegram after every processing cycle.
"""

from __future__ import annotations

import html

import requests

from config import Settings


def _escape(value: str) -> str:
    return html.escape(
        value or "",
        quote=True,
    )


class TelegramReporter:

    def __init__(
        self,
        settings: Settings,
    ) -> None:

        self.base_url = (
            "https://api.telegram.org/bot"
            f"{settings.telegram_bot_token}"
        )

        self.chat_id = (
            settings.telegram_chat_id
        )

        self.timeout = (
            settings.request_timeout_seconds
        )

    def send_report(
        self,
        created: list[tuple[str, str]],
        failures: list[tuple[str, str]],
        skipped_old: int = 0,
        duplicate: int = 0,
        no_image: int = 0,
    ) -> None:

        lines = [
            "<b>تقرير دورة أخبار 365Scores</b>",
            "",
        ]

        lines.append(
            f"<b>المسودات الجديدة:</b> "
            f"{len(created)}"
        )

        lines.append(
            f"<b>أخبار قديمة خارج 6 ساعات:</b> "
            f"{skipped_old}"
        )

        lines.append(
            f"<b>مكررة:</b> "
            f"{duplicate}"
        )

        lines.append(
            f"<b>بدون صورة:</b> "
            f"{no_image}"
        )

        lines.append(
            f"<b>أخطاء قابلة لإعادة المحاولة:</b> "
            f"{len(failures)}"
        )

        lines.append("")

        if created:

            lines.append(
                "<b>الأخبار التي أُضيفت كمسودة:</b>"
            )

            for title, source_url in created:

                lines.append(
                    f"• {_escape(title)}"
                )

                lines.append(
                    f'<a href="{_escape(source_url)}">'
                    "رابط الخبر الأصلي"
                    "</a>"
                )

                lines.append("")

        else:

            lines.append(
                "لم تتم إضافة أي مسودة جديدة "
                "في هذه الدورة."
            )

        if failures:

            lines.append("")
            lines.append(
                "<b>تفاصيل الأخطاء:</b>"
            )

            for url, error in failures[:10]:

                lines.append(
                    f'<a href="{_escape(url)}">'
                    "رابط الخبر الأصلي"
                    "</a>"
                )

                lines.append(
                    "  "
                    f"{_escape(error[:300])}"
                )

        message = "\n".join(
            lines
        )

        response = requests.post(
            f"{self.base_url}/sendMessage",
            json={
                "chat_id": self.chat_id,
                "text": message,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            },
            timeout=self.timeout,
        )

        if not response.ok:

            raise RuntimeError(
                "Telegram sendMessage failed: "
                f"{response.status_code}: "
                f"{response.text[:500]}"
            )
