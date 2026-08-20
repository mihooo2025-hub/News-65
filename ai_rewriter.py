from __future__ import annotations

import json
import logging
import os
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

RULES_FILE_PATH = os.path.join(os.path.dirname(__file__), "writing_rules.txt")


def load_prompt_template() -> str:
    """يقرأ قالب القواعد من ملف writing_rules.txt الخارجي."""
    if os.path.exists(RULES_FILE_PATH):
        with open(RULES_FILE_PATH, "r", encoding="utf-8") as f:
            return f.read()
    raise FileNotFoundError(f"لم يتم العثور على ملف القواعد: {RULES_FILE_PATH}")


class GeminiRewriter:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.api_keys = self._get_api_keys_list()
        self.current_key_index = 0
        self.client = genai.Client(api_key=self.api_keys[self.current_key_index])

    def _get_api_keys_list(self) -> list[str]:
        raw_keys = getattr(self.settings, "gemini_api_key", "") or ""
        backup_key = getattr(self.settings, "gemini_api_key_backup", "") or ""
        
        keys = [k.strip() for k in raw_keys.split(",") if k.strip()]
        if backup_key and backup_key.strip() not in keys:
            keys.append(backup_key.strip())
            
        return keys if keys else [raw_keys]

    def _switch_to_next_key(self):
        if len(self.api_keys) > 1:
            self.current_key_index = (self.current_key_index + 1) % len(self.api_keys)
            new_key = self.api_keys[self.current_key_index]
            logger.info("Switching to API Key index %d", self.current_key_index)
            self.client = genai.Client(api_key=new_key)

    def _get_models_list(self) -> list[str]:
        raw_model = self.settings.gemini_model or "gemini-2.5-flash"
        models = [m.strip() for m in raw_model.split(",") if m.strip()]
        return models if models else ["gemini-2.5-flash"]

    @staticmethod
    def _clean_and_parse_json(text: str) -> dict:
        """يستخرج ويحلل نص الـ JSON بأمان لمنع استثناءات الأحرف والنصوص المقطوعة."""
        match = re.search(r'```(?:json)?\s*(\{.*\})\s*```', text, re.DOTALL)
        if match:
            text = match.group(1)
        
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            cleaned_text = re.sub(r'[\r\n]+', ' ', text)
            cleaned_text = re.sub(r',(\s*[\}\]])', r'\1', cleaned_text)
            return json.loads(cleaned_text)

    def rewrite(self, article: SourceArticle) -> RewrittenArticle:
        allowed_for_model = [
            c for c in self.settings.categories
            if c not in EXCLUDED_CATEGORIES and c != "Uncategorized"
        ]

        prompt_template = load_prompt_template()
        prompt = prompt_template.format(
            allowed_categories=json.dumps(allowed_for_model, ensure_ascii=False),
            excluded_categories=json.dumps(sorted(EXCLUDED_CATEGORIES), ensure_ascii=False),
            source_text=article.text,
        )

        models = self._get_models_list()
        response = None
        last_exception = None

        for key_attempt in range(len(self.api_keys)):
            for model_name in models:
                max_retries = 2
                for attempt in range(max_retries):
                    try:
                        logger.info(
                            "Attempting rewriting | Model: %s | Key Index: %d | Attempt %d/%d", 
                            model_name, self.current_key_index, attempt + 1, max_retries
                        )
                        response = self.client.models.generate_content(
                            model=model_name,
                            contents=prompt,
                            config={
                                "temperature": 0.2,
                                "max_output_tokens": 4096,
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
                            logger.warning("Rate limit (429) hit on Key %d with model %s.", self.current_key_index, model_name)
                            if len(self.api_keys) > 1 and key_attempt < len(self.api_keys) - 1:
                                self._switch_to_next_key()
                                break
                            
                            wait_time = 10
                            logger.warning("Retrying in %d seconds...", wait_time)
                            time.sleep(wait_time)
                        else:
                            logger.warning("Model %s failed with non-rate-limit error: %s", model_name, exc)
                            if attempt < max_retries - 1:
                                time.sleep(10)

                if response and response.text:
                    break

            if response and response.text:
                break

        if not response or not response.text:
            if last_exception:
                raise RuntimeError(f"Gemini generation failed on all keys and models: {last_exception}")
            raise RuntimeError("Gemini returned an empty response")

        try:
            data = self._clean_and_parse_json(response.text)
        except Exception as exc:
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
