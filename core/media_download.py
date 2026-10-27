"""Single-video downloads in a separate resumable local process."""
import hashlib,os,shutil,subprocess,sys,threading,time
from core.child_process import child_env, python_command
from pathlib import Path
from urllib.parse import urlsplit,urlunsplit
from core.export_queue import read,save

class Cancelled(Exception):pass

def validate_url(url):
    url=url.strip();parts=urlsplit(url)
    if parts.scheme not in ("http","https") or not parts.hostname or parts.username or parts.password:
        raise ValueError("أدخل رابط فيديو HTTP أو HTTPS صحيحًا")
    return urlunsplit((parts.scheme.lower(),parts.netloc,parts.path,parts.query,""))

PLATFORM_HOSTS = {
    "facebook": ("facebook.com", "fb.watch", "fb.me"),
    "x": ("twitter.com", "x.com", "t.co"),
    "tiktok": ("tiktok.com",),
    "youtube": ("youtube.com", "youtu.be"),
    "instagram": ("instagram.com",),
}

PLATFORM_NAMES = {
    "facebook": "فيسبوك",
    "x": "X (تويتر)",
    "tiktok": "تيك توك",
    "youtube": "يوتيوب",
    "instagram": "إنستجرام",
    "direct": "ملف مباشر",
    "other": "موقع آخر",
}

# Platforms where yt-dlp frequently needs the browser session, not just the URL.
SIGN_IN_PLATFORMS = ("facebook", "x", "instagram")

URL_HINT = "الصق رابط فيديو من يوتيوب أو فيسبوك أو X أو تيك توك أو إنستجرام"
COOKIES_HINT = "قد يحتاج ملف cookies إذا كان الفيديو غير عام"
AUTH_ERROR = ("هذا الفيديو يحتاج تسجيل الدخول أو ملف cookies. صدّر cookies من المتصفح "
              "(Netscape .txt) وحدّد الملف ثم أعد المحاولة.")
GENERIC_ERROR = "تعذر الوصول للفيديو. راجع الرابط أو الاتصال، وقد يحتاج yt-dlp تحديثًا."
NO_VIDEO_ERROR = ("الرابط لا يحتوي فيديو قابلًا للتحميل. في X: تأكد أن التغريدة نفسها بها فيديو "
                  "مرفوع، وليست اقتباسًا أو رابطًا خارجيًا.")
NOT_FOUND_ERROR = "الفيديو غير متاح: قد يكون محذوفًا أو خاصًا أو ممنوعًا في منطقتك."
GEO_ERROR = "الفيديو محجوب جغرافيًا من منطقتك، ويحتاج عادةً اتصالًا أو حسابًا من منطقة أخرى."
UNSUPPORTED_ERROR = "هذا الرابط غير مدعوم من yt-dlp. جرّب رابط المنشور الأصلي، وقد يحتاج yt-dlp تحديثًا."

_MEDIA_SUFFIXES = (".mp4", ".webm", ".mkv", ".mov", ".m4v", ".m4a", ".mp3")


def platform_of(url) -> str:
    """اسم المنصة من الرابط فقط — لا يلمس الشبكة."""
    try:
        cleaned = validate_url(url)
    except ValueError:
        return "other"
    parts = urlsplit(cleaned)
    host = (parts.hostname or "").lower()
    for name, hosts in PLATFORM_HOSTS.items():
        if any(host == suffix or host.endswith("." + suffix) for suffix in hosts):
            return name
    if Path(parts.path).suffix.lower() in _MEDIA_SUFFIXES:
        return "direct"
    return "other"


def sign_in_required(platform: str) -> bool:
    return platform in SIGN_IN_PLATFORMS


_AUTH_MARKERS = (
    "login required", "log in to", "sign in", "private",
    "cookies", "members-only", "requires authentication", "authentication",
    "confirm your age", "age-restricted", "age restricted",
)

# Order matters: the first matching rule wins.
_FAILURE_RULES = (
    (("no video could be found", "does not contain a video", "no video formats", "no video"),
     NO_VIDEO_ERROR),
    (("not available in your country", "geo restricted", "geo-restricted",
      "blocked in your country", "geo-restricted"), GEO_ERROR),
    (_AUTH_MARKERS, AUTH_ERROR),
    (("unsupported url", "no suitable extractor"), UNSUPPORTED_ERROR),
    (("not available", "unavailable", "has been deleted", "was deleted", "removed by",
      "http error 404", "404 not found", "does not exist"), NOT_FOUND_ERROR),
)


def describe_failure(exc) -> str:
    """رسالة عربية قابلة للتنفيذ بدل نص yt-dlp الخام."""
    text = str(exc).lower()
    for markers, message in _FAILURE_RULES:
        if any(marker in text for marker in markers):
            return message
    return GENERIC_ERROR


def runtime_path():
    candidate=shutil.which("node")
    if candidate:return candidate
    bundled=Path.home()/".cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin/node.exe"
    return str(bundled) if bundled.is_file() else None

def format_selector(height):
    """تنسيق التحميل: قيد صارم ثم قيد لين ثم أفضل متاح — بعض المواقع
    لا تعلن ارتفاعًا مطابقًا (تيك توك يعلن 640 حتى لمقطع 360p)."""
    return (f"bv*[height<={height}]+ba/b[height<={height}]"
            f"/b[height<=?{height}]/b")


def job_folder(url,directory,height,media_kind="video"):
    if media_kind not in ("video","audio"):raise ValueError("نوع تحميل غير مدعوم")
    identity=f"{validate_url(url)}|{height}" if media_kind=="video" else f"{validate_url(url)}|audio|mp3|192"
    key=hashlib.sha256(identity.encode()).hexdigest()[:24]
    return Path(directory).resolve()/".download_jobs"/key

def intact(result):
    try:
        path=Path(result["path"]);stat=path.stat()
        return stat.st_size==result["size"] and stat.st_mtime_ns==result["mtime_ns"]
    except (OSError,KeyError,TypeError):return False

def launch(url,directory,height=720,cookies="",mode="inspect",media_kind="video"):
    url=validate_url(url)
    if height not in (360,480,720,1080):raise ValueError("جودة غير مدعومة")
    if mode not in ("inspect","download"):raise ValueError("عملية غير مدعومة")
    if cookies and not Path(cookies).is_file():raise ValueError("ملف cookies غير موجود")
    folder=job_folder(url,directory,height,media_kind);folder.mkdir(parents=True,exist_ok=True)
    current=read(folder/"status.json")
    if current.get("state")=="running" and time.time()-current.get("heartbeat",0)<30:
        raise RuntimeError("هناك عملية تحميل/فحص تعمل بالفعل لهذا الرابط")
    previous=read(folder/"result.json")
    if mode=="download" and intact(previous):
        save(folder/"status.json",{"state":"finished","result":previous,"reused":True});return folder
    (folder/"cancel").unlink(missing_ok=True)
    save(folder/"request.json",{"url":url,"directory":str(Path(directory).resolve()),
        "height":height,"cookies":cookies,"mode":mode,"media_kind":media_kind,
        "platform":platform_of(url)})
    save(folder/"status.json",{"state":"running","phase":mode,"heartbeat":time.time()})
    flags=subprocess.CREATE_NO_WINDOW if os.name=="nt" else 0
    try:
        with (folder/"worker.log").open("a",encoding="utf-8") as log:
            subprocess.Popen(python_command("core.media_download", folder),
                cwd=str(Path(__file__).resolve().parents[1]),stdout=log,stderr=log,creationflags=flags,
                env=child_env())
    except Exception:
        save(folder/"status.json",{"state":"failed","error":"تعذر تشغيل عامل التحميل"});raise
    return folder

def single_video(info):
    if not info or info.get("_type") in ("playlist","multi_video") or "entries" in info:
        raise ValueError("أدخل رابط فيديو واحد؛ القوائم والحسابات غير مدعومة الآن")
    if info.get("is_live") or info.get("live_status") in ("is_live","is_upcoming"):
        raise ValueError("استخدم فيديو مسجلًا؛ البث الجاري غير مدعوم")
    if (info.get("age_limit") or 0)>=18:
        raise ValueError("تم رفض الفيديو لأنه مصنف للبالغين")

def public_info(info):
    from utils.sanitize import load_blocklist,mask_text
    return {"title":mask_text(info.get("title") or "فيديو",load_blocklist()),
        "duration":info.get("duration"),"site":info.get("extractor_key"),"id":info.get("id")}

def run(folder):
    folder=Path(folder);request=read(folder/"request.json")
    state={"state":"running","phase":request.get("mode"),"progress":0}
    done=threading.Event()
    def checkpoint():
        while not done.is_set():
            state["heartbeat"]=time.time();save(folder/"status.json",dict(state));done.wait(1)
    thread=threading.Thread(target=checkpoint,daemon=True);thread.start()
    def cancel():
        if (folder/"cancel").exists():raise Cancelled()
    def progress(data):
        cancel();total=data.get("total_bytes") or data.get("total_bytes_estimate") or 0
        state.update(phase="downloading",downloaded=data.get("downloaded_bytes",0),
            progress=min(.99,data.get("downloaded_bytes",0)/total) if total else 0,
            speed=data.get("speed"),eta=data.get("eta"))
        if data.get("status")=="finished":state["phase"]="merging"
    class QuietLogger:
        def debug(self,*args):pass
        def warning(self,*args):pass
        def error(self,*args):pass
    try:
        import yt_dlp
        from utils.ffmpeg import get_ffmpeg_binaries,probe_duration
        ffmpeg,_=get_ffmpeg_binaries()
        height=request["height"]
        options={"quiet":True,"no_warnings":True,"logger":QuietLogger(),"noplaylist":True,
            "socket_timeout":20,"retries":3,"fragment_retries":3,"continuedl":True,
            "concurrent_fragment_downloads":1,"ffmpeg_location":str(Path(ffmpeg).parent),
            "format":format_selector(height),
            "merge_output_format":"mp4","windowsfilenames":True,
            "outtmpl":str(Path(request["directory"])/f"%(extractor_key)s_%(id)s_{folder.name[:10]}_{height}p.%(ext)s"),
            "progress_hooks":[progress],"postprocessor_hooks":[lambda data:cancel()]}
        media_kind=request.get("media_kind","video")
        if media_kind=="audio":
            options.update(format="bestaudio/best",postprocessors=[{
                "key":"FFmpegExtractAudio","preferredcodec":"mp3","preferredquality":"192"}],
                outtmpl=str(Path(request["directory"])/f"%(extractor_key)s_%(id)s_{folder.name[:10]}_audio.%(ext)s"))
            options.pop("merge_output_format",None)
        if (urlsplit(request["url"]).hostname or "").lower().endswith("tiktok.com"):
            from yt_dlp.networking.impersonate import ImpersonateTarget
            options["impersonate"]=ImpersonateTarget.from_str("chrome")
        node=runtime_path()
        if node:options["js_runtimes"]={"node":{"path":node}}
        if request.get("cookies"):
            shutil.copyfile(request["cookies"],folder/"cookies.txt")
            options["cookiefile"]=str(folder/"cookies.txt")
        cancel()
        with yt_dlp.YoutubeDL(options) as ydl:
            info=ydl.extract_info(request["url"],download=False);single_video(info);cancel()
            metadata=public_info(info);save(folder/"info.json",metadata);state["info"]=metadata
            if request["mode"]=="download":
                state["phase"]="downloading"
                # Download the inspected entry, not a playlist or a second URL extraction.
                ydl.process_info(info);cancel()
                path=Path(info.get("filepath") or ydl.prepare_filename(info))
                if not path.is_file():
                    alternate=path.with_suffix(".mp3" if media_kind=="audio" else ".mp4")
                    if alternate.is_file():path=alternate
                duration=probe_duration(str(path)) if path.is_file() else 0
                if duration<=0:
                    raise ValueError("التحميل لم ينتج ملف فيديو صالحًا")
                metadata["duration"]=duration
                save(folder/"info.json",metadata)
                stat=path.stat();result={"path":str(path.resolve()),"size":stat.st_size,"mtime_ns":stat.st_mtime_ns,"info":metadata,"media_kind":media_kind}
                save(folder/"result.json",result);state["result"]=result;state["progress"]=1
            state["state"]="finished"
    except Cancelled:state["state"]="cancelled"
    except Exception as exc:
        import re
        print(type(exc).__name__ + ": " + re.sub(r"https?://\S+", "[URL]", str(exc))[:1000], flush=True)
        state["state"]="cancelled" if (folder/"cancel").exists() else "failed"
        # Error classes and safe hints only: never display signed URLs or cookies.
        state["error"]=str(exc) if isinstance(exc,ValueError) else describe_failure(exc)
    finally:
        done.set();thread.join();state["heartbeat"]=time.time();save(folder/"status.json",state)
        (folder/"cookies.txt").unlink(missing_ok=True)

if __name__=="__main__":run(sys.argv[1])
