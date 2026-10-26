"""Standalone background audio extraction without speech/LLM models."""
import hashlib,json,os,subprocess,sys,time
from core.child_process import python_command
from pathlib import Path
from core.export_queue import save,read
from utils.ffmpeg import get_ffmpeg_binaries,probe_duration
from utils.resume import video_identity

def extract(source,target,start=0,end=None,fmt="mp3",bitrate=192):
    if fmt not in ("mp3","wav"):raise ValueError("صيغة غير مدعومة")
    duration=probe_duration(str(source));end=duration if end is None else end
    if not 0<=start<end<=duration+0.01:raise ValueError("حدد بداية ونهاية صحيحتين داخل مدة الفيديو")
    target=Path(target);target.parent.mkdir(parents=True,exist_ok=True)
    temporary=target.with_name(target.stem+".partial"+target.suffix)
    ffmpeg,_=get_ffmpeg_binaries()
    cmd=[ffmpeg,"-y","-hide_banner","-loglevel","error","-ss",str(start),"-i",str(source),"-t",str(end-start),"-vn","-map","0:a:0","-threads","2"]
    cmd+=(["-c:a","libmp3lame","-b:a",f"{bitrate}k"] if fmt=="mp3" else ["-c:a","pcm_s16le","-ac","1","-ar","16000"])
    cmd+=[str(temporary)]
    try:
        result=subprocess.run(cmd,capture_output=True,text=True)
        if result.returncode:
            raise ValueError("تعذر استخراج الصوت؛ تأكد أن الفيديو يحتوي على مسار صوت صالح")
        if not temporary.is_file() or probe_duration(str(temporary))<=0:raise ValueError("لم ينتج ملف صوت صالح")
        temporary.replace(target)
    finally:temporary.unlink(missing_ok=True)
    return str(target)

def launch(source,directory,start,end,fmt,bitrate):
    signature=[video_identity(source),start,end,fmt,bitrate]
    key=hashlib.sha256(json.dumps(signature,sort_keys=True).encode()).hexdigest()[:20]
    directory=Path(directory).resolve();folder=directory/".audio_jobs"/key
    folder.mkdir(parents=True,exist_ok=True)
    target=directory/f"{Path(source).stem}_{key}.{fmt}"
    state=read(folder/"status.json")
    if state.get("state")=="running" and time.time()-state.get("heartbeat",0)<30:
        raise ValueError("استخراج الصوت يعمل بالفعل")
    if state.get("state")=="finished" and target.is_file() and target.stat().st_size==state.get("size") and target.stat().st_mtime_ns==state.get("mtime_ns"):return folder
    save(folder/"request.json",dict(source=str(source),target=str(target),start=start,end=end,fmt=fmt,bitrate=bitrate))
    save(folder/"status.json",{"state":"running","heartbeat":time.time()})
    flags=subprocess.CREATE_NO_WINDOW if os.name=="nt" else 0
    try:
        with (folder/"worker.log").open("a",encoding="utf-8") as log:
            subprocess.Popen(python_command("core.audio_export", folder),
                cwd=str(Path(__file__).resolve().parents[1]),stdout=log,stderr=log,creationflags=flags)
    except Exception:
        save(folder/"status.json",{"state":"failed","error":"تعذر تشغيل استخراج الصوت"});raise
    return folder

def run(folder):
    import threading
    folder=Path(folder);done=threading.Event()
    def heartbeat():
        while not done.is_set():
            save(folder/"status.json",{"state":"running","heartbeat":time.time()});done.wait(1)
    thread=threading.Thread(target=heartbeat,daemon=True);thread.start()
    try:
        path=extract(**read(folder/"request.json"));state={"state":"finished","path":path,"size":Path(path).stat().st_size,"mtime_ns":Path(path).stat().st_mtime_ns}
    except Exception as exc:state={"state":"failed","error":str(exc)}
    finally:done.set();thread.join();save(folder/"status.json",state)

if __name__=="__main__":run(sys.argv[1])
