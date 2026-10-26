import tempfile,unittest
from pathlib import Path
from unittest.mock import patch,Mock
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core.audio_export import extract

class AudioExportTests(unittest.TestCase):
    def test_invalid_range_and_format(self):
        with patch("core.audio_export.probe_duration",return_value=10):
            for start,end in ((5,4),(-1,5),(0,11)):
                with self.assertRaises(ValueError):extract("source.mp4","out.mp3",start,end)
            with self.assertRaises(ValueError):extract("source.mp4","out.exe",fmt="exe")

    def test_failed_extract_keeps_existing_output(self):
        with tempfile.TemporaryDirectory() as temp:
            target=Path(temp)/"audio.mp3";target.write_bytes(b"previous")
            with patch("core.audio_export.probe_duration",return_value=10),patch("core.audio_export.subprocess.run",return_value=Mock(returncode=1)):
                with self.assertRaises(ValueError):extract("source.mp4",target)
            self.assertEqual(target.read_bytes(),b"previous")
