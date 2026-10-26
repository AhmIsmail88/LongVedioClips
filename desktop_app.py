# -*- coding: utf-8 -*-
"""استوديو المقاطع — نقطة التشغيل فقط.

الواجهة نفسها في `ui/pages.py`، والثيم والنصوص في `core/ui_theme.py`.

التشغيل (من جذر المشروع):
    .venv\\Scripts\\python.exe desktop_app.py

فحص سريع بدون نافذة:
    .venv\\Scripts\\python.exe desktop_app.py --smoke
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

from core.ui_theme import (  # noqa: F401 - إعادة تصدير للاختبارات والأدوات
    AR_STRINGS,
    QSS,
    STAGE_LABELS,
    STAGE_TEXT_RE,
    STRINGS,
    THUMB_CACHE,
    VIDEO_STEM_RE,
    VIDEO_SUFFIXES,
    apply_dark_palette,
    build_pipeline_command,
    cached_frame,
    clip_unit,
    frame_pixmap,
    open_folder,
    resource_path,
    set_ph,
    set_tip,
    stage_label,
    tr,
)

REPO_ROOT = Path(__file__).resolve().parent

def main() -> int:
    from core.child_process import ensure_stdio

    ensure_stdio()
    try:
        from PySide6.QtCore import Qt
        from PySide6.QtGui import QFont, QIcon
        from PySide6.QtWidgets import QApplication

        from ui.pages import MainWindow
    except ImportError:
        print("PySide6 غير مثبت — شغّل: .venv\\Scripts\\pip install -r requirements-gui.txt")
        return 1
    if "--smoke" in sys.argv:
        return run_smoke(QApplication, MainWindow)

    app = QApplication(sys.argv)
    app.setLayoutDirection(Qt.RightToLeft)
    app.setStyle("Fusion")
    apply_dark_palette(app)
    app.setFont(QFont("Segoe UI", 10))
    app.setStyleSheet(QSS)
    icon = resource_path("assets", "icon.png")
    if icon.is_file():
        app.setWindowIcon(QIcon(str(icon)))
    window = MainWindow()
    window.resize(1280, 800)
    window.show()
    return app.exec()


def run_smoke(QApplication, MainWindow) -> int:  # noqa: N803 - يمررهم main
    """فحص سريع: يبني الواجهة، يتنقل بين الصفحات، ويلتقط صورًا — بدون نافذة ظاهرة."""
    from PySide6.QtCore import Qt

    app = QApplication.instance() or QApplication([])
    app.setLayoutDirection(Qt.RightToLeft)
    app.setStyle("Fusion")
    apply_dark_palette(app)
    app.setStyleSheet(QSS)
    window = MainWindow()
    window.setAttribute(Qt.WA_DontShowOnScreen, True)  # رندر حقيقي، بدون نافذة مرئية
    window.resize(1280, 800)
    window.show()
    temp = Path(os.environ.get("TEMP", "."))
    for key in ("lib", "imp", "watch", "queue", "ana", "rev", "exp", "tools"):
        window.go_to(key)
        app.processEvents()
        window.grab().save(str(temp / f"lvc_smoke_{key}.png"))

    # The primary action must never scroll out of the page at the default size.
    def assert_primary_action(where: str) -> None:
        window.go_to("imp")
        app.processEvents()
        page = window.import_page
        button = page.start_btn
        top = button.mapTo(page, button.rect().topLeft()).y()
        assert button.isVisibleTo(page), f"{where}: start button is hidden"
        assert top >= 0 and top + button.height() <= page.height(), (
            f"{where}: start button outside the viewport "
            f"(top={top}, height={button.height()}, page={page.height()})")
        assert not page.advanced_card.isVisible(), f"{where}: advanced settings must start collapsed"

    assert_primary_action("ar")
    window.import_page.advanced_toggle.setChecked(True)
    app.processEvents()
    assert window.import_page.advanced_card.isVisible(), "advanced toggle does not reveal the card"
    window.import_page.advanced_toggle.setChecked(False)
    app.processEvents()
    # Exercise populated review controls, not just six empty pages.
    import tempfile
    from dataclasses import asdict
    from config import Config
    from core.export_queue import save
    from utils.resume import video_identity
    with tempfile.TemporaryDirectory(prefix='lvc-desktop-smoke-') as test_root:
        folder=Path(test_root);source=folder/'source.mp4';source.write_bytes(b'test-only')
        clips=[dict(index=index,start=0.,end=2.,score=80.,reason='',text='نص تجريبي',title='') for index in [1,2]]
        save(folder/'analysis.json',dict(video_path=str(source),output_dir=str(folder/'output'),duration=3.,
            final_clips=clips,segments=[],source_identity=video_identity(source),config=asdict(Config(video_path=str(source)))))
        window.review_page.set_project(folder)
        app.processEvents()
        assert window.review_page.excerpt.minimumHeight() >= 72, "excerpt is still collapsed"
        assert window.review_page.table.verticalHeader().defaultSectionSize() >= window.review_page.table.cellWidget(0,2).height(), "row height clips the time editor"
        window.review_page.table.cellWidget(1,0).setChecked(False)
        window.review_page.table.cellWidget(0,2).setValue(.25)
        window.review_page.persist()
        assert window.review_page.rows()[0]['start']==.25
        assert not window.review_page.rows()[1]['selected']
        window.ui_language.setCurrentIndex(1);window.go_to('rev');app.processEvents()
        assert window.top_title.text()=='Review'
        assert_primary_action("en")
        window.go_to('rev');app.processEvents()
        window.grab().save(str(temp/'lvc_smoke_review_en.png'))
        window.ui_language.setCurrentIndex(0);window.go_to('rev');app.processEvents()
        window.grab().save(str(temp/'lvc_smoke_review_ar.png'))
        window.go_to('tools');app.processEvents();window.grab().save(str(temp/'lvc_smoke_tools_ar.png'))
        window.go_to('imp');app.processEvents();window.grab().save(str(temp/'lvc_smoke_import_ar.png'))
        window.review_page.folder=None
    assert 'streamlit' not in sys.modules
    assert 'PySide6.QtWebEngineWidgets' not in sys.modules
    print("SMOKE OK: native Qt only")
    print("shots:", temp / "lvc_smoke_*.png")
    print("cmd cover:", build_pipeline_command("lecture.mp4", {"mode": "cover", "part_length": 90, "captions": True}))
    print("cmd highlights:", build_pipeline_command("lecture.mp4", {"mode": "highlights", "clips": 5, "platform": "tiktok"}))
    return 0


if __name__ == "__main__":
    from core.child_process import dispatch

    if dispatch(sys.argv):
        raise SystemExit(0)
    raise SystemExit(main())
