"""Persistent index for locally reviewed projects; no media deletion."""
import hashlib,json
from pathlib import Path
from core.export_queue import read,save
INDEX=Path(__file__).resolve().parents[1]/"projects"/"index.json"
HIDDEN=Path(__file__).resolve().parents[1]/"projects"/"hidden.json"

def register(snapshot):
    analysis=read(snapshot)
    if not analysis.get("source_identity"):return
    key=hashlib.sha256(json.dumps(analysis['source_identity'],sort_keys=True).encode()).hexdigest()[:20]
    library=read(INDEX)
    library[key]={"name":Path(analysis['video_path']).name,"snapshot":str(Path(snapshot).resolve()),
        "source":analysis['video_path'],"clips":len(analysis.get('final_clips',[])),
        "work_dir":str(Path(analysis['output_dir']).parent)}
    save(INDEX,library)

def projects():return read(INDEX)

def hidden():
    """مسارات مشاريع أُزيلت من المكتبة (الملفات باقية على القرص)."""
    entries=read(HIDDEN)
    if not isinstance(entries,list):return []
    return [str(Path(item)) for item in entries if isinstance(item,str)]

def hide(snapshot):
    target=str(Path(snapshot).resolve())
    entries=hidden()
    if target not in entries:
        entries.append(target)
        save(HIDDEN,entries)
    return entries

def unregister(snapshot):
    """يُخرج المشروع من المكتبة دون حذف أي ملف."""
    target=Path(snapshot).resolve()
    library=read(INDEX);removed=False
    for key,item in list(library.items()):
        snapshot_path=Path(str(item.get("snapshot",""))).resolve() if item.get("snapshot") else None
        work_dir=Path(str(item.get("work_dir",""))).resolve() if item.get("work_dir") else None
        matches=((snapshot_path and (snapshot_path==target or snapshot_path.parent==target))
                 or (work_dir and (work_dir==target or work_dir==target.parent)))
        if matches:
            library.pop(key);removed=True
    if removed:save(INDEX,library)
    marker=target/"analysis.json" if target.is_dir() else target
    hide(marker)
    return removed

def protects(folder):
    """يمنع حذف مجلدات فيها مشاريع — حتى تلك التي أُزيلت من المكتبة."""
    folder=Path(folder).resolve()
    if any(Path(item['work_dir']).resolve()==folder for item in projects().values() if item.get('work_dir')):
        return True
    return any(Path(entry).resolve().parent==folder for entry in hidden())
