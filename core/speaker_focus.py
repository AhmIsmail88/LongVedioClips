"""Experimental audio-gated mouth-motion focus; no identity or voice model."""
import wave

class AudioActivity:
    def __init__(self,path):
        import numpy as np
        with wave.open(str(path),'rb') as audio:
            if audio.getsampwidth()!=2 or audio.getnchannels()!=1:
                raise ValueError('Expected mono 16-bit PCM')
            self.rate=audio.getframerate()
            self.samples=np.frombuffer(audio.readframes(audio.getnframes()),dtype='<i2').astype('float32')/32768

    def rms_at(self,seconds,window=.12):
        import numpy as np
        start=max(0,int((seconds-window)*self.rate))
        end=min(len(self.samples),int((seconds+window)*self.rate))
        if end<=start:
            return 0.0
        sample=self.samples[start:end]
        return float(np.sqrt(np.mean(sample*sample)))

class MouthFocus:
    """Estimate left/right only with sustained, distinct motion during audio.

    This is a heuristic, not diarization or a trained active-speaker model.
    Unknown confidence returns None, allowing callers to show both people.
    """
    def __init__(self,hold_seconds=.4,switch_seconds=1.0):
        self.hold_seconds=hold_seconds
        self.switch_seconds=switch_seconds
        self.previous={}
        self.scores={}
        self.active=None
        self.pending=None
        self.pending_since=0.0
        self.last_evidence=-1000.0
        self.last_switch=-1000.0

    def update(self,frame,boxes,seconds,audio_rms):
        import cv2
        import numpy as np
        boxes=sorted(boxes,key=lambda b:b[0]+b[2]/2)
        if len(boxes)!=2:
            self.previous.clear()
            self.scores.clear()
            self.pending=self.active=None
            return None
        for side,box in zip(('left','right'),boxes,strict=False):
            x,y,w,h=map(int,box)
            frame_h,frame_w=frame.shape[:2]
            x1,x2=max(0,x),min(frame_w,x+w)
            y1,y2=max(0,y),min(frame_h,y+h)
            face=frame[y1:y2,x1:x2]
            if not face.size:
                self.previous.pop(side,None)
                self.scores[side]=0
                continue
            gray=cv2.resize(cv2.cvtColor(face,cv2.COLOR_BGR2GRAY),(64,64))
            mouth=gray[40:59,12:52].astype('float32')
            control=gray[12:32,12:52].astype('float32')
            old=self.previous.get(side)
            activity=0.0
            if old is not None:
                activity=max(0.0,float(np.mean(abs(mouth-old[0])))
                                  -float(np.mean(abs(control-old[1]))))
            self.previous[side]=(mouth,control)
            self.scores[side]=.7*self.scores.get(side,0)+.3*activity
        best=max(self.scores,key=self.scores.get)
        other='right' if best=='left' else 'left'
        confidence=(audio_rms>=.008 and self.scores[best]>=1.2
                    and self.scores[best]>=1.5*self.scores[other]+.5)
        if not confidence:
            self.pending=None
            if seconds-self.last_evidence>.5:
                self.active=None
            return self.active
        self.last_evidence=seconds
        if self.pending!=best:
            self.pending=best
            self.pending_since=seconds
        if (seconds-self.pending_since>=self.hold_seconds
                and (self.active is None or self.active==best
                     or seconds-self.last_switch>=self.switch_seconds)):
            if self.active!=best:
                self.last_switch=seconds
            self.active=best
        return self.active

def smooth_boxes(previous,current):
    """Keep stable spatial order and damp small detection-box changes."""
    ordered=sorted(current,key=lambda b:b[0]+b[2]/2)
    if len(previous)!=len(ordered):
        return ordered
    return [tuple(int(.7*a+.3*b) for a,b in zip(old,new,strict=True))
            for old,new in zip(previous,ordered,strict=True)]
