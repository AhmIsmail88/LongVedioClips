import tempfile,unittest
from pathlib import Path
from unittest.mock import patch
from dataclasses import asdict
from concurrent.futures import ThreadPoolExecutor
from models.schemas import FinalClip
from core import desktop_pipeline as pipeline,export_queue
from utils.resume import video_identity

class CaptionSettingsTests(unittest.TestCase):
    def test_caption_settings_reach_the_config(self):
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / "source.mp4"
            source.write_bytes(b"fixture")
            cfg = pipeline.make_config(source, {"glossary": "المزهر => المذهب",
                                                "asr_hotwords": "ابن تيمية"}, Path(temp))
            self.assertEqual(cfg.glossary, "المزهر => المذهب")
            self.assertEqual(cfg.asr_hotwords, "ابن تيمية")

    def test_defaults_are_empty(self):
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / "source.mp4"
            source.write_bytes(b"fixture")
            cfg = pipeline.make_config(source, {}, Path(temp))
            self.assertEqual((cfg.glossary, cfg.asr_hotwords), ("", ""))


class DesktopRegressionTests(unittest.TestCase):
    def snapshot(self,folder):
        source=folder/'source.mp4';source.write_bytes(b'fixture')
        cfg=pipeline.make_config(source,{},folder)
        snapshot=dict(video_path=str(source),source_identity=video_identity(source),duration=10.,segments=[],
            final_clips=[FinalClip(i,0.,5.,80.,'','text').to_dict() for i in [1,2]],config=asdict(cfg))
        export_queue.save(folder/'analysis.json',snapshot)
        return source

    def test_analysis_does_not_start_export(self):
        with tempfile.TemporaryDirectory() as temp:
            folder=Path(temp);source=folder/'source.mp4';source.write_bytes(b'test')
            with patch('app.analyze',return_value=([FinalClip(1,0.,2.,80.,'','text')],[],3.)) as analyze, patch('core.project_library.register'), patch('core.export_queue.launch') as launch:
                pipeline.analyze_source(source,{'language':'ar','mode':'cover','part_length':90},folder)
            launch.assert_not_called();self.assertTrue(analyze.call_args.args[0].cover_full)
            self.assertEqual(analyze.call_args.args[0].language,'ar')
            self.assertTrue((folder/'analysis.json').is_file())

    def test_selected_export_uses_edits_and_screening(self):
        with tempfile.TemporaryDirectory() as temp:
            folder=Path(temp);self.snapshot(folder)
            rows=[dict(index=1,selected=True,start=1.,end=4.,title='edited title',focus='left'),dict(index=2,selected=False,start=0.,end=5.)]
            with patch('core.export_queue.launch') as launch:pipeline.export_review(folder,rows,preview=True)
            args=launch.call_args.args;self.assertEqual([clip.index for clip in args[1]],[1])
            self.assertEqual((args[1][0].start,args[1][0].end),(1.,4.));self.assertTrue(args[2].content_scan)
            self.assertTrue(args[4]['preview']);self.assertEqual(args[4]['titles']['1'],'edited title')

    def test_invalid_boundaries_prevent_export(self):
        with tempfile.TemporaryDirectory() as temp:
            folder=Path(temp);self.snapshot(folder)
            with patch('core.export_queue.launch') as launch, self.assertRaises(ValueError):
                pipeline.export_review(folder,[dict(index=1,selected=True,start=5.,end=2.)])
            launch.assert_not_called()

    def test_changed_source_prevents_export(self):
        with tempfile.TemporaryDirectory() as temp:
            folder=Path(temp);source=self.snapshot(folder);source.write_bytes(b'changed source')
            with self.assertRaises(ValueError):pipeline.export_review(folder,[dict(index=1,selected=True,start=0.,end=2.)])

    def test_same_names_do_not_share_project(self):
        with tempfile.TemporaryDirectory() as temp:
            folder=Path(temp);(folder/'a').mkdir();(folder/'b').mkdir()
            a=folder/'a'/'same.mp4';b=folder/'b'/'same.mp4';a.write_bytes(b'one');b.write_bytes(b'two')
            self.assertNotEqual(pipeline.project_folder(a,folder),pipeline.project_folder(b,folder))

    def test_concurrent_status_writes_remain_complete(self):
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/'status.json'
            def write(index):
                for step in range(15):export_queue.save(path,dict(writer=index,step=step,text='data'*200))
            with ThreadPoolExecutor(max_workers=4) as pool:list(pool.map(write,range(4)))
            self.assertEqual(export_queue.read(path)['step'],14)
            self.assertFalse(list(Path(temp).glob('*.tmp')))
