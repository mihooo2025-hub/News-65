"""
إعدادات المشروع العامة.
عدّل هذا الملف فقط لتغيير التصنيفات أو التوقيتات — لا تلمس بقية الملفات.
"""

# ==== مصدر الأخبار ====
SOURCE_NEWS_LIST_URL = "https://www.365scores.com/ar/news"
SOURCE_BASE_URL = "https://www.365scores.com"

# ==== نافذة الفحص ====
HOURS_WINDOW = 3          # يفحص آخر 3 ساعات من الأخبار في كل دورة
RUN_EVERY_HOURS = 1       # تكرار التشغيل (يُضبط أيضًا في workflow الـ GitHub Action)

# ==== فواصل زمنية لتجنب الأخطاء (بالثواني) ====
DELAY_BETWEEN_REWRITES_SEC = 10
DELAY_BETWEEN_PUBLISHES_SEC = 3

# ==== دقّة تطابق العناوين لمنع التكرار (fuzzy match) ====
DEDUP_TITLE_SIMILARITY_THRESHOLD = 0.75

# ==== التصنيفات المسموح بها (كما في لوحة ووردبريس) ====
CLUB_CATEGORIES = [
    "ريال مدريد",
    "برشلونه",
    "ليفربول",
    "مانشستر يونايتد",
    "مانشستر سيتي",
    "تشلسي",
    "ارسنال",
    "بايرن ميونخ",
    "باريس سان جرمان",
    "ميلان",
    "يوفنتوس",
    "انتر ميلان",
    "بروسيا دورتموند",
    "اتليتكو مدريد",
]

TOPIC_CATEGORIES = [
    "سوق الانتقالات",
]

# يُمنع اختيار هذه التصنيفات نهائيًا من قبل الذكاء الاصطناعي
EXCLUDED_CATEGORIES = [
    "اهم الاخبار",
    "مقالات وتحليلات",
]

# هذا التصنيف (بالإنجليزية) يُختار في كل خبر بدون استثناء (خاص بقسم الرئيسية)
ALWAYS_INCLUDE_CATEGORY = "Uncategorized"

# القائمة الكاملة المسموح للذكاء الاصطناعي الاختيار منها (بدون الاستثناءات وبدون التصنيف الدائم)
ALLOWED_CATEGORIES = CLUB_CATEGORIES + TOPIC_CATEGORIES

# ==== ملفات الحالة (تُحفظ وتُحدَّث داخل المستودع عبر GitHub Actions) ====
SEEN_STATE_FILE = "state/seen_articles.json"

# ==== نماذج Gemini (الأول أساسي والثاني احتياطي) ====
GEMINI_MODELS = [
    "gemini-3.6-flash",          # النموذج الأساسي (الأول)
    "gemini-3.5-flash-lite"      # النموذج الاحتياطي (الثاني)
]

GEMINI_API_URL_TEMPLATE = (
    "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={key}"
)

# ==== وسم HTTP ====
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
REQUEST_TIMEOUT_SEC = 20
