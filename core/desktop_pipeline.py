"""Desktop analysis adapter. Rendering only starts after review."""
import hashlib,json,sys,uuid
from pathlib import Path
from dataclasses import asdict
from config import Config,PLATFORM_PRESETS
from core.export_queue import read,save
from utils.resume import video_identity

TIME_EPSILON = 0.05  # منزلتان عشريتان في الواجهة: التقريب قد يتجاوز المدة بـ 0.005 ثانية


def span_ok(start, end, duration, eps=TIME_EPSILON) -> bool:
    """توقيت صالح مع سماحية تقريب العرض."""
    return 0 <= start < end <= duration + eps


def clamped_span(start, end, duration):
    """توقيت مقطوع داخل حدود الفيديو."""
    return (max(0.0, min(float(start), float(duration))),
            max(0.0, min(float(end), float(duration))))


def clips_from_rows(snapshot, rows):
    """يبني المقاطع المختارة من صفوف المراجعة، ويقطع النهايات على مدة الفيديو."""
    from models.schemas import FinalClip
    duration = float(snapshot['duration'])
    known = {c['index']: c for c in snapshot['final_clips']}
    clips, titles, focus = [], {}, {}
    for row in rows:
        if not row.get('selected'):
            continue
        index = int(row['index'])
        start = float(row['start'])
        end = float(row['end'])
        if index not in known or not span_ok(start, end, duration):
            raise ValueError('راجع توقيت المقطع ' + str(index))
        start, end = clamped_span(start, end, duration)
        clips.append(FinalClip.from_dict({**known[index], 'start': start, 'end': end}))
        titles[str(index)] = row.get('title', '')
        focus[str(index)] = row.get('focus', 'largest')
    if not clips:
        raise ValueError('اختار مقطعًا واحدًا على الأقل')
    return clips, titles, focus

def project_folder(source,root=None):
    from core.app_paths import DATA_ROOT
    root=Path(root or DATA_ROOT/'projects'/'desktop')
    identity=video_identity(source)
    digest=hashlib.sha256(json.dumps(identity,sort_keys=True).encode()).hexdigest()[:16]
    return root/digest

def make_config(source,settings,folder):
    cfg=Config(video_path=str(Path(source).resolve()),output_dir=str(Path(folder)/'output'),
        cover_full=settings.get('mode')=='cover',part_length=float(settings.get('part_length',90)),
        num_clips=int(settings.get('clips',12)),burn_captions=bool(settings.get('captions')),
        add_title_overlay=bool(settings.get('title')),fix_lighting=bool(settings.get('lighting')),
        use_smart_reframe=bool(settings.get('reframe')),preserve_full_frame=not bool(settings.get('reframe')),
        content_scan=bool(settings.get('screening',True)),language=settings.get('language') or None,
        whisper_model=settings.get('whisper_model','medium'),device=settings.get('device','auto'),
        glossary=settings.get('glossary',''),asr_hotwords=settings.get('asr_hotwords',''),
        candidate_strategy=settings.get('strategy','hybrid'),ollama_model=settings.get('ollama_model','qwen3.5:9b'),
        ollama_host=settings.get('ollama_host','http://localhost:11434'),
        reframe_layout=settings.get('layout','single'),podcast_focus=settings.get('focus','largest'),podcast_top_share=float(settings.get('top_share',.6)),resume=True)
    if not cfg.cover_full:
        preset=PLATFORM_PRESETS.get(settings.get('platform'),{})
        cfg.min_duration=float(settings.get('min_duration',preset.get('min_duration',20))) if settings.get('manual_duration') else float(preset.get('min_duration',20))
        cfg.max_duration=float(settings.get('max_duration',preset.get('max_duration',60))) if settings.get('manual_duration') else float(preset.get('max_duration',60))
        if not 0<cfg.min_duration<=cfg.max_duration:raise ValueError('راجع الحد الأدنى والأقصى لمدة اللقطات')
    if not .5<=cfg.podcast_top_share<=.7:raise ValueError('مساحة المتحدث يجب أن تكون بين 50% و70%')
    if cfg.candidate_strategy not in ('hybrid','windows'):raise ValueError('طريقة اكتشاف غير مدعومة')
    return cfg

def analyze_source(source,settings,folder):
    from app import analyze
    from utils.logger import Logger
    from core.project_library import register
    folder=Path(folder);cfg=make_config(source,settings,folder);stats={}
    clips,segments,duration=analyze(cfg,Logger(total_stages=5),stats_out=stats)
    snapshot=dict(video_path=cfg.video_path,output_dir=cfg.output_dir,source_identity=video_identity(source),
        final_clips=[c.to_dict() for c in clips],segments=[s.to_dict() for s in segments],
        duration=duration,video_duration_seconds=duration,revision=uuid.uuid4().hex,
        cover_full=cfg.cover_full,stats=stats,config=asdict(cfg))
    save(folder/'analysis.json',snapshot);register(folder/'analysis.json')
    return snapshot

def _edits_for(snapshot,cfg,folder):
    """تعديلات الكابشن والكادر المرتبطة بنفس المصدر فقط."""
    from models.schemas import TranscriptSegment
    segments=[TranscriptSegment.from_dict(s) for s in snapshot['segments']]
    caption_edit=read(cfg.project_output_dir()/'caption_edits.json') or read(folder/'caption_edits.json')
    if caption_edit.get('source')==snapshot['source_identity']:
        segments=[TranscriptSegment.from_dict(s) for s in caption_edit['segments']]
    crop_edit=read(cfg.project_output_dir()/'crop_edits.json') or read(folder/'crop_edits.json')
    crops=crop_edit.get('crops',{}) if crop_edit.get('source')==snapshot['source_identity'] else {}
    return segments,crops


def retry_failed(folder,preview=False,light=True):
    """يعيد تصدير المقاطع التي فشلت فقط، دون المساس بقرارات المراجعة."""
    from core.export_queue import launch
    from models.schemas import FinalClip
    folder=Path(folder);snapshot=read(folder/'analysis.json')
    if not snapshot:raise ValueError('حلّل الفيديو أولًا')
    state=read(folder/'export_job'/'status.json')
    failed=[int(index) for index in (state.get('failed') or [])]
    if not failed:raise ValueError('لا يوجد مقطع فاشل')
    saved={row['index']:row for row in (read(folder/'desktop_review.json') or [])}
    known={c['index']:c for c in snapshot['final_clips']};duration=float(snapshot['duration'])
    cfg=Config(**snapshot['config']);clips=[];titles={};focus={}
    for index in failed:
        clip=known.get(index)
        if not clip:continue
        row=saved.get(index) or {}
        start=float(row.get('start',clip['start']));end=float(row.get('end',clip['end']))
        if not 0<=start<end<=duration:continue
        clips.append(FinalClip.from_dict({**clip,'start':start,'end':end}))
        titles[str(index)]=row.get('title','');focus[str(index)]=row.get('focus','largest')
    if not clips:raise ValueError('لا يوجد مقطع فاشل صالح لإعادة المحاولة')
    segments,crops=_edits_for(snapshot,cfg,folder)
    launch(folder/'export_job',clips,cfg,segments,
        dict(preview=preview,light=light,titles=titles,focus=focus,crops=crops))
    return len(clips)


def export_review(folder,rows,preview=False,light=True,top_three=False):
    from core.export_queue import launch
    folder=Path(folder);snapshot=read(folder/'analysis.json')
    if not snapshot:raise ValueError('حلّل الفيديو أولًا')
    if video_identity(snapshot['video_path'])!=snapshot['source_identity']:
        raise ValueError('تغير ملف المصدر؛ أعد التحليل قبل التصدير')
    cfg=Config(**snapshot['config'])
    clips,titles,focus=clips_from_rows(snapshot,rows)
    if top_three:clips=sorted(clips,key=lambda c:(-c.score,c.index))[:3]
    segments,crops=_edits_for(snapshot,cfg,folder)
    save(folder/'desktop_review.json',rows)
    queue=folder/'export_job'
    launch(queue,clips,cfg,segments,dict(preview=preview,light=light,titles=titles,focus=focus,crops=crops))
    return queue

if __name__=='__main__':
    analyze_source(sys.argv[1],json.loads(sys.argv[2]),sys.argv[3])
