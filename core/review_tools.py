"""Review edits and safe fallback proposals shared by UI and tests."""
from models.schemas import CandidateSegment,TranscriptSegment
from utils.sanitize import load_blocklist,mask_text

def word_candidates(segments,min_duration,max_duration):
    words=[w for s in segments for w in s.words if w.text.strip()]
    result=[]
    for i in range(0,len(words),max(1,len(words)//40)):
        chunk=[]
        for word in words[i:]:
            if word.end-words[i].start>max_duration:break
            chunk.append(word)
        if chunk and chunk[-1].end-chunk[0].start>=min_duration:
            result.append(CandidateSegment(chunk[0].start,chunk[-1].end,' '.join(w.text for w in chunk),source="word_fallback"))
    return result

def edited_segments(rows,original,duration):
    out=[];blocks=load_blocklist()
    for i,row in enumerate(rows):
        start,end=float(row['start']),float(row['end'])
        if not 0<=start<end<=duration:raise ValueError(f"راجع توقيت سطر الكابشن {i+1}")
        text=mask_text(str(row['text']),blocks)
        old=original[i] if i<len(original) else None
        unchanged=old and (old.start,old.end,old.text)==(start,end,text)
        out.append(TranscriptSegment(start,end,text,old.words if unchanged else []))
    if any(a.start>b.start for a,b in zip(out,out[1:],strict=False)):raise ValueError("رتّب الكابشن حسب وقت البداية")
    return out


def check_language_confidence(language,detected,probability):
    if language is None and probability<.60:
        from utils.logger import PipelineError
        raise PipelineError("تحديد اللغة تلقائيًا غير موثوق؛ اختر لغة الكلام يدويًا ثم أعد التحليل.",
            hint=f"اللغة المقترحة: {detected}، الثقة: {probability:.0%}. لن يُعتمد هذا التفريغ.")
