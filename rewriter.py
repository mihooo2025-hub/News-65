"""
إعادة صياغة الخبر عبر Google AI Studio (Gemini) مجانًا.
يوجد مفتاحان: عند فشل الأول (حصة منتهية / خطأ) يتم التبديل تلقائيًا للثاني.
"""

import json
import os
import re
import requests

from config import (
    ALLOWED_CATEGORIES,
    GEMINI_MODELS,
    GEMINI_API_URL_TEMPLATE,
    REQUEST_TIMEOUT_SEC,
)

RULES_FILE = os.path.join(os.path.dirname(__file__), "rules_ar.md")


def _load_system_prompt() -> str:
    with open(RULES_FILE, "r", encoding="utf-8") as f:
        rules = f.read()
    categories_list = "\n".join(f"- {c}" for c in ALLOWED_CATEGORIES)
    return rules.replace("{{ALLOWED_CATEGORIES}}", categories_list)


SYSTEM_PROMPT = _load_system_prompt()


def _extract_json(text: str) -> dict:
    text = text.strip()
    # إزالة أسوار Markdown إن وُجدت رغم التعليمات
    text = re.sub(r"^```(json)?", "", text.strip())
    text = re.sub(r"```$", "", text.strip())
    return json.loads(text)


def _call_gemini(api_key: str, source_title: str, source_body: str) -> dict:
    last_model_error = None
    
    # التنقل عبر مصفوفة النماذج (الأساسي ثم الاحتياطي)
    for model_name in GEMINI_MODELS:
        url = GEMINI_API_URL_TEMPLATE.format(model=model_name, key=api_key)
        payload = {
            "system_instruction": {"parts": [{"text": SYSTEM_PROMPT}]},
            "contents": [
                {
                    "role": "user",
                    "parts": [
                        {
                            "text": f"عنوان الخبر الأصلي:\n{source_title}\n\nنص الخبر الأصلي:\n{source_body}"
                        }
                    ],
                }
            ],
            "generationConfig": {
                "temperature": 0.4,
                "response_mime_type": "application/json",
            },
        }
        try:
            resp = requests.post(url, json=payload, timeout=REQUEST_TIMEOUT_SEC)

            if resp.status_code == 429:
                raise QuotaExceededError(f"Gemini quota exceeded (status 429): {resp.text[:200]}")
            resp.raise_for_status()

            data = resp.json()
            try:
                raw_text = data["candidates"][0]["content"]["parts"][0]["text"]
            except (KeyError, IndexError) as e:
                raise ValueError(f"استجابة Gemini غير متوقعة: {data}") from e

            return _extract_json(raw_text)

        except Exception as e:
            last_model_error = e
            continue

    raise RuntimeError(f"فشلت كافة نماذج Gemini لهذا المفتاح. آخر خطأ: {last_model_error}")


class QuotaExceededError(Exception):
    pass


def rewrite_article(source_title: str, source_body: str, api_key_1: str, api_key_2: str):
    """
    يحاول المفتاح الأول، وعند فشله (حصة منتهية أو أي خطأ) يبدّل تلقائيًا للمفتاح الثاني.
    يُعيد dict: {title, body_html, categories} أو يرفع استثناء إذا فشل الاثنان.
    """
    last_error = None
    for key in (api_key_1, api_key_2):
        if not key:
            continue
        try:
            result = _call_gemini(key, source_title, source_body)
            if not all(k in result for k in ("title", "body_html", "categories")):
                raise ValueError(f"بنية JSON ناقصة من Gemini: {result}")
            # تصفية أي تصنيف غير مسموح به قد يتسلل رغم التعليمات
            result["categories"] = [c for c in result["categories"] if c in ALLOWED_CATEGORIES][:3]
            return result
        except Exception as e:  # noqa: BLE001 - نريد المتابعة للمفتاح التالي مهما كان الخطأ
            last_error = e
            continue

    raise RuntimeError(f"فشلت إعادة الصياغة بكلا المفتاحين. آخر خطأ: {last_error}")
