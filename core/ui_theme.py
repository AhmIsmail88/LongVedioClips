# -*- coding: utf-8 -*-
"""الثيم والنصوص والدوال المشتركة بين التطبيق ووحدات الواجهة.

لا يستورد PySide6 عند التحميل: كل ما يحتاج Qt يستورده داخل الدالة، حتى يبقى
ملف الواجهة قابلاً للاستيراد في الاختبارات ورسائل الخطأ الودّية.
"""
from __future__ import annotations

import json
import os
import sys
import re
from pathlib import Path

from core.app_paths import DATA_ROOT, REPO_ROOT  # noqa: E402 - مسارات مشتركة


def resource_path(*parts) -> Path:
    """مسار ملف مرفق مع التطبيق (من المصدر أو من داخل ملف مُغلّف)."""
    base = Path(getattr(sys, "_MEIPASS", REPO_ROOT))
    return base.joinpath(*parts)

STRINGS = {
    "app_title": "MomentForge AI",
    "brand": "MomentForge",
    "brand_sub": "AI",
    "footer": "المعالجة محلية · التحميل يحتاج اتصالًا",
    "nav": {
        "lib": "مكتبتي",
        "imp": "استيراد",
        "ana": "التحليل",
        "rev": "المراجعة",
        "exp": "التصدير",
        "tools": "الأدوات",
        "watch": "المشاهدة",
        "queue": "الطابور",
    },
    "pages": {
        "lib": ("مكتبتي", "كل مشاريعك في مكان واحد."),
        "imp": ("استيراد", "اختر فيديو، حدّد الوضع، وابدأ التحليل."),
        "ana": ("التحليل", "التقدّم والسجل الحي."),
        "rev": ("المراجعة", "اختار المقاطع وعدّلها قبل بدء التصدير."),
        "exp": ("التصدير", "تصدير متتابع، معاينة خفيفة واستكمال عند الحاجة."),
        "watch": ("المشاهدة", "شغّل رابطًا داخل البرنامج بدون تنزيل."),
        "queue": ("الطابور", "ارفع عدة فيديوهات وشغّلها واحدًا بعد الآخر."),
        "tools": ("الأدوات", "تحميل الروابط، الصوت والنص والمحرر المتقدم."),
    },
    "choose_file": "اختيار ملف الفيديو",
    "no_file": "لسه مفيش ملف مختار — اختر فيديو للبدء.",
    "drop_hint": "اسحب ملف فيديو هنا أو اختر ملفًا — MP4، MKV، MOV، AVI، WEBM",
    "preset_label": "الإعداد:",
    "preset_save": "حفظ باسم…",
    "preset_delete": "حذف الإعداد",
    "preset_custom": "مخصص",
    "preset_name": "اسم الإعداد",
    "preset_confirm": "حذف الإعداد «{name}»؟",
    "output_where": "مكان الحفظ",
    "output_auto": "تلقائي — داخل مجلد المشاريع",
    "change": "تغيير",
    "reset": "افتراضي",
    "mode_cover": "تغطية كاملة — كل المحاضرة كأجزاء متتالية",
    "mode_highlights": "أهم اللحظات",
    "part_length": "طول الجزء:",
    "clip_count": "عدد المقاطع:",
    "platform": "المنصة:",
    "platforms": ["بدون", "تيك توك", "ريلز", "شورتس"],
    "platform_values": ["none", "tiktok", "reels", "shorts"],
    "t_captions": "كابشن مدمجة",
    "t_title": "عنوان تلقائي",
    "t_lighting": "إصلاح الإضاءة",
    "t_reframe": "قص ذكي",
    "t_reframe_tip": "أبطأ — يتتبع الوجوه والتركيز",
    "start": "ابدأ التحليل",
    "stop": "إيقاف",
    "open_out": "افتح مجلد المخرجات",
    "status_idle": "جاهز — اختر فيديو وابدأ من صفحة الاستيراد.",
    "status_running": "جاري التحليل…",
    "status_done": "تم بنجاح.",
    "status_failed": "فشل — راجع السجل بالأسفل.",
    "status_stopped": "تم الإيقاف.",
    "refresh": "تحديث",
    "open_folder": "فتح المجلد",
    "empty_clips": "لسه مفيش نتائج — شغّل تحليل الأول.",
    "empty_library": "لسه مفيش مشاريع في مجلد output.",
    "sec_clips": "المقاطع الناتجة",
    "sec_out": "مجلد المخرجات",
    "tools_audio": "استخراج الصوت",
    "tools_pick_video": "اختيار فيديو",
    "tools_pick_out": "مكان الحفظ (MP3)",
    "tools_extract": "استخراج الصوت",
    "tools_text": "استخراج النص",
    "tools_soon": "قريبًا",
    "choose_video_title": "اختر ملف الفيديو",
    "choose_save_title": "اختر مكان حفظ ملف الصوت",
    "video_filter": "فيديو (*.mp4 *.mkv *.mov *.avi *.webm);;كل الملفات (*)",
}

AR_STRINGS=json.loads(json.dumps(STRINGS,ensure_ascii=False))
UI_LANGUAGE='ar'
def tr(value):
    from core.desktop_language import translate
    return translate(value,UI_LANGUAGE)


# ---------------------------------------------------------------------------
# ألوان التصميم (غرفة المونتاج — فحمي دافئ + عنبري)
# ---------------------------------------------------------------------------
QSS = """
* { font-family: "Segoe UI", "Tahoma", sans-serif; font-size: 14px; }
QLabel { color: #F5F7FA; background: transparent; }
QToolTip { background: #24313D; color: #F5F7FA; border: 1px solid rgba(255,255,255,0.15); padding: 4px 8px; }
QMainWindow, #root { background: #10171E; }
#side { background: #19232D; border-left: 1px solid rgba(255,255,255,0.07); }
#brand { font-size: 21px; font-weight: 700; color: #FFA000; }
#brandSub { font-size: 12px; color: #A8B4C0; }
#sideFooter { font-size: 12px; color: #A8B4C0; }
#topTitle { font-size: 27px; font-weight: 700; color: #F5F7FA; }
#topSub { font-size: 12px; color: #A8B4C0; }
#topbar { border-bottom: 1px solid rgba(255,255,255,0.07); }
QPushButton {
    background: transparent; color: #F5F7FA;
    border: 1px solid rgba(255,255,255,0.16); border-radius: 9px;
    padding: 7px 14px;
}
QPushButton:hover { border-color: rgba(255,202,40,0.65); color: #FFD95A; }
QPushButton:pressed { background: rgba(255,255,255,0.04); }
QPushButton:disabled { color: rgba(245,247,250,0.35); border-color: rgba(255,255,255,0.08); }
QPushButton#primary {
    background: #FFCA28; color: #352600; border: none; font-weight: 600;
    padding: 9px 22px;
}
QPushButton#primary:hover { background: #FFD95A; }
QPushButton#primary:disabled { background: rgba(255,202,40,0.35); color: rgba(53,38,0,0.6); }
QPushButton#danger { color: #E06C5A; border-color: rgba(224,108,90,0.45); }
QPushButton#danger:hover { background: rgba(224,108,90,0.12); }
QPushButton#nav {
    text-align: right; padding: 9px 12px; border: none; border-radius: 9px;
    color: #D4DDE5; font-size: 13px;
}
QPushButton#nav:hover { background: #24313D; color: #F5F7FA; }
QPushButton#nav:checked { background: rgba(255,202,40,0.12); color: #F5F7FA; font-weight: 600; }
QFrame#card { background: #19232D; border: 1px solid rgba(255,255,255,0.07); border-radius: 12px; }
QLabel#secTitle { font-size: 13px; font-weight: 600; color: #F5F7FA; }
QLabel#hint { color: #A8B4C0; font-size: 12px; }
QLabel#status { color: #D4DDE5; }
QLabel#statusOk { color: #6FBF8B; }
QLabel#statusErr { color: #E06C5A; }
QComboBox, QSpinBox, QDoubleSpinBox, QLineEdit {
    background: #24313D; border: 1px solid rgba(255,255,255,0.10); border-radius: 8px;
    padding: 6px 10px; color: #F5F7FA; min-height: 20px;
}
QComboBox:focus, QSpinBox:focus, QDoubleSpinBox:focus, QLineEdit:focus { border-color: rgba(255,202,40,0.6); }
QComboBox::drop-down { width: 24px; }
QComboBox QAbstractItemView {
    background: #24313D; color: #F5F7FA; border: 1px solid rgba(255,255,255,0.12);
    selection-background-color: #FFCA28; selection-color: #352600; outline: none;
}
QCheckBox, QRadioButton { color: #F5F7FA; spacing: 8px; }
QCheckBox::indicator, QRadioButton::indicator { width: 16px; height: 16px; }
QCheckBox::indicator { border: 1px solid rgba(255,255,255,0.30); border-radius: 4px; background: transparent; }
QCheckBox::indicator:checked { background: #FFCA28; border-color: #FFCA28; }
QRadioButton::indicator { width: 16px; height: 16px; border: 1px solid rgba(255,255,255,0.35); border-radius: 8px; background: transparent; }
QRadioButton::indicator:checked { background: #FFCA28; border-color: #FFCA28; }
QPlainTextEdit#console, QPlainTextEdit#console > QWidget, QPlainTextEdit#console QWidget {
    background-color: #10171E; color: #D4DDE5;
}
QTableWidget {background:#19232D;color:#F5F7FA;gridline-color:#33414D;border:1px solid #33414D;border-radius:10px;}
QTableWidget::item:selected { background: #3A3520; color: #FFD95A; }
QHeaderView::section {background:#24313D;color:#D4DDE5;padding:10px;border:none;}
QListWidget {
    background: #19232D; color: #F5F7FA; border: 1px solid rgba(255,255,255,0.07);
    border-radius: 10px; padding: 6px; outline: none;
}
QListWidget::item { padding: 7px 8px; border-radius: 7px; }
QListWidget::item:hover { background: #24313D; }
QListWidget::item:selected { background: rgba(255,202,40,0.14); color: #FFD95A; }
QProgressBar {
    background: #33414D; border: none; border-radius: 5px; height: 8px; text-align: center;
}
QProgressBar::chunk { background: #FFCA28; border-radius: 5px; }
QScrollBar:vertical { background: transparent; width: 9px; margin: 2px; }
QScrollBar::handle:vertical { background: #465462; border-radius: 4px; min-height: 30px; }
QScrollBar::handle:vertical:hover { background: #596A79; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
QScrollBar:horizontal { background: transparent; height: 9px; margin: 2px; }
QScrollBar::handle:horizontal { background: #465462; border-radius: 4px; min-width: 30px; }
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal { width: 0; }
"""

QSS += """
QTabWidget::pane { border: 1px solid #33414D; border-radius: 12px; background: #19232D; }
QTabBar::tab { background: #151E27; color: #A8B4C0; padding: 12px 22px; margin: 3px; border-radius: 8px; }
QTabBar::tab:selected { background: #FFCA28; color: #352600; font-weight: 600; }
QPlainTextEdit { background: #151E27; color: #F5F7FA; border: 1px solid #33414D; border-radius: 8px; padding: 10px; }
QScrollArea, QScrollArea > QWidget > QWidget { background: #10171E; }
"""

# Layer 2: states, tokens and page chrome added in the UI pass. These rules
# come last on purpose so they win over the base block above.
#
# Colour roles (single meaning per hue):
#   amber  #FFCA28 -> primary action + active page only
#   teal   #2EC4B6 -> progress, selected items, active toggles
#   red    #E06C5A -> destructive / errors
QSS += """
QCheckBox::indicator:checked { background: #2EC4B6; border-color: #2EC4B6; }
QRadioButton::indicator:checked { background: #2EC4B6; border-color: #2EC4B6; }
QTabBar::tab:selected {
    background: #24313D; color: #7FD8CE; font-weight: 600;
    border: 1px solid rgba(46,196,182,0.45);
}
QProgressBar::chunk { background: #2EC4B6; }
QTableWidget::item:selected { background: #1E3338; color: #7FD8CE; }
QPushButton:hover { border-color: rgba(255,255,255,0.35); color: #FFFFFF; }
QPushButton#sectionToggle:hover { border-color: rgba(46,196,182,0.60); color: #7FD8CE; }
QPushButton#sectionToggle:checked { border-color: rgba(46,196,182,0.60); color: #7FD8CE; }
QComboBox QAbstractItemView { selection-background-color: #2EC4B6; selection-color: #06211F; }
QListWidget::item:selected { background: rgba(46,196,182,0.16); color: #7FD8CE; }
QPushButton { border-radius: 8px; padding: 7px 16px; }
QPushButton:focus { border-color: #FFCA28; }
QPushButton#primary { border-radius: 8px; }
QPushButton#primary:focus { background: #FFD95A; }
QPushButton#primary:disabled {
    background: #24313D; color: rgba(245,247,250,0.35);
    border: 1px solid rgba(255,255,255,0.08);
}
QPushButton#nav { border-radius: 8px; }
QPushButton#nav:checked { background: rgba(255,202,40,0.18); color: #FFD95A; font-weight: 700; }
QPushButton#sectionToggle {
    text-align: right; background: #19232D; border: 1px solid rgba(255,255,255,0.10);
    border-radius: 12px; padding: 10px 16px; color: #D4DDE5;
}
QPushButton#sectionToggle:hover { border-color: rgba(255,202,40,0.55); color: #FFD95A; }
QPushButton#sectionToggle:checked { border-color: rgba(255,202,40,0.55); color: #FFD95A; }
QFrame#card, QTableWidget, QListWidget { border-radius: 12px; }
QFrame#footerBar { background: #151E27; border-top: 1px solid rgba(255,255,255,0.08); }
QLabel#plan { color: #D4DDE5; font-size: 13px; }
QLabel#excerpt {
    color: #D4DDE5; font-size: 13px; background: #151E27;
    border: 1px solid #2A353F; border-radius: 8px; padding: 10px 12px;
}
QComboBox:disabled, QLineEdit:disabled, QSpinBox:disabled, QDoubleSpinBox:disabled {
    color: rgba(245,247,250,0.38); background: #1B2530;
    border-color: rgba(255,255,255,0.05);
}
QComboBox:disabled::drop-down { border: none; }
QCheckBox:disabled, QRadioButton:disabled { color: rgba(245,247,250,0.38); }
QCheckBox::indicator:disabled, QRadioButton::indicator:disabled {
    border-color: rgba(255,255,255,0.12); background: transparent;
}
QDoubleSpinBox[invalid="true"], QSpinBox[invalid="true"] {
    border: 1px solid #E06C5A; background: rgba(224,108,90,0.10);
}
QProgressBar { background: #24313D; border-radius: 8px; min-height: 14px; }
QLabel#thumb {
    background: #10171E; border: 1px solid rgba(255,255,255,0.07);
    border-radius: 8px; color: #6C7A88; font-size: 12px;
}
QLabel#cardTitle { font-size: 14px; font-weight: 600; color: #F5F7FA; }
QFrame#libraryCard {
    background: #19232D; border: 1px solid rgba(255,255,255,0.07); border-radius: 12px;
}
QFrame#libraryCard:hover { border-color: rgba(255,202,40,0.45); }
QFrame#emptyCard { background: #151E27; border: 1px dashed rgba(255,255,255,0.14); border-radius: 12px; }
QPushButton#cardAction { padding: 6px 12px; font-size: 13px; }
QFrame#dropZone[hasFile="false"] {
    background: #151E27; border: 1px dashed rgba(255,202,40,0.40); border-radius: 12px;
}
QFrame#dropZone[hasFile="true"] { background: #19232D; border: 1px solid rgba(255,255,255,0.07); border-radius: 12px; }
QProgressBar::chunk { border-radius: 8px; }
"""

VIDEO_STEM_RE = re.compile(r"\[(\d)/5\]")
STAGE_TEXT_RE = re.compile(r"^\[(\d)/5\]\s*(.+)$")
VIDEO_SUFFIXES = {".mp4", ".mkv", ".mov", ".avi", ".webm", ".m4v"}

# لُطف أسماء مراحل خط الأنابيب (المفاتيح كما تطبعها utils.logger).
STAGE_LABELS = {
    "Loading video...": ("تحميل الفيديو", "Loading video"),
    "Extracting/transcribing audio...": ("استخراج الصوت والتفريغ", "Transcribing audio"),
    "Generating candidates...": ("توليد المرشحات", "Generating candidates"),
    "Ranking candidates with Qwen...": ("ترتيب المقاطع", "Ranking clips"),
    "Selecting and filtering clips...": ("اختيار المقاطع", "Selecting clips"),
}


def stage_label(text: str) -> str:
    pair = STAGE_LABELS.get(text.strip())
    if not pair:
        return text.strip()
    return pair[0] if UI_LANGUAGE == 'ar' else pair[1]


def build_pipeline_command(video: str, settings: dict) -> list[str]:
    """يبني أمر تشغيل خط الأنابيب من إعدادات الواجهة."""
    from core.desktop_pipeline import project_folder
    root=settings.get('projects_root') or None
    folder=project_folder(video,root) if Path(video).is_file() else DATA_ROOT/'projects'/'desktop'/'example'
    from core.child_process import python_command
    return python_command('core.desktop_pipeline', video,
                          json.dumps(settings, ensure_ascii=False), folder)



THUMB_CACHE: dict[tuple[str, int], object] = {}


def frame_pixmap(video_path, seconds: float, width: int = 336):
    """لقطة من المصدر كـQPixmap — تُستخدم في المراجعة والمكتبة."""
    try:
        source = Path(str(video_path))
        if not source.is_file() or source.stat().st_size < 1024:
            return None
        with source.open('rb') as handle:
            head = handle.read(16)
        if not (head[4:8] == b'ftyp' or head[:4] in (b'\x1aE\xdf\xa3', b'RIFF')
                or head[4:8] in (b'moov', b'mdat')):
            return None
        import cv2
        from PySide6.QtGui import QImage, QPixmap
        try:
            cv2.utils.logging.setLogLevel(cv2.utils.logging.LOG_LEVEL_SILENT)
        except Exception:  # noqa: BLE001 - إسكات سجل ffmpeg الداخلي
            pass
        capture = cv2.VideoCapture(str(video_path))
        try:
            capture.set(cv2.CAP_PROP_POS_MSEC, max(0.0, float(seconds)) * 1000)
            okay, frame = capture.read()
        finally:
            capture.release()
        if not okay or frame is None:
            return None
        height = max(1, round(width * frame.shape[0] / max(1, frame.shape[1])))
        frame = cv2.resize(frame, (width, height))
        rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        image = QImage(rgb.data, rgb.shape[1], rgb.shape[0], rgb.strides[0], QImage.Format_RGB888).copy()
        return QPixmap.fromImage(image)
    except Exception:  # noqa: BLE001 - الصورة تحسين بصري فقط
        return None


def cached_frame(video_path, seconds: float, width: int = 336):
    if not video_path:
        return None
    key = (str(video_path), int(round(max(0.0, float(seconds)))))
    if key in THUMB_CACHE:
        return THUMB_CACHE[key]
    pixmap = frame_pixmap(video_path, seconds, width)
    if pixmap is not None:
        if len(THUMB_CACHE) > 16:
            THUMB_CACHE.pop(next(iter(THUMB_CACHE)))
        THUMB_CACHE[key] = pixmap
    return pixmap


def set_tip(widget, text: str) -> None:
    """Tooltip that follows the UI language (see change_language)."""
    widget.setToolTip(text)
    widget.setProperty('tipKey', text)


def set_ph(widget, text: str) -> None:
    """Placeholder that follows the UI language (see change_language)."""
    widget.setPlaceholderText(text)
    widget.setProperty('placeholderKey', text)


def clip_unit(count: int) -> str:
    if count == 1:
        return tr('مقطع واحد')
    if 3 <= count <= 10:
        return tr('مقاطع')
    return tr('مقطع')


def open_folder(path: Path) -> None:
    try:
        if path.exists():
            os.startfile(str(path))  # noqa: S606 - Windows shell open
    except OSError:
        pass


def apply_dark_palette(app) -> None:
    """لوحة داكنة على مستوى التطبيق — تكمل الـQSS وتصلح خلفيات الكونسول والقوائم."""
    from PySide6.QtGui import QColor, QPalette

    pal = app.palette()
    pairs = {
        QPalette.Window: "#10171E",
        QPalette.WindowText: "#F5F7FA",
        QPalette.Base: "#151E27",
        QPalette.AlternateBase: "#19232D",
        QPalette.Text: "#F5F7FA",
        QPalette.Button: "#19232D",
        QPalette.ButtonText: "#F5F7FA",
        QPalette.ToolTipBase: "#24313D",
        QPalette.ToolTipText: "#F5F7FA",
        QPalette.PlaceholderText: "#A8B4C0",
        QPalette.Highlight: "#2EC4B6",
        QPalette.HighlightedText: "#06211F",
        QPalette.Link: "#7FD8CE",
    }
    for role, color in pairs.items():
        pal.setColor(role, QColor(color))
    app.setPalette(pal)

def set_language(language: str) -> None:
    """يغيّر لغة الواجهة دون إعادة ربط الكائنات المستوردة."""
    global UI_LANGUAGE
    UI_LANGUAGE = language if language in ("ar", "en") else "ar"


def apply_language(language: str) -> None:
    """يترجم نصوص الواجهة في مكانها حتى تبقى مراجع STRINGS صالحة."""
    from core.desktop_language import translated_strings
    set_language(language)
    STRINGS.clear()
    STRINGS.update(translated_strings(language))
