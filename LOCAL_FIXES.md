# إصلاحات محلية — لا تُفقد مع التحديثات

> **مهم:** الملفات دي فيها إصلاحات محلية خاصة بالمشروع ده. أي تحديث ينزّل نسخة جديدة
> فوق المشروع **هيمسحها**. الإصلاحات محفوظة كنسخة احتياطية عند AutoCoder في:
> `C:\Users\Ahmed\.openclaw-autoclaw\agents\auto-coder\workspace\.openclaw\backups\lvc-2026-09-26\`
> (مع سكربت `restore.py` يرجعها بأمر واحد).

## الإصلاحات المحلية (2026-09-26)

| # | الإصلاح | الملفات | ليه |
|---|---|---|---|
| 1 | **`num_ctx=8192` و`num_predict=4096`** في نداء Ollama | `config.py` + `core/clip_selector.py` | سياق Ollama الافتراضي (4096) كان **يقصّ البرومبت صامتاً**: دفعة 13-16 مرشحاً ≈ 5700 توكن، والواصل كان 2050 فقط → تعليمات النظام وأغلب المرشحين تُحذف → رد مقطوع (`Expecting ',' delimiter`) → "الموديل فشل". القياس: 0 مدخل → 13/13 بعد الإصلاح. |
| 2 | **ضبط حدود الكليب على حدود الكلمات** (`snap_to_word_edges`) | `core/quality_filter.py` + `core/clip_processor.py` | القص كان ممكن يقع في نص كلمة. دلوقتي: بداية داخل كلمة ترجع لبدايتها، ونهاية داخل كلمة تكمّل الكلمة — وقت التحليل **ووقت الرندر** (فالتعديلات اليدوية محمية). |
| 3 | **زر "تحميل كل المقاطع ZIP"** بعد التصدير | `streamlit_app.py` | تحميل كل الكليبات في ملف واحد (ZIP بدون ضغط إضافي — MP4 مضغوط أصلاً). |
| 4 | **حد المنصة يقصّر الطول فعلاً** | `streamlit_app.py` | اختيار منصة (شورتس/تيك توك 60ث · ريلز 90ث) لازم يفرض حدّه الأقصى حتى مع "التقسيم بالتساوي". |

## إزاي ترجّعها بعد أي تحديث

```bash
python "C:\Users\Ahmed\.openclaw-autoclaw\agents\auto-coder\workspace\.openclaw\backups\lvc-2026-09-26\restore.py"
```

أو قول لـ AutoCoder: **"رجّع النسخة الاحتياطية"**.

## الحل الجذري (موصى به)

ارفع الإصلاحات دي في الريبو نفسه (`git commit`) عشان ما تمسحهاش التحديثات:

```bash
cd D:\coding\py\long-video-clips
git init            # لو مفيش repo محلي
git add config.py app.py streamlit_app.py core/ requirements.txt README.md
git commit -m "local fixes: Ollama num_ctx, word-edge clipping, clips ZIP, platform cap"
git remote add origin https://github.com/AhmIsmail88/LongVedioClips.git
git push -u origin main
```
