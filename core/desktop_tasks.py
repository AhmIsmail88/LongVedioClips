"""Background tasks for the single native desktop interface."""
import hashlib,json,os,subprocess,sys,threading,time,zipfile
from core.child_process import python_command
from pathlib import Path
from core.export_queue import read,save
from utils.resume import video_identity

class Cancelled(Exception):pass

def intact(result):
    try:
        return bool(result.get('files')) and all(Path(f['path']).stat().st_size==f['size'] and
            Path(f['path']).stat().st_mtime_ns==f['mtime_ns'] for f in result['files'])
    except (OSError,KeyError,TypeError):return False

def launch(operation,request):
    if operation not in ('audio','text','split'):raise ValueError('عملية غير مدعومة')
    if not str(request.get('directory','')).strip():raise ValueError('اختر مجلد الحفظ')
    source=Path(request['source']).resolve()
    if not source.is_file():raise ValueError('ملف المصدر غير موجود')
    identity=video_identity(source);request={**request,'source':str(source),'operation':operation,'source_identity':identity}
    key=hashlib.sha256(json.dumps(request,sort_keys=True).encode()).hexdigest()[:20]
    from core.app_paths import DATA_ROOT
    root=DATA_ROOT
    folder=root/'projects'/'tasks'/key;folder.mkdir(parents=True,exist_ok=True)
    status=read(folder/'status.json')
    if status.get('state')=='running' and time.time()-status.get('heartbeat',0)<30:raise ValueError('المهمة تعمل بالفعل')
    if intact(status.get('result',{})):return folder
    (folder/'cancel').unlink(missing_ok=True)
    request['key']=key;save(folder/'request.json',request)
    save(folder/'status.json',dict(state='running',phase='startup',heartbeat=time.time(),progress=0))
    flags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0
    try:
        with (folder/'worker.log').open('a',encoding='utf-8') as log:
            subprocess.Popen(python_command('core.desktop_tasks', folder),cwd=root,
                stdout=log,stderr=log,creationflags=flags)
    except Exception:
        save(folder/'status.json',dict(state='failed',error='تعذر تشغيل المهمة'));raise
    return folder

def run(folder):
    import re
    from utils.logger import Logger
    folder=Path(folder);request=read(folder/'request.json');done=threading.Event()
    state=dict(state='running',progress=0,phase=request['operation'],pid=os.getpid())
    def check():
        if (folder/'cancel').exists():raise Cancelled()
    def heartbeat():
        while not done.is_set():
            state['heartbeat']=time.time();save(folder/'status.json',dict(state));done.wait(1)
    thread=threading.Thread(target=heartbeat,daemon=True);thread.start()
    class TaskLogger(Logger):
        def info(self,message):
            check();super().info(message);state['message']=message
            transcribe=re.search(r'Transcribing\.\.\. (\d+)%',message)
            part=re.search(r'Part (\d+)/(\d+) written',message)
            if transcribe:state['progress']=min(.95,int(transcribe[1])/100*.9)
            if part:state['progress']=min(.98,int(part[1])/int(part[2]))
        def stage(self,index,message):check();super().stage(index,message)
    logger=TaskLogger()
    def transcribe():
        from core.transcriber import transcribe as whisper
        return whisper(video_path=request['source'],model_size=request.get('model','medium'),
            language=request.get('language') or None,device=request.get('device','auto'),
            compute_type_gpu='float16',compute_type_cpu='int8',logger=logger)
    try:
        check()
        if video_identity(request['source'])!=request['source_identity']:raise ValueError('تغير ملف المصدر؛ أعد بدء المهمة')
        output=Path(request['directory']).resolve()/f"{Path(request['source']).stem}_{request['key']}"
        output.mkdir(parents=True,exist_ok=True);files=[]
        if request['operation']=='audio':
            from core.audio_export import extract
            fmt=request.get('format','mp3');path=output/f'audio.{fmt}'
            extract(request['source'],path,start=float(request.get('start',0)),end=request.get('end'),
                fmt=fmt,bitrate=int(request.get('bitrate',192)))
            files=[path]
        elif request['operation']=='text':
            from utils.docx_export import segments_to_text,segments_to_docx_bytes
            from core.captions import transcript_to_srt
            segments=transcribe();check()
            text=segments_to_text(segments,include_timestamps=bool(request.get('timestamps')))
            files=[output/'transcript.txt',output/'transcript.docx',output/'captions.srt']
            files[0].write_text(text,encoding='utf-8')
            files[1].write_bytes(segments_to_docx_bytes(segments,include_timestamps=bool(request.get('timestamps'))))
            files[2].write_text(transcript_to_srt(segments),encoding='utf-8')
            state['text_path']=str(files[0]);save(output/'segments.json',[s.to_dict() for s in segments])
        else:
            from utils.audio_split import split_audio
            segments=transcribe() if request.get('speech_pauses') else None;check()
            files=split_audio(request['source'],output,num_parts=request.get('num_parts'),
                part_seconds=request.get('part_seconds'),out_format=request.get('format','mp3'),
                segments=segments,logger=logger)
            check();archive=output/'audio_parts.zip'
            with zipfile.ZipFile(archive,'w',compression=zipfile.ZIP_STORED) as bundle:
                for path in files:check();bundle.write(path,path.name)
            files.append(archive)
        check()
        state.update(state='finished',progress=1,result={'files':[dict(path=str(p.resolve()),size=p.stat().st_size,mtime_ns=p.stat().st_mtime_ns) for p in files],
            'directory':str(output),'operation':request['operation']})
    except Cancelled:state['state']='cancelled'
    except Exception as exc:
        state.update(state='failed',error=str(exc));print(type(exc).__name__+': '+str(exc),flush=True)
    finally:
        done.set();thread.join();state['heartbeat']=time.time();save(folder/'status.json',state)

if __name__=='__main__':run(sys.argv[1])
