"""
إرسال تقرير نهاية كل دورة إلى قناة تلجرام عبر بوت.
"""

import requests

from config import REQUEST_TIMEOUT_SEC


def send_report(bot_token: str, chat_id: str, published_titles, failed_count: int, skipped_no_image: int):
    lines = ["📊 تقرير دورة نبض الملاعب"]

    if published_titles:
        lines.append(f"\n✅ تم نشر {len(published_titles)} خبر:")
        for i, title in enumerate(published_titles, 1):
            lines.append(f"{i}. {title}")
    else:
        lines.append("\n— لا توجد أخبار جديدة تم نشرها في هذه الدورة.")

    if skipped_no_image:
        lines.append(f"\n🖼️ تم تجاهل {skipped_no_image} خبر بسبب عدم توفر صورة بارزة.")
    if failed_count:
        lines.append(f"\n⚠️ فشلت معالجة {failed_count} خبر (سيُعاد المحاولة في الدورة القادمة).")

    text = "\n".join(lines)

    try:
        requests.post(
            f"https://api.telegram.org/bot{bot_token}/sendMessage",
            data={"chat_id": chat_id, "text": text},
            timeout=REQUEST_TIMEOUT_SEC,
        )
    except requests.RequestException:
        # لا نوقف السكريبت إذا فشل إرسال التقرير فقط
        pass
