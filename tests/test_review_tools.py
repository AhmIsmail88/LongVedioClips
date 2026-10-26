import tempfile,unittest
from pathlib import Path
from unittest.mock import patch
from dataclasses import asdict
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from models.schemas import Word,TranscriptSegment,FinalClip
from config import Config
from core.review_tools import edited_segments,word_candidates
from core import export_queue,project_library

class ReviewTests(unittest.TestCase):
    def test_word_fallback_for_long_segment(self):
        segments=[TranscriptSegment(0,40,"نص",[Word(i,i+.8,"كلمة") for i in range(40)])]
        proposals=word_candidates(segments,8,10)
        self.assertTrue(proposals)
        self.assertTrue(all(8<=c.duration<=10 for c in proposals))

    def test_caption_edit_invalidates_stale_words(self):
        old=TranscriptSegment(0,2,"نص قديم",[Word(0,1,"نص"),Word(1,2,"قديم")])
        new=edited_segments([{"start":0,"end":2,"text":"نص جديد"}],[old],5)
        self.assertEqual(new[0].words,[]);self.assertEqual(new[0].text,"نص جديد")
        same=edited_segments([{"start":0,"end":2,"text":old.text}],[old],5)
        self.assertEqual(same[0].words,old.words)

    def test_bad_caption_times_rejected(self):
        with self.assertRaises(ValueError):edited_segments([{"start":3,"end":2,"text":"نص"}],[],5)

    def test_screening_blocks_review_required_and_missing_models(self):
        for status in ("review_required","unavailable"):
            with tempfile.TemporaryDirectory() as temp:
                folder=Path(temp)
                cfg=Config(video_path="test.mp4",content_scan=True,burn_captions=False,add_title_overlay=False)
                clip=FinalClip(1,0,5,80,"","نص")
                export_queue.save(folder/"request.json",{"config":asdict(cfg),"clips":[clip.to_dict()],"segments":[],"options":{}})
                with patch("core.content_scan.scan",return_value={"status":status}),patch("core.clip_processor.render_clips") as render:
                    export_queue.run(folder);render.assert_not_called()
                self.assertEqual(export_queue.read(folder/"status.json")["failed"],[1])

    def test_degraded_screening_still_exports(self):
        # A build without torch/transformers screens visuals only: that must not
        # fail every clip (the reason is reported instead).
        with tempfile.TemporaryDirectory() as temp:
            folder=Path(temp)
            cfg=Config(video_path="test.mp4",content_scan=True,burn_captions=False,add_title_overlay=False)
            clip=FinalClip(1,0,5,80,"","نص")
            export_queue.save(folder/"request.json",{"config":asdict(cfg),"clips":[clip.to_dict()],"segments":[],"options":{}})
            degraded={"status":"clear","degraded":True,"findings":[],"audio":{"status":"unavailable"}}
            with patch("core.content_scan.scan",return_value=degraded),patch("core.clip_processor.render_clips",return_value=[clip]) as render:
                export_queue.run(folder)
            render.assert_called_once()
            state=export_queue.read(folder/"status.json")
            self.assertEqual(state["failed"],[])

    def test_screening_verdict_marks_audio_degradation(self):
        from core import content_scan
        clear=content_scan.verdict([],None,3)
        self.assertEqual(clear["status"],"clear")
        self.assertEqual(clear["audio"]["status"],"ok")
        self.assertNotIn("degraded",clear)
        degraded=content_scan.verdict([],"ImportError: no torch",3)
        self.assertEqual(degraded["status"],"clear")
        self.assertTrue(degraded["degraded"])
        self.assertEqual(degraded["audio"]["status"],"unavailable")
        flagged=content_scan.verdict([{"kind":"visual","time":1.0,"score":0.9}],"ImportError",3)
        self.assertEqual(flagged["status"],"review_required")

    def test_library_protects_registered_project(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);snapshot=root/"snapshot.json"
            export_queue.save(snapshot,{"video_path":"test.mp4","source_identity":{"video":"test.mp4"},"output_dir":str(root/"output"),"final_clips":[]})
            with patch.object(project_library,"INDEX",root/"index.json"):
                project_library.register(snapshot)
                self.assertTrue(project_library.projects());self.assertTrue(project_library.protects(root))

    def test_low_confidence_auto_language_stops(self):
        from core.review_tools import check_language_confidence
        from utils.logger import PipelineError
        with self.assertRaises(PipelineError):check_language_confidence(None,"en",.39)
        check_language_confidence("ar","ar",.39)
        check_language_confidence(None,"ar",.99)
