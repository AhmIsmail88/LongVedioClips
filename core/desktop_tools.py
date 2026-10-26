"""Native Qt tools; no browser or web server involved."""
import os,time
from pathlib import Path
from PySide6.QtCore import Qt,QTimer
from PySide6.QtWidgets import (QWidget,QVBoxLayout,QHBoxLayout,QFormLayout,QLabel,QLineEdit,QPushButton,
    QComboBox,QCheckBox,QSpinBox,QDoubleSpinBox,QTabWidget,QProgressBar,QListWidget,QListWidgetItem,
    QFileDialog,QMessageBox,QPlainTextEdit)
from core.export_queue import read
from core.desktop_language import translate
from core.media_download import (COOKIES_HINT, PLATFORM_NAMES, URL_HINT, platform_of,
                                 sign_in_required)

class NativeTools(QWidget):
    def __init__(self,on_source=None,busy=None,parent=None):
        super().__init__(parent);self.on_source=on_source;self.busy=busy or (lambda:False);self.language='ar';self.jobs={k:Path(v) for k,v in read(Path(__file__).resolve().parents[1]/'projects'/'native_tools.json').items()};self.active=None
        root=QVBoxLayout(self);root.setContentsMargins(24,18,24,18);root.setSpacing(14)
        self.tabs=QTabWidget();root.addWidget(self.tabs)
        self.controls={};self.tab_labels=['تحميل الروابط','استخراج الصوت','استخراج النص','تقسيم الصوت']
        for operation,label in zip(['download','audio','text','split'],self.tab_labels,strict=True):
            page=QWidget();layout=QVBoxLayout(page);layout.setContentsMargins(20,20,20,20);layout.setSpacing(14)
            form=QFormLayout();form.setSpacing(12);layout.addLayout(form)
            source=QLineEdit();source.setLayoutDirection(Qt.LeftToRight)
            pick=QPushButton('اختيار ملف');pick.clicked.connect(lambda unused=False,op=operation:self.pick_source(op))
            source_row=QHBoxLayout();source_row.addWidget(source,1)
            if operation!='download':source_row.addWidget(pick)
            form.addRow(QLabel('رابط فيديو واحد' if operation=='download' else 'ملف الفيديو أو الصوت'),source_row)
            directory=QLineEdit(str(Path(__file__).resolve().parents[1]/('downloads' if operation=='download' else 'media_exports')))
            directory.setLayoutDirection(Qt.LeftToRight);choose=QPushButton('مكان الحفظ');choose.clicked.connect(lambda unused=False,op=operation:self.pick_directory(op))
            directory_row=QHBoxLayout();directory_row.addWidget(directory,1);directory_row.addWidget(choose);form.addRow(QLabel('مجلد الحفظ'),directory_row)
            values=dict(source=source,directory=directory,page=page)
            if operation=='download':
                kind=QComboBox();kind.addItem('فيديو','video');kind.addItem('صوت فقط (MP3)','audio')
                height=QComboBox()
                for size in [360,480,720,1080]:height.addItem(str(size),size)
                height.setCurrentIndex(2);kind.currentIndexChanged.connect(lambda unused,k=kind,h=height:h.setEnabled(k.currentData()=='video'))
                cookies=QLineEdit();cookies.setLayoutDirection(Qt.LeftToRight)
                form.addRow(QLabel('نوع التحميل'),kind);form.addRow(QLabel('جودة التحميل القصوى'),height)
                form.addRow(QLabel('ملف cookies اختياري — Netscape .txt'),cookies)
                inspect=QPushButton('عرض معلومات الفيديو');inspect.clicked.connect(self.inspect);layout.addWidget(inspect)
                platform_label=QLabel(URL_HINT);platform_label.setWordWrap(True);platform_label.setObjectName('hint')
                form.insertRow(1,QLabel('المنصة'),platform_label)
                source.textChanged.connect(lambda text,op=operation:self.update_platform_hint(op,text))
                values.update(kind=kind,height=height,cookies=cookies,platform=platform_label)
            elif operation=='audio':
                fmt=QComboBox();fmt.addItem('MP3','mp3');fmt.addItem('WAV · mono 16kHz','wav')
                bitrate=QComboBox()
                for rate in [128,192,256,320]:bitrate.addItem(str(rate),rate)
                bitrate.setCurrentIndex(1);fmt.currentIndexChanged.connect(lambda unused,f=fmt,b=bitrate:b.setEnabled(f.currentData()=='mp3'))
                full=QCheckBox('الملف كاملًا');full.setChecked(True)
                start=QDoubleSpinBox();end=QDoubleSpinBox()
                for spin in [start,end]:spin.setRange(0,864000);spin.setDecimals(2);spin.setEnabled(False)
                end.setValue(60)
                full.toggled.connect(lambda checked,s=start,e=end:(s.setEnabled(not checked),e.setEnabled(not checked)))
                form.addRow(QLabel('صيغة الصوت'),fmt);form.addRow(QLabel('جودة MP3 (kbps)'),bitrate);form.addRow(full)
                form.addRow(QLabel('البداية (ث)'),start);form.addRow(QLabel('النهاية (ث)'),end)
                values.update(format=fmt,bitrate=bitrate,full=full,start=start,end=end)
            else:
                language=QComboBox();language.addItem('تلقائي','');language.addItem('العربية','ar');language.addItem('English','en')
                model=QComboBox();model.addItems(['small','medium','large-v3']);model.setCurrentText('medium')
                form.addRow(QLabel('لغة الكلام'),language);form.addRow(QLabel('Whisper'),model)
                values.update(language=language,model=model)
                if operation=='text':
                    timestamps=QCheckBox('إظهار الطوابع الزمنية');form.addRow(timestamps);values['timestamps']=timestamps
                else:
                    mode=QComboBox();mode.addItem('حسب عدد الأجزاء','count');mode.addItem('حسب مدة الجزء','duration')
                    count=QSpinBox();count.setRange(1,1000);count.setValue(3)
                    seconds=QDoubleSpinBox();seconds.setRange(3,86400);seconds.setValue(90);seconds.setEnabled(False)
                    mode.currentIndexChanged.connect(lambda unused,m=mode,c=count,s=seconds:(c.setEnabled(m.currentData()=='count'),s.setEnabled(m.currentData()=='duration')))
                    pauses=QCheckBox('قص عند وقفات الكلام');fmt=QComboBox();fmt.addItem('MP3','mp3');fmt.addItem('M4A','m4a')
                    form.addRow(QLabel('طريقة التقسيم'),mode);form.addRow(QLabel('عدد الأجزاء'),count);form.addRow(QLabel('مدة الجزء (ث)'),seconds)
                    form.addRow(QLabel('صيغة الصوت'),fmt);form.addRow(pauses)
                    values.update(mode=mode,count=count,seconds=seconds,pauses=pauses,format=fmt)
            run=QPushButton('تحميل / استكمال' if operation=='download' else 'ابدأ المهمة');run.setObjectName('primary')
            run.clicked.connect(lambda unused=False,op=operation:self.start(op));layout.addWidget(run);values['run']=run
            self.controls[operation]=values;self.tabs.addTab(page,label)
        self.status=QLabel('اختار الأداة والملف للبدء');self.status.setWordWrap(True);root.addWidget(self.status)
        self.progress=QProgressBar();self.progress.setRange(0,100);root.addWidget(self.progress)
        self.results=QListWidget();self.results.itemDoubleClicked.connect(self.open_result);root.addWidget(self.results,1)
        self.text=QPlainTextEdit();self.text.setReadOnly(True);self.text.setMaximumHeight(190);self.text.hide();root.addWidget(self.text)
        actions=QHBoxLayout();self.cancel=QPushButton('إلغاء بعد الخطوة الحالية');self.cancel.clicked.connect(self.cancel_job);actions.addWidget(self.cancel)
        self.use_video=QPushButton('استخدم الفيديو للتحليل');self.use_video.clicked.connect(self.use_for_video);actions.addWidget(self.use_video)
        self.use_text=QPushButton('استخدم الصوت لاستخراج النص');self.use_text.clicked.connect(lambda:self.use_for('text'));actions.addWidget(self.use_text)
        self.use_split=QPushButton('استخدم الصوت للتقسيم');self.use_split.clicked.connect(lambda:self.use_for('split'));actions.addWidget(self.use_split)
        root.addLayout(actions);self.tabs.currentChanged.connect(self.refresh)
        for operation,folder in self.jobs.items():
            if operation not in self.controls:continue
            request=read(folder/'request.json');values=self.controls[operation]
            for key,field in [('source','url' if operation=='download' else 'source'),('directory','directory'),('cookies','cookies')]:
                if key in values and field in request:values[key].setText(str(request[field]))
            for key,field in [('kind','media_kind'),('height','height'),('format','format'),('bitrate','bitrate'),('language','language')]:
                if key in values and field in request:
                    index=values[key].findData(request[field])
                    if index>=0:values[key].setCurrentIndex(index)
        self.timer=QTimer(self);self.timer.timeout.connect(self.refresh);self.timer.start(1200);self.refresh()

    def persist_jobs(self):
        from core.export_queue import save
        save(Path(__file__).resolve().parents[1]/'projects'/'native_tools.json',{k:str(v) for k,v in self.jobs.items()})

    def tr(self,text):return translate(text,self.language)

    def update_platform_hint(self,operation,text):
        values=self.controls.get(operation) or {}
        label=values.get('platform')
        if label is None:return
        link=(text or '').strip()
        if not link:
            label.setText(self.tr(URL_HINT));return
        platform=platform_of(link)
        name=self.tr(PLATFORM_NAMES.get(platform,PLATFORM_NAMES['other']))
        label.setText(name+' — '+self.tr(COOKIES_HINT) if sign_in_required(platform) else name)
    def set_language(self,language):
        self.language=language
        for index,label in enumerate(self.tab_labels):self.tabs.setTabText(index,self.tr(label))
        if 'download' in self.controls:
            self.update_platform_hint('download',self.controls['download']['source'].text())
        self.refresh()
    def current_operation(self):return ['download','audio','text','split'][self.tabs.currentIndex()]
    def pick_source(self,operation):
        path,_=QFileDialog.getOpenFileName(self,self.tr('اختيار ملف'),'','Media (*.mp4 *.mkv *.mov *.avi *.webm *.mp3 *.wav *.m4a *.aac *.ogg *.flac *.opus);;All files (*)')
        if path:self.controls[operation]['source'].setText(path)
    def pick_directory(self,operation):
        path=QFileDialog.getExistingDirectory(self,self.tr('مكان الحفظ'),self.controls[operation]['directory'].text())
        if path:self.controls[operation]['directory'].setText(path)
    def is_busy(self):
        for operation,folder in self.jobs.items():
            state=read(folder/'status.json')
            if operation!='download' and state.get('state')=='running' and time.time()-state.get('heartbeat',0)<30:return True
        return False
    def inspect(self):
        from core.media_download import launch
        values=self.controls['download']
        try:
            self.jobs['download']=launch(values['source'].text(),values['directory'].text(),values['height'].currentData(),
                values['cookies'].text().strip(),'inspect',media_kind=values['kind'].currentData())
            self.persist_jobs();self.refresh()
        except Exception as exc:QMessageBox.warning(self,self.tr('التحميل'),self.tr(str(exc)))
    def start(self,operation):
        values=self.controls[operation]
        try:
            if operation=='download':
                from core.media_download import launch
                folder=self.jobs.get(operation)
                if not folder or not read(folder/'info.json'):raise ValueError(self.tr('اعرض معلومات الفيديو أولًا'))
                request=read(folder/'request.json')
                if not self.matches_download(request):raise ValueError(self.tr('الإعدادات تغيرت؛ اعرض المعلومات مجددًا'))
                self.jobs[operation]=launch(request['url'],request['directory'],request['height'],request.get('cookies',''),
                    'download',media_kind=request.get('media_kind','video'))
            else:
                if self.is_busy() or self.busy():raise ValueError(self.tr('انتظر المهمة الحالية قبل بدء مهمة ثقيلة أخرى'))
                from core.desktop_tasks import launch
                request=dict(source=values['source'].text().strip().strip('"'),directory=values['directory'].text().strip())
                if operation=='audio':request.update(format=values['format'].currentData(),bitrate=values['bitrate'].currentData(),
                    start=0 if values['full'].isChecked() else values['start'].value(),end=None if values['full'].isChecked() else values['end'].value())
                else:
                    request.update(language=values['language'].currentData(),model=values['model'].currentText())
                    if operation=='text':request['timestamps']=values['timestamps'].isChecked()
                    else:request.update(num_parts=values['count'].value() if values['mode'].currentData()=='count' else None,
                        part_seconds=values['seconds'].value() if values['mode'].currentData()=='duration' else None,
                        speech_pauses=values['pauses'].isChecked(),format=values['format'].currentData())
                self.jobs[operation]=launch(operation,request)
            self.persist_jobs();self.refresh()
        except Exception as exc:QMessageBox.warning(self,self.tr('الأدوات'),self.tr(str(exc)))
    def matches_download(self,request):
        from core.media_download import validate_url
        values=self.controls['download']
        try:return (validate_url(values['source'].text())==request['url'] and
            Path(values['directory'].text()).resolve()==Path(request['directory']).resolve() and
            values['height'].currentData()==request['height'] and values['kind'].currentData()==request.get('media_kind','video') and
            values['cookies'].text().strip()==request.get('cookies',''))
        except (ValueError,KeyError,OSError):return False
    def refresh(self,*unused):
        operation=self.current_operation();folder=self.jobs.get(operation)
        self.text.hide();self.active=None
        self.use_video.setEnabled(False);self.use_text.setEnabled(False);self.use_split.setEnabled(False);self.cancel.setEnabled(False)
        if not folder:self.results.clear();self.progress.setValue(0);self.status.setText(self.tr('اختار الأداة والملف للبدء'));return
        state=read(folder/'status.json');live=state.get('state')=='running'
        self.cancel.setEnabled(live);self.controls[operation]['run'].setEnabled(not live)
        self.progress.setValue(round(float(state.get('progress',0))*100))
        names={'running':'جارٍ العمل','finished':'انتهى','failed':'حدث خطأ','cancelled':'تم الإيقاف'}
        message=self.tr(names.get(state.get('state'),'جارٍ العمل'))
        if live and time.time()-state.get('heartbeat',0)>30:message=self.tr('المهمة لا تستجيب؛ يمكن إعادة المحاولة')
        if state.get('error'):message+=' · '+str(state['error'])
        files=[]
        if operation=='download':
            from core.media_download import intact
            metadata=read(folder/'info.json')
            if metadata:message+=' · '+str(metadata.get('title',''))
            request=read(folder/'request.json');self.controls[operation]['run'].setEnabled(not live and bool(metadata) and self.matches_download(request))
            result=state.get('result') or read(folder/'result.json')
            if intact(result):files=[dict(path=result['path'])]
        else:files=state.get('result',{}).get('files',[])
        self.status.setText(message)
        signature=tuple(f['path'] for f in files)
        changed=signature!=getattr(self,'_files',None)
        if changed:self.results.clear();self._files=signature
        for file in files:
            path=Path(file['path']);item=QListWidgetItem(path.name);item.setData(Qt.UserRole,str(path));self.results.addItem(item) if changed else None
        media=next((Path(f['path']) for f in files if Path(f['path']).suffix.lower() in ('.mp4','.mkv','.webm','.mov','.mp3','.wav','.m4a')),None)
        self.active=str(media) if media else None
        if media:
            self.use_video.setEnabled(media.suffix.lower() in ('.mp4','.mkv','.webm','.mov'))
            self.use_text.setEnabled(True);self.use_split.setEnabled(True)
        text_path=state.get('text_path')
        if text_path and Path(text_path).is_file():
            
            text_key=(text_path,Path(text_path).stat().st_mtime_ns)
            if text_key!=getattr(self,'_text_key',None):self.text.setPlainText(Path(text_path).read_text(encoding='utf-8'));self._text_key=text_key
            self.text.show()
    def cancel_job(self):
        folder=self.jobs.get(self.current_operation())
        if folder:(folder/'cancel').touch();self.status.setText(self.tr('طُلب الإلغاء؛ التوقف بعد الخطوة الحالية'))
    def open_result(self,item):
        path=item.data(Qt.UserRole)
        if path and Path(path).is_file():os.startfile(path)
    def use_for_video(self):
        if self.active and self.on_source:self.on_source(self.active)
    def use_for(self,operation):
        if self.active:
            self.controls[operation]['source'].setText(self.active);self.tabs.setCurrentIndex(['download','audio','text','split'].index(operation))
