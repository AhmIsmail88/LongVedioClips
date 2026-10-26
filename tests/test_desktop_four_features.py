import tempfile,unittest,zipfile
from pathlib import Path
from dataclasses import asdict
from unittest.mock import patch
from core.desktop_pipeline import make_config,export_review
from core.desktop_archive import create_archive
from core.export_queue import save,read
from models.schemas import FinalClip
from utils.resume import video_identity

class FourFeatureTests(unittest.TestCase):
    def test_manual_duration_and_podcast_settings_reach_config(self):
        cfg=make_config('source.mp4',dict(mode='highlights',strategy='windows',manual_duration=True,min_duration=9,max_duration=32,top_share=.7,layout='stacked'),'folder')
        self.assertEqual((cfg.min_duration,cfg.max_duration),(9,32))
        self.assertEqual(cfg.podcast_top_share,.7);self.assertEqual(cfg.candidate_strategy,'windows')
        preset=make_config('source.mp4',dict(platform='reels',manual_duration=False,min_duration=9,max_duration=32),'folder')
        self.assertEqual((preset.min_duration,preset.max_duration),(15,90))
    def test_invalid_duration_and_share_rejected(self):
        for kwargs in [dict(manual_duration=True,min_duration=20,max_duration=9),dict(top_share=.8)]:
            with self.assertRaises(ValueError):make_config('source.mp4',kwargs,'folder')
    def test_best_three_respects_selection_edits_and_review(self):
        with tempfile.TemporaryDirectory() as temp:
            folder=Path(temp);source=folder/'source.mp4';source.write_bytes(b'fixture')
            cfg=make_config(source,{},folder)
            clips=[FinalClip(i,0,4,score,'','text').to_dict() for i,score in enumerate([70,90,60,80,100],1)]
            save(folder/'analysis.json',dict(video_path=str(source),source_identity=video_identity(source),duration=5,segments=[],config=asdict(cfg),final_clips=clips))
            rows=[dict(index=i,selected=i!=5,start=1,end=3,title='edited',focus='left') for i in range(1,6)]
            with patch('core.export_queue.launch') as launch:export_review(folder,rows,top_three=True)
            result=launch.call_args.args[1];self.assertEqual([c.index for c in result],[2,4,1])
            self.assertEqual(result[0].start,1);self.assertEqual(read(folder/'desktop_review.json'),rows)
    def test_zip_duplicate_names_and_atomic_failure(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);(root/'a').mkdir();(root/'b').mkdir()
            paths=[root/'a'/'clip.mp4',root/'b'/'clip.mp4']
            for i,p in enumerate(paths):p.write_bytes(bytes([i])*1024)
            target=root/'clips.zip';create_archive(paths,target)
            with zipfile.ZipFile(target) as z:
                self.assertEqual(len(z.namelist()),2);self.assertIsNone(z.testzip())
            original=target.read_bytes()
            with patch('zipfile.ZipFile.write',side_effect=OSError('disk failed')):
                with self.assertRaises(OSError):create_archive(paths,target)
            self.assertEqual(target.read_bytes(),original);self.assertFalse(list(root.glob('*.partial')))
