"""GUI wiring tests for the «استخراج النص» section in streamlit_app.

Runs with no browser, no Streamlit runtime, and no whisper/ffmpeg: a
recording stand-in for the `streamlit` module is injected into sys.modules
before streamlit_app is imported, so the section is driven exactly the way
Streamlit would drive it and every widget call it makes can be asserted
on. core.transcriber.transcribe is stubbed out, so nothing heavy runs.

Run:
    .venv\\Scripts\\python.exe tests\\test_text_extraction_section.py
"""

from __future__ import annotations

import io
import shutil
import sys
import types
import unittest
import zipfile
from pathlib import Path
from xml.etree import ElementTree

sys.path.insert(0, str(Path(__file__).resolve().parent))

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from models.schemas import TranscriptSegment  # noqa: E402


# --------------------------------------------------------------------------
# Stand-in for the streamlit module
# --------------------------------------------------------------------------


class _UploadedFile:
    """Just enough of Streamlit's UploadedFile for save_uploaded_file()."""

    def __init__(self, name: str, data: bytes):
        self.name = name
        self._data = data

    @property
    def size(self) -> int:
        return len(self._data)

    def getbuffer(self) -> memoryview:
        return memoryview(self._data)


class _Placeholder:
    """Records, and reports progress into the fake module's call list."""

    def __init__(self, calls: list, kind: str):
        self._calls = calls
        self._kind = kind

    def markdown(self, body, **kwargs):
        self._calls.append((f"{self._kind}.markdown", (body,), {}))

    def code(self, body, language=None):
        self._calls.append((f"{self._kind}.code", (body,), {"language": language}))

    def progress(self, value, **kwargs):
        self._calls.append((f"{self._kind}.progress", (value,), {}))


class _Container:
    def __init__(self, calls: list, kind: str):
        self._calls = calls
        self._kind = kind

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _SessionState(dict):
    """dict that also answers attribute access, like st.session_state.

    The app assigns state with `st.session_state.work_dir = ...` - real
    Streamlit supports both styles, so a plain dict stand-in is not enough.
    """

    def __getattr__(self, name):
        try:
            return self[name]
        except KeyError as exc:
            raise AttributeError(name) from exc

    def __setattr__(self, name, value):
        self[name] = value


class FakeStreamlit(types.ModuleType):
    def __init__(self) -> None:
        super().__init__("streamlit")
        self.calls: list[tuple[str, tuple, dict]] = []
        self.session_state = _SessionState()
        self.media_file = None
        self.video_file = None
        self.split_file = None
        self.checkbox_values: dict[str, bool] = {}
        self.selectbox_values: dict[str, object] = {}
        self.slider_values: dict[str, object] = {}
        self.clicked_labels: set[str] = set()

    # -- bookkeeping ------------------------------------------------------

    def _record(self, name, args, kwargs):
        self.calls.append((name, args, kwargs))

    def named(self, name: str) -> list[tuple[str, tuple, dict]]:
        return [call for call in self.calls if call[0] == name]

    def labels(self, name: str) -> list[str]:
        return [call[1][0] for call in self.named(name) if call[1]]

    def forget(self) -> None:
        self.calls.clear()

    # -- chrome -----------------------------------------------------------

    def set_page_config(self, **kwargs):
        self._record("set_page_config", (), kwargs)

    def title(self, body, **kwargs):
        self._record("title", (body,), kwargs)

    def header(self, body, **kwargs):
        self._record("header", (body,), kwargs)

    def subheader(self, body, **kwargs):
        self._record("subheader", (body,), kwargs)

    def caption(self, body, **kwargs):
        self._record("caption", (body,), kwargs)

    def markdown(self, body, **kwargs):
        self._record("markdown", (body,), kwargs)

    def divider(self, **kwargs):
        self._record("divider", (), kwargs)

    def info(self, body, **kwargs):
        self._record("info", (body,), kwargs)

    def success(self, body, **kwargs):
        self._record("success", (body,), kwargs)

    def warning(self, body, **kwargs):
        self._record("warning", (body,), kwargs)

    def error(self, body, **kwargs):
        self._record("error", (body,), kwargs)

    def code(self, body, language=None):
        self._record("code", (body,), {"language": language})

    # -- layout -----------------------------------------------------------

    def progress(self, value):
        self._record("progress", (value,), {})
        return _Placeholder(self.calls, "progress")

    def empty(self):
        self._record("empty", (), {})
        return _Placeholder(self.calls, "empty")

    def expander(self, label, expanded=False):
        self._record("expander", (label,), {"expanded": expanded})
        return _Container(self.calls, "expander")

    def spinner(self, body, **kwargs):
        self._record("spinner", (body,), kwargs)
        return _Container(self.calls, "spinner")

    def columns(self, spec, **kwargs):
        count = spec if isinstance(spec, int) else len(spec)
        self._record("columns", (spec,), kwargs)
        return [_Container(self.calls, f"column{i}") for i in range(count)]

    @property
    def sidebar(self):
        self._record("sidebar", (), {})
        return _Container(self.calls, "sidebar")

    # -- media ------------------------------------------------------------

    def audio(self, data, **kwargs):
        self._record("audio", (getattr(data, "name", data),), kwargs)

    def video(self, data, **kwargs):
        self._record("video", (getattr(data, "name", data),), kwargs)

    # -- widgets ----------------------------------------------------------

    def file_uploader(self, label, type=None, key=None, **kwargs):
        self._record("file_uploader", (label,), {"type": type, "key": key})
        if key == "text_media_uploader":
            return self.media_file
        if key == "audio_split_uploader":
            return self.split_file
        return self.video_file

    def checkbox(self, label, value=False, key=None, help=None, disabled=False):
        self._record("checkbox", (label,), {"value": value, "key": key})
        return self.checkbox_values.get(label, value)

    def button(self, label, type=None, disabled=False, key=None, **kwargs):
        self._record("button", (label,), {"disabled": disabled})
        return label in self.clicked_labels

    def slider(self, label, min_value=None, max_value=None, value=None, **kwargs):
        self._record("slider", (label,), kwargs)
        return self.slider_values.get(label, value)

    def selectbox(self, label, options=(), index=0, format_func=None, **kwargs):
        self._record("selectbox", (label,), {"options": list(options)})
        if label in self.selectbox_values:
            return self.selectbox_values[label]
        if not options:
            return None
        return options[min(index, len(options) - 1)]

    def text_input(self, label, value="", **kwargs):
        self._record("text_input", (label,), kwargs)
        return value

    def text_area(self, label, value="", height=None, key=None, **kwargs):
        self._record("text_area", (label,), {"value": value, "key": key})
        return value

    def download_button(self, label, data=None, file_name=None, mime=None, key=None, **kwargs):
        self._record(
            "download_button",
            (label,),
            {"data": data, "file_name": file_name, "mime": mime, "key": key},
        )
        return False


fake_st = FakeStreamlit()
sys.modules["streamlit"] = fake_st

import streamlit_app  # noqa: E402
from core import transcriber  # noqa: E402
from utils import audio_split  # noqa: E402
from utils.docx_export import DOCX_MIME  # noqa: E402
from utils.logger import PipelineError  # noqa: E402

ARABIC_LINE = "الجملة الأولى من النص المستخرج."
SECOND_LINE = "and the second one."
SEGMENTS = [
    TranscriptSegment(start=0.0, end=3.0, text=ARABIC_LINE),
    TranscriptSegment(start=95.0, end=99.0, text=SECOND_LINE),
]


def _download(st, file_name):
    matches = [
        call for call in st.named("download_button") if call[2]["file_name"] == file_name
    ]
    assert len(matches) == 1, f"expected exactly one {file_name} button, got {len(matches)}"
    return matches[0][2]


def _zip_part(data: bytes, part: str) -> bytes:
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        return archive.read(part)


def _document_text(data: bytes) -> str:
    from utils.docx_export import W_NS

    root = ElementTree.fromstring(_zip_part(data, "word/document.xml"))
    return "\n".join(node.text or "" for node in root.iter(f"{{{W_NS}}}t"))


class _SectionTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self.st = fake_st
        self.st.calls.clear()
        self.st.session_state.clear()
        self.st.media_file = None
        self.st.video_file = None
        self.st.split_file = None
        self.st.checkbox_values.clear()
        self.st.selectbox_values.clear()
        self.st.slider_values.clear()
        self.st.clicked_labels.clear()

        self.transcribe_calls: list[dict] = []
        self._real_transcribe = transcriber.transcribe
        self._real_probe = streamlit_app.probe_duration

        def _stub_transcribe(**kwargs):
            self.transcribe_calls.append(kwargs)
            return list(SEGMENTS)

        transcriber.transcribe = _stub_transcribe
        # Keeps the old flow's duration probe away from the real ffmpeg.
        streamlit_app.probe_duration = lambda path: None

    def tearDown(self) -> None:
        transcriber.transcribe = self._real_transcribe
        streamlit_app.probe_duration = self._real_probe
        work_dir = self.st.session_state.get("work_dir")
        if work_dir:
            shutil.rmtree(work_dir, ignore_errors=True)

    def _run_section(self, *, timestamps: bool = False, click: bool = True, media=None):
        self.st.media_file = media
        self.st.checkbox_values["إظهار الطوابع الزمنية"] = timestamps
        if click:
            self.st.clicked_labels.add("📝 استخرج النص")
        streamlit_app.render_text_extraction_section(
            model_size="tiny", language="auto (تلقائي)", device="cpu"
        )


class UploaderTests(_SectionTestBase):
    def test_uploader_accepts_both_audio_and_video(self):
        self._run_section(media=None)
        uploader = [
            call
            for call in self.st.named("file_uploader")
            if call[2]["key"] == "text_media_uploader"
        ]
        self.assertEqual(len(uploader), 1)
        offered = set(uploader[0][2]["type"])
        for ext in ("mp3", "m4a", "wav", "aac", "ogg", "flac", "opus", "wma", "mp4", "mkv"):
            self.assertIn(ext, offered)
        # Nothing to do yet: no transcription is attempted without a file.
        self.assertEqual(self.transcribe_calls, [])
        self.assertTrue(self.st.named("info"))

    def test_section_header_and_caption_are_rendered(self):
        self._run_section(media=None)
        self.assertIn("📝 استخراج النص", self.st.labels("header"))
        self.assertTrue(self.st.named("divider"))
        self.assertTrue(self.st.named("caption"))


class ExtractionTests(_SectionTestBase):
    def setUp(self) -> None:
        super().setUp()
        self.media = _UploadedFile("interview.mp3", b"fake-audio-bytes")
        self._run_section(media=self.media)

    def test_transcribe_is_called_with_the_expected_settings(self):
        self.assertEqual(len(self.transcribe_calls), 1)
        kwargs = self.transcribe_calls[0]
        self.assertEqual(set(kwargs), {
            "video_path", "model_size", "language", "device",
            "compute_type_gpu", "compute_type_cpu", "logger",
        })
        self.assertTrue(kwargs["video_path"].endswith("interview.mp3"))
        self.assertEqual(kwargs["model_size"], "tiny")
        self.assertIsNone(kwargs["language"], "'auto (تلقائي)' must mean auto-detect")
        self.assertEqual(kwargs["device"], "cpu")
        self.assertEqual(kwargs["compute_type_gpu"], "float16")
        self.assertEqual(kwargs["compute_type_cpu"], "int8")
        self.assertIsInstance(kwargs["logger"], streamlit_app.StreamlitLogger)

    def test_uploaded_bytes_reach_disk(self):
        path = Path(self.transcribe_calls[0]["video_path"])
        self.assertTrue(path.exists())
        self.assertEqual(path.read_bytes(), b"fake-audio-bytes")

    def test_text_is_shown_in_a_copyable_text_area(self):
        areas = self.st.named("text_area")
        self.assertEqual(len(areas), 1)
        self.assertEqual(areas[0][2]["value"], f"{ARABIC_LINE}\n{SECOND_LINE}")

    def test_txt_download_matches_the_text_area(self):
        payload = _download(self.st, "interview.txt")
        self.assertEqual(payload["data"].decode("utf-8"), f"{ARABIC_LINE}\n{SECOND_LINE}")
        self.assertEqual(payload["mime"], "text/plain")

    def test_docx_download_is_a_valid_package_holding_the_text(self):
        payload = _download(self.st, "interview.docx")
        self.assertEqual(payload["mime"], DOCX_MIME)
        data = payload["data"]
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            self.assertIsNone(archive.testzip())
            self.assertIn("[Content_Types].xml", archive.namelist())
        text = _document_text(data)
        self.assertIn(ARABIC_LINE, text)
        self.assertIn(SECOND_LINE, text)
        # Titled with the uploaded file's name, and RTL-marked.
        self.assertIn("interview", text)
        raw = _zip_part(data, "word/document.xml").decode("utf-8")
        self.assertIn("<w:bidi/>", raw)
        self.assertIn("<w:rtl/>", raw)

    def test_no_timestamps_by_default(self):
        self.assertNotIn("[00:00]", _download(self.st, "interview.txt")["data"].decode("utf-8"))

    def test_success_message_counts_the_segments(self):
        messages = self.st.labels("success")
        self.assertTrue(any("2" in message and "interview.mp3" in message for message in messages))

    def test_first_rerun_renders_from_session_state_without_retranscribing(self):
        self.st.forget()
        self.st.clicked_labels.clear()
        streamlit_app.render_text_extraction_section(
            model_size="tiny", language="auto (تلقائي)", device="cpu"
        )
        # Both download buttons and the text are still offered...
        self.assertEqual(len(self.transcribe_calls), 1)
        self.assertEqual(
            self.st.named("text_area")[0][2]["value"], f"{ARABIC_LINE}\n{SECOND_LINE}"
        )
        self.assertEqual(len(self.st.named("download_button")), 2)

    def test_another_file_does_not_reuse_the_previous_transcript(self):
        self.st.forget()
        # A plain rerun (no button click) with a different upload: the
        # stale transcript of the previous file must not be shown for it.
        self.st.clicked_labels.clear()
        self.st.media_file = _UploadedFile("other.mp4", b"other-bytes")
        streamlit_app.render_text_extraction_section(
            model_size="tiny", language="auto (تلقائي)", device="cpu"
        )
        self.assertEqual(self.st.named("text_area"), [])
        self.assertEqual(self.st.named("download_button"), [])
        self.assertEqual(len(self.transcribe_calls), 1)

    def test_video_upload_is_previewed_with_a_video_player(self):
        self.st.forget()
        self.st.media_file = _UploadedFile("talk.mp4", b"fake-video-bytes")
        self.st.clicked_labels.clear()
        streamlit_app.render_text_extraction_section(
            model_size="tiny", language="auto (تلقائي)", device="cpu"
        )
        self.assertEqual(self.st.labels("video"), ["talk.mp4"])
        self.assertEqual(self.st.labels("audio"), [])


class TimestampToggleTests(_SectionTestBase):
    def test_timestamps_flow_into_both_the_text_and_the_docx(self):
        self._run_section(
            timestamps=True, media=_UploadedFile("interview.m4a", b"audio")
        )
        text = self.st.named("text_area")[0][2]["value"]
        self.assertEqual(text, f"[00:00] {ARABIC_LINE}\n[01:35] {SECOND_LINE}")
        self.assertEqual(
            _download(self.st, "interview.txt")["data"].decode("utf-8"), text
        )
        # The .docx carries the same stamps as its own run text.
        docx_text = _document_text(_download(self.st, "interview.docx")["data"])
        self.assertIn(text, docx_text)
        self.assertIn(f"[01:35] {SECOND_LINE}", docx_text)


class FailureTests(_SectionTestBase):
    def test_pipeline_error_is_reported_with_its_hint(self):
        def _boom(**kwargs):
            raise PipelineError("Transcription produced no text.", hint="Silent file?")

        transcriber.transcribe = _boom
        self._run_section(media=_UploadedFile("silent.mp3", b"x"))

        self.assertEqual(self.st.labels("error"), ["Transcription produced no text."])
        self.assertEqual(self.st.labels("info"), ["Silent file?"])
        self.assertNotIn("text_extraction", self.st.session_state)
        self.assertEqual(self.st.named("text_area"), [])

    def test_unexpected_error_shows_a_traceback_instead_of_crashing(self):
        def _boom(**kwargs):
            raise ValueError("something odd")

        transcriber.transcribe = _boom
        self._run_section(media=_UploadedFile("weird.wav", b"x"))

        # The section catches it: a message plus the traceback in an
        # expander, and no half-written result in session state.
        self.assertEqual(self.st.labels("error"), ["خطأ غير متوقع: something odd"])
        tracebacks = [call[1][0] for call in self.st.named("code")]
        self.assertTrue(any("ValueError: something odd" in body for body in tracebacks))
        self.assertNotIn("text_extraction", self.st.session_state)


class TempFileCachingTests(_SectionTestBase):
    def test_the_two_uploaders_do_not_invalidate_each_others_cache(self):
        video = _UploadedFile("movie.mp4", b"video-bytes")
        audio = _UploadedFile("voice.wav", b"audio-bytes")

        streamlit_app.save_uploaded_file(video)
        self.assertEqual(self.st.session_state["saved_file_key"], "movie.mp4_11")
        video_path = Path(self.st.session_state["work_dir"]) / "movie.mp4"

        streamlit_app.save_uploaded_file(audio, cache_key_slot="saved_media_key")
        # The clip flow's cache entry is untouched by the audio upload...
        self.assertEqual(self.st.session_state["saved_file_key"], "movie.mp4_11")
        self.assertEqual(self.st.session_state["saved_media_key"], "voice.wav_11")

        # ...so neither file is rewritten on the next rerun.
        before = video_path.stat().st_mtime_ns
        streamlit_app.save_uploaded_file(video)
        streamlit_app.save_uploaded_file(audio, cache_key_slot="saved_media_key")
        self.assertEqual(video_path.stat().st_mtime_ns, before)


class OldFlowIntactTests(_SectionTestBase):
    def test_main_still_renders_the_clip_flow_and_the_new_section(self):
        streamlit_app.main()

        # The original uploader only offers video extensions.
        video_uploader = [
            call
            for call in self.st.named("file_uploader")
            if call[2].get("key") is None
        ]
        self.assertEqual(len(video_uploader), 1)
        self.assertEqual(
            set(video_uploader[0][2]["type"]), {"mp4", "mkv", "mov", "avi", "webm"}
        )

        # Sidebar settings and the analyse button are all still there.
        for label in (
            "عدد المقاطع",
            "المنصة (بتحدد أطوال الكليب المقترحة)",
            "اللغة",
            "حجم نموذج Whisper",
            "الجهاز",
            "نموذج Ollama",
            "عنوان Ollama",
            "قوة إصلاح الإضاءة",
            "قسّم الفيديو بالتساوي حسب عدد المقاطع",
            "حرق الكابشن تلقائيًا",
            "عنوان جذاب في أول 3-5 ثواني",
            "توحيد مستوى الصوت (loudnorm)",
            "إصلاح الإضاءة تلقائيًا",
        ):
            with self.subTest(widget=label):
                self.assertTrue(
                    label in self.st.labels("slider")
                    or label in self.st.labels("selectbox")
                    or label in self.st.labels("text_input")
                    or label in self.st.labels("checkbox")
                )
        self.assertIn("🔍 حلّل الفيديو", self.st.labels("button"))
        self.assertIn("ارفع الفيديو", self.st.labels("file_uploader"))

        # ...and the new, independent section is rendered alongside it. No
        # media is uploaded in this test, so the section shows its upload
        # prompt instead of the extract button (which appears only after a
        # file is uploaded).
        self.assertIn("📝 استخراج النص", self.st.labels("header"))
        self.assertIn("ارفع ملف صوت أو فيديو", self.st.labels("file_uploader"))

    def test_main_with_a_video_keeps_both_flows_available(self):
        self.st.video_file = _UploadedFile("movie.mp4", b"video-bytes")
        streamlit_app.main()
        # Old flow: the uploaded video is previewed and the analyse button
        # is enabled.
        self.assertIn("movie.mp4", self.st.labels("video"))
        analyze_buttons = [
            call for call in self.st.named("button") if call[1][0] == "🔍 حلّل الفيديو"
        ]
        self.assertEqual(analyze_buttons[0][2]["disabled"], False)
        # New section: its own uploader is still empty, so it just invites
        # the user to upload something.
        self.assertIn("📝 استخراج النص", self.st.labels("header"))
        self.assertTrue(self.st.named("info"))


class AudioSplitSectionTests(_SectionTestBase):
    """Wiring tests for the «✂️ تقسيم الصوت» section."""

    def setUp(self) -> None:
        super().setUp()
        self.st.split_file = _UploadedFile("podcast.mp3", b"fake-audio-bytes")
        streamlit_app.probe_duration = lambda path: 90.0
        self.split_calls: list[dict] = []
        self._real_split = audio_split.split_audio

        def _stub_split(source_path, output_dir, **kwargs):
            self.split_calls.append(
                {
                    "source_path": str(source_path),
                    "output_dir": str(output_dir),
                    **kwargs,
                }
            )
            out_dir = Path(output_dir)
            out_dir.mkdir(parents=True, exist_ok=True)
            written = []
            for index in range(1, 4):
                part = out_dir / f"podcast_part_{index:02d}.mp3"
                part.write_bytes(b"part-bytes-" + bytes([index]))
                written.append(part)
            return written

        audio_split.split_audio = _stub_split

    def tearDown(self) -> None:
        audio_split.split_audio = self._real_split
        super().tearDown()

    def _run(self) -> None:
        streamlit_app.render_audio_split_section()

    def test_uploader_accepts_audio_and_video(self):
        self._run()
        uploaders = [
            call
            for call in self.st.named("file_uploader")
            if call[2]["key"] == "audio_split_uploader"
        ]
        self.assertEqual(len(uploaders), 1)
        offered = set(uploaders[0][2]["type"])
        for ext in ("mp3", "wav", "m4a", "mp4", "mkv"):
            self.assertIn(ext, offered)

    def test_preview_states_the_part_count_without_splitting(self):
        self._run()
        self.assertTrue(any("3" in message for message in self.st.labels("info")))
        self.assertEqual(self.split_calls, [])

    def test_split_button_writes_parts_and_offers_downloads(self):
        self.st.clicked_labels.add("✂️ قسّم الصوت")
        self._run()
        self.assertEqual(len(self.split_calls), 1)
        call = self.split_calls[0]
        self.assertTrue(str(call["source_path"]).endswith("podcast.mp3"))
        self.assertEqual(call["num_parts"], 3)
        self.assertIsNone(call["part_seconds"])

        labels = self.st.labels("download_button")
        part_labels = [
            label for label in labels if label.startswith("⬇️ تنزيل podcast_part_")
        ]
        self.assertEqual(len(part_labels), 3)
        self.assertIn("⬇️ تنزيل كل الأجزاء (ZIP)", labels)

        audio_calls = [
            call for call in self.st.named("audio") if "podcast_part_" in call[1][0]
        ]
        self.assertEqual(len(audio_calls), 3)

        zip_call = [
            call
            for call in self.st.named("download_button")
            if call[1][0].endswith("(ZIP)")
        ][0]
        with zipfile.ZipFile(io.BytesIO(zip_call[2]["data"])) as archive:
            self.assertEqual(
                sorted(archive.namelist()),
                ["podcast_part_01.mp3", "podcast_part_02.mp3", "podcast_part_03.mp3"],
            )

    def test_too_many_parts_is_refused_before_splitting(self):
        self.st.slider_values["عدد الأجزاء"] = 50
        self.st.clicked_labels.add("✂️ قسّم الصوت")
        self._run()
        self.assertEqual(self.split_calls, [])
        self.assertTrue(self.st.named("warning"))
        self.assertEqual(self.st.named("download_button"), [])

    def test_duration_mode_passes_part_seconds(self):
        self.st.selectbox_values["طريقة التقسيم"] = "حسب مدة الجزء"
        self.st.slider_values["مدة كل جزء (ثواني)"] = 40.0
        self.st.clicked_labels.add("✂️ قسّم الصوت")
        self._run()
        self.assertEqual(len(self.split_calls), 1)
        self.assertIsNone(self.split_calls[0]["num_parts"])
        self.assertEqual(self.split_calls[0]["part_seconds"], 40.0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
