"""Regression tests for complete coverage, resume invalidation and captions."""
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app
from config import Config
from core import clip_processor, quality_filter as qf, transcriber
from core.captions import _seconds_to_ass_time
from core.smart_reframe import SmoothedCameraman
from models.schemas import FinalClip, TranscriptSegment, Word
from utils.logger import PipelineError
from _harness import QuietLogger


class CompleteCoverageTests(unittest.TestCase):
    def test_all_content_including_silence_and_short_tail_is_kept(self):
        for duration in (1, 40, 60, 61, 120, 121, 3057):
            clips = qf.plan_complete_parts(duration, 60)
            import math
            self.assertEqual(len(clips), max(1, math.ceil(duration / 60)))
            self.assertEqual(clips[0].start, 0)
            self.assertEqual(clips[-1].end, duration)
            for left, right in zip(clips, clips[1:], strict=False):
                self.assertEqual(left.end, right.start)
            self.assertEqual(qf.timeline_coverage(clips, duration)['gaps'], [])

    def test_shared_boundary_moves_once_for_word(self):
        segments = [TranscriptSegment(59, 61, 'word', [Word(59, 61, 'word')])]
        parts = qf.plan_complete_parts(120, 60, segments)
        self.assertEqual(parts[0].end, parts[1].start)
        self.assertIn(parts[0].end, (59, 61))

    def test_overlap_cannot_hide_missing_content(self):
        clips = [FinalClip(1, 0, 60, 0, '', ''), FinalClip(2, 58, 118, 0, '', '')]
        result = qf.timeline_coverage(clips, 120)
        self.assertEqual(result['covered_seconds'], 118)
        self.assertEqual(result['gaps'], [(118, 120)])

    def test_full_coverage_does_not_need_whisper_or_ollama(self):
        with tempfile.TemporaryDirectory() as folder:
            config = Config(video_path=str(Path(folder) / 'silent.mp4'),
                            output_dir=folder, cover_full=True, resume=False)
            with patch.object(app, 'check_ffmpeg_installed'), \
                 patch.object(app, 'probe_duration', return_value=121), \
                 patch.object(app.transcriber, 'transcribe') as whisper, \
                 patch.object(app.clip_selector, 'score_candidates') as ollama:
                clips, segments, duration = app.analyze(config, QuietLogger())
            whisper.assert_not_called()
            ollama.assert_not_called()
            self.assertEqual(segments, [])
            self.assertEqual(qf.timeline_coverage(clips, duration)['coverage_ratio'], 1)

    def test_failed_captions_do_not_prevent_full_coverage(self):
        with tempfile.TemporaryDirectory() as folder:
            config = Config(video_path=str(Path(folder) / 'silent.mp4'),
                            output_dir=folder, cover_full=True, burn_captions=True,
                            resume=False)
            with patch.object(app, 'check_ffmpeg_installed'), \
                 patch.object(app, 'probe_duration', return_value=121), \
                 patch.object(app.transcriber, 'transcribe', side_effect=PipelineError('silent')):
                clips, _, duration = app.analyze(config, QuietLogger())
            self.assertEqual(qf.timeline_coverage(clips, duration)['coverage_ratio'], 1)


class ExportTests(unittest.TestCase):
    def test_settings_title_source_and_resume_invalidate_render(self):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / 'source.mp4'
            source.write_bytes(b'source')
            config = Config(video_path=str(source), output_dir=folder, cover_full=True)
            clip = FinalClip(1, 0, 5, 0, '', '', 'title')
            def write(**kwargs):
                Path(kwargs['output_path']).write_bytes(b'rendered')
            with patch.object(clip_processor, 'nvenc_available', return_value=False), \
                 patch.object(clip_processor.lighting, 'plan_lighting',
                              return_value=SimpleNamespace(active=False, filter_string=None)), \
                 patch.object(clip_processor, 'render_vertical_clip', side_effect=write) as render:
                def run():
                    clip_processor.render_clips([clip], str(source), config, QuietLogger())
                run()
                run()
                self.assertEqual(render.call_count, 1)
                config.normalize_audio = False
                run()
                self.assertEqual(render.call_count, 2)
                clip.title = 'changed'
                run()
                self.assertEqual(render.call_count, 3)
                source.write_bytes(b'other source')
                run()
                self.assertEqual(render.call_count, 4)
                config.resume = False
                run()
                run()
                self.assertEqual(render.call_count, 6)

    def test_coverage_render_does_not_move_shared_boundaries(self):
        with tempfile.TemporaryDirectory() as folder:
            config = Config(video_path='fake.mp4', output_dir=folder, cover_full=True,
                            resume=False)
            clips = qf.plan_complete_parts(120, 60)
            segments = [TranscriptSegment(59, 61, 'word', [Word(59, 61, 'word')])]
            with patch.object(clip_processor, 'nvenc_available', return_value=False), \
                 patch.object(clip_processor.lighting, 'plan_lighting',
                              return_value=SimpleNamespace(active=False, filter_string=None)), \
                 patch.object(clip_processor, 'render_vertical_clip'):
                clip_processor.render_clips(clips, 'fake.mp4', config, QuietLogger(), segments)
            self.assertEqual(clips[0].end, 60)
            self.assertEqual(clips[1].start, 60)


class TimingAndCropTests(unittest.TestCase):
    def test_caption_timestamp_carries_to_next_minute(self):
        self.assertEqual(_seconds_to_ass_time(59.999), '0:01:00.00')
        self.assertEqual(_seconds_to_ass_time(3599.999), '1:00:00.00')

    def test_portrait_crop_uses_requested_height(self):
        camera = SmoothedCameraman(720, 1280, 720, 1600)
        x1, y1, x2, y2 = camera.get_crop_box()
        self.assertEqual((x2-x1, y2-y1), (720, 1280))


class LazyCudaFallbackTests(unittest.TestCase):
    def test_cuda_generator_failure_restarts_on_cpu_without_duplicates(self):
        def failing():
            yield SimpleNamespace(start=0, end=1, text='partial', words=[])
            raise RuntimeError('CUDA decode failed')
        gpu = SimpleNamespace(transcribe=lambda *a, **k: (
            failing(), SimpleNamespace(language='en', language_probability=1)))
        cpu = SimpleNamespace(transcribe=lambda *a, **k: (
            iter([SimpleNamespace(start=0, end=2, text='complete', words=[])]),
            SimpleNamespace(language='en', language_probability=1)))
        fake_module = SimpleNamespace(WhisperModel=object)
        with patch.dict(sys.modules, {'faster_whisper': fake_module}), \
             patch.object(transcriber, '_cuda_transcribe_broken', False), \
             patch.object(transcriber, '_cuda_is_available', return_value=True), \
             patch.object(transcriber, '_load_whisper_model', side_effect=[gpu, cpu]) as load, \
             patch.object(transcriber, 'extract_audio'), \
             patch.object(transcriber, '_audio_seconds_for_progress', return_value=2):
            result = transcriber.transcribe('fake.mp4', 'tiny', 'en', 'auto',
                                           'float16', 'int8', QuietLogger())
        self.assertEqual([s.text for s in result], ['complete'])
        self.assertEqual(load.call_args_list[1].args[1], 'cpu')


if __name__ == '__main__':
    unittest.main()
