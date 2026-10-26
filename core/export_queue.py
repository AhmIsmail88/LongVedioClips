"""Durable serial export worker; UI never runs the encoders itself."""
import json, os, subprocess, sys, time
from core.child_process import python_command
from pathlib import Path
from dataclasses import asdict
from config import Config
from models.schemas import FinalClip, TranscriptSegment
from utils.logger import Logger

def save(path,data):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    import tempfile
    # Heartbeats and result writes can run at the same time. Each writer
    # needs its own temporary file; sharing status.tmp can lose a write.
    handle,temporary=tempfile.mkstemp(prefix=path.name+'.',suffix='.tmp',dir=str(path.parent))
    temp=Path(temporary)
    try:
        with os.fdopen(handle,'w',encoding='utf-8') as stream:
            json.dump(data,stream,ensure_ascii=False,default=str)
        for attempt in range(6):
            try:temp.replace(path);break
            except PermissionError:
                if attempt==5:raise
                time.sleep(.05)
    finally:temp.unlink(missing_ok=True)

def read(path):
    try:return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError,ValueError):return {}

def launch(folder,clips,config,segments,options):
    folder=Path(folder);folder.mkdir(parents=True,exist_ok=True)
    status=read(folder/"status.json")
    if status.get("state")=="running" and time.time()-status.get("heartbeat",0)<20:
        raise RuntimeError("يوجد تصدير يعمل بالفعل")
    (folder/"stop").unlink(missing_ok=True)
    save(folder/"request.json",{"clips":[c.to_dict() for c in clips],"config":asdict(config),
        "segments":[s.to_dict() for s in segments],"options":options})
    save(folder/"status.json",{"state":"running","heartbeat":time.time(),"completed":[],"total":len(clips)})
    flags=subprocess.CREATE_NO_WINDOW if os.name=="nt" else 0
    with (folder/"worker.log").open("a",encoding="utf-8") as log:
        subprocess.Popen(python_command("core.export_queue", folder),
            cwd=str(Path(__file__).resolve().parents[1]),stdout=log,stderr=log,creationflags=flags)

def run(folder):
    import threading
    from core.clip_processor import render_clips
    folder=Path(folder);request=read(folder/"request.json")
    cfg=Config(**request["config"]);cfg.resume=True
    options=request.get("options",{})
    segments=[TranscriptSegment.from_dict(s) for s in request["segments"]]
    state={"state":"running","phase":"startup","completed":[],"failed":[],"total":len(request["clips"]),"started":time.time(),"preview":bool(options.get("preview"))}
    done=threading.Event()
    def heartbeat():
        while not done.is_set():
            state["heartbeat"]=time.time();save(folder/"status.json",dict(state));done.wait(2)
    thread=threading.Thread(target=heartbeat,daemon=True);thread.start()
    try:
        if options.get("light"):
            os.environ["OMP_NUM_THREADS"]="2"
            try:
                import torch
                torch.set_num_threads(2)
            except ImportError:pass
            cfg.render_threads=2
        if options.get("preview"):
            cfg.output_width=360;cfg.output_height=640
            cfg.output_dir=str(Path(cfg.output_dir)/"previews")
        if (cfg.burn_captions or cfg.add_title_overlay) and not segments:
            state["phase"]="transcribing"
            from core.transcriber import transcribe
            segments=transcribe(video_path=cfg.video_path,model_size=cfg.whisper_model,
                language=cfg.language,device=cfg.device,compute_type_gpu=cfg.compute_type_gpu,
                compute_type_cpu=cfg.compute_type_cpu,logger=Logger())
        for data in request["clips"]:
            if (folder/"stop").exists():
                state["state"]="paused";break
            state["phase"]="rendering"
            clip=FinalClip.from_dict(data);state["current"]=clip.index
            cfg.use_smart_reframe=request["config"]["use_smart_reframe"]
            cfg.reframe_layout=request["config"]["reframe_layout"]
            cfg.podcast_focus=options.get("focus",{}).get(str(clip.index),request["config"]["podcast_focus"])
            cfg.manual_crop=options.get("crops",{}).get(str(clip.index),{})
            if cfg.manual_crop:
                cfg.use_smart_reframe=True
                cfg.reframe_layout="single"
            if cfg.content_scan and segments and not cfg.cover_full:
                from core.quality_filter import _word_points,_pause_points,snap_to_word_edges,snap_to_pause_center
                snap_to_word_edges(clip,_word_points(segments))
                snap_to_pause_center(clip,_pause_points(segments),1.2)
            if cfg.content_scan:
                from core.content_scan import scan
                state["phase"]="screening"
                review=scan(cfg.video_path,clip.start,clip.end,folder/"screening")
                state.setdefault("screening",{})[str(clip.index)]=review
                if review["status"]!="clear":
                    state["failed"].append(clip.index)
                    continue
                state["phase"]="rendering"
            cfg.title_overrides={str(clip.index):options.get("titles",{}).get(str(clip.index),"")}
            result=render_clips([clip],cfg.video_path,cfg,Logger(),segments)
            if result:state["completed"].append(result[0].to_dict())
            else:state["failed"].append(clip.index)
        else:state["state"]="finished"
    except Exception as exc:
        state["state"]="failed";state["error"]=str(exc)
    finally:
        done.set();thread.join();state["heartbeat"]=time.time();save(folder/"status.json",state)

if __name__=="__main__":run(sys.argv[1])
