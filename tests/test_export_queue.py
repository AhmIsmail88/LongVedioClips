import unittest, tempfile
from pathlib import Path
from dataclasses import asdict
from unittest.mock import patch
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from config import Config
from core import export_queue
from models.schemas import FinalClip

class QueueTests(unittest.TestCase):
    def request(self,folder):
        clips=[FinalClip(i,i*5,i*5+5,80,"","نص") for i in (1,2)]
        export_queue.save(folder/"request.json",{"config":asdict(Config(video_path="source.mp4",
            output_dir=str(folder),burn_captions=False,add_title_overlay=False)),
            "clips":[c.to_dict() for c in clips],"segments":[],"options":{}})

    def test_pause_after_current_and_resume(self):
        with tempfile.TemporaryDirectory() as temp:
            folder=Path(temp);self.request(folder);seen=[]
            def render(clips,*args):
                seen.append(clips[0].index);clips[0].output_path="done.mp4"
                (folder/"stop").touch()
                return clips
            with patch("core.clip_processor.render_clips",side_effect=render):export_queue.run(folder)
            state=export_queue.read(folder/"status.json")
            self.assertEqual(state["state"],"paused")
            self.assertEqual(seen,[1]);self.assertEqual(len(state["completed"]),1)
            (folder/"stop").unlink()
            with patch("core.clip_processor.render_clips",side_effect=lambda clips,*args:clips):export_queue.run(folder)
            self.assertEqual(export_queue.read(folder/"status.json")["state"],"finished")
            self.assertEqual(len(export_queue.read(folder/"status.json")["completed"]),2)

    def test_failure_does_not_block_next_clip(self):
        with tempfile.TemporaryDirectory() as temp:
            folder=Path(temp);self.request(folder)
            with patch("core.clip_processor.render_clips",side_effect=lambda clips,*args:[] if clips[0].index==1 else clips):
                export_queue.run(folder)
            state=export_queue.read(folder/"status.json")
            self.assertEqual(state["failed"],[1]);self.assertEqual(len(state["completed"]),1)

    def test_duplicate_live_job_rejected(self):
        import time
        with tempfile.TemporaryDirectory() as temp:
            folder=Path(temp)
            export_queue.save(folder/"status.json",{"state":"running","heartbeat":time.time()})
            with self.assertRaises(RuntimeError):export_queue.launch(folder,[],Config(),[],{})
