"""Real frame/FFmpeg tests; detection is deterministic to isolate framing."""
import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core import smart_reframe as sr
from utils.ffmpeg import get_ffmpeg_binaries
from _harness import QuietLogger

HAS_CV = importlib.util.find_spec('cv2') and importlib.util.find_spec('scenedetect')


@unittest.skipUnless(HAS_CV, 'Install smart-reframe dependencies for frame integration tests')
class SmartReframeIntegrationTests(unittest.TestCase):
    def test_stacked_layout_and_lost_detection_preserve_audio(self):
        import cv2
        from scenedetect import FrameTimecode
        ffmpeg, ffprobe = get_ffmpeg_binaries()
        with tempfile.TemporaryDirectory(prefix='reframe_test_') as directory:
            source = str(Path(directory)/'source.mp4')
            output = str(Path(directory)/'stacked.mp4')
            subprocess.run([
                ffmpeg, '-y', '-hide_banner', '-loglevel', 'error',
                '-f', 'lavfi', '-i', 'color=red:s=640x360:r=12:d=1',
                '-f', 'lavfi', '-i', 'sine=frequency=440:duration=1',
                '-vf', 'drawbox=x=320:y=0:w=320:h=360:color=blue:t=fill',
                '-c:v', 'libx264', '-pix_fmt', 'yuv420p', '-c:a', 'aac',
                '-t', '1', source], check=True, capture_output=True)
            pair = [{'box': [90, 80, 60, 60], 'score': 3600},
                    {'box': [470, 80, 60, 60], 'score': 3600}]
            detections = iter([pair] + [[]] * 20)
            with patch.dict(sys.modules, {'ultralytics': None}), \
                 patch.object(sr, '_load_face_detector', return_value=object()), \
                 patch.object(sr, '_detect_scenes', return_value=(
                     [(FrameTimecode(0,12.0),FrameTimecode(12,12.0))],12)), \
                 patch.object(sr, '_detect_face_candidates', side_effect=lambda *a: next(detections)):
                sr.render_vertical_clip_smart(source, output, 0, 1, 180, 320,
                                              'aac', QuietLogger(), layout='stacked')
            cap=cv2.VideoCapture(output)
            frames=[]
            while True:
                ok,frame=cap.read()
                if not ok:
                    break
                frames.append(frame)
            cap.release()
            self.assertEqual(len(frames),12)
            self.assertEqual(frames[0].shape[:2],(320,180))
            # Left source person stays above right source person.
            self.assertGreater(int(frames[0][50,90,2]),200)
            self.assertGreater(int(frames[0][270,90,0]),200)
            # After detection expires, both source halves are visible.
            self.assertGreater(int(frames[-1][160,45,2]),200)
            self.assertGreater(int(frames[-1][160,135,0]),200)
            result=subprocess.run([ffprobe,'-v','error','-show_streams',
                                   '-of','json',output],check=True,capture_output=True,text=True)
            streams=json.loads(result.stdout)['streams']
            self.assertIn('audio',[s['codec_type'] for s in streams])

    def test_manual_right_and_auto_unknown_focus_render_correctly(self):
        import cv2
        from scenedetect import FrameTimecode
        ffmpeg,_=get_ffmpeg_binaries()
        with tempfile.TemporaryDirectory(prefix='focus_render_') as directory:
            source=str(Path(directory)/'source.mp4')
            subprocess.run([ffmpeg,'-y','-hide_banner','-loglevel','error',
                '-f','lavfi','-i','color=red:s=640x360:r=12:d=1',
                '-f','lavfi','-i','sine=frequency=440:duration=1',
                '-vf','drawbox=x=320:y=0:w=320:h=360:color=blue:t=fill',
                '-c:v','libx264','-pix_fmt','yuv420p','-c:a','aac','-t','1',source],
                check=True,capture_output=True)
            pair=[{'box':[90,80,60,60],'score':3600},
                  {'box':[470,80,60,60],'score':3600}]
            for focus,layout in [('right','stacked'),('right','single'),('auto','single')]:
                with self.subTest(focus=focus,layout=layout):
                    output=str(Path(directory)/f'{focus}_{layout}.mp4')
                    with patch.dict(sys.modules,{'ultralytics':None}), \
                         patch.object(sr,'_load_face_detector',return_value=object()), \
                         patch.object(sr,'_detect_scenes',return_value=(
                             [(FrameTimecode(0,12.0),FrameTimecode(12,12.0))],12)), \
                         patch.object(sr,'_detect_face_candidates',return_value=pair):
                        sr.render_vertical_clip_smart(source,output,0,1,180,320,'aac',
                            QuietLogger(),layout=layout,focus_mode=focus)
                    cap=cv2.VideoCapture(output)
                    ok,frame=cap.read()
                    cap.release()
                    self.assertTrue(ok)
                    if focus=='right':
                        self.assertGreater(int(frame[150,90,0]),200)
                    else:
                        # No mouth evidence: retain both sides in a wide view.
                        self.assertGreater(int(frame[160,45,2]),200)
                        self.assertGreater(int(frame[160,135,0]),200)

    def test_general_frame_preserves_tall_sources(self):
        import numpy as np
        frame=np.zeros((640,180,3),dtype=np.uint8)
        frame[:,40:140,2]=255
        output=sr._create_general_frame(frame,180,320)
        self.assertEqual(output.shape,(320,180,3))
        self.assertEqual(int(output[160,90,2]),255)


if __name__=='__main__':
    unittest.main()
