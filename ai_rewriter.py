from __future__ import annotations

import json
import logging
import re
import time

from google import genai

from config import EXCLUDED_CATEGORIES, Settings
from models import RewrittenArticle, SourceArticle

logger = logging.getLogger(__name__)

SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "html": {"type": "string"},
        "categories": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["title", "html", "categories"],
}

PROMPT_TEMPLATE = r"""
أنت محرر رياضي محترف متخصص في أخبار كرة القدم للمواقع الإخبارية العربية.
حوّل الخبر الأصلي الآتي إلى خبر عربي مختصر ومهني وطبيعي وفق القواعد التالية، من دون إضافة أي معلومة غير موجودة في النص الأصلي.

قواعد إعادة الكتابة:
- لا تكتف بتبديل الكلمات بمرادفات؛ أعد بناء الخبر بأسلوب صحفي عربي طبيعي.
- ابدأ مباشرة بأهم معلومة.
- استخدم العربية الفصحى الحديثة.
- اكتب أسماء اللاعبين والأندية باللغة العربية.
- احذف التكرار والخلفيات الطويلة والتفاصيل الثانوية.
- احتفظ بالحدث الرئيسي والأسماء والأرقام والتصريحات الضرورية.
- استهدف عادة 100 إلى 150 كلمة، لكن إذا كان الأصل قصيرًا لا تضف معلومات لزيادة الطول.
- يجب أن تكون النتيجة أقصر بوضوح من المصدر عندما يكون المصدر طويلًا.
- لا تغير درجة التأكيد: الاحتمال يبقى احتمالًا، والمفاوضات لا تصبح اتفاقًا، والرغبة لا تصبح صفقة.
- لا تختلق اقتباسات أو تفاصيل أو أسبابًا غير موجودة في المصدر.
- حافظ على السياق الزمني والحالة الحالية أو السابقة للأشخاص والأندية.
- استخدم من 2 إلى 4 فقرات قصيرة.
- استخدم HTML بسيطًا باستخدام <p> فقط.
- لا تستخدم Markdown.
- لا تستخدم عناوين فرعية.
- لا تضف روابط داخل النص.
- لا تذكر اسم الموقع المصدر إلا إذا كان ضروريًا لفهم المعلومة.

العنوان:
- أنشئ عنوانًا واحدًا فقط من 4 إلى 9 كلمات تقريبًا.
- اجعله جذابًا ودقيقًا وغير مضلل.
- يمكن استخدام التشويق أو السؤال عندما يناسب طبيعة الخبر.
- في الأخبار الرسمية استخدم صياغة مباشرة.
- لا تذكر اسم المصدر.
- يجب أن يكون كل ما يوحي به العنوان مدعومًا داخل النص.

التصنيفات:
- اختر من 0 إلى 2 تصنيفين فقط من القائمة المسموحة أدناه، لأن المشروع يضيف "Uncategorized" تلقائيًا ليصبح الحد الأقصى 3 تصنيفات إجمالًا.
- لا تستخدم التصنيفين المستبعدين مهما كان السبب.
- إذا كان الخبر متعلقًا بشكل أساسي بنادٍ معين فاختر تصنيف النادي إذا كان موجودًا.
- إذا كان متعلقًا بناديين بشكل مباشر فيمكن اختيار الناديين.
- أضف "سوق الانتقالات" فقط عندما يكون الخبر عن صفقة أو انتقال أو مفاوضات أو اهتمام أو رحيل أو تجديد عقد مرتبط بمستقبل لاعب.
- لا تضف تصنيف نادي لمجرد ذكر اسمه عابرًا.
- لا تختر أكثر من 3 تصنيفات.
- لا تختر أي تصنيف خارج القائمة.

القائمة المسموحة:
{allowed_categories}

التصنيفان المستبعدان:
{excluded_categories}

نص الخبر الأصلي:
---
{source_text}
---

أعد JSON مطابقًا للبنية المطلوبة فقط.
"""


class GeminiRewriter:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.client = genai.Client(api_key=settings.gemini_api_key)

    def _get_models_list(self) -> list[str]:
        raw_model = self.settings.gemini_model or "gemini-2.5-flash"
        models = [m.strip() for m in raw_model.split(",") if m.strip()]
        return models if models else ["gemini-2.5-flash"]

    def rewrite(self, article: SourceArticle) -> RewrittenArticle:
        allowed_for_model = [
            c for c in self.settings.categories
            if c not in EXCLUDED_CATEGORIES and c != "Uncategorized"
        ]

        prompt = PROMPT_TEMPLATE.format(
            allowed_categories=json.dumps(allowed_for_model, ensure_ascii=False),
            excluded_categories=json.dumps(sorted(EXCLUDED_CATEGORIES), ensure_ascii=False),
            source_text=article.text,
        )

        models = self._get_models_list()
        response = None
        last_exception = None

        for model_name in models:
            max_retries = 3
            for attempt in range(max_retries):
                try:
                    logger.info("Attempting rewriting with model: %s (attempt %d/%d)", model_name, attempt + 1, max_retries)
                    response = self.client.models.generate_content(
                        model=model_name,
                        contents=prompt,
                        config={
                            "temperature": 0.2,
                            "max_output_tokens": 1800,
                            "response_mime_type": "application/json",
                            "response_json_schema": SCHEMA,
                        },
                    )
                    if response and response.text:
                        break
                except Exception as exc:
                    err_msg = str(exc)
                    last_exception = exc
                    if "429" in err_msg or "RESOURCE_EXHAUSTED" in err_msg:
                        wait_time = 22 + (attempt * 5)
                        logger.warning("Rate limit hit (429) on model %s. Retrying in %d seconds... Error: %s", model_name, wait_time, exc)
                        time.sleep(wait_time)
                    else:
                        logger.warning("Model %s failed with non-rate-limit error: %s", model_name, exc)
                        break

            if response and response.text:
                break

        if not response or not response.text:
            if last_exception:
                raise RuntimeError(f"Gemini generation failed on all models: {last_exception}")
            raise RuntimeError("Gemini returned an empty response")

        try:
            data = json.loads(response.text)
        except json.JSONDecodeError as exc:
            raise RuntimeError("Gemini returned invalid JSON") from exc

        title = str(data.get("title", "")).strip()
        html = str(data.get("html", "")).strip()

        categories = data.get("categories") if isinstance(data.get("categories"), list) else []
        categories = [str(c).strip() for c in categories if isinstance(c, str) and str(c).strip()]
        categories = list(dict.fromkeys(categories))
        categories = [c for c in categories if c in allowed_for_model][:2]

        html = self._sanitize_html(html)
        if not title or not html:
            raise RuntimeError("Gemini output is missing title or body")

        return RewrittenArticle(title=title, html=html, categories=categories)

    @staticmethod
    def _sanitize_html(html: str) -> str:
        html = re.sub(r"```(?:html)?", "", html, flags=re.IGNORECASE).replace("```", "")
        html = re.sub(r"<\s*/?\s*body\b[^>]*>", "", html, flags=re.IGNORECASE)
        html = re.sub(r"<\s*/?\s*html\b[^>]*>", "", html, flags=re.IGNORECASE)

        paragraphs = re.findall(r"<p\b[^>]*>.*?</p>", html, flags=re.IGNORECASE | re.DOTALL)
        if paragraphs:
            return "\n".join(p.strip() for p in paragraphs[:4])

        text = re.sub(r"<[^>]+>", " ", html)
        text = re.sub(r"\s+", " ", text).strip()
        return f"<p>{text}</p>" if text else ""
