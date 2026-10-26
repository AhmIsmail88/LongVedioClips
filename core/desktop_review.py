"""Native caption and framing editors for the desktop review page."""
import math
from pathlib import Path
from config import Config
from core.export_queue import read,save
from utils.resume import video_identity

def load_project(folder):
    folder=Path(folder);snapshot=read(folder/'analysis.json')
    if not snapshot:raise ValueError('افتح مشروعًا أولًا')
    if video_identity(snapshot['video_path'])!=snapshot['source_identity']:raise ValueError('تغير ملف المصدر؛ أعد التحليل')
    return snapshot,Config(**snapshot['config'])

def save_captions(folder,rows):
    from core.review_tools import edited_segments
    from models.schemas import TranscriptSegment
    snapshot,cfg=load_project(folder)
    original=[TranscriptSegment.from_dict(s) for s in snapshot.get('segments',[])]
    edited=edited_segments(rows,original,float(snapshot['duration']))
    save(cfg.project_output_dir()/'caption_edits.json',dict(source=snapshot['source_identity'],segments=[s.to_dict() for s in edited]))
    return edited

def save_crop(folder,index,enabled,center,zoom):
    snapshot,cfg=load_project(folder)
    if index not in [c['index'] for c in snapshot['final_clips']]:raise ValueError('مقطع غير موجود')
    if not (math.isfinite(center) and math.isfinite(zoom) and 0<=center<=1 and 1<=zoom<=3):raise ValueError('كادر غير صالح')
    path=cfg.project_output_dir()/'crop_edits.json';previous=read(path)
    crops=previous.get('crops',{}) if previous.get('source')==snapshot['source_identity'] else {}
    if enabled:crops[str(index)]=dict(center=center,zoom=zoom)
    else:crops.pop(str(index),None)
    save(path,dict(source=snapshot['source_identity'],crops=crops))

def propose_alternative(folder,index,rows):
    import uuid
    from models.schemas import FinalClip
    snapshot,cfg=load_project(folder)
    ranked=sorted(read(cfg.project_output_dir()/'scores.json').get('clips',[]),key=lambda c:c.get('score',0),reverse=True)
    alternate=next((c for c in ranked if c.get('score',0)>=50 and 0<=c['start']<c['end']<=snapshot['duration'] and not any(
        max(0,min(c['end'],row['end'])-max(c['start'],row['start']))>.15*min(c['end']-c['start'],row['end']-row['start'])
        for row in rows if row['end']>row['start'])),None)
    if not alternate:return False
    replacement=FinalClip.from_dict(dict(alternate,index=index,title='')).to_dict()
    snapshot['final_clips']=[replacement if c['index']==index else c for c in snapshot['final_clips']]
    snapshot['revision']=uuid.uuid4().hex;save(Path(folder)/'analysis.json',snapshot)
    save(Path(folder)/'desktop_review.json',[dict(replacement,selected=True,focus='largest') if row['index']==index else row for row in rows])
    return True

from PySide6.QtCore import QObject,Qt,QTimer
from PySide6.QtGui import QImage,QPixmap
from PySide6.QtWidgets import (QWidget,QVBoxLayout,QHBoxLayout,QFormLayout,QLabel,QTableWidget,QTableWidgetItem,
    QDoubleSpinBox,QPushButton,QComboBox,QCheckBox,QHeaderView,QMessageBox)

def crop_box(width: int, height: int, center: float = 0.5, zoom: float = 1.0):
    """مستطيل القص 9:16 داخل الإطار الأصلي — دالة خالصة قابلة للاختبار."""
    width, height = int(width), int(height)
    if width <= 0 or height <= 0:
        raise ValueError('أبعاد إطار غير صالحة')
    crop_h = height
    crop_w = int(round(height * 9 / 16))
    if crop_w > width:
        crop_w = width
        crop_h = int(round(width * 16 / 9))
    if not 0 <= center <= 1:
        center = .5
    if zoom and zoom > 1:
        crop_w = max(2, int(round(crop_w / zoom)))
        crop_h = max(2, int(round(crop_h / zoom)))
    left = int(round(max(0, min(width - crop_w, center * width - crop_w / 2))))
    top = int(round(max(0, min(height - crop_h, (height - crop_h) / 2))))
    return left, top, crop_w, crop_h


class NativeReviewEditor(QObject):
    def __init__(self,parent=None):
        super().__init__(parent);self.folder=None;self.snapshot={};self.language='ar'
        self.caption_page=QWidget();layout=QVBoxLayout(self.caption_page)
        hint=QLabel('عدّل نص الكابشن وتوقيته ثم احفظ؛ الأسطر غير المعدلة تحتفظ بتوقيت الكلمات');hint.setWordWrap(True);layout.addWidget(hint)
        self.captions=QTableWidget(0,3);self.captions.setHorizontalHeaderLabels(['البداية (ث)','النهاية (ث)','النص'])
        self.captions.horizontalHeader().setSectionResizeMode(2,QHeaderView.Stretch);layout.addWidget(self.captions,1)
        save_button=QPushButton('احفظ تعديلات الكابشن');save_button.setObjectName('primary');save_button.clicked.connect(self.save_text);layout.addWidget(save_button)
        self.crop_page=QWidget();layout=QVBoxLayout(self.crop_page);form=QFormLayout();layout.addLayout(form)
        self.clip=QComboBox();self.clip.currentIndexChanged.connect(self.load_crop);form.addRow(QLabel('المقطع'),self.clip)
        self.manual=QCheckBox('كادر ثابت يدوي لهذا المقطع');form.addRow(self.manual)
        self.center=QDoubleSpinBox();self.center.setRange(0,1);self.center.setSingleStep(.01);self.center.setValue(.5);self.center.setDecimals(2)
        self.zoom=QDoubleSpinBox();self.zoom.setRange(1,3);self.zoom.setSingleStep(.05);self.zoom.setDecimals(2)
        self.moment=QDoubleSpinBox();self.moment.setRange(0,86400);self.moment.setDecimals(2)
        form.addRow(QLabel('مركز الكادر من اليسار إلى اليمين'),self.center);form.addRow(QLabel('التقريب'),self.zoom)
        form.addRow(QLabel('موضع المعاينة داخل المقطع (ث)'),self.moment)
        actions=QHBoxLayout();preview=QPushButton('اعرض معاينة الكادر');preview.clicked.connect(self.preview);actions.addWidget(preview)
        save_button=QPushButton('احفظ الكادر لهذا المقطع');save_button.setObjectName('primary');save_button.clicked.connect(self.save_framing);actions.addWidget(save_button);layout.addLayout(actions)
        compare=QHBoxLayout();compare.setSpacing(12)
        before_col=QVBoxLayout();self.before_title=QLabel('قبل — الإطار الكامل');self.before_title.setObjectName('hint')
        before_col.addWidget(self.before_title)
        self.original=QLabel('اعرض معاينة الكادر');self.original.setObjectName('thumb');self.original.setAlignment(Qt.AlignCenter);self.original.setMinimumHeight(220)
        before_col.addWidget(self.original,1)
        after_col=QVBoxLayout();self.after_title=QLabel('بعد — الناتج 9:16');self.after_title.setObjectName('hint')
        after_col.addWidget(self.after_title)
        self.framed=QLabel('اعرض معاينة الكادر');self.framed.setObjectName('thumb');self.framed.setAlignment(Qt.AlignCenter);self.framed.setMinimumHeight(220)
        after_col.addWidget(self.framed,1)
        compare.addLayout(before_col,3);compare.addLayout(after_col,2);layout.addLayout(compare,1)
        self.compare_info=QLabel('');self.compare_info.setObjectName('hint');layout.addWidget(self.compare_info)
        self.preview_timer=QTimer(self.crop_page);self.preview_timer.setSingleShot(True);self.preview_timer.setInterval(350)
        self.preview_timer.timeout.connect(lambda:self.preview(quiet=True))
        for signal in (self.center.valueChanged,self.zoom.valueChanged,self.moment.valueChanged,self.manual.toggled):
            signal.connect(lambda *unused:self.preview_timer.start())

    def set_language(self,language):
        self.language=language

    def t(self,text):
        from core.desktop_language import translate
        return translate(str(text),self.language)
    def set_project(self,folder,snapshot):
        self.folder=Path(folder);self.snapshot=snapshot;cfg=Config(**snapshot['config'])
        edited=read(cfg.project_output_dir()/'caption_edits.json')
        segments=edited.get('segments',[]) if edited.get('source')==snapshot['source_identity'] else snapshot.get('segments',[])
        self.captions.setRowCount(len(segments))
        for row,segment in enumerate(segments):
            for col,name in enumerate(['start','end','text']):self.captions.setItem(row,col,QTableWidgetItem(str(segment[name])))
        self.clip.blockSignals(True);self.clip.clear()
        for clip in snapshot['final_clips']:self.clip.addItem(str(clip['index']),clip['index'])
        self.clip.blockSignals(False);self.load_crop()
    def save_text(self):
        if not self.folder:return
        try:
            rows=[dict(start=float(self.captions.item(row,0).text()),end=float(self.captions.item(row,1).text()),text=self.captions.item(row,2).text()) for row in range(self.captions.rowCount())]
            save_captions(self.folder,rows)
            QMessageBox.information(self.caption_page,self.t('الكابشن'),self.t('تم الحفظ'))
        except Exception as exc:QMessageBox.warning(self.caption_page,self.t('الكابشن'),self.t(exc))
    def load_crop(self,*unused):
        if not self.folder or self.clip.currentData() is None:return
        index=self.clip.currentData();cfg=Config(**self.snapshot['config']);saved=read(cfg.project_output_dir()/'crop_edits.json')
        crop=saved.get('crops',{}).get(str(index),{}) if saved.get('source')==self.snapshot['source_identity'] else {}
        self.manual.setChecked(bool(crop));self.center.setValue(crop.get('center',.5));self.zoom.setValue(crop.get('zoom',1))
        clip=next(c for c in self.snapshot['final_clips'] if c['index']==index);self.moment.setRange(0,max(0,clip['end']-clip['start']-.04));self.moment.setValue(0)
        self.preview_timer.start()
    def save_framing(self):
        if not self.folder or self.clip.currentData() is None:return
        try:
            save_crop(self.folder,self.clip.currentData(),self.manual.isChecked(),self.center.value(),self.zoom.value())
            QMessageBox.information(self.crop_page,self.t('الكادر'),self.t('تم الحفظ'))
        except Exception as exc:QMessageBox.warning(self.crop_page,self.t('الكادر'),self.t(exc))
    def _show(self,label,image):
        import cv2
        rgb=cv2.cvtColor(image,cv2.COLOR_BGR2RGB);hh,ww=rgb.shape[:2]
        pixmap=QPixmap.fromImage(QImage(rgb.data,ww,hh,rgb.strides[0],QImage.Format_RGB888).copy())
        width=max(260,label.width()-8);height=max(200,label.height()-8)
        label.setPixmap(pixmap.scaled(width,height,Qt.KeepAspectRatio,Qt.SmoothTransformation))

    def _readable_source(self):
        path=Path(str(self.snapshot.get('video_path','')))
        try:
            if not path.is_file() or path.stat().st_size<1024:return False
            with path.open('rb') as handle:
                head=handle.read(16)
        except OSError:
            return False
        return head[4:8] in (b'ftyp',b'moov',b'mdat') or head[:4] in (b'\x1aE\xdf\xa3',b'RIFF')

    def preview(self,quiet=False):
        if not self.folder or self.clip.currentData() is None:return
        try:
            if not self._readable_source():raise ValueError('تعذر قراءة صورة المعاينة')
            import cv2
            clip=next(c for c in self.snapshot['final_clips'] if c['index']==self.clip.currentData())
            cap=cv2.VideoCapture(self.snapshot['video_path'])
            try:
                cap.set(cv2.CAP_PROP_POS_MSEC,(clip['start']+self.moment.value())*1000);okay,frame=cap.read()
            finally:cap.release()
            if not okay:raise ValueError('تعذر قراءة صورة المعاينة')
            manual=self.manual.isChecked();center=self.center.value() if manual else .5;zoom=self.zoom.value() if manual else 1.0
            h,w=frame.shape[:2];left,top,cw,ch=crop_box(w,h,center,zoom)
            marked=frame.copy()
            cv2.rectangle(marked,(left,top),(left+cw,top+ch),(40,202,255),max(2,round(min(w,h)/160)))
            self._show(self.original,marked);self._show(self.framed,frame[top:top+ch,left:left+cw])
            self.compare_info.setText(f"{self.t('الكادر')} {cw}×{ch}   ·   {self.t('مركز')} {center:.2f}   ·   {self.t('تقريب')} {zoom:.2f}")
        except Exception as exc:
            if quiet:
                self.original.setText(self.t('تعذر قراءة صورة المعاينة'));self.framed.clear();self.compare_info.clear()
            else:
                QMessageBox.warning(self.crop_page,self.t('المعاينة'),self.t(exc))
