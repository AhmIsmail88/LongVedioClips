# -*- coding: utf-8 -*-
"""صفحات الواجهة (PySide6) وأدوات التشغيل في الخلفية.

كل فئة قابلة للاستيراد: لا شيء هنا يعتمد على تنفيذ `main()`.
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

from core.ui_theme import (  # noqa: F401 - تُستخدم عبر الفئات أدناه
    AR_STRINGS,
    QSS,
    REPO_ROOT,
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

try:
    from PySide6.QtCore import Qt, QThread, Signal, QTimer, QUrl
    from PySide6.QtGui import QColor, QIcon, QKeySequence, QPalette, QShortcut
    from PySide6.QtWidgets import (
        QApplication, QButtonGroup, QCheckBox, QComboBox, QDialog, QDoubleSpinBox,
        QFileDialog, QFrame, QGridLayout, QHBoxLayout, QHeaderView, QInputDialog,
        QLabel, QLineEdit, QListWidget, QListWidgetItem, QMainWindow, QMessageBox,
        QPlainTextEdit, QProgressBar, QPushButton, QRadioButton, QScrollArea,
        QSlider, QSpinBox, QSplitter, QStackedWidget, QTableWidget, QTableWidgetItem,
        QVBoxLayout, QWidget,
    )
    QT_IMPORT_ERROR: Exception | None = None
except Exception as _qt_error:  # noqa: BLE001 - يُبلَّغ عنه في main برسالة واضحة
    QT_IMPORT_ERROR = _qt_error



if QT_IMPORT_ERROR is None:
    # ---------------- خط التشغيل في الخلفية ----------------
    class ArchiveRunner(QThread):
        result=Signal(str,str)
        def __init__(self,paths,target,parent):
            super().__init__(parent);self.paths=paths;self.target=target
        def run(self):
            try:
                from core.desktop_archive import create_archive
                path=create_archive(self.paths,self.target);self.result.emit(path,'')
            except Exception as exc:self.result.emit('',str(exc))

    class PipelineRunner(QThread):
        line = Signal(str)
        finished_rc = Signal(int)

        def __init__(self, argv: list[str]) -> None:
            super().__init__()
            self.argv = argv
            self.proc: subprocess.Popen | None = None
            self._stop_requested = False

        def run(self) -> None:  # noqa: D102
            env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
            flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
            try:
                self.proc = subprocess.Popen(
                    self.argv,
                    cwd=str(REPO_ROOT),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    bufsize=1,
                    env=env,
                    creationflags=flags,
                )
            except OSError as exc:
                self.line.emit(f"خطأ في التشغيل: {exc}")
                self.finished_rc.emit(-1)
                return
            if self._stop_requested:self.stop()
            assert self.proc.stdout is not None
            for raw in iter(self.proc.stdout.readline, ""):
                self.line.emit(raw.rstrip("\n"))
            rc = self.proc.wait()
            if self._stop_requested:
                rc = -2  # أوقفه المستخدم
            self.finished_rc.emit(rc)

        def stop(self) -> None:
            self._stop_requested = True
            if self.proc and self.proc.poll() is None:
                if os.name=='nt':
                    subprocess.Popen(['taskkill','/PID',str(self.proc.pid),'/T','/F'],
                        stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,creationflags=subprocess.CREATE_NO_WINDOW)
                else:self.proc.terminate()

    class StreamResolver(QThread):
        """يحلّ الرابط في الخلفية حتى لا تتجمد الواجهة أثناء الاستخراج."""

        ready = Signal(object)
        failed = Signal(str)

        def __init__(self, url: str, height: int = 720, cookies: str = "") -> None:
            super().__init__()
            self.url = url
            self.height = height
            self.cookies = cookies

        def run(self) -> None:  # noqa: D102
            try:
                from core.streaming import resolve
                self.ready.emit(resolve(self.url, self.height, self.cookies))
            except Exception as exc:  # noqa: BLE001 - تُعرض رسالة المستخدم
                self.failed.emit(str(exc) or "تعذر تجهيز البث")

    class QueueRunner(QThread):
        """يحلّل كل مصدر في الطابور بالترتيب: عملية فرعية واحدة في كل مرة."""

        item_started = Signal(str)
        item_line = Signal(str, str)
        item_finished = Signal(str, int)
        queue_done = Signal()

        def __init__(self, items: list[dict]) -> None:
            super().__init__()
            self.items = list(items)
            self.proc: subprocess.Popen | None = None
            self._stop_requested = False

        def run(self) -> None:  # noqa: D102
            env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
            flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
            for item in self.items:
                if self._stop_requested:
                    break
                argv = build_pipeline_command(item.get("source", ""), item.get("settings") or {})
                self.item_started.emit(item["id"])
                try:
                    self.proc = subprocess.Popen(
                        argv, cwd=str(REPO_ROOT), stdout=subprocess.PIPE,
                        stderr=subprocess.STDOUT, text=True, encoding="utf-8",
                        errors="replace", bufsize=1, env=env, creationflags=flags)
                except OSError as exc:
                    self.item_line.emit(item["id"], f"تعذر تشغيل التحليل: {exc}")
                    self.item_finished.emit(item["id"], -1)
                    continue
                assert self.proc.stdout is not None
                for raw in iter(self.proc.stdout.readline, ""):
                    self.item_line.emit(item["id"], raw.rstrip("\n"))
                code = self.proc.wait()
                self.item_finished.emit(item["id"], -2 if self._stop_requested else code)
            self.queue_done.emit()

        def stop(self) -> None:
            self._stop_requested = True
            if self.proc and self.proc.poll() is None:
                if os.name == "nt":
                    subprocess.Popen(
                        ["taskkill", "/PID", str(self.proc.pid), "/T", "/F"],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                        creationflags=subprocess.CREATE_NO_WINDOW)
                else:
                    self.proc.terminate()

    # ---------------- الصفحات ----------------
    class ImportPage(QWidget):
        def __init__(self, on_start) -> None:
            super().__init__()
            self.on_start = on_start
            self.video_path: str | None = None

            outer = QVBoxLayout(self)
            outer.setContentsMargins(0, 0, 0, 0)
            outer.setSpacing(0)
            scroll = QScrollArea()
            scroll.setWidgetResizable(True)
            scroll.setFrameShape(QFrame.NoFrame)
            body = QWidget()
            root = QVBoxLayout(body)
            root.setContentsMargins(24, 18, 24, 18)
            root.setSpacing(12)

            self.output_root: str | None = None
            self._applying = False
            self.setAcceptDrops(True)
            preset_row = QHBoxLayout()
            preset_row.setSpacing(8)
            preset_row.addWidget(QLabel(STRINGS["preset_label"]))
            self.preset_combo = QComboBox()
            self.preset_combo.setMinimumWidth(180)
            self.preset_combo.setMaximumWidth(320)
            self.preset_combo.currentIndexChanged.connect(self.apply_selected_preset)
            preset_row.addWidget(self.preset_combo, 1)
            self.preset_save = QPushButton(STRINGS["preset_save"])
            set_tip(self.preset_save, 'احفظ الإعدادات الحالية باسم لإعادة استخدامها')
            self.preset_save.clicked.connect(self.save_preset)
            preset_row.addWidget(self.preset_save)
            self.preset_delete = QPushButton(STRINGS["preset_delete"])
            self.preset_delete.setObjectName("danger")
            self.preset_delete.setEnabled(False)
            set_tip(self.preset_delete, 'احذف الإعداد المحدد من القائمة')
            self.preset_delete.clicked.connect(self.delete_preset)
            preset_row.addWidget(self.preset_delete)
            root.addLayout(preset_row)
            file_card = QFrame()
            file_card.setObjectName("dropZone")
            self.file_card = file_card
            file_card.setProperty("hasFile", False)
            fc = QVBoxLayout(file_card)
            fc.setContentsMargins(16, 16, 16, 16)
            fc.setSpacing(10)
            t1 = QLabel(STRINGS["choose_file"])
            t1.setObjectName("secTitle")
            fc.addWidget(t1)
            row = QHBoxLayout()
            pick = QPushButton(STRINGS["choose_file"])
            pick.clicked.connect(self.pick_file)
            self.file_label = QLabel(STRINGS["drop_hint"])
            self.file_label.setObjectName("hint")
            self.file_label.setWordWrap(True)
            row.addWidget(pick)
            row.addWidget(self.file_label, 1)
            fc.addLayout(row)
            out_row = QHBoxLayout()
            out_row.addWidget(QLabel(STRINGS["output_where"]))
            self.output_label = QLabel(STRINGS["output_auto"])
            self.output_label.setObjectName("hint")
            self.output_label.setWordWrap(True)
            out_row.addWidget(self.output_label, 1)
            self.output_pick = QPushButton(STRINGS["change"])
            set_tip(self.output_pick, 'مكان حفظ المشروع ومخرجاته')
            self.output_pick.clicked.connect(self.pick_output_root)
            out_row.addWidget(self.output_pick)
            self.output_reset = QPushButton(STRINGS["reset"])
            self.output_reset.setEnabled(False)
            set_tip(self.output_reset, 'ارجع للحفظ داخل مجلد المشاريع')
            self.output_reset.clicked.connect(self.reset_output_root)
            out_row.addWidget(self.output_reset)
            fc.addLayout(out_row)
            root.addWidget(file_card)

            mode_card = QFrame()
            mode_card.setObjectName("card")
            mc = QVBoxLayout(mode_card)
            mc.setContentsMargins(16, 16, 16, 16)
            mc.setSpacing(10)
            t2 = QLabel("وضع التقطيع")
            t2.setObjectName("secTitle")
            mc.addWidget(t2)
            self.radio_cover = QRadioButton(STRINGS["mode_cover"])
            self.radio_high = QRadioButton(STRINGS["mode_highlights"])
            self.radio_cover.setChecked(True)
            self.radio_cover.toggled.connect(self.sync_mode)
            mc.addWidget(self.radio_cover)

            cover_row = QHBoxLayout()
            cover_row.addSpacing(26)
            cover_row.addWidget(QLabel(STRINGS["part_length"]))
            self.part_combo = QComboBox()
            for seconds in (60, 75, 90):
                self.part_combo.addItem(f"{seconds} ثانية", seconds)
            self.part_combo.setCurrentIndex(2)
            cover_row.addWidget(self.part_combo)
            cover_row.addStretch(1)
            mc.addLayout(cover_row)
            mc.addWidget(self.radio_high)

            high_row = QHBoxLayout()
            high_row.addSpacing(26)
            high_row.addWidget(QLabel(STRINGS["clip_count"]))
            self.clips_spin = QSpinBox()
            self.clips_spin.setRange(1, 300)
            self.clips_spin.setValue(12)
            high_row.addWidget(self.clips_spin)
            high_row.addSpacing(14)
            high_row.addWidget(QLabel(STRINGS["platform"]))
            self.platform_combo = QComboBox()
            for label, value in zip(STRINGS["platforms"], STRINGS["platform_values"], strict=False):
                self.platform_combo.addItem(label, value)
            high_row.addWidget(self.platform_combo)
            high_row.addStretch(1)
            mc.addLayout(high_row)
            root.addWidget(mode_card)

            opts_card = QFrame()
            opts_card.setObjectName("card")
            oc = QGridLayout(opts_card)
            oc.setContentsMargins(16, 16, 16, 16)
            oc.setHorizontalSpacing(26)
            oc.setVerticalSpacing(10)
            self.cb_captions = QCheckBox(STRINGS["t_captions"])
            self.cb_title = QCheckBox(STRINGS["t_title"])
            self.cb_lighting = QCheckBox(STRINGS["t_lighting"])
            self.cb_reframe = QCheckBox(STRINGS["t_reframe"])
            set_tip(self.cb_reframe, STRINGS["t_reframe_tip"])
            oc.addWidget(self.cb_captions, 0, 0)
            oc.addWidget(self.cb_title, 0, 1)
            oc.addWidget(self.cb_lighting, 1, 0)
            oc.addWidget(self.cb_reframe, 1, 1)
            self.cb_scan=QCheckBox('فحص المحتوى قبل التصدير')
            self.cb_scan.setChecked(True)
            oc.addWidget(self.cb_scan,2,0,1,2)
            root.addWidget(opts_card)
            advanced=QFrame();advanced.setObjectName('card')
            ac=QGridLayout(advanced);ac.setContentsMargins(16,12,16,12)
            self.language_combo=QComboBox()
            for label,value in [('تلقائي',''),('العربية','ar'),('English','en')]:self.language_combo.addItem(label,value)
            self.model_combo=QComboBox();self.model_combo.addItems(['small','medium','large-v3']);self.model_combo.setCurrentText('medium')
            self.layout_combo=QComboBox();self.layout_combo.addItem('شخص واحد','single');self.layout_combo.addItem('بودكاست: فوق / تحت','stacked')
            ac.addWidget(QLabel('لغة الكلام'),0,0);ac.addWidget(self.language_combo,0,1)
            ac.addWidget(QLabel('Whisper'),0,2);ac.addWidget(self.model_combo,0,3)
            ac.addWidget(QLabel('تخطيط الكادر'),1,0);ac.addWidget(self.layout_combo,1,1)
            self.device_combo=QComboBox()
            for value in ['auto','cuda','cpu']:self.device_combo.addItem(value,value)
            self.ollama_model=QLineEdit('qwen3.5:9b');self.ollama_host=QLineEdit('http://localhost:11434')
            self.ollama_model.setLayoutDirection(Qt.LeftToRight);self.ollama_host.setLayoutDirection(Qt.LeftToRight)
            ac.addWidget(QLabel('جهاز المعالجة'),1,2);ac.addWidget(self.device_combo,1,3)
            ac.addWidget(QLabel('موديل Ollama'),2,0);ac.addWidget(self.ollama_model,2,1)
            ac.addWidget(QLabel('عنوان Ollama'),2,2);ac.addWidget(self.ollama_host,2,3)
            self.strategy_combo=QComboBox()
            self.strategy_combo.addItem('أفكار مكتملة + تقييم ذكي','hybrid');self.strategy_combo.addItem('نوافذ زمنية + تقييم','windows')
            self.top_share=QSpinBox();self.top_share.setRange(50,70);self.top_share.setSingleStep(5);self.top_share.setValue(60);self.top_share.setSuffix(' %')
            ac.addWidget(QLabel('اختيار اللقطات'),3,0);ac.addWidget(self.strategy_combo,3,1)
            ac.addWidget(QLabel('مساحة المتحدث في الأعلى'),3,2);ac.addWidget(self.top_share,3,3)
            self.manual_duration=QCheckBox('تحديد مدة اللقطات يدويًا')
            self.min_seconds=QDoubleSpinBox();self.max_seconds=QDoubleSpinBox()
            for control in [self.min_seconds,self.max_seconds]:control.setRange(1,3600);control.setDecimals(1)
            self.min_seconds.setValue(20);self.max_seconds.setValue(60)
            ac.addWidget(self.manual_duration,4,0,1,2)
            ac.addWidget(QLabel('أقل مدة (ث)'),5,0);ac.addWidget(self.min_seconds,5,1)
            ac.addWidget(QLabel('أقصى مدة (ث)'),5,2);ac.addWidget(self.max_seconds,5,3)
            self.glossary=QPlainTextEdit();self.glossary.setObjectName('glossary')
            self.glossary.setPlaceholderText('سطر لكل تصحيح: المزهر => المذهب')
            self.glossary.setMaximumHeight(84)
            self.hotwords=QLineEdit()
            self.hotwords.setPlaceholderText('كلمات متوقعة في المحتوى، مفصولة بفواصل')
            ac.addWidget(QLabel('قاموس تصحيح الكابشن'),6,0,1,1);ac.addWidget(self.glossary,6,1,1,3)
            ac.addWidget(QLabel('كلمات متوقعة للتفريغ'),7,0,1,1);ac.addWidget(self.hotwords,7,1,1,3)
            set_tip(self.glossary,'يمين السطر = الخطأ، ويساره = الصحيح. مثال: المزهر => المذهب')
            set_tip(self.hotwords,'تُمرَّر لـWhisper لتوجيه المفردات (hotwords)')
            self.manual_duration.toggled.connect(self.sync_mode)
            self.layout_combo.currentIndexChanged.connect(self.sync_mode);self.cb_reframe.toggled.connect(self.sync_mode)
            self.advanced_card = advanced
            self.advanced_card.setVisible(False)
            self.advanced_toggle = QPushButton('إعدادات متقدمة')
            self.advanced_toggle.setObjectName('sectionToggle')
            self.advanced_toggle.setCheckable(True)
            self.advanced_toggle.setChecked(False)
            set_tip(self.advanced_toggle, 'النماذج والاتصال وبصمة الإنتاج — الافتراضي يكفي غالبًا')
            self.advanced_toggle.toggled.connect(self.advanced_card.setVisible)
            root.addWidget(self.advanced_toggle)
            root.addWidget(self.advanced_card)

            root.addStretch(1)

            footer = QFrame()
            footer.setObjectName("footerBar")
            fv = QHBoxLayout(footer)
            fv.setContentsMargins(20, 10, 20, 10)
            fv.setSpacing(14)
            self.plan_label = QLabel("")
            self.plan_label.setObjectName("plan")
            self.plan_label.setWordWrap(True)
            fv.addWidget(self.plan_label, 1)
            hint = QLabel("كل شيء يعمل محليًا على جهازك.")
            hint.setObjectName("hint")
            fv.addWidget(hint)
            self.start_btn = QPushButton(STRINGS["start"])
            self.start_btn.setObjectName("primary")
            self.start_btn.setEnabled(False)
            self.start_btn.setMinimumWidth(180)
            self.start_btn.clicked.connect(self.start)
            fv.addWidget(self.start_btn)
            scroll.setWidget(body)
            outer.addWidget(scroll, 1)
            outer.addWidget(footer)
            for signal in (self.radio_cover.toggled, self.radio_high.toggled,
                           self.part_combo.currentIndexChanged, self.clips_spin.valueChanged,
                           self.platform_combo.currentIndexChanged, self.cb_captions.toggled,
                           self.cb_title.toggled, self.cb_lighting.toggled, self.cb_reframe.toggled,
                           self.cb_scan.toggled, self.manual_duration.toggled,
                           self.min_seconds.valueChanged, self.max_seconds.valueChanged,
                           self.strategy_combo.currentIndexChanged, self.layout_combo.currentIndexChanged,
                           self.device_combo.currentIndexChanged, self.model_combo.currentIndexChanged,
                           self.language_combo.currentIndexChanged, self.top_share.valueChanged,
                           self.ollama_model.textChanged, self.ollama_host.textChanged):
                signal.connect(self.update_plan)
                signal.connect(self._mark_custom)
            self.sync_mode()
            self.update_plan()
            self.refresh_presets()
            self.restore_last()

        def sync_mode(self) -> None:
            cover = self.radio_cover.isChecked()
            self.part_combo.setEnabled(cover)
            self.clips_spin.setEnabled(not cover)
            self.platform_combo.setEnabled(not cover)
            self.strategy_combo.setEnabled(not cover);self.manual_duration.setEnabled(not cover)
            manual=not cover and self.manual_duration.isChecked()
            self.min_seconds.setEnabled(manual);self.max_seconds.setEnabled(manual)
            self.top_share.setEnabled(self.cb_reframe.isChecked() and self.layout_combo.currentData()=='stacked')

        def update_plan(self, *unused) -> None:
            """ملخص القرار في الشريط السفلي — يُقرأ قبل الضغط على زر البدء."""
            cover = self.radio_cover.isChecked()
            if cover:
                parts = [tr('تغطية كاملة'), f"{self.part_combo.currentData()} {tr('ثانية')}"]
            else:
                parts = [f"{self.clips_spin.value()} {clip_unit(self.clips_spin.value())}"]
                if self.platform_combo.currentData() != 'none':
                    parts.append(self.platform_combo.currentText())
                if self.manual_duration.isChecked():
                    parts.append(f"{self.min_seconds.value():g}–{self.max_seconds.value():g} {tr('ثانية')}")
            options = [box.text() for box in (self.cb_captions, self.cb_title, self.cb_lighting,
                                              self.cb_reframe, self.cb_scan) if box.isChecked()]
            if options:
                parts.append(' · '.join(options))
            if self.output_root:
                parts.append(Path(self.output_root).name)
            text = '  ·  '.join(parts)
            self.plan_label.setText(text)
            # Keep the language switch from re-translating a stale composition.
            self.plan_label.setProperty('sourceLabel', text)
            self.plan_label.setProperty('displayLabel', text)

        def pick_file(self) -> None:
            path, _ = QFileDialog.getOpenFileName(
                self, STRINGS["choose_video_title"], "", STRINGS["video_filter"]
            )
            if path:
                self.set_source(path)

        def settings(self) -> dict:
            return {
                "strategy":self.strategy_combo.currentData(),"top_share":self.top_share.value()/100,
                "manual_duration":self.manual_duration.isChecked(),"min_duration":self.min_seconds.value(),"max_duration":self.max_seconds.value(),
                "mode": "cover" if self.radio_cover.isChecked() else "highlights",
                "part_length": self.part_combo.currentData(),
                "clips": self.clips_spin.value(),
                "platform": self.platform_combo.currentData(),
                "captions": self.cb_captions.isChecked(),
                "title": self.cb_title.isChecked(),
                "lighting": self.cb_lighting.isChecked(),
                "reframe": self.cb_reframe.isChecked(),
                "screening":self.cb_scan.isChecked(),"language":self.language_combo.currentData(),
                "device":self.device_combo.currentData(),"ollama_model":self.ollama_model.text().strip(),"ollama_host":self.ollama_host.text().strip(),
                "whisper_model":self.model_combo.currentText(),"layout":self.layout_combo.currentData(),
                "projects_root":self.output_root or "",
                "glossary":self.glossary.toPlainText().strip(),"asr_hotwords":self.hotwords.text().strip(),
            }

        def refresh_presets(self, select: str | None = None) -> None:
            from core.desktop_presets import load
            names = sorted(load().get('presets') or {})
            self.preset_combo.blockSignals(True)
            self.preset_combo.clear()
            self.preset_combo.addItem(STRINGS['preset_custom'], '')
            for name in names:
                self.preset_combo.addItem(name, name)
            index = self.preset_combo.findData(select or '')
            self.preset_combo.setCurrentIndex(index if index >= 0 else 0)
            self.preset_combo.blockSignals(False)
            self.preset_delete.setEnabled(bool(self.preset_combo.currentData()))

        def apply_selected_preset(self, *unused) -> None:
            name = self.preset_combo.currentData()
            self.preset_delete.setEnabled(bool(name))
            if not name:
                return
            from core.desktop_presets import load
            settings = (load().get('presets') or {}).get(name)
            if settings:
                self.apply_settings(settings)

        def apply_settings(self, settings: dict) -> None:
            """يكتب إعدادًا محفوظًا على عناصر الواجهة دون اعتباره تعديلًا يدويًا."""
            self._applying = True
            try:
                cover = (settings.get('mode') or 'cover') != 'highlights'
                self.radio_cover.setChecked(cover)
                self.radio_high.setChecked(not cover)
                part = self.part_combo.findData(settings.get('part_length'))
                if part >= 0:
                    self.part_combo.setCurrentIndex(part)
                if settings.get('clips') is not None:
                    self.clips_spin.setValue(int(settings['clips']))
                platform = self.platform_combo.findData(settings.get('platform'))
                if platform >= 0:
                    self.platform_combo.setCurrentIndex(platform)
                strategy = self.strategy_combo.findData(settings.get('strategy'))
                if strategy >= 0:
                    self.strategy_combo.setCurrentIndex(strategy)
                if settings.get('top_share') is not None:
                    self.top_share.setValue(round(float(settings['top_share']) * 100))
                self.manual_duration.setChecked(bool(settings.get('manual_duration')))
                if settings.get('min_duration') is not None:
                    self.min_seconds.setValue(float(settings['min_duration']))
                if settings.get('max_duration') is not None:
                    self.max_seconds.setValue(float(settings['max_duration']))
                for box, field in ((self.cb_captions, 'captions'), (self.cb_title, 'title'),
                                   (self.cb_lighting, 'lighting'), (self.cb_reframe, 'reframe'),
                                   (self.cb_scan, 'screening')):
                    if settings.get(field) is not None:
                        box.setChecked(bool(settings[field]))
                language = self.language_combo.findData(settings.get('language') or '')
                if language >= 0 and self.language_combo.currentData() != (settings.get('language') or ''):
                    self.language_combo.setCurrentIndex(language)
                model = self.model_combo.findText(settings.get('whisper_model') or '')
                if model >= 0:
                    self.model_combo.setCurrentIndex(model)
                layout = self.layout_combo.findData(settings.get('layout'))
                if layout >= 0:
                    self.layout_combo.setCurrentIndex(layout)
                device = self.device_combo.findData(settings.get('device'))
                if device >= 0:
                    self.device_combo.setCurrentIndex(device)
                if settings.get('ollama_model'):
                    self.ollama_model.setText(str(settings['ollama_model']))
                if settings.get('ollama_host'):
                    self.ollama_host.setText(str(settings['ollama_host']))
                if settings.get('glossary') is not None:
                    self.glossary.setPlainText(str(settings['glossary']))
                if settings.get('asr_hotwords') is not None:
                    self.hotwords.setText(str(settings['asr_hotwords']))
                root = settings.get('projects_root') or ''
                self.output_root = root or None
                self.output_label.setText(root or STRINGS['output_auto'])
                self.output_reset.setEnabled(bool(root))
            finally:
                self._applying = False
            self.sync_mode()
            self.update_plan()

        def save_preset(self) -> None:
            from core.desktop_presets import save_preset
            current = self.preset_combo.currentData() or ''
            name, accepted = QInputDialog.getText(
                self, STRINGS['preset_save'], STRINGS['preset_name'], text=current)
            if not accepted:
                return
            try:
                save_preset(name, self.settings())
            except ValueError as error:
                QMessageBox.warning(self, STRINGS['preset_save'], str(error))
                return
            self.refresh_presets(' '.join(str(name).split()).strip())

        def delete_preset(self) -> None:
            name = self.preset_combo.currentData()
            if not name:
                return
            answer = QMessageBox.question(self, STRINGS['preset_delete'],
                STRINGS['preset_confirm'].replace('{name}', name),
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
            if answer != QMessageBox.Yes:
                return
            from core.desktop_presets import delete_preset
            delete_preset(name)
            self.refresh_presets('')

        def restore_last(self) -> None:
            from core.desktop_presets import load
            data = load()
            if data.get('last'):
                self.apply_settings(data['last'])
            self.refresh_presets(data.get('last_name') or '')

        def remember_settings(self) -> None:
            from core.desktop_presets import remember
            remember(self.settings(), self.preset_combo.currentData() or '')

        def _mark_custom(self, *unused) -> None:
            if self._applying or not self.preset_combo.currentData():
                return
            self.preset_combo.blockSignals(True)
            self.preset_combo.setCurrentIndex(0)
            self.preset_combo.blockSignals(False)
            self.preset_delete.setEnabled(False)

        def pick_output_root(self) -> None:
            folder = QFileDialog.getExistingDirectory(self, STRINGS["output_where"], self.output_root or "")
            if not folder:
                return
            target = Path(folder)
            try:
                target.mkdir(parents=True, exist_ok=True)
                probe = target / ".lvc-write-test"
                probe.write_text("ok", encoding="utf-8")
                probe.unlink()
            except OSError as error:
                QMessageBox.warning(self, STRINGS["output_where"], str(error))
                return
            self.output_root = str(target)
            self.output_label.setText(str(target))
            self.output_reset.setEnabled(True)
            self.update_plan()

        def reset_output_root(self) -> None:
            self.output_root = None
            self.output_label.setText(STRINGS["output_auto"])
            self.output_reset.setEnabled(False)
            self.update_plan()

        def set_source(self, path: str, probe: bool = True) -> None:
            target = Path(path)
            if not target.is_file():
                QMessageBox.warning(self, tr('الملف'), tr('ملف المصدر غير موجود'))
                return
            self.video_path = str(target)
            self.start_btn.setEnabled(True)
            self.file_card.setProperty("hasFile", True)
            self.file_card.style().unpolish(self.file_card); self.file_card.style().polish(self.file_card)
            size_mb = target.stat().st_size / 1e6
            extra = ""
            if probe:
                try:
                    sys.path.insert(0, str(REPO_ROOT))
                    from utils.ffmpeg import probe_duration
                    extra = f" — {probe_duration(self.video_path)/60:.1f} {tr('دقيقة')}"
                except Exception:  # noqa: BLE001 - العرض فقط
                    extra = ""
            self.file_label.setText(f"{target.name} — {size_mb:.0f}MB{extra}")

        def dragEnterEvent(self, event) -> None:
            urls = event.mimeData().urls() if event.mimeData().hasUrls() else []
            if any(Path(url.toLocalFile()).suffix.lower() in VIDEO_SUFFIXES for url in urls):
                event.acceptProposedAction()
            else:
                event.ignore()

        def dropEvent(self, event) -> None:
            for url in event.mimeData().urls():
                candidate = Path(url.toLocalFile())
                if candidate.suffix.lower() in VIDEO_SUFFIXES and candidate.is_file():
                    self.set_source(str(candidate))
                    event.acceptProposedAction()
                    return
            event.ignore()

        def start(self) -> None:
            if self.video_path:
                from utils.corrections import parse_glossary
                try:
                    parse_glossary(self.glossary.toPlainText())
                except ValueError as error:
                    QMessageBox.warning(self, tr('تصحيحات الكابشن'), str(error))
                    return
                self.remember_settings()
                self.on_start(self.video_path, self.settings())

    class AnalyzePage(QWidget):
        def __init__(self) -> None:
            super().__init__()
            self.runner: PipelineRunner | None = None
            self.project_dir: Path | None = None
            self.on_finished = None
            self._started: float | None = None
            self._stage: str = ''

            root = QVBoxLayout(self)
            root.setContentsMargins(24, 18, 24, 18)
            root.setSpacing(12)

            head = QHBoxLayout()
            self.status = QLabel(STRINGS["status_idle"])
            self.status.setObjectName("status")
            head.addWidget(self.status, 1)
            self.stop_btn = QPushButton(STRINGS["stop"])
            self.stop_btn.setObjectName("danger")
            self.stop_btn.setEnabled(False)
            self.stop_btn.clicked.connect(self.stop)
            head.addWidget(self.stop_btn)
            self.open_btn = QPushButton(STRINGS["open_out"])
            self.open_btn.setEnabled(False)
            self.open_btn.clicked.connect(lambda: open_folder(self.project_dir) if self.project_dir else None)
            head.addWidget(self.open_btn)
            root.addLayout(head)

            self.progress = QProgressBar()
            self.progress.setRange(0, 5)
            self.progress.setValue(0)
            self.progress.setTextVisible(False)
            root.addWidget(self.progress)
            self.pct_label = QLabel(tr('التقدم') + ' · 0%')
            self.pct_label.setObjectName('hint')
            root.addWidget(self.pct_label)

            self.console = QPlainTextEdit()
            self.console.setObjectName("console")
            self.console.setReadOnly(True)
            self.console.setLayoutDirection(Qt.LeftToRight)
            self.console.setMaximumBlockCount(4000)
            from PySide6.QtGui import QColor, QPalette

            cpal = self.console.palette()
            cpal.setColor(QPalette.Base, QColor("#10171E"))
            cpal.setColor(QPalette.Text, QColor("#D4DDE5"))
            cpal.setColor(QPalette.Window, QColor("#10171E"))
            self.console.setPalette(cpal)
            self.console.viewport().setAutoFillBackground(True)
            root.addWidget(self.console, 1)

        def start_run(self, video: str, settings: dict) -> None:
            if self.runner and self.runner.isRunning():
                return
            self.console.clear()
            self._started = time.time()
            self._stage = ''
            self.set_progress(0)
            self.status.setText(f"{STRINGS['status_running']} — {Path(video).name}")
            self.status.setObjectName("status")
            self.stop_btn.setEnabled(True)
            self.open_btn.setEnabled(False)
            from core.desktop_pipeline import project_folder
            self.project_dir=project_folder(video,settings.get('projects_root') or None)
            self.snapshot=None
            argv = build_pipeline_command(video, settings)
            self.console.appendPlainText("$ " + " ".join(argv))
            self.runner = PipelineRunner(argv)
            self.runner.line.connect(self.append_line)
            self.runner.finished_rc.connect(self.finish)
            self.runner.start()

        def append_line(self, text: str) -> None:
            self.console.appendPlainText(text)
            match = VIDEO_STEM_RE.search(text)
            if match:
                stage=STAGE_TEXT_RE.match(text.strip())
                if stage:self._stage=stage.group(2).strip()[:70]
                self.set_progress(int(match.group(1)))

        def set_progress(self, value: int) -> None:
            value = max(0, min(5, int(value)))
            self.progress.setValue(value)
            percent = round(value * 20)
            parts = [tr('التقدم') + f' {percent}%']
            if self._stage:
                parts.append(stage_label(self._stage))
            if self._started and 0 < percent < 100:
                elapsed = time.time() - self._started
                if elapsed > 8:
                    remaining = max(0.0, elapsed / (percent / 100) - elapsed)
                    parts.append(tr('المتبقي') + f" ≈ {int(remaining // 60):02d}:{int(remaining % 60):02d}")
            self.pct_label.setText('  ·  '.join(parts))

        def stop(self) -> None:
            if self.runner:
                self.runner.stop()
                self.status.setText(STRINGS["status_stopped"])

        def finish(self, rc: int) -> None:
            self.stop_btn.setEnabled(False)
            self._started = None
            self._stage = ''
            self.set_progress(5 if rc == 0 else self.progress.value())
            if rc == 0:
                self.status.setText(STRINGS["status_done"])
                self.status.setObjectName("statusOk")
                self.open_btn.setEnabled(True)
            elif rc == -2:
                self.status.setText(STRINGS["status_stopped"])
                self.status.setObjectName("status")
            else:
                self.status.setText(STRINGS["status_failed"])
                self.status.setObjectName("statusErr")
            self.status.style().unpolish(self.status)
            self.status.style().polish(self.status)
            if self.on_finished:
                self.on_finished(rc)

    class ReviewPage(QWidget):
        def __init__(self):
            super().__init__();self.folder=None;self.snapshot={};self.on_export=None;self._history=[]
            root=QVBoxLayout(self);root.setContentsMargins(24,18,24,18)
            self.summary=QLabel('حلّل فيديو لعرض المقاطع المقترحة هنا');self.summary.setObjectName('hint');root.addWidget(self.summary)
            from PySide6.QtWidgets import QTabWidget
            from core.desktop_review import NativeReviewEditor
            self.tabs=QTabWidget();root.addWidget(self.tabs,1)
            clips_page=QWidget();clips_layout=QVBoxLayout(clips_page)
            self.editor=NativeReviewEditor(self)
            self.tabs.addTab(clips_page,'المقاطع');self.tabs.addTab(self.editor.caption_page,'الكابشن');self.tabs.addTab(self.editor.crop_page,'الكادر')
            self.table=QTableWidget(0,7);self.table.setHorizontalHeaderLabels(['اختيار','المقطع','البداية (ث)','النهاية (ث)','العنوان','التركيز','التقييم'])
            self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
            self.table.horizontalHeader().setSectionResizeMode(4,QHeaderView.Stretch)
            self.table.verticalHeader().setVisible(False)
            # Row height must fit the time editors, otherwise their arrows clip.
            self.table.verticalHeader().setDefaultSectionSize(44)
            # Soft tint for the current cell: the global amber highlight hides the value.
            table_palette=self.table.palette()
            table_palette.setColor(QPalette.Highlight,QColor('#1E3338'))
            table_palette.setColor(QPalette.HighlightedText,QColor('#7FD8CE'))
            self.table.setPalette(table_palette)
            # Details panel: a selecting table alone cannot answer "what is
            # inside this clip and why was it picked?".
            self.details=QFrame();self.details.setObjectName('card')
            self.details.setMinimumWidth(300);self.details.setMaximumWidth(390)
            dv=QVBoxLayout(self.details);dv.setContentsMargins(14,14,14,14);dv.setSpacing(10)
            self.thumb=QLabel('—');self.thumb.setObjectName('thumb');self.thumb.setAlignment(Qt.AlignCenter)
            self.thumb.setMinimumHeight(170);self.thumb.setMaximumHeight(210)
            dv.addWidget(self.thumb)
            self.detail_meta=QLabel('');self.detail_meta.setObjectName('hint');self.detail_meta.setWordWrap(True)
            dv.addWidget(self.detail_meta)
            panel_actions=QHBoxLayout()
            self.play_button=QPushButton('تشغيل المقطع');set_tip(self.play_button,'معاينة من المصدر داخل نافذة');self.play_button.clicked.connect(self.preview_source);panel_actions.addWidget(self.play_button)
            self.alt_button=QPushButton('اقترح بديل');set_tip(self.alt_button,'استبدل المقطع بأقرب بديل غير متداخل');self.alt_button.clicked.connect(self.replace_clip);panel_actions.addWidget(self.alt_button)
            dv.addLayout(panel_actions)
            self.reason_label=QLabel('');self.reason_label.setObjectName('hint');self.reason_label.setWordWrap(True)
            dv.addWidget(self.reason_label)
            self.excerpt=QLabel('');self.excerpt.setWordWrap(True);self.excerpt.setTextInteractionFlags(Qt.TextSelectableByMouse);self.excerpt.setObjectName('excerpt')
            self.excerpt.setAlignment(Qt.AlignTop);self.excerpt.setMinimumHeight(84)
            dv.addWidget(self.excerpt,1)
            self.split=QSplitter(Qt.Horizontal);self.split.setChildrenCollapsible(False)
            self.split.addWidget(self.table);self.split.addWidget(self.details)
            self.split.setStretchFactor(0,3);self.split.setStretchFactor(1,1)
            self.split.setSizes([760,330])
            clips_layout.addWidget(self.split,1)
            self.table.currentCellChanged.connect(self.show_excerpt)
            for keys, handler in (
                    (QKeySequence("Ctrl+Z"), self.undo),
                    (QKeySequence(Qt.Key_Space), self.preview_source),
                    (QKeySequence("X"), self.toggle_current),
                    (QKeySequence(Qt.Key_Delete), self.drop_current)):
                shortcut = QShortcut(keys, self.table)
                shortcut.setContext(Qt.WidgetWithChildrenShortcut)
                shortcut.activated.connect(handler)

            options=QHBoxLayout()
            self.preview=QCheckBox('معاينة خفيفة 360p');self.light=QCheckBox('وضع خفيف على اللاب');self.light.setChecked(True)
            set_tip(self.preview,'نسخة 360p أخف وأسرع لمراجعة الشكل')
            set_tip(self.light,'يقلّل استهلاك الجهاز أثناء التصدير')
            self.top_three=QCheckBox('ابدأ بأفضل 3 مقاطع فقط')
            set_tip(self.top_three,'يصدّر أعلى 3 مقاطع تقييمًا فقط')
            options.addWidget(self.preview);options.addWidget(self.light);options.addWidget(self.top_three)
            options.addStretch(1)
            self.invalid_label=QLabel('');self.invalid_label.setObjectName('statusErr')
            options.addWidget(self.invalid_label)
            root.addLayout(options)
            actions=QHBoxLayout()
            select=QPushButton('اختيار الكل');select.clicked.connect(lambda:self.select_all(True));actions.addWidget(select)
            reject=QPushButton('استبعاد الكل');reject.setObjectName('danger');reject.clicked.connect(self.reject_all);actions.addWidget(reject)
            up=QPushButton('أعلى');set_tip(up,'حرّك المقطع لأعلى في ترتيب التصدير');up.clicked.connect(lambda:self.move_selected(-1));actions.addWidget(up)
            down=QPushButton('أسفل');set_tip(down,'حرّك المقطع لأسفل في ترتيب التصدير');down.clicked.connect(lambda:self.move_selected(1));actions.addWidget(down)
            undo=QPushButton('تراجع');set_tip(undo,'ارجع عن آخر تغيير في الاختيار أو الترتيب (Ctrl+Z)');undo.clicked.connect(self.undo);actions.addWidget(undo)
            root.addLayout(actions)
            self.export_button=QPushButton('ابدأ تصدير المختار');self.export_button.setObjectName('primary');self.export_button.setEnabled(False)
            self.export_button.clicked.connect(self.export);root.addWidget(self.export_button)
            self.timer=QTimer(self);self.timer.setInterval(1000);self.timer.timeout.connect(self.persist);self.timer.start()

        def set_project(self,folder):
            from core.export_queue import read
            self.folder=Path(folder);self.snapshot=read(self.folder/'analysis.json')
            if not self.snapshot:return
            self.editor.set_project(self.folder,self.snapshot)
            saved=[row for row in (read(self.folder/'desktop_review.json') or []) if 'index' in row]
            saved_by_index={row['index']:row for row in saved}
            clips=self.snapshot['final_clips']
            # Saved order wins: it is the export order the user arranged.
            rows=[]
            for entry in saved:
                clip=next((item for item in clips if item['index']==entry.get('index')),None)
                if clip:rows.append({**clip,**entry})
            for clip in clips:
                if clip['index'] not in saved_by_index:rows.append(clip)
            self._history=[]
            self._populate(rows)
            if rows:self.table.setCurrentCell(0,1);self.show_excerpt()

        def _populate(self,rows):
            self.table.setRowCount(len(rows));self._indices=[]
            for row,item in enumerate(rows):
                index=int(item['index']);self._indices.append(index)
                checkbox=QCheckBox();checkbox.setChecked(item.get('selected',True));self.table.setCellWidget(row,0,checkbox)
                self.table.setItem(row,1,QTableWidgetItem(str(index)))
                for column,field in [(2,'start'),(3,'end')]:
                    control=QDoubleSpinBox();control.setDecimals(2);control.setRange(0,float(self.snapshot['duration']));control.setValue(float(item[field]))
                    control.setMinimumWidth(96);control.valueChanged.connect(self.validate_times)
                    control.valueChanged.connect(self.show_excerpt);self.table.setCellWidget(row,column,control)
                title=QLineEdit(item.get('title',''));self.table.setCellWidget(row,4,title)
                focus=QComboBox()
                for label,value in [('تلقائي آمن','largest'),('يسار المصدر','left'),('يمين المصدر','right'),('تقدير المتحدث','auto')]:focus.addItem(label,value)
                focus.setCurrentIndex(max(0,focus.findData(item.get('focus','largest'))));self.table.setCellWidget(row,5,focus)
                self.table.setItem(row,6,QTableWidgetItem(str(round(float(item.get('score') or 0)))))
            self.update_summary()
            self.validate_times()

        def _restored_rows(self,rows):
            clips={clip['index']:clip for clip in self.snapshot.get('final_clips',[])}
            return [{**clips[int(row['index'])],**row} for row in rows if int(row['index']) in clips]

        def push_history(self):
            if not self.snapshot:return
            self._history.append(self.rows())
            if len(self._history)>20:self._history.pop(0)

        def undo(self):
            if not self._history:return
            rows=self._restored_rows(self._history.pop())
            self._populate(rows)
            self.persist()

        def move_selected(self,delta):
            row=self.table.currentRow()
            if row<0 or not self.snapshot:return
            target=row+delta
            if not 0<=target<self.table.rowCount():return
            self.push_history()
            rows=self.rows();rows[row],rows[target]=rows[target],rows[row]
            self._populate(self._restored_rows(rows))
            self.table.setCurrentCell(target,1);self.show_excerpt();self.persist()

        def replace_clip(self):
            row=self.table.currentRow()
            if row<0 or not self.folder:return
            try:
                from core.desktop_review import propose_alternative
                self.persist();self.push_history()
                if propose_alternative(self.folder,self._indices[row],self.rows()):self.set_project(self.folder);self.window().change_language()
                else:QMessageBox.information(self,tr('المراجعة'),tr('لا يوجد بديل مناسب غير متداخل'))
            except Exception as exc:QMessageBox.warning(self,tr('المراجعة'),str(exc))

        def update_summary(self):
            clips=self.snapshot.get('final_clips',[])
            if clips:self.summary.setText(str(len(clips))+' '+tr('مقطع جاهز للمراجعة — القرارات تُحفظ تلقائيًا'))

        def _clock(self,seconds):
            seconds=max(0.0,float(seconds));minutes=int(seconds//60);rest=seconds-minutes*60
            return f"{minutes:02d}:{rest:05.2f}"

        def show_excerpt(self,*unused):
            row=self.table.currentRow();clips=self.snapshot.get('final_clips',[])
            if not 0<=row<len(clips):
                self.excerpt.clear();self.reason_label.clear();self.detail_meta.clear()
                self.thumb.clear();self.thumb.setText('—');return
            clip=clips[row]
            start=self.table.cellWidget(row,2).value();end=self.table.cellWidget(row,3).value()
            self.detail_meta.setText(
                f"{tr('المقطع')} {clip['index']} {tr('من')} {len(clips)}\n"
                f"{self._clock(start)} → {self._clock(end)}  ·  {end-start:.1f} {tr('ثانية')}\n"
                f"{tr('التقييم')} {round(clip.get('score',0))}")
            text=clip.get('text','')[:1500]
            self.excerpt.setText(text);self.excerpt.setVisible(bool(text.strip()))
            reason=(clip.get('reason') or '').strip()
            self.reason_label.setText((tr('سبب الاختيار')+': '+reason[:200]) if reason else '')
            pixmap=cached_frame(self.snapshot.get('video_path',''),(start+end)/2,384)
            self.thumb.clear()
            if pixmap is None:
                self.thumb.setText(tr('لا توجد صورة معاينة'))
            else:
                target=max(240,self.thumb.width() or 300)
                self.thumb.setPixmap(pixmap.scaled(target,420,Qt.KeepAspectRatio,Qt.SmoothTransformation))

        def preview_source(self):
            row=self.table.currentRow()
            if row<0 or not self.snapshot:return
            start=self.table.cellWidget(row,2).value();end=self.table.cellWidget(row,3).value()
            if not 0<=start<end<=self.snapshot['duration']:
                QMessageBox.warning(self,'المعاينة','راجع بداية المقطع ونهايته');return
            from PySide6.QtMultimedia import QMediaPlayer,QAudioOutput
            from PySide6.QtMultimediaWidgets import QVideoWidget
            dialog=QDialog(self);dialog.setWindowTitle('معاينة المصدر / Source preview');dialog.resize(850,540)
            layout=QVBoxLayout(dialog);video=QVideoWidget();layout.addWidget(video,1)
            player=QMediaPlayer(dialog);audio=QAudioOutput(dialog);player.setAudioOutput(audio);player.setVideoOutput(video)
            def ready(status):
                if status==QMediaPlayer.LoadedMedia:player.setPosition(round(start*1000));player.play()
            player.mediaStatusChanged.connect(ready)
            player.positionChanged.connect(lambda position:player.pause() if position>=end*1000 else None)
            replay=QPushButton('إعادة المعاينة / Replay');replay.clicked.connect(lambda:(player.setPosition(round(start*1000)),player.play()));layout.addWidget(replay)
            dialog.finished.connect(lambda unused:player.stop());dialog.player=player;dialog.audio=audio
            self.preview_dialog=dialog
            player.setSource(QUrl.fromLocalFile(self.snapshot['video_path']));dialog.show()

        def rows(self):
            return [dict(index=index,selected=self.table.cellWidget(row,0).isChecked(),
                start=self.table.cellWidget(row,2).value(),end=self.table.cellWidget(row,3).value(),
                title=self.table.cellWidget(row,4).text(),focus=self.table.cellWidget(row,5).currentData())
                for row,index in enumerate(getattr(self,'_indices',[]))]

        def persist(self):
            if self.folder and hasattr(self,'_indices'):
                from core.export_queue import save
                rows=self.rows()
                if rows!=getattr(self,'_saved_rows',None):save(self.folder/'desktop_review.json',rows);self._saved_rows=rows

        def _row_valid(self,row):
            start=self.table.cellWidget(row,2);end=self.table.cellWidget(row,3)
            if start is None or end is None:return True
            duration=float(self.snapshot.get('duration') or 0)
            return 0<=start.value()<end.value()<=duration+1e-6

        def validate_times(self,*unused):
            if not self.snapshot:
                return
            invalid=0
            for row in range(self.table.rowCount()):
                bad=not self._row_valid(row)
                if bad:invalid+=1
                for column in (2,3):
                    control=self.table.cellWidget(row,column)
                    if control is None or control.property('invalid') is bad:continue
                    control.setProperty('invalid',bad)
                    control.style().unpolish(control);control.style().polish(control)
            self.invalid_label.setText((tr('توقيت غير صالح')+f': {invalid}') if invalid else '')
            enabled=bool(self.table.rowCount()) and invalid==0
            if self.export_button.isEnabled()!=enabled:self.export_button.setEnabled(enabled)

        def reject_all(self):
            if self.table.rowCount()==0:return
            answer=QMessageBox.question(self,tr('استبعاد الكل'),
                tr('استبعاد كل المقاطع؟ يمكنك التراجع باختيار الكل.'),
                QMessageBox.Yes|QMessageBox.No,QMessageBox.No)
            if answer==QMessageBox.Yes:self.select_all(False)

        def select_all(self,value):
            if not self.snapshot or self.table.rowCount()==0:return
            self.push_history()
            for row in range(self.table.rowCount()):self.table.cellWidget(row,0).setChecked(value)
            self.persist()

        def toggle_current(self):
            row=self.table.currentRow()
            if row<0:return
            box=self.table.cellWidget(row,0)
            if box is not None:
                self.push_history();box.setChecked(not box.isChecked());self.persist()

        def drop_current(self):
            row=self.table.currentRow()
            box=self.table.cellWidget(row,0) if row>=0 else None
            if box is not None and box.isChecked():
                self.push_history();box.setChecked(False);self.persist()

        def export(self):
            if not self.folder:return
            if self.window().heavy_busy() or self.window().tools_page.is_busy():
                QMessageBox.information(self,tr('مهمة تعمل'),tr('انتظر المهمة الحالية قبل بدء مهمة ثقيلة أخرى'));return
            self.validate_times()
            if not self.export_button.isEnabled():
                QMessageBox.warning(self,tr('التصدير'),tr('راجع بداية المقطع ونهايته'));return
            try:
                from core.desktop_pipeline import export_review
                self.persist();job=export_review(self.folder,self.rows(),self.preview.isChecked(),self.light.isChecked(),top_three=self.top_three.isChecked())
                if self.on_export:self.on_export(self.folder,job)
            except Exception as exc:QMessageBox.warning(self,'التصدير',str(exc))

    class ExportPage(QWidget):
        def __init__(self) -> None:
            super().__init__()
            self.project_dir: Path | None = None
            self.queue=None;self.archive_worker=None
            root = QVBoxLayout(self)
            root.setContentsMargins(24, 18, 24, 18)
            root.setSpacing(12)
            card = QFrame()
            card.setObjectName("card")
            cv = QVBoxLayout(card)
            cv.setContentsMargins(16, 16, 16, 16)
            cv.setSpacing(8)
            t = QLabel(STRINGS["sec_out"])
            t.setObjectName("secTitle")
            cv.addWidget(t)
            self.path_label = QLabel("—")
            self.path_label.setObjectName("hint")
            cv.addWidget(self.path_label)
            self.count_label = QLabel("")
            cv.addWidget(self.count_label)
            row = QHBoxLayout()
            open_btn = QPushButton(STRINGS["open_folder"])
            open_btn.clicked.connect(lambda: open_folder(self.project_dir) if self.project_dir else None)
            row.addWidget(open_btn)
            refresh = QPushButton(STRINGS["refresh"])
            refresh.clicked.connect(self.refresh)
            row.addWidget(refresh)
            self.zip_btn=QPushButton('حفظ المقاطع الجاهزة ZIP');set_tip(self.zip_btn,'اضغط المقاطع المكتملة في ملف واحد');self.zip_btn.setEnabled(False);self.zip_btn.clicked.connect(self.save_zip);row.addWidget(self.zip_btn)
            row.addStretch(1)
            cv.addLayout(row)
            root.addWidget(card)
            self.status_label=QLabel('لم يبدأ التصدير');root.addWidget(self.status_label)
            self.progress=QProgressBar();self.progress.setRange(0,100);root.addWidget(self.progress)
            self.stop_btn=QPushButton('وقف بعد المقطع الحالي');set_tip(self.stop_btn,'يكمل المقطع الجاري ثم يتوقف');self.stop_btn.clicked.connect(self.stop);root.addWidget(self.stop_btn)
            self.retry_btn=QPushButton('أعد محاولة الفاشل فقط');set_tip(self.retry_btn,'يعيد تصدير المقاطع التي فشلت فقط دون المساس بالباقي');self.retry_btn.setEnabled(False);self.retry_btn.clicked.connect(self.retry_failed);root.addWidget(self.retry_btn)
            self.results=QListWidget();self.results.itemDoubleClicked.connect(self.open_result);root.addWidget(self.results,1)
            self.timer=QTimer(self);self.timer.setInterval(1500);self.timer.timeout.connect(self.refresh);self.timer.start()

        def save_zip(self):
            from core.export_queue import read
            if not self.queue or (self.archive_worker and self.archive_worker.isRunning()):return
            paths=[c['output_path'] for c in read(self.queue/'status.json').get('completed',[]) if c.get('output_path') and Path(c['output_path']).is_file()]
            if not paths:return
            target,_=QFileDialog.getSaveFileName(self,tr('حفظ المقاطع الجاهزة ZIP'),str(Path(self.path_label.text())/'clips.zip'),'ZIP (*.zip)')
            if not target:return
            if not target.lower().endswith('.zip'):target+='.zip'
            self.archive_worker=ArchiveRunner(paths,target,self)
            self.archive_worker.result.connect(self.zip_ready);self.archive_worker.finished.connect(self.refresh)
            self.zip_btn.setEnabled(False);self.archive_worker.start()
        def zip_ready(self,path,error):
            if error:QMessageBox.warning(self,'ZIP',error)
            else:QMessageBox.information(self,'ZIP',tr('تم حفظ ZIP')+'\n'+path)

        def stop(self):
            if self.queue:(self.queue/'stop').touch()

        def retry_failed(self):
            from core.desktop_pipeline import retry_failed
            from core.export_queue import read
            if not self.project_dir:return
            options=read(self.queue/'request.json').get('options') or {}
            try:
                count=retry_failed(self.project_dir,bool(options.get('preview')),bool(options.get('light',True)))
                self.refresh()
                QMessageBox.information(self,'التصدير',tr('جارٍ إعادة محاولة')+f' {count}')
            except Exception as exc:
                QMessageBox.warning(self,'التصدير',str(exc))

        def open_result(self,item):
            target=item.data(Qt.UserRole)
            if target and Path(target).is_file():os.startfile(target)

        def start_queue(self,folder,queue):
            self.project_dir=Path(folder);self.queue=Path(queue);self.refresh()

        def set_project(self, project_dir: Path) -> None:
            self.project_dir = project_dir
            self.refresh()

        def refresh(self) -> None:
            if not self.project_dir:
                self.path_label.setText("—")
                self.count_label.setText("")
                return
            from core.export_queue import read
            analysis=read(self.project_dir/'analysis.json')
            output=Path(analysis['output_dir'])/Path(analysis['video_path']).stem if analysis else self.project_dir
            self.path_label.setText(str(output))
            self.queue=self.project_dir/'export_job'
            state=read(self.queue/'status.json');completed=state.get('completed',[]);total=state.get('total',0)
            self.zip_btn.setEnabled(any(c.get('output_path') and Path(c['output_path']).is_file() for c in completed) and not (self.archive_worker and self.archive_worker.isRunning()))
            labels={'running':'جارٍ التصدير','finished':'انتهى','paused':'متوقف مؤقتًا','failed':'حدث خطأ'}
            if analysis and state.get('preview'):
                output=Path(analysis['output_dir'])/'previews'/Path(analysis['video_path']).stem
                self.path_label.setText(str(output))
            self.status_label.setText(tr(labels.get(state.get('state'),'لم يبدأ التصدير'))+f" · {len(completed)} / {total}")
            self.stop_btn.setEnabled(state.get('state')=='running')
            self.retry_btn.setEnabled(bool(state.get('failed')) and state.get('state')!='running')
            self.progress.setValue(round(100*(len(completed)+len(state.get('failed',[])))/total) if total else 0)
            self.results.clear()
            for clip in completed:
                path=clip.get('output_path')
                if path:
                    item=QListWidgetItem(Path(path).name);item.setData(Qt.UserRole,path);self.results.addItem(item)
            if state.get('error'):self.status_label.setText(str(state['error']))
            if state.get('failed'):self.status_label.setText(self.status_label.text()+' · '+tr('يحتاج مراجعة')+': '+str(state['failed']))
            clips = output / "clips"
            count = len(list(clips.glob("*.mp4"))) if clips.exists() else 0
            self.count_label.setText(tr("عدد المقاطع:")+" "+str(count))

    class LibraryPage(QWidget):
        """بطاقات المشاريع بدل قائمة نصوص: صورة، اسم، عدد المقاطع، التاريخ."""

        def __init__(self) -> None:
            super().__init__()
            self.on_open = None
            self.on_new = None
            root = QVBoxLayout(self)
            root.setContentsMargins(24, 18, 24, 18)
            root.setSpacing(12)
            head = QHBoxLayout()
            self.search = QLineEdit()
            set_ph(self.search, "ابحث باسم الفيديو أو المجلد…")
            self.search.setClearButtonEnabled(True)
            self.search.setMaximumWidth(320)
            set_tip(self.search, "يُصفّي البطاقات بالاسم أثناء الكتابة")
            self.search.textChanged.connect(self.refresh)
            head.addWidget(self.search)
            head.addStretch(1)
            folder_btn = QPushButton(STRINGS["open_folder"])
            folder_btn.clicked.connect(lambda: open_folder(REPO_ROOT / "projects" / "desktop"))
            head.addWidget(folder_btn)
            refresh = QPushButton(STRINGS["refresh"])
            refresh.clicked.connect(self.refresh)
            head.addWidget(refresh)
            root.addLayout(head)
            self.scroll = QScrollArea()
            self.scroll.setWidgetResizable(True)
            self.scroll.setFrameShape(QFrame.NoFrame)
            self.holder = QWidget()
            self.grid = QGridLayout(self.holder)
            self.grid.setContentsMargins(0, 0, 0, 0)
            self.grid.setSpacing(14)
            self.scroll.setWidget(self.holder)
            root.addWidget(self.scroll, 1)
            self.refresh()

        def entries(self):
            """The registry first (any projects root), then a legacy folder scan."""
            from core.export_queue import read
            from core.project_library import hidden, projects
            blocked = {str(Path(item).resolve()) for item in hidden()}
            needle = (self.search.text() if hasattr(self, "search") else "").strip().lower()
            found = {}
            for item in projects().values():
                snapshot = Path(item.get("snapshot", ""))
                if not snapshot.is_file() or str(snapshot.resolve()) in blocked:
                    continue
                found[str(snapshot.resolve())] = dict(
                    snapshot=snapshot, folder=snapshot.parent,
                    name=Path(item.get("name") or snapshot.parent.name).name,
                    clips=int(item.get("clips") or 0),
                    source=item.get("source", ""), first_start=0.0)
            desktop = REPO_ROOT / "projects" / "desktop"
            if desktop.exists():
                for snapshot in desktop.glob("*/analysis.json"):
                    key = str(snapshot.resolve())
                    if key in found or key in blocked:
                        continue
                    data = read(snapshot)
                    clips = data.get("final_clips") or []
                    found[key] = dict(
                        snapshot=snapshot, folder=snapshot.parent,
                        name=Path(data.get("video_path", snapshot.parent.name)).name,
                        clips=len(clips), source=data.get("video_path", ""),
                        first_start=float(clips[0].get("start", 0)) if clips else 0.0)
            items = [entry for entry in found.values()
                     if not needle or needle in entry["name"].lower()
                     or needle in entry["folder"].name.lower()]
            for item in items:
                try:
                    item["mtime"] = item["snapshot"].stat().st_mtime
                except OSError:
                    item["mtime"] = 0.0
            return sorted(items, key=lambda entry: entry["mtime"], reverse=True)

        def _date(self, stamp: float) -> str:
            if not stamp:
                return ""
            import datetime
            return datetime.datetime.fromtimestamp(stamp).strftime("%Y-%m-%d")

        def _card(self, item):
            card = QFrame()
            card.setObjectName("libraryCard")
            body = QVBoxLayout(card)
            body.setContentsMargins(12, 12, 12, 12)
            body.setSpacing(8)
            thumb = QLabel()
            thumb.setObjectName("thumb")
            thumb.setAlignment(Qt.AlignCenter)
            thumb.setFixedHeight(120)
            pixmap = cached_frame(item.get("source", ""), float(item.get("first_start") or 0.0), 300)
            if pixmap is None:
                thumb.setText(tr('لا توجد صورة معاينة'))
            else:
                thumb.setPixmap(pixmap.scaled(280, 120, Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation))
            body.addWidget(thumb)
            name = QLabel(item["name"])
            name.setObjectName("cardTitle")
            name.setWordWrap(True)
            name.setToolTip(str(item["snapshot"]))
            body.addWidget(name)
            label = tr('مقطع واحد') if item['clips'] == 1 else f"{item['clips']} {clip_unit(item['clips'])}"
            meta = QLabel(f"{label}  ·  {self._date(item['mtime'])}")
            meta.setObjectName("hint")
            body.addWidget(meta)
            buttons = QHBoxLayout()
            action = QPushButton(tr('فتح المشروع'))
            action.setObjectName("cardAction")
            action.clicked.connect(lambda unused=False, target=item: self._open(target))
            buttons.addWidget(action, 1)
            remove = QPushButton(tr('إزالة من المكتبة'))
            remove.setObjectName("cardAction")
            set_tip(remove, 'يخفي المشروع من المكتبة فقط — لا يُحذف أي ملف')
            remove.clicked.connect(lambda unused=False, target=item: self._remove(target))
            buttons.addWidget(remove)
            body.addLayout(buttons)
            return card

        def _remove(self, item) -> None:
            answer = QMessageBox.question(
                self, tr('إزالة من المكتبة'),
                tr('إزالة «{name}» من المكتبة؟ لن تُحذف أي ملفات.').replace('{name}', Path(item['name']).name),
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
            if answer != QMessageBox.Yes:
                return
            from core.project_library import unregister
            unregister(item['snapshot'])
            self.refresh()

        def _open(self, item) -> None:
            folder = item["folder"]
            if self.on_open and (folder / "analysis.json").is_file():
                self.on_open(folder)
            else:
                open_folder(folder)

        def _clear(self) -> None:
            while self.grid.count():
                holder = self.grid.takeAt(0)
                widget = holder.widget()
                if widget is not None:
                    widget.setParent(None)

        def _empty(self):
            card = QFrame()
            card.setObjectName("emptyCard")
            card.setMaximumHeight(260)
            body = QVBoxLayout(card)
            body.setContentsMargins(24, 36, 24, 36)
            body.setSpacing(10)
            title = QLabel(tr('لا توجد مشاريع محفوظة بعد'))
            title.setObjectName("cardTitle")
            title.setAlignment(Qt.AlignCenter)
            body.addWidget(title)
            hint = QLabel(tr('حلّل فيديو من صفحة الاستيراد وسيظهر هنا تلقائيًا'))
            hint.setObjectName("hint")
            hint.setAlignment(Qt.AlignCenter)
            body.addWidget(hint)
            button = QPushButton(STRINGS["nav"]["imp"])
            button.setObjectName("primary")
            button.setMaximumWidth(220)
            button.clicked.connect(lambda: self.on_new() if self.on_new else None)
            body.addWidget(button, 0, Qt.AlignCenter)
            return card

        def refresh(self) -> None:
            self._clear()
            items = self.entries()
            if not items:
                self.grid.addWidget(self._empty(), 0, 0, 1, 3, Qt.AlignTop)
                return
            columns = 3
            for index, item in enumerate(items):
                self.grid.addWidget(self._card(item), index // columns, index % columns)
            for column in range(columns):
                self.grid.setColumnStretch(column, 1)
            self.grid.setRowStretch((len(items) - 1) // columns + 1, 1)

    class WatchDownloader(QThread):
        """ينزّل نسخة محلية للمشاهدة (للتقديم الكامل) ثم تُحذف عند الإيقاف."""

        progress = Signal(int)
        ready = Signal(str, str)
        failed = Signal(str)

        def __init__(self, url: str, height: int, cookies: str, target: str) -> None:
            super().__init__()
            self.url = url
            self.height = height
            self.cookies = cookies
            self.target = target
            self._cancel = False
            self.title = ""

        def cancel(self) -> None:
            self._cancel = True

        def run(self) -> None:  # noqa: D102
            try:
                from yt_dlp import YoutubeDL
                from utils.ffmpeg import get_ffmpeg_binaries
                ffmpeg, _ = get_ffmpeg_binaries()
            except Exception as exc:  # noqa: BLE001
                self.failed.emit(f"تعذر تجهيز التنزيل: {exc}")
                return

            def hook(data):
                if self._cancel:
                    raise RuntimeError("cancelled")
                if data.get("status") == "finished":
                    self.title = str((data.get("info_dict") or {}).get("title") or "")
                    return
                total = data.get("total_bytes") or data.get("total_bytes_estimate") or 0
                done = data.get("downloaded_bytes") or 0
                if total:
                    self.progress.emit(max(0, min(100, int(done * 100 / total))))

            options = {
                "quiet": True, "no_warnings": True, "noprogress": True, "noplaylist": True,
                "format": f"bv*[height<={self.height}]+ba/b[height<={self.height}]",
                "outtmpl": self.target, "merge_output_format": "mp4",
                "ffmpeg_location": str(Path(ffmpeg).parent),
                "progress_hooks": [hook], "overwrites": True, "retries": 2,
            }
            if self.cookies:
                options["cookiefile"] = self.cookies
            try:
                with YoutubeDL(options) as ydl:
                    ydl.download([self.url])
                if self._cancel:
                    return
                if not Path(self.target).is_file():
                    self.failed.emit("لم ينتج ملف صالح للتنزيل")
                    return
                self.ready.emit(self.target, self.title)
            except Exception as exc:  # noqa: BLE001
                if not self._cancel:
                    self.failed.emit(f"تعذر التنزيل: {str(exc)[:160]}")


    class WatchPage(QWidget):
        """مشاهدة رابط داخل البرنامج: بلا تنزيل، بلا إعلانات، والأصل يبقى على الموقع."""

        def __init__(self, on_use=None) -> None:
            super().__init__()
            from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
            from PySide6.QtMultimediaWidgets import QVideoWidget

            self.on_use = on_use
            self.proxy = None
            self.info: dict = {}
            self.resolver: StreamResolver | None = None
            self._seeking = False
            self._seekable = False
            self._fullscreen = None
            self._local_file = ""
            self.downloader: WatchDownloader | None = None
            from core.watch_cache import cleanup
            cleanup()
            self.player = QMediaPlayer(self)
            self.audio = QAudioOutput(self)
            self.player.setAudioOutput(self.audio)
            self.audio.setVolume(0.8)

            root = QVBoxLayout(self)
            root.setContentsMargins(24, 18, 24, 18)
            root.setSpacing(12)

            row = QHBoxLayout()
            self.url = QLineEdit()
            self.url.setLayoutDirection(Qt.LeftToRight)
            self.url.setPlaceholderText("https://…")
            self.url.returnPressed.connect(self.play_url)
            row.addWidget(self.url, 1)
            self.play_btn = QPushButton("شغّل")
            self.play_btn.setObjectName("primary")
            self.play_btn.clicked.connect(self.play_url)
            row.addWidget(self.play_btn)
            self.stop_btn = QPushButton("إيقاف")
            self.stop_btn.setObjectName("danger")
            self.stop_btn.setEnabled(False)
            self.stop_btn.clicked.connect(self.stop_stream)
            row.addWidget(self.stop_btn)
            root.addLayout(row)

            options_row = QHBoxLayout()
            options_row.addWidget(QLabel("الجودة:"))
            self.quality = QComboBox()
            for size in (480, 720, 1080):
                self.quality.addItem(f"{size}p", size)
            self.quality.setCurrentIndex(1)
            options_row.addWidget(self.quality)
            options_row.addWidget(QLabel("ملف cookies (اختياري):"))
            self.cookies = QLineEdit()
            self.cookies.setLayoutDirection(Qt.LeftToRight)
            set_ph(self.cookies, "cookies.txt — لفيديوهات فيسبوك/X الخاصة")
            options_row.addWidget(self.cookies, 1)
            self.cookies_btn = QPushButton("اختيار ملف")
            self.cookies_btn.clicked.connect(self.pick_cookies)
            options_row.addWidget(self.cookies_btn)
            root.addLayout(options_row)

            self.status = QLabel("ألصق رابط يوتيوب أو فيسبوك أو X وسيُشغّل هنا مباشرة")
            self.status.setObjectName("hint")
            self.status.setWordWrap(True)
            root.addWidget(self.status)

            self.video = QVideoWidget()
            self.video.setObjectName("video")
            self.video.setMinimumHeight(320)
            self.player.setVideoOutput(self.video)
            root.addWidget(self.video, 1)

            controls = QHBoxLayout()
            self.toggle_btn = QPushButton("تشغيل/إيقاف مؤقت")
            self.toggle_btn.setEnabled(False)
            self.toggle_btn.clicked.connect(self.toggle)
            controls.addWidget(self.toggle_btn)
            self.position = QSlider(Qt.Horizontal)
            self.position.setRange(0, 0)
            self.position.setEnabled(False)
            self.position.sliderMoved.connect(self.seek)
            controls.addWidget(self.position, 1)
            self.clock = QLabel("00:00 / 00:00")
            self.clock.setObjectName("hint")
            controls.addWidget(self.clock)
            self.volume = QSlider(Qt.Horizontal)
            self.volume.setRange(0, 100)
            self.volume.setValue(80)
            self.volume.setMaximumWidth(110)
            set_tip(self.volume, "مستوى الصوت")
            self.volume.valueChanged.connect(lambda value: self.audio.setVolume(value / 100))
            controls.addWidget(self.volume)
            self.back_btn = QPushButton("−10ث")
            set_tip(self.back_btn, "ارجع عشر ثوانٍ")
            self.back_btn.setEnabled(False)
            self.back_btn.clicked.connect(lambda: self.nudge(-10))
            controls.addWidget(self.back_btn)
            self.forward_btn = QPushButton("+10ث")
            set_tip(self.forward_btn, "تقدّم عشر ثوانٍ")
            self.forward_btn.setEnabled(False)
            self.forward_btn.clicked.connect(lambda: self.nudge(10))
            controls.addWidget(self.forward_btn)
            self.speed = QComboBox()
            for label, rate in (("0.5×", 0.5), ("0.75×", 0.75), ("1×", 1.0),
                                ("1.25×", 1.25), ("1.5×", 1.5), ("2×", 2.0)):
                self.speed.addItem(label, rate)
            self.speed.setCurrentIndex(2)
            set_tip(self.speed, "سرعة التشغيل")
            self.speed.currentIndexChanged.connect(self.apply_speed)
            controls.addWidget(self.speed)
            self.fullscreen_btn = QPushButton("ملء الشاشة")
            set_tip(self.fullscreen_btn, "ملء الشاشة (F11) — للخروج Esc")
            self.fullscreen_btn.clicked.connect(self.toggle_fullscreen)
            controls.addWidget(self.fullscreen_btn)
            root.addLayout(controls)
            QShortcut(QKeySequence("F11"), self, activated=self.toggle_fullscreen)

            actions = QHBoxLayout()
            self.download_btn = QPushButton("نزّل للمشاهدة")
            set_tip(self.download_btn, "ينزّل نسخة مؤقتة لتعمل أزرار التقديم، وتُحذف عند الإيقاف")
            self.download_btn.clicked.connect(self.download_for_watch)
            actions.addWidget(self.download_btn)
            self.use_btn = QPushButton("استخدم هذا الرابط للتحليل")
            self.use_btn.setEnabled(False)
            self.use_btn.clicked.connect(self.use_for_analysis)
            actions.addWidget(self.use_btn)
            self.mode_label = QLabel("")
            self.mode_label.setObjectName("hint")
            actions.addWidget(self.mode_label, 1)
            root.addLayout(actions)

            self.load_preferences()
            self.player.positionChanged.connect(self.on_position)
            self.player.durationChanged.connect(self.on_duration)
            self.player.mediaStatusChanged.connect(self.on_status)
            self.player.errorOccurred.connect(self.on_error)

        # -- preferences -----------------------------------------------------
        def preferences_path(self):
            return REPO_ROOT / "projects" / "watch.json"

        def load_preferences(self) -> None:
            from core.export_queue import read
            data = read(self.preferences_path())
            if data.get("cookies"):
                self.cookies.setText(str(data["cookies"]))
            size = data.get("quality")
            index = self.quality.findData(size)
            if index >= 0:
                self.quality.setCurrentIndex(index)
            rate = data.get("speed")
            speed_index = self.speed.findData(rate) if rate is not None else -1
            if speed_index >= 0:
                self.speed.setCurrentIndex(speed_index)

        def save_preferences(self) -> None:
            from core.export_queue import save
            save(self.preferences_path(), {"quality": self.quality.currentData(),
                                           "cookies": self.cookies.text().strip(),
                                           "speed": self.speed.currentData()})

        def pick_cookies(self) -> None:
            path, _ = QFileDialog.getOpenFileName(
                self, "اختيار ملف cookies", "", "Cookies (*.txt);;كل الملفات (*)")
            if path:
                self.cookies.setText(path)

        # -- playback --------------------------------------------------------
        def play_url(self) -> None:
            url = self.url.text().strip()
            if not url:
                self.status.setText("ألصق رابط الفيديو أولًا")
                return
            cookies = self.cookies.text().strip()
            if cookies and not Path(cookies).is_file():
                self.status.setText("ملف cookies غير موجود")
                return
            if self.resolver and self.resolver.isRunning():
                return
            self.stop_stream(keep_url=True)
            self.save_preferences()
            self.status.setText("جارٍ تجهيز البث…")
            self.play_btn.setEnabled(False)
            self.resolver = StreamResolver(url, int(self.quality.currentData() or 720), cookies)
            self.resolver.ready.connect(self.on_ready)
            self.resolver.failed.connect(self.on_failed)
            self.resolver.start()

        def on_ready(self, info: dict) -> None:
            from core.streaming import StreamProxy
            self.info = info or {}
            proxy = StreamProxy(
                url=self.info.get("url", ""), headers=self.info.get("headers") or {},
                video_url=self.info.get("video_url", ""), audio_url=self.info.get("audio_url", ""),
                mime=self.info.get("mime") or "video/mp4",
                session=self.info.get("session"))
            try:
                local = proxy.start()
            except Exception as exc:  # noqa: BLE001
                self.status.setText(f"تعذر بدء البث: {exc}")
                self.play_btn.setEnabled(True)
                return
            self.proxy = proxy
            self._seekable = self.info.get("mode") == "http"
            self.position.setEnabled(self._seekable)
            self.position.setRange(0, 1000 if self._seekable else 1)
            self.position.setValue(0)
            title = self.info.get("title") or "فيديو"
            site = self.info.get("site") or ""
            duration = self.info.get("duration")
            length = f" · {self.clock_text(duration)}" if duration else ""
            self.status.setText(f"{title} — {site}{length}" if site else f"{title}{length}")
            self.render_mode()
            self.player.setSource(QUrl(local))
            self.player.play()
            self.play_btn.setEnabled(True)
            self.stop_btn.setEnabled(True)
            self.toggle_btn.setEnabled(True)
            self.use_btn.setEnabled(True)
            self.back_btn.setEnabled(self._seekable)
            self.forward_btn.setEnabled(self._seekable)
            self.apply_speed()

        def on_failed(self, message: str) -> None:
            self.status.setText(message)
            self.play_btn.setEnabled(True)
            self.toggle_btn.setEnabled(False)
            self.stop_btn.setEnabled(bool(self.proxy))

        def stop_stream(self, keep_url: bool = False) -> None:
            # A running helper thread must be drained before the page is destroyed,
            # otherwise Qt aborts with "QThread: Destroyed while thread is still running".
            if self.downloader is not None and self.downloader.isRunning():
                self.downloader.cancel()
                self.downloader.wait(2000)
            if self.resolver is not None and self.resolver.isRunning():
                self.resolver.wait(3000)
            self.player.stop()
            self.player.setSource(QUrl())
            if self.proxy is not None:
                self.proxy.stop()
                self.proxy = None
            session=(self.info or {}).get("session")
            if session is not None:
                session.close()
            if self._local_file:
                from core.watch_cache import remove
                remove(self._local_file)
                self._local_file = ""
            self.info = {}
            self.stop_btn.setEnabled(False)
            self.toggle_btn.setEnabled(False)
            self.use_btn.setEnabled(False)
            self.back_btn.setEnabled(False)
            self.forward_btn.setEnabled(False)
            self.position.setEnabled(False)
            self.position.setRange(0, 1)
            self.position.setValue(0)
            self.clock.setText("00:00 / 00:00")
            self.mode_label.clear()
            if self.downloader and self.downloader.isRunning():
                self.downloader.cancel()
            self.download_btn.setEnabled(True)
            if not keep_url:
                self.status.setText("ألصق رابط يوتيوب أو فيسبوك أو X وسيُشغّل هنا مباشرة")

        def toggle(self) -> None:
            if self.player.playbackState() == self.player.PlaybackState.PlayingState:
                self.player.pause()
            else:
                self.player.play()

        def apply_speed(self, *unused) -> None:
            rate = float(self.speed.currentData() or 1.0)
            self.player.setPlaybackRate(rate)

        def nudge(self, seconds: int) -> None:
            if not self._seekable:
                return
            duration = max(0, self.player.duration())
            target = self.player.position() + int(seconds * 1000)
            self.player.setPosition(max(0, min(duration, target)))

        def toggle_fullscreen(self) -> None:
            from PySide6.QtMultimediaWidgets import QVideoWidget
            if self._fullscreen is not None:
                self._fullscreen.close()
                return
            dialog = QDialog(self)
            dialog.setWindowTitle("مشاهدة")
            dialog.setStyleSheet("background:#000;")
            view = QVideoWidget(dialog)
            layout = QVBoxLayout(dialog)
            layout.setContentsMargins(0, 0, 0, 0)
            layout.addWidget(view)
            self.player.setVideoOutput(view)
            dialog.finished.connect(lambda _result: self._leave_fullscreen(dialog))
            self._fullscreen = dialog
            dialog.showFullScreen()

        def _leave_fullscreen(self, dialog) -> None:
            self.player.setVideoOutput(self.video)
            self._fullscreen = None
            dialog.deleteLater()

        def seek(self, value: int) -> None:
            if self._seekable:
                self._seeking = True
                self.player.setPosition(value)
                self._seeking = False

        # -- local copy -------------------------------------------------------
        def download_for_watch(self) -> None:
            url = self.url.text().strip()
            if not url:
                self.status.setText("ألصق رابط الفيديو أولًا")
                return
            if self.downloader and self.downloader.isRunning():
                return
            cookies = self.cookies.text().strip()
            if cookies and not Path(cookies).is_file():
                self.status.setText("ملف cookies غير موجود")
                return
            from core.watch_cache import target_for
            self.save_preferences()
            self.download_btn.setEnabled(False)
            self.status.setText("جارٍ التنزيل للمشاهدة…")
            self.downloader = WatchDownloader(
                url, int(self.quality.currentData() or 720), cookies, str(target_for(url)))
            self.downloader.progress.connect(self._on_download_progress)
            self.downloader.ready.connect(self._on_download_ready)
            self.downloader.failed.connect(self._on_download_failed)
            self.downloader.start()

        def _on_download_progress(self, percent: int) -> None:
            self.status.setText(f"جارٍ التنزيل للمشاهدة… {percent}%")

        def _on_download_ready(self, path: str, title: str = "") -> None:
            title = title or (self.info or {}).get("title") or "نسخة محلية"
            self.stop_stream(keep_url=True)
            self._local_file = path
            self._seekable = True
            self.player.setSource(QUrl.fromLocalFile(path))
            self.position.setEnabled(True)
            self.position.setRange(0, 0)
            self.back_btn.setEnabled(True)
            self.forward_btn.setEnabled(True)
            self.stop_btn.setEnabled(True)
            self.toggle_btn.setEnabled(True)
            self.use_btn.setEnabled(True)
            self.player.play()
            self.apply_speed()
            self.status.setText(f"{title} — نسخة محلية")
            self.mode_label.setText(tr("نسخة محلية — التقديم يعمل"))
            self.download_btn.setEnabled(True)

        def _on_download_failed(self, message: str) -> None:
            self.status.setText(message)
            self.download_btn.setEnabled(True)

        def use_for_analysis(self) -> None:
            if self.on_use and self.url.text().strip():
                self.on_use(self.url.text().strip())

        # -- player signals ---------------------------------------------------
        def render_mode(self) -> None:
            if not self.info:
                self.mode_label.clear()
                return
            if self.info.get("is_live"):
                parts = [tr("بث مباشر")]
            elif self.info.get("mode") == "mux":
                parts = [tr("دمج صوت وصورة على الطاير")]
            else:
                parts = [tr("ملف مباشر")]
            if not self.info.get("has_audio"):
                parts.append(tr("بدون صوت"))
            if not self._seekable:
                parts.append(tr("التقديم غير متاح"))
            self.mode_label.setText(" · ".join(parts))

        def refresh_texts(self) -> None:
            self.render_mode()

        def clock_text(self, seconds) -> str:
            try:
                total = int(float(seconds or 0))
            except (TypeError, ValueError):
                return "00:00"
            return f"{total // 60:02d}:{total % 60:02d}"

        def total_ms(self) -> int:
            """المدة المعروفة من الرابط أهم من مدة البث المتجددة (5 ثوانٍ)."""
            if self._seekable:
                return self.player.duration()
            known = self.info.get("duration") or 0
            return int(float(known) * 1000) if known else 0

        def on_position(self, value: int) -> None:
            if self._seeking:
                return
            if self._seekable:
                self.position.setValue(value)
            total = self.total_ms()
            if total > 0:
                self.clock.setText(f"{self.clock_text(value / 1000)} / {self.clock_text(total / 1000)}")
            else:
                self.clock.setText(self.clock_text(value / 1000))

        def on_duration(self, value: int) -> None:
            if self._seekable:
                self.position.setRange(0, max(0, value))

        def on_status(self, status) -> None:
            if status == self.player.MediaStatus.BufferedMedia:
                self.toggle_btn.setEnabled(True)

        def on_error(self, *_args) -> None:
            message = self.player.errorString() or "تعذر تشغيل البث"
            if self.proxy is not None:
                self.status.setText(f"{message} — جرّب جودة أقل أو حدّث yt-dlp")
    class QueuePage(QWidget):
        """طابور مصادر متتابع: أضف ملفات، شغّلها واحدًا بعد الآخر، وتابع الحالة."""

        STATE_LABELS = {
            "pending": "بالانتظار",
            "running": "جارٍ",
            "done": "تم",
            "failed": "فشل",
            "skipped": "متوقف",
        }

        def __init__(self, settings_provider) -> None:
            super().__init__()
            self.settings_provider = settings_provider
            self.on_open = None
            self.on_item_done = None
            self.runner: QueueRunner | None = None
            self._current = ""
            self._ids: list[str] = []
            root = QVBoxLayout(self)
            root.setContentsMargins(24, 18, 24, 18)
            root.setSpacing(12)
            head = QHBoxLayout()
            self.summary = QLabel("")
            self.summary.setObjectName("hint")
            head.addWidget(self.summary, 1)
            self.add_btn = QPushButton("إضافة ملفات")
            set_tip(self.add_btn, "أضف عدة فيديوهات بالترتيب الذي تريد معالجتها به")
            self.add_btn.clicked.connect(self.add_files)
            head.addWidget(self.add_btn)
            self.start_btn = QPushButton("ابدأ الطابور")
            self.start_btn.setObjectName("primary")
            self.start_btn.setEnabled(False)
            self.start_btn.clicked.connect(self.start)
            head.addWidget(self.start_btn)
            self.stop_btn = QPushButton("إيقاف بعد الحالي")
            self.stop_btn.setObjectName("danger")
            self.stop_btn.setEnabled(False)
            self.stop_btn.clicked.connect(self.stop)
            head.addWidget(self.stop_btn)
            self.remove_btn = QPushButton("إزالة المحدد")
            self.remove_btn.clicked.connect(self.remove_selected)
            head.addWidget(self.remove_btn)
            self.clear_btn = QPushButton("مسح المنتهي")
            self.clear_btn.clicked.connect(self.clear_finished)
            head.addWidget(self.clear_btn)
            self.retry_btn = QPushButton("إعادة محاولة الفاشل")
            set_tip(self.retry_btn, "يعيد العناصر الفاشلة إلى الانتظار لتشغيلها مرة أخرى")
            self.retry_btn.setEnabled(False)
            self.retry_btn.clicked.connect(self.retry_failed)
            head.addWidget(self.retry_btn)
            root.addLayout(head)
            self.table = QTableWidget(0, 3)
            self.table.setHorizontalHeaderLabels(["الحالة", "الملف", "ملاحظة"])
            self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeToContents)
            self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
            self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeToContents)
            self.table.horizontalHeader().setStretchLastSection(False)
            self.table.verticalHeader().setVisible(False)
            self.table.verticalHeader().setDefaultSectionSize(38)
            self.table.itemDoubleClicked.connect(lambda _item: self.open_selected())
            root.addWidget(self.table, 1)
            self.log_caption = QLabel("سجل المهمة الحالية")
            self.log_caption.setObjectName("hint")
            root.addWidget(self.log_caption)
            self.log = QPlainTextEdit()
            self.log.setObjectName("console")
            self.log.setReadOnly(True)
            self.log.setLayoutDirection(Qt.LeftToRight)
            self.log.setMaximumBlockCount(500)
            self.log.setMaximumHeight(140)
            root.addWidget(self.log)
            self.refresh()

        # -- helpers ---------------------------------------------------------
        def is_running(self) -> bool:
            return bool(self.runner and self.runner.isRunning())

        def items(self):
            from core.desktop_queue import load
            return load()["items"]

        def selected_id(self):
            row = self.table.currentRow()
            return self._ids[row] if 0 <= row < len(self._ids) else ""

        def _state_text(self, state: str) -> str:
            return tr(self.STATE_LABELS.get(state, "بالانتظار"))

        # -- actions ---------------------------------------------------------
        def add_files(self) -> None:
            from core.desktop_queue import add
            paths, _ = QFileDialog.getOpenFileNames(
                self, STRINGS["choose_video_title"], "", STRINGS["video_filter"])
            if not paths:
                return
            settings = self.settings_provider()
            skipped = []
            for path in paths:
                try:
                    add(path, settings)
                except ValueError as exc:
                    skipped.append(str(exc))
            self.refresh()
            if skipped:
                QMessageBox.information(self, STRINGS["nav"]["queue"], "\n".join(skipped[:6]))

        def remove_selected(self) -> None:
            item_id = self.selected_id()
            if not item_id:
                return
            from core.desktop_queue import remove
            remove(item_id)
            self.refresh()

        def clear_finished(self) -> None:
            from core.desktop_queue import clear_finished
            clear_finished()
            self.refresh()

        def retry_failed(self) -> None:
            from core.desktop_queue import reset_failed
            if reset_failed():
                self.refresh()

        def start(self) -> None:
            if self.is_running():
                return
            from core.desktop_queue import load, recover
            recover()
            pending = [item for item in load()["items"] if item.get("state") == "pending"]
            if not pending:
                QMessageBox.information(self, STRINGS["nav"]["queue"], "لا توجد ملفات بالانتظار")
                return
            window = self.window()
            if hasattr(window, "tools_page") and window.tools_page.is_busy():
                QMessageBox.information(self, tr("مهمة تعمل"), tr("انتظر المهمة الحالية قبل بدء مهمة ثقيلة أخرى"))
                return
            self.log.clear()
            self.runner = QueueRunner(pending)
            self.runner.item_started.connect(self._on_started)
            self.runner.item_line.connect(self._on_line)
            self.runner.item_finished.connect(self._on_finished)
            self.runner.queue_done.connect(self._on_done)
            self.runner.start()
            self.refresh()

        def stop(self) -> None:
            if self.runner:
                self.runner.stop()
            self.stop_btn.setEnabled(False)

        def open_selected(self) -> None:
            item_id = self.selected_id()
            if not item_id or not self.on_open:
                return
            from core.export_queue import read
            from core.desktop_queue import load
            item = next((entry for entry in load()["items"] if entry.get("id") == item_id), None)
            if not item:
                return
            folder = Path(item.get("folder") or "")
            if folder and (folder / "analysis.json").is_file() and read(folder / "analysis.json"):
                self.on_open(folder)
            else:
                open_folder(Path(item["source"]).parent)

        # -- runner signals --------------------------------------------------
        def _on_started(self, item_id: str) -> None:
            from core.desktop_queue import update
            self._current = item_id
            update(item_id, state="running", error="")
            self.refresh()

        def _on_line(self, item_id: str, text: str) -> None:
            if text:
                self.log.appendPlainText(text)

        def _on_finished(self, item_id: str, code: int) -> None:
            from core.desktop_queue import update
            from core.desktop_pipeline import project_folder
            state = "done" if code == 0 else ("skipped" if code == -2 else "failed")
            folder = ""
            if state == "done":
                try:
                    folder = str(project_folder(self._source_of(item_id),
                                                (self._settings_of(item_id) or {}).get("projects_root") or None))
                except Exception:  # noqa: BLE001 - المسار تحسين فقط
                    folder = ""
            error = "" if state == "done" else (self.log.toPlainText().strip().splitlines() or [""])[-1][:240]
            update(item_id, state=state, folder=folder, error=error, finished=time.time())
            self._current = ""
            self.refresh()
            if state == "done" and self.on_item_done:
                self.on_item_done()

        def _on_done(self) -> None:
            self.runner = None
            self.refresh()

        def _source_of(self, item_id: str) -> str:
            item = next((entry for entry in self.items() if entry.get("id") == item_id), None)
            return item["source"] if item else ""

        def _settings_of(self, item_id: str) -> dict:
            item = next((entry for entry in self.items() if entry.get("id") == item_id), None)
            return (item or {}).get("settings") or {}

        # -- table -----------------------------------------------------------
        def refresh(self) -> None:
            items = self.items()
            running = self.is_running()
            self.table.setRowCount(len(items))
            self._ids = []
            for row, item in enumerate(items):
                self._ids.append(item.get("id", ""))
                state = item.get("state", "pending")
                self.table.setItem(row, 0, QTableWidgetItem(self._state_text(state)))
                self.table.setItem(row, 1, QTableWidgetItem(Path(item.get("source", "")).name))
                note = (item.get("error") or "")[:140]
                if not note and state == "pending":
                    note = tr("جاهز للتحليل")
                self.table.setItem(row, 2, QTableWidgetItem(note))
            pending = sum(1 for item in items if item.get("state") == "pending")
            done = sum(1 for item in items if item.get("state") == "done")
            failed = sum(1 for item in items if item.get("state") == "failed")
            parts = [f"{pending} {tr('بالانتظار')}", f"{done} {tr('تم')}"]
            if failed:
                parts.append(f"{failed} {tr('فشل')}")
            if running:
                parts.append(tr("يعمل الآن"))
            self.summary.setText((tr("الطابور: ") + " · ".join(parts)) if items else tr("الطابور فارغ — أضف ملفات للبدء"))
            self.start_btn.setEnabled(pending > 0 and not running)
            self.stop_btn.setEnabled(running)
            self.add_btn.setEnabled(not running)
            self.remove_btn.setEnabled(not running)
            self.clear_btn.setEnabled(not running and (done + failed) > 0)
            self.retry_btn.setEnabled(not running and failed > 0)


    class MainWindow(QMainWindow):
        def __init__(self) -> None:
            super().__init__()
            self.setWindowTitle(STRINGS["app_title"])
            icon_file = resource_path("assets", "icon.png")
            if icon_file.is_file():
                self.setWindowIcon(QIcon(str(icon_file)))
            self.setMinimumSize(900, 620)

            root = QWidget()
            root.setObjectName("root")
            self.setCentralWidget(root)
            hbox = QHBoxLayout(root)
            hbox.setContentsMargins(0, 0, 0, 0)
            hbox.setSpacing(0)

            side = QFrame()
            side.setObjectName("side")
            side.setFixedWidth(210)
            sv = QVBoxLayout(side)
            sv.setContentsMargins(12, 16, 12, 14)
            sv.setSpacing(4)
            brand = QLabel(STRINGS["brand"])
            brand.setObjectName("brand")
            brand.setAlignment(Qt.AlignRight)
            brand_sub = QLabel(STRINGS["brand_sub"])
            brand_sub.setObjectName("brandSub")
            brand_sub.setAlignment(Qt.AlignRight)
            brand_sub.setLayoutDirection(Qt.LeftToRight)
            sv.addWidget(brand)
            sv.addWidget(brand_sub)
            sv.addSpacing(14)
            self.ui_language=QComboBox();self.ui_language.setObjectName('uiLanguage')
            self.ui_language.addItem('العربية','ar');self.ui_language.addItem('English','en')


            self.nav_buttons: dict[str, QPushButton] = {}
            group = QButtonGroup(self)
            group.setExclusive(True)
            for key in ("lib", "imp", "watch", "queue", "ana", "rev", "exp", "tools"):
                btn = QPushButton(STRINGS["nav"][key])
                btn.setObjectName("nav")
                btn.setCheckable(True)
                btn.setLayoutDirection(Qt.RightToLeft)
                btn.clicked.connect(lambda _=False, k=key: self.go_to(k))
                group.addButton(btn)
                sv.addWidget(btn)
                self.nav_buttons[key] = btn
            sv.addStretch(1)
            sv.addWidget(self.ui_language)
            sv.addSpacing(6)
            footer = QLabel(STRINGS["footer"])
            footer.setObjectName("sideFooter")
            footer.setWordWrap(True)
            sv.addWidget(footer)

            main = QFrame()
            mv = QVBoxLayout(main)
            mv.setContentsMargins(0, 0, 0, 0)
            mv.setSpacing(0)
            topbar = QFrame()
            topbar.setObjectName("topbar")
            tv = QVBoxLayout(topbar)
            tv.setContentsMargins(24, 14, 24, 12)
            tv.setSpacing(2)
            self.top_title = QLabel("")
            self.top_title.setObjectName("topTitle")
            self.top_sub = QLabel("")
            self.top_sub.setObjectName("topSub")
            tv.addWidget(self.top_title)
            tv.addWidget(self.top_sub)
            mv.addWidget(topbar)

            self.stack = QStackedWidget()
            self.pages: dict[str, QWidget] = {}
            self.import_page = ImportPage(self.start_pipeline)
            QShortcut(QKeySequence("Ctrl+O"), self, activated=self.import_page.pick_file)
            QShortcut(QKeySequence("Ctrl+Return"), self, activated=self.import_page.start)
            self.watch_page = WatchPage(on_use=self.accept_stream)
            self.queue_page = QueuePage(self.import_page.settings)
            self.queue_page.on_open = self.open_project
            self.analyze_page = AnalyzePage()
            self.review_page = ReviewPage()
            self.export_page = ExportPage()
            self.library_page = LibraryPage()
            self.library_page.on_open=self.open_project
            self.library_page.on_new=lambda: self.go_to('imp')
            self.queue_page.on_item_done=self.library_page.refresh
            from core.desktop_tools import NativeTools
            self.tools_page = NativeTools(on_source=self.accept_source,busy=self.heavy_busy)
            self.pages = {
                "lib": self.library_page,
                "imp": self.import_page,
                "watch": self.watch_page,
                "queue": self.queue_page,
                "ana": self.analyze_page,
                "rev": self.review_page,
                "exp": self.export_page,
                "tools": self.tools_page,
            }
            order = ("lib", "imp", "watch", "queue", "ana", "rev", "exp", "tools")
            for key in order:
                if key == "imp":
                    # ImportPage owns its scroll area so the primary action
                    # stays pinned in the footer instead of scrolling away.
                    self.stack.addWidget(self.pages[key]);continue
                scroll=QScrollArea();scroll.setWidgetResizable(True);scroll.setFrameShape(QFrame.NoFrame);scroll.setWidget(self.pages[key]);self.stack.addWidget(scroll)
            self._order = order
            self.analyze_page.on_finished = self.after_run
            self.review_page.on_export=self.start_export
            mv.addWidget(self.stack, 1)

            for position, key in enumerate(order, start=1):
                QShortcut(QKeySequence(f"Ctrl+{position}"), self,
                          activated=lambda target=key: self.go_to(target))
            hbox.addWidget(side)
            hbox.addWidget(main, 1)
            self.go_to("imp")
            self.ui_language.currentIndexChanged.connect(self.change_language)
            from core.export_queue import read
            preferred=read(REPO_ROOT/'projects'/'interface.json').get('language','ar')
            self.ui_language.setCurrentIndex(1 if preferred=='en' else 0)
            self.change_language()


        def change_language(self):
            from core import ui_theme
            from core.desktop_language import translate
            language=self.ui_language.currentData()
            ui_theme.apply_language(language)
            QApplication.instance().setLayoutDirection(Qt.RightToLeft if language=='ar' else Qt.LeftToRight)
            self.setWindowTitle(STRINGS['app_title'])
            for widget in self.findChildren(QLabel)+self.findChildren(QPushButton)+self.findChildren(QCheckBox)+self.findChildren(QRadioButton):
                original=widget.property('sourceLabel')
                if original is None:original=widget.text();widget.setProperty('sourceLabel',original)
                # Do not replace dynamic status messages or user filenames.
                last=widget.property('displayLabel')
                if last is not None and widget.text()!=last and widget.text()!=original:continue
                text=translate(original,language);widget.setText(text);widget.setProperty('displayLabel',text)
            for widget in self.findChildren(QWidget):
                key=widget.property('tipKey')
                if key:widget.setToolTip(translate(key,language))
            for widget in self.findChildren(QLineEdit):
                key=widget.property('placeholderKey')
                if key:widget.setPlaceholderText(translate(key,language))
            for widget in self.findChildren(QComboBox):
                if widget is self.ui_language:continue
                for index in range(widget.count()):
                    original=widget.itemData(index,Qt.UserRole+1)
                    if original is None:original=widget.itemText(index);widget.setItemData(index,original,Qt.UserRole+1)
                    widget.setItemText(index,translate(original,language))
            for table in self.findChildren(QTableWidget):
                for column in range(table.columnCount()):
                    item=table.horizontalHeaderItem(column)
                    if item:
                        original=item.data(Qt.UserRole)
                        if original is None:original=item.text();item.setData(Qt.UserRole,original)
                        item.setText(translate(original,language))
            for button in self.nav_buttons.values():button.setLayoutDirection(Qt.RightToLeft if language=='ar' else Qt.LeftToRight)
            align='right' if language=='ar' else 'left'
            # The active-page bar must sit on the inner edge in both directions.
            inner='left' if language=='ar' else 'right'
            self.setStyleSheet(
                f'QPushButton#nav {{ text-align:{align}; border:none; padding:9px 10px; '
                f'border-{inner}:3px solid transparent; }}'
                f'QPushButton#nav:checked {{ border-{inner}:3px solid #FFCA28; }}'
                f'QPushButton#sectionToggle {{ text-align:{align}; }}')
            self.import_page.update_plan()
            self.analyze_page.set_progress(self.analyze_page.progress.value())
            from core.export_queue import save
            if '--smoke' not in sys.argv:save(REPO_ROOT/'projects'/'interface.json',{'language':language})
            self.tools_page.set_language(language)
            self.review_page.editor.set_language(language)
            for i,label in enumerate(['المقاطع','الكابشن','الكادر']):self.review_page.tabs.setTabText(i,translate(label,language))
            self.review_page.update_summary()
            self.review_page.show_excerpt()
            self.export_page.refresh()
            self.library_page.refresh()
            self.queue_page.refresh()
            self.watch_page.refresh_texts()
            self.go_to(self._order[self.stack.currentIndex()])

        def go_to(self, key: str) -> None:
            self.stack.setCurrentIndex(self._order.index(key))
            self.nav_buttons[key].setChecked(True)
            if key == "queue":
                self.queue_page.refresh()
            if key != "watch":
                self.watch_page.stop_stream(keep_url=True)
            title, sub = STRINGS["pages"][key]
            self.top_title.setText(title)
            self.top_sub.setText(sub)

        def heavy_busy(self):
            from core.export_queue import read
            runner=self.analyze_page.runner
            queue=self.export_page.queue
            state=read(queue/'status.json') if queue else {}
            return bool((runner and runner.isRunning()) or self.queue_page.is_running()
                        or (state.get('state')=='running' and time.time()-state.get('heartbeat',0)<30))

        def accept_source(self,path):
            self.import_page.set_source(path)
            self.go_to('imp')

        def accept_stream(self,url):
            from core.media_download import launch
            destination = REPO_ROOT / "downloads"
            try:
                folder = launch(url, destination, 720, "", "inspect")
            except Exception as exc:
                QMessageBox.warning(self, STRINGS["nav"]["watch"], str(exc))
                return
            self.go_to("tools")
            self.tools_page.jobs["download"] = folder
            self.tools_page.persist_jobs()
            self.tools_page.refresh()
            self.tools_page.tabs.setCurrentIndex(0)

        def open_project(self,folder):
            self.review_page.set_project(folder);self.export_page.set_project(folder);self.change_language();self.go_to('rev')

        def start_pipeline(self, video: str, settings: dict) -> None:
            if self.analyze_page.runner and self.analyze_page.runner.isRunning():
                self.go_to('ana');return
            if self.heavy_busy() or self.tools_page.is_busy():
                QMessageBox.information(self,tr('مهمة تعمل'),tr('انتظر المهمة الحالية قبل بدء مهمة ثقيلة أخرى'));return
            self.review_page.export_button.setEnabled(False)
            self.go_to("ana")
            self.analyze_page.start_run(video, settings)

        def start_export(self,folder,queue):
            self.export_page.start_queue(folder,queue);self.go_to("exp")

        def after_run(self,rc=0) -> None:
            if rc!=0:return
            project = self.analyze_page.project_dir
            if project:
                self.review_page.set_project(project)
                self.export_page.set_project(project)
            self.library_page.refresh()
            self.change_language()
            self.go_to("rev")

        def closeEvent(self,event):
            runner=self.analyze_page.runner
            if (runner and runner.isRunning()) or self.queue_page.is_running() or (self.export_page.archive_worker and self.export_page.archive_worker.isRunning()):
                QMessageBox.information(self,"مهمة تعمل","أوقف التحليل أو انتظر استخراج الصوت قبل إغلاق النافذة")
                event.ignore();return
            self.review_page.persist()
            self.watch_page.stop_stream(keep_url=True)
            event.accept()
