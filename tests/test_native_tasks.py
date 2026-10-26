import tempfile,unittest
from pathlib import Path
from unittest.mock import patch
from core.desktop_tasks import intact,run
from core.desktop_review import save_crop
from core.export_queue import read,save
from config import Config
from dataclasses import asdict
from utils.resume import video_identity

class NativeTaskTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.folder=Path(self.tmp.name);self.source=self.folder/'source.mp4';self.source.write_bytes(b'source')
        self.cfg=Config(video_path=str(self.source),output_dir=str(self.folder/'output'))
        save(self.folder/'analysis.json',dict(video_path=str(self.source),source_identity=video_identity(self.source),config=asdict(self.cfg),duration=4,segments=[],final_clips=[dict(index=1,start=0,end=4)]))
    def test_crop_rejects_changed_source_and_invalid_numbers(self):
        with self.assertRaises(ValueError):save_crop(self.folder,1,True,float('nan'),1)
        save_crop(self.folder,1,True,.2,1.5)
        self.assertEqual(read(self.cfg.project_output_dir()/'crop_edits.json')['crops']['1']['center'],.2)
        self.source.write_bytes(b'changed')
        with self.assertRaises(ValueError):save_crop(self.folder,1,False,.5,1)
    def test_cancelled_task_does_not_extract(self):
        save(self.folder/'request.json',dict(operation='audio',source=str(self.source)))
        (self.folder/'cancel').touch()
        with patch('core.audio_export.extract') as extract:run(self.folder)
        extract.assert_not_called();self.assertEqual(read(self.folder/'status.json')['state'],'cancelled')
    def test_cache_requires_unchanged_outputs(self):
        f=self.source;stat=f.stat();result=dict(files=[dict(path=str(f),size=stat.st_size,mtime_ns=stat.st_mtime_ns)])
        self.assertTrue(intact(result));f.write_bytes(b'changed');self.assertFalse(intact(result))
