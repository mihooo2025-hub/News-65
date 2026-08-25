"""
نقطة التشغيل الرئيسية. يُشغَّل تلقائيًا كل ساعة عبر GitHub Actions.

المتغيرات السرية المطلوبة (تُضاف في: Settings → Secrets and variables → Actions):
  WP_URL              رابط موقع ووردبريس (مثال: https://example.com)
  WP_USERNAME         اسم مستخدم ووردبريس
  WP_APP_PASSWORD     كلمة مرور التطبيقات (Application Password)
  GEMINI_API_KEY_1    مفتاح Google AI Studio الأول
  GEMINI_API_KEY_2    مفتاح Google AI Studio الثاني (احتياطي)
  TELEGRAM_BOT_TOKEN  توكن بوت تلجرام
  TELEGRAM_CHAT_ID    معرف القناة/الشخص المستقبل للتقرير
"""

import os
import sys
import time

import scraper
import rewriter
import dedup
from wordpress_client import WordPressClient
import telegram_notify
from config import DELAY_BETWEEN_REWRITES_SEC, DELAY_BETWEEN_PUBLISHES_SEC


def env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        print(f"[تحذير] المتغير البيئي {name} غير موجود.")
    return value


def main():
    wp_url = env("WP_URL")
    wp_user = env("WP_USERNAME")
    wp_pass = env("WP_APP_PASSWORD")
    gemini_key_1 = env("GEMINI_API_KEY_1")
    gemini_key_2 = env("GEMINI_API_KEY_2")
    telegram_token = env("TELEGRAM_BOT_TOKEN")
    telegram_chat = env("TELEGRAM_CHAT_ID")

    if not (wp_url and wp_user and wp_pass and gemini_key_1):
        print("[خطأ] أسرار أساسية ناقصة. إيقاف التنفيذ.")
        sys.exit(1)

    wp = WordPressClient(wp_url, wp_user, wp_pass)
    state = dedup.load_seen()

    print("جاري جلب قائمة الأخبار من 365scores...")
    try:
        articles = scraper.get_recent_articles()
    except Exception as e:  # noqa: BLE001
        print(f"[خطأ] فشل جلب قائمة الأخبار: {e}")
        articles = []

    print(f"عدد الأخبار المكتشفة: {len(articles)}")

    published_titles = []
    failed_count = 0
    skipped_no_image = 0

    for art in articles:
        url = art["url"]
        original_title = art["title"]

        if dedup.is_duplicate(state, url, original_title):
            continue

        try:
            detail = scraper.fetch_article_detail(url)
        except Exception as e:  # noqa: BLE001
            print(f"[خطأ] فشل جلب تفاصيل الخبر ({url}): {e}")
            failed_count += 1
            continue

        if not detail:
            failed_count += 1
            continue

        if not detail.get("image_url"):
            print(f"[تجاهل] لا توجد صورة بارزة: {detail.get('title')}")
            skipped_no_image += 1
            # لا يُسجَّل كمنشور حتى لا يُعاد اعتباره تكرارًا لاحقًا إن ظهرت له صورة
            continue

        try:
            rewritten = rewriter.rewrite_article(
                detail["title"], detail["body_text"], gemini_key_1, gemini_key_2
            )
        except Exception as e:  # noqa: BLE001
            print(f"[خطأ] فشلت إعادة الصياغة ({url}): {e}")
            failed_count += 1
            time.sleep(DELAY_BETWEEN_REWRITES_SEC)
            continue

        time.sleep(DELAY_BETWEEN_REWRITES_SEC)

        media_id = wp.upload_featured_image(
            detail["image_url"], filename=f"{abs(hash(url))}.jpg"
        )
        if not media_id:
            print(f"[تجاهل] تعذّر رفع الصورة البارزة: {rewritten['title']}")
            skipped_no_image += 1
            continue

        category_ids = wp.resolve_category_ids(rewritten["categories"])

        try:
            wp.create_draft_post(
                title=rewritten["title"],
                content_html=rewritten["body_html"],
                category_ids=category_ids,
                featured_media_id=media_id,
            )
        except Exception as e:  # noqa: BLE001
            print(f"[خطأ] فشل النشر في ووردبريس ({url}): {e}")
            failed_count += 1
            continue

        dedup.mark_published(state, url, original_title, rewritten["title"])

        # حفظ عنوان الخبر ورابط المصدر لتضمينهما في تقرير تلجرام
        published_titles.append((rewritten["title"], url))

        print(f"[نُشر] {rewritten['title']}")

        time.sleep(DELAY_BETWEEN_PUBLISHES_SEC)

    dedup.save_seen(state)

    if telegram_token and telegram_chat:
        telegram_notify.send_report(
            telegram_token,
            telegram_chat,
            published_titles,
            failed_count,
            skipped_no_image,
        )

    print(
        f"انتهت الدورة. نُشر: {len(published_titles)} | "
        f"فشل: {failed_count} | بلا صورة: {skipped_no_image}"
    )


if __name__ == "__main__":
    main()
