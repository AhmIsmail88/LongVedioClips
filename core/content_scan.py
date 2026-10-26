"""Local conservative screening. Findings/missing models stop automatic export."""
import hashlib,json,tempfile
from pathlib import Path
from functools import lru_cache
from core.export_queue import read,save
from utils.resume import video_identity
MODEL="MIT/ast-finetuned-audioset-10-10-0.4593"
EXPOSED={"FEMALE_BREAST_EXPOSED","FEMALE_GENITALIA_EXPOSED","MALE_GENITALIA_EXPOSED","BUTTOCKS_EXPOSED","ANUS_EXPOSED"}

@lru_cache(maxsize=1)
def visual_model():
    from nudenet import NudeDetector
    return NudeDetector()

@lru_cache(maxsize=1)
def audio_model():
    import torch
    from transformers import AutoFeatureExtractor,AutoModelForAudioClassification
    torch.set_num_threads(2)
    local=Path(__file__).resolve().parents[1]/"models"/"audio-screening"
    source=str(local) if (local/"model.safetensors").is_file() else MODEL
    return (AutoFeatureExtractor.from_pretrained(source,local_files_only=True),
        AutoModelForAudioClassification.from_pretrained(source,local_files_only=True,use_safetensors=True).eval())

def verdict(findings,audio_error=None,frames=0):
    """قرار الفحص: الفحص البصري إلزامي، وفحص الصوت يُعلَن كإضعاف لا كفشل."""
    result={"status":"review_required" if findings else "clear","findings":findings,
            "frames_sampled":frames,
            "note":"فحص عينة صورة كل ثانية وتصنيف الصوت؛ النتيجة ليست ضمانًا ولا حكمًا شرعيًا"}
    if audio_error:
        result["degraded"]=True
        result["audio"]={"status":"unavailable","error":audio_error}
    else:
        result["audio"]={"status":"ok"}
    return result


def audio_available():
    """هل فحص الصوت ممكن في هذه البيئة؟ (torch + transformers)."""
    import importlib.util
    return all(importlib.util.find_spec(name) for name in ("torch","transformers","soundfile"))


def scan(source,start,end,cache_dir):
    audio_ok=audio_available()
    key=hashlib.sha256(json.dumps([2,video_identity(source),start,end,MODEL,audio_ok],sort_keys=True).encode()).hexdigest()
    path=Path(cache_dir)/f"{key}.json"
    cached=read(path)
    if cached.get("status") in ("clear","review_required"):return cached
    findings=[];samples=0;audio_error=None
    try:
        import cv2,numpy as np,subprocess
        from utils.ffmpeg import get_ffmpeg_binaries
        detector=visual_model()
        cap=cv2.VideoCapture(str(source))
        try:
            for seconds in np.arange(start,end,1.0):
                cap.set(cv2.CAP_PROP_POS_MSEC,float(seconds)*1000);ok,frame=cap.read()
                if not ok:raise ValueError("تعذر قراءة إطار أثناء الفحص")
                samples+=1
                for detection in detector.detect(frame):
                    if detection['class'] in EXPOSED and detection['score']>=.45:
                        findings.append({"kind":"visual","time":float(seconds),"score":float(detection['score'])})
        finally:cap.release()
        try:
            import torch,soundfile as sf
            extractor,model=audio_model()
            ffmpeg,_=get_ffmpeg_binaries()
            with tempfile.TemporaryDirectory(prefix="content_scan_") as temp:
                for seconds in np.arange(start,end,10.0):
                    wav=Path(temp)/"sample.wav"
                    subprocess.run([ffmpeg,"-y","-loglevel","error","-ss",str(seconds),"-i",str(source),
                        "-t",str(min(10,end-seconds)),"-vn","-ar","16000","-ac","1",str(wav)],check=True,capture_output=True)
                    audio,sr=sf.read(wav,dtype="float32")
                    inputs=extractor(audio,sampling_rate=sr,return_tensors="pt")
                    with torch.inference_mode():scores=torch.sigmoid(model(**inputs).logits)[0]
                    for i,score in enumerate(scores):
                        label=model.config.id2label[i].lower()
                        if any(word in label for word in ("music","musical instrument","singing")) and float(score)>=.35:
                            findings.append({"kind":"music","time":float(seconds),"score":float(score)});break
        except Exception as exc:
            # الصوت يحتاج torch/transformers؛ في بناء بدونها نُكمل بالفحص البصري
            # ونعلن الإضعاف بدل إفشال كل المقاطع.
            audio_error=f"{type(exc).__name__}: الصوت غير متاح"
        result=verdict(findings,audio_error,samples)
        save(path,result);return result
    except Exception as exc:
        return {"status":"unavailable","findings":[],"frames_sampled":samples,
                "error":f"الفحص غير مكتمل: {type(exc).__name__}"}
