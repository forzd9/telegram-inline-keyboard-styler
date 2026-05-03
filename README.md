# Telegram Inline Keyboard Styler Bot

بوت Telegram بلغة Python يستقبل ملفات `.py` و`.php`، يحللها لاستخراج أزرار Inline Keyboard، ثم يمرّ على الأزرار واحدًا واحدًا ليجعل المستخدم يختار اللون المناسب لكل زر، وبعدها يعيد كتابة الملف نفسه داخل مساحة العمل الخاصة بالبوت.

## التشغيل

1. ثبّت المتطلبات:
```bash
pip install -r requirements.txt
```

2. عيّن التوكن:
```bash
export BOT_TOKEN="YOUR_TELEGRAM_BOT_TOKEN"
```

3. شغّل البوت:
```bash
python main.py
```

## ملاحظات
- يدعم جلسات متعددة لمستخدمين مختلفين.
- يحفظ التقدم في SQLite.
- يعيد بناء الملف من النسخة الاحتياطية الداخلية في كل تعديل.
- يوفّر أوامر: `/start` و`/resume` و`/cancel` و`/status`.
