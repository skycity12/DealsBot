# DealsBot — صيّاد العروض

بوت بيراقب عروض amazon.eg وبينشرها على قناة تليجرام وصفحة فيسبوك. **الوضع الافتراضي: تجربة (dry-run) — مفيش نشر حقيقي.**

## الأقفال (في `scanner_config.json`)
| الإعداد | الافتراضي | معناه |
|---|---|---|
| `dry_run` | `true` | القفل العام: لا نشر لأي منصة |
| `weekly_live` | `false` | التجميعة الأسبوعية + التثبيت + الاستطلاع |
| `FB_DRY_RUN` (secret/env) | `true` | نشر الفيسبوك (لازم `false` **و** `dry_run=false`) |
| `growth_mode` | `true` | لينكات منتجات عادية **من غير تاج الأفلييت**، ومن غير سطر عمولة. التاج `offershunte0f-21` فاضل في الكونفيج |
| `facebook_show_prices` | `false` | مفيش أسعار/خصومات على الفيسبوك (الأسعار مسحوبة من صفحات أمازون) |

## التقسيم
كل تشغيلة بتجمع لحد 20 عرض: `telegram_per_run` (10) للتليجرام و`fb_per_run` (10) للفيسبوك، بالتناوب حسب قوة الخصم، ومفيش ASIN على المنصتين. أقل من 20 → تقسيم بالتساوي. فشل منصة ما بيوقفش التانية ومبيتنقلش العرض للتانية. فيه سقف يومي لكل منصة (`max_alerts_per_day` / `fb_max_per_day`) وساعات هدوء (`quiet_hours`)، وأي عرض بيتأجل بيستنى التجميعة الأسبوعية.

## إعداد صفحة الفيسبوك (يدويًا)
1. أنشئ تطبيق على developers.facebook.com (نوع Business أو None). التطبيق في Development mode وإنت أدمن الصفحة، فمش محتاج App Review.
2. من Graph API Explorer اطلب User Token بالصلاحيات `pages_manage_posts` و`pages_read_engagement` و`pages_show_list`.
3. حوّله لـ long-lived (60 يوم): `GET /oauth/access_token?grant_type=fb_exchange_token&client_id=APP_ID&client_secret=APP_SECRET&fb_exchange_token=SHORT_TOKEN`.
4. هات توكن الصفحة: `GET /me/accounts?fields=id,name,access_token,tasks` وخد `id` و`access_token` بتوع صفحتك (لازم الـ `tasks` فيها `CREATE_CONTENT`).
5. خزّن القيم دي كـ **secrets** (في GitHub: Settings → Secrets) أو في `.env` محلي — **ومتترفعش على GitHub** (`.env` في `.gitignore`):
   `FB_PAGE_ID` و`FB_PAGE_ACCESS_TOKEN` و`FB_APP_ID` و`FB_APP_SECRET`.
6. إصدار الـ Graph API في `fb_graph_version` (الافتراضي `v27.0` — اتأكد منه من وثائق فيسبوك).
7. قبل أول نشر في اليوم البوت بيعمل `debug_token`؛ لو التوكن باظ (`is_valid=false` أو كود 190) بيوقف الفيسبوك بس ويبعتلك تنبيه خاص على تليجرام (`TELEGRAM_CHAT_ID`)، والتليجرام يكمّل.

لتفعيل النشر الحقيقي على الفيسبوك (بقرارك إنت): `dry_run=false` في الكونفيج + `FB_DRY_RUN=false` في الـ secrets.

## ملفات
`scanner.py` (المحرك) · `splitter.py` · `facebook_formatter.py` · `facebook_publisher.py` · `weekly.py` (التجميعة/الاستطلاع) · `categories.py` · `botstate.py` · `docs/WEEKLY.md` · `docs/draft/` (مسودة workflow غير مفعّلة) · `tests/` (`python -m unittest discover -s tests`).
