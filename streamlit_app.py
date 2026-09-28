"""
Lightweight local GUI for the Long Video -> TikTok Clips pipeline.

Built with Streamlit: runs entirely in your browser but 100% locally -
no data leaves your machine. This is a thin presentation layer only; all
actual work still happens in core/, exactly as it does from app.py.

Run with:
    streamlit run streamlit_app.py
"""

from __future__ import annotations

import re
import shutil
import time
import zipfile
import tempfile
import traceback
from pathlib import Path

import streamlit as st

from config import (
    Config,
    PLATFORM_PRESETS,
    SUPPORTED_AUDIO_EXTENSIONS,
    SUPPORTED_MEDIA_EXTENSIONS,
    SUPPORTED_VIDEO_EXTENSIONS,
    plan_clip_band,
    suggest_num_clips,
)
from app import analyze as analyze_pipeline
from core import clip_processor, transcriber
from core.quality_filter import describe_shortfall
from models.schemas import FinalClip
from utils import audio_split
from utils.docx_export import DOCX_MIME, segments_to_docx_bytes, segments_to_text
from utils.ffmpeg import probe_duration
from utils.logger import Logger, PipelineError

st.set_page_config(
    page_title="Long Video → Clips",
    page_icon="🎬",
    layout="centered",
)


class StreamlitLogger(Logger):
    """Redirects the pipeline's normal print-based logging into a live,
    scrolling status panel in the Streamlit UI instead of the terminal.
    """

    def __init__(self, total_stages: int, progress_bar, status_placeholder, log_placeholder):
        super().__init__(total_stages=total_stages, verbose=True)
        self._progress_bar = progress_bar
        self._status_placeholder = status_placeholder
        self._log_placeholder = log_placeholder
        self._lines: list[str] = []

    def _flush(self) -> None:
        self._log_placeholder.code("\n".join(self._lines[-200:]), language=None)

    def stage(self, index: int, message: str) -> None:
        self._status_placeholder.markdown(f"**[{index}/{self.total_stages}] {message}**")
        self._progress_bar.progress(index / self.total_stages)
        self._lines.append(f"[{index}/{self.total_stages}] {message}")
        self._flush()

    def info(self, message: str) -> None:
        self._lines.append(f"    - {message}")
        self._flush()

    def warn(self, message: str) -> None:
        self._lines.append(f"⚠️  {message}")
        self._flush()

    def error(self, message: str) -> None:
        self._lines.append(f"❌ {message}")
        self._flush()


class TranscribeLogger(StreamlitLogger):
    """StreamlitLogger that also drives the progress bar from the
    transcriber's own progress lines.

    core.transcriber reports progress as "% done with the audio timeline",
    so the bar can follow the real thing instead of sitting at zero until
    the run ends.
    """

    _PERCENT_RE = re.compile(r"Transcribing\.\.\. (\d+)%")

    def info(self, message: str) -> None:
        super().info(message)
        match = self._PERCENT_RE.search(message)
        if match:
            self._progress_bar.progress(min(1.0, int(match.group(1)) / 100.0))


def _find_previous_work_dir(name: str, size: int) -> Path | None:
    """A recent temp folder that already holds this exact video file.

    Antigravity / streamlit can die mid-analysis; the per-session temp
    folder survives on disk, so adopting it lets a fresh session resume
    (transcript / candidates / scores / rendered clips) instead of
    starting the transcription and the LLM ranking over.
    """
    for candidate in sorted(
        Path(tempfile.gettempdir()).glob("lvc_*"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    ):
        if not candidate.is_dir() or candidate.name == "lvc_smart_check":
            continue
        video = candidate / name
        try:
            if video.is_file() and video.stat().st_size == size:
                return candidate
        except OSError:
            continue
    return None


def save_uploaded_file(uploaded_file, cache_key_slot: str = "saved_file_key") -> Path:
    """Save the uploaded video to a temp folder that persists for the
    session, since the pipeline needs a real file path on disk (nothing
    is ever sent anywhere - this is purely local disk I/O).

    Cached per (name, size) so calling this repeatedly for the same file
    across Streamlit reruns (e.g. while the user is still adjusting
    sliders) doesn't rewrite a potentially large file to disk every time.
    `cache_key_slot` lets a second, independent uploader keep its own
    cache entry instead of invalidating the first one's.
    """
    if "work_dir" not in st.session_state:
        previous = _find_previous_work_dir(
            Path(uploaded_file.name).name, int(uploaded_file.size)
        )
        if previous is not None:
            st.session_state.work_dir = str(previous)
            st.info(
                "لقيت مجلد جلسة سابقة لنفس الفيديو - التحليل هيكمل من "
                "آخر نقطة محفوظة بدل ما يبدأ من الأول."
            )
        else:
            st.session_state.work_dir = tempfile.mkdtemp(prefix="lvc_")
    work_dir = Path(st.session_state.work_dir)
    dest = work_dir / uploaded_file.name

    cache_key = f"{uploaded_file.name}_{uploaded_file.size}"
    if st.session_state.get(cache_key_slot) != cache_key or not dest.exists():
        with open(dest, "wb") as f:
            f.write(uploaded_file.getbuffer())
        st.session_state[cache_key_slot] = cache_key
    return dest


def _as_bullet(line: str) -> str:
    """Turn one line of quality_filter.describe_shortfall() output into a
    Markdown bullet for the GUI warning."""
    text = line.strip()
    if text.startswith("-"):
        text = text[1:].strip()
    return f"- {text}"


def get_video_duration_seconds(uploaded_file, video_path: Path) -> float | None:
    """Probe (and cache) the uploaded video's duration, so we can suggest
    a sensible clip count before the user even presses Run.
    """
    cache_key = f"{uploaded_file.name}_{uploaded_file.size}"
    if st.session_state.get("probed_key") != cache_key:
        try:
            st.session_state.probed_duration = probe_duration(str(video_path))
        except Exception:
            st.session_state.probed_duration = None
        st.session_state.probed_key = cache_key
    return st.session_state.probed_duration


def render_text_extraction_section(
    model_size: str, language: str, device: str
) -> None:
    """«استخراج النص»: transcribe an uploaded audio file *or* video and
    hand the text back for copying or downloading.

    Deliberately independent of the clip pipeline above: no candidate
    generation, no LLM, no render. It shares only the Whisper settings
    from the sidebar (`model_size`, `language`, `device`) and the same
    temp-file caching, so nothing about the old video → clips flow is
    touched by this section.
    """
    st.divider()
    st.header("📝 استخراج النص")
    st.caption(
        "ارفع ملف صوت (mp3, m4a, wav …) أو فيديو، وهنطلّعلك النص المكتوب "
        "جاهز للنسخ أو التنزيل كملف Word أو نصي. الكلام بيتحوّل على جهازك."
    )

    media_file = st.file_uploader(
        "ارفع ملف صوت أو فيديو",
        type=[ext.lstrip(".") for ext in sorted(SUPPORTED_MEDIA_EXTENSIONS)],
        key="text_media_uploader",
    )
    include_timestamps = st.checkbox(
        "إظهار الطوابع الزمنية",
        value=False,
        key="text_include_timestamps",
        help="يحطّ وقت كل جملة قبل النص (مثال: [01:23] النص).",
    )

    if media_file is None:
        st.info("ارفع ملف صوت أو فيديو الأول عشان نبدأ استخراج النص.")
        return

    # Shares the session temp folder with the clip flow, but keeps its own
    # cache slot so the two uploads never invalidate each other.
    media_path = save_uploaded_file(media_file, cache_key_slot="saved_media_key")
    media_key = f"{media_file.name}_{media_file.size}"

    is_audio = Path(media_file.name).suffix.lower() in SUPPORTED_AUDIO_EXTENSIONS
    try:
        # A preview is a convenience only - an unplayable codec in the
        # browser must not stop the extraction below.
        if is_audio:
            st.audio(media_file)
        else:
            st.video(media_file)
    except Exception:  # noqa: BLE001
        pass

    extract_clicked = st.button("📝 استخرج النص", type="primary")

    if extract_clicked:
        st.subheader("الاستخراج")
        progress_bar = st.progress(0.0)
        status_placeholder = st.empty()
        with st.expander("سجل تفاصيل الاستخراج", expanded=False):
            log_placeholder = st.empty()

        status_placeholder.markdown(
            "**جارٍ استخراج النص... قد يستغرق هذا وقتًا حسب طول الملف**"
        )
        # No logger.stage() here: with a single stage it would pin the bar
        # at 100% before any work happened. TranscribeLogger moves it from
        # the transcriber's own percentage lines instead.
        logger = TranscribeLogger(1, progress_bar, status_placeholder, log_placeholder)
        settings = Config()

        try:
            with st.spinner("جارٍ تحويل الصوت إلى نص..."):
                segments = transcriber.transcribe(
                    video_path=str(media_path),
                    model_size=model_size,
                    language=None if language.startswith("auto") else language,
                    device=device,
                    compute_type_gpu=settings.compute_type_gpu,
                    compute_type_cpu=settings.compute_type_cpu,
                    logger=logger,
                )
            progress_bar.progress(1.0)
            status_placeholder.markdown("**تم استخراج النص ✅**")
            # Kept in session state so the copy box and both download
            # buttons survive the reruns their own clicks trigger.
            st.session_state.text_extraction = {
                "media_key": media_key,
                "segments": segments,
            }
        except PipelineError as e:
            st.error(e.message)
            if e.hint:
                st.info(e.hint)
        except Exception as e:  # noqa: BLE001
            st.error(f"خطأ غير متوقع: {e}")
            with st.expander("تفاصيل تقنية"):
                st.code(traceback.format_exc())

    extraction = st.session_state.get("text_extraction")
    if not extraction or extraction["media_key"] != media_key:
        return

    segments = extraction["segments"]
    text = segments_to_text(segments, include_timestamps=include_timestamps)
    file_stem = Path(media_file.name).stem
    variant = "with_ts" if include_timestamps else "plain"

    st.success(f"تم استخراج {len(segments)} مقطع نصي من «{media_file.name}».")
    # A text area (not st.code) so selecting and copying the transcript
    # works normally; the key carries the file + timestamp choice, so a new
    # upload or a toggle change shows fresh text instead of stale state.
    st.text_area(
        "النص المستخرج — اضغط داخل المربع لتحديده ونسخه",
        value=text,
        height=320,
        key=f"text_output_{media_key}_{variant}",
    )

    col_txt, col_docx = st.columns(2)
    with col_txt:
        st.download_button(
            "⬇️ تنزيل .txt",
            data=text.encode("utf-8"),
            file_name=f"{file_stem}.txt",
            mime="text/plain",
            key=f"dl_txt_{media_key}_{variant}",
        )
    with col_docx:
        st.download_button(
            "⬇️ تنزيل Word (.docx)",
            data=segments_to_docx_bytes(
                segments, title=file_stem, include_timestamps=include_timestamps
            ),
            file_name=f"{file_stem}.docx",
            mime=DOCX_MIME,
            key=f"dl_docx_{media_key}_{variant}",
        )


def render_audio_split_section() -> None:
    """«تقسيم الصوت»: cut an uploaded audio file (or a video's audio track)
    into parts - the audio sibling of the clip pipeline's split-evenly mode.
    Independent of both other sections; nothing here touches the clip flow.
    """
    st.divider()
    st.header("✂️ تقسيم الصوت")
    st.caption(
        "ارفع صوت (أو فيديو وهناخد الصوت بتاعه)، واختار عدد الأجزاء أو مدة "
        "كل جزء، والأداة بتقطّعه محليًا بـ ffmpeg لأجزاء جاهزة للتنزيل."
    )

    audio_file = st.file_uploader(
        "ارفع ملف صوت أو فيديو للتقسيم",
        type=[ext.lstrip(".") for ext in sorted(SUPPORTED_MEDIA_EXTENSIONS)],
        key="audio_split_uploader",
    )
    if audio_file is None:
        st.info("ارفع ملف صوت أو فيديو الأول عشان نبدأ التقسيم.")
        return

    media_path = save_uploaded_file(audio_file, cache_key_slot="saved_split_key")
    media_key = f"{audio_file.name}_{audio_file.size}"

    is_audio = Path(audio_file.name).suffix.lower() in SUPPORTED_AUDIO_EXTENSIONS
    try:
        if is_audio:
            st.audio(audio_file)
        else:
            st.video(audio_file)
    except Exception:  # noqa: BLE001
        pass

    try:
        duration = probe_duration(str(media_path))
    except PipelineError as e:
        st.error(e.message)
        if e.hint:
            st.info(e.hint)
        return
    if duration <= 0:
        st.error("معرفش أقرا مدة الملف ده — تأكد إنه ملف صوت/فيديو صالح.")
        return
    st.caption(f"مدة الملف: {duration / 60:.1f} دقيقة ({duration:.0f} ثانية).")

    mode = st.selectbox(
        "طريقة التقسيم",
        options=["حسب عدد الأجزاء", "حسب مدة الجزء"],
        key="audio_split_mode",
    )
    num_parts: int | None = None
    part_seconds: float | None = None
    if mode == "حسب عدد الأجزاء":
        num_parts = st.slider(
            "عدد الأجزاء",
            min_value=1,
            max_value=50,
            value=3,
            key="audio_split_num_parts",
        )
    else:
        part_seconds = st.slider(
            "مدة كل جزء (ثواني)",
            min_value=float(audio_split.MIN_PART_SECONDS),
            max_value=3600.0,
            value=60.0,
            step=1.0,
            key="audio_split_part_seconds",
        )

    try:
        parts = audio_split.plan_parts(
            duration, num_parts=num_parts, part_seconds=part_seconds
        )
    except ValueError as e:
        st.warning(f"⛔ {e}")
        return

    average_share = sum(end - start for start, end in parts) / len(parts)
    st.info(
        f"هيتقسم لـ **{len(parts)}** جزء، كل جزء ~{average_share:.0f} ثانية "
        "(آخر جزء ممكن يكون أقصر شوية)."
    )

    clicked = st.button("✂️ قسّم الصوت", type="primary", key="audio_split_go")
    if clicked:
        out_dir = (
            Path(st.session_state.work_dir)
            / "audio_split"
            / f"{Path(audio_file.name).stem}_{audio_file.size}"
        )
        st.subheader("التقسيم")
        progress_bar = st.progress(0.0)
        status_placeholder = st.empty()
        with st.expander("سجل تفاصيل التقسيم", expanded=False):
            log_placeholder = st.empty()

        logger = StreamlitLogger(1, progress_bar, status_placeholder, log_placeholder)
        try:
            with st.spinner("جارٍ تقسيم الصوت..."):
                part_paths = audio_split.split_audio(
                    media_path,
                    out_dir,
                    num_parts=num_parts,
                    part_seconds=part_seconds,
                    logger=logger,
                )
            progress_bar.progress(1.0)
            status_placeholder.markdown("**تم التقسيم ✅**")
            st.session_state.audio_split = {
                "source_key": media_key,
                "parts": [str(part) for part in part_paths],
            }
        except PipelineError as e:
            st.error(e.message)
            if e.hint:
                st.info(e.hint)
        except Exception as e:  # noqa: BLE001
            st.error(f"خطأ غير متوقع: {e}")
            with st.expander("تفاصيل تقنية"):
                st.code(traceback.format_exc())

    split_state = st.session_state.get("audio_split")
    if not split_state or split_state["source_key"] != media_key:
        return

    part_paths = [
        Path(part) for part in split_state["parts"] if Path(part).exists()
    ]
    if not part_paths:
        return

    st.success(f"تم تقسيم «{audio_file.name}» لـ {len(part_paths)} جزء.")
    for part in part_paths:
        st.markdown(f"**{part.name}** — {part.stat().st_size / 1024 / 1024:.2f} MB")
        try:
            st.audio(str(part))
        except Exception:  # noqa: BLE001
            pass
        with open(part, "rb") as f:
            st.download_button(
                f"⬇️ تنزيل {part.name}",
                data=f.read(),
                file_name=part.name,
                mime="audio/mpeg" if part.suffix == ".mp3" else "audio/mp4",
                key=f"dl_part_{part.name}",
            )
    st.download_button(
        "⬇️ تنزيل كل الأجزاء (ZIP)",
        data=audio_split.parts_zip_bytes(part_paths),
        file_name=f"{Path(audio_file.name).stem}_parts.zip",
        mime="application/zip",
        key="dl_parts_zip",
    )


def build_clips_zip(clips, dest_dir: Path) -> Path | None:
    """Zip every rendered clip into one archive for a single download.

    Uses ZIP_STORED (no compression): the clips are already-compressed
    H.264, so deflating would burn real CPU for almost no size gain.
    Returns the archive path, or None when there is nothing to zip.
    """
    existing = [
        Path(c.output_path)
        for c in clips
        if getattr(c, "output_path", None) and Path(c.output_path).exists()
    ]
    if not existing:
        return None
    dest_dir.mkdir(parents=True, exist_ok=True)
    zip_path = dest_dir / "all_clips.zip"
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_STORED) as zf:
        for p in sorted(existing, key=lambda x: x.name):
            zf.write(p, arcname=p.name)
    return zip_path


# Stale-folders sweep: anything older than this is assumed abandoned
# (a closed tab / crashed session) and is removed at startup. Long
# enough that resuming an interrupted analysis still works days later.
_STALE_WORK_DIR_AGE_HOURS = 72.0


# NOTE: there is intentionally NO cleanup of the session's own folder at
# shutdown. Resume (config.resume) needs these folders to SURVIVE a
# normal exit so a re-opened app can pick a half-finished analysis back
# up; the age-based sweep below handles disk hygiene instead. An atexit
# hook used to delete the folder on clean shutdown, which silently
# destroyed every resume artifact - do not re-add it.


def _sweep_stale_work_dirs() -> int:
    """Delete lvc_* folders left behind by earlier (closed) sessions.

    cleanup_on_exit() below can only ever run while a session is
    alive, so closed tabs and crashed sessions used to leave their
    uploads/analysis/clips on disk forever. The current session's
    folder is always skipped, and recent folders are left alone.
    """
    current = str(st.session_state.get("work_dir") or "")
    cutoff = time.time() - _STALE_WORK_DIR_AGE_HOURS * 3600
    removed = 0
    for path in Path(tempfile.gettempdir()).glob("lvc_*"):
        if not path.is_dir() or str(path) == current:
            continue
        try:
            if path.stat().st_mtime >= cutoff:
                continue
            shutil.rmtree(path, ignore_errors=True)
            removed += 1
        except OSError:
            continue
    return removed


def main() -> None:
    # Once per session: sweep leftovers from earlier sessions.
    if not st.session_state.get("_stale_sweep_done"):
        _sweep_stale_work_dirs()
        st.session_state["_stale_sweep_done"] = True

    st.title("🎬 Long Video → TikTok Clips")
    st.caption(
        "يعمل بالكامل على جهازك — لا يتم رفع أي فيديو أو صوت أو نص إلى أي خادم خارجي."
    )

    uploaded_file = st.file_uploader(
        "ارفع الفيديو",
        type=[ext.lstrip(".") for ext in SUPPORTED_VIDEO_EXTENSIONS],
    )

    video_path: Path | None = None
    video_duration_seconds: float | None = None
    suggested_clips = 5
    slider_key = "num_clips_no_file"

    if uploaded_file is not None:
        video_path = save_uploaded_file(uploaded_file)
        video_duration_seconds = get_video_duration_seconds(uploaded_file, video_path)
        if video_duration_seconds:
            suggested_clips = suggest_num_clips(video_duration_seconds)
        # Dynamic key: a newly uploaded file gets a fresh suggested
        # default; re-uploading the same file keeps whatever the user
        # last set for it.
        slider_key = f"num_clips_{uploaded_file.name}_{uploaded_file.size}"

    with st.sidebar:
        st.header("الإعدادات")
        num_clips = st.slider(
            "عدد المقاطع", min_value=1, max_value=30, value=suggested_clips, key=slider_key
        )
        resume_enabled = st.checkbox(
            "استكمال من آخر نقطة (لو الواجهة أو السيرفر قفل)",
            value=True,
            help=(
                "لو التحليل اتقطع في النص، إعادة رفع نفس الفيديو بنفس "
                "الإعدادات هتكمّل من آخر مرحلة خلصت (ترجمة/مرشحات/تقييم/"
                "كليبات مصدّرة) بدل ما تبدأ من الأول."
            ),
            key="resume_enabled",
        )
        split_evenly = st.checkbox(
            "قسّم الفيديو بالتساوي حسب عدد المقاطع",
            value=True,
            help=(
                "المدة بتتحسب تلقائيًا = طول الفيديو ÷ عدد المقاطع، فإنت تختار "
                "العدد بس. لو أطفأتها، الشرائح اللي تحت هي اللي تحدد أقل/أقصى مدة."
            ),
        )
        platform_choice = st.selectbox(
            "المنصة (بتحدد أطوال الكليب المقترحة)",
            options=["none", "tiktok", "reels", "shorts"],
            format_func=lambda p: {"none": "بدون", "tiktok": "تيك توك", "reels": "ريلز", "shorts": "شورتس"}[p],
            key="platform_choice",
        )
        _preset = PLATFORM_PRESETS.get(platform_choice, {})
        if split_evenly:
            # The clip count is the only input here, so show the length it
            # produces instead of two sliders frozen at unrelated numbers.
            if video_duration_seconds:
                _share = video_duration_seconds / max(1, num_clips)
                _preset_max = _preset.get("max_duration")
                # A chosen platform's cap always wins over the even
                # split: picking 'shorts' must never hand back
                # 3-minute clips just because the count is low.
                _eff_max = min(_share, _preset_max) if _preset_max else _share
                st.markdown(
                    f"**⏱️ مدة كل مقطع: {_eff_max:.0f} ثانية** (≈ {_eff_max / 60:.1f} دقيقة)"
                )
                st.caption(
                    "بتتحسب = طول الفيديو ÷ عدد المقاطع، وبتتغيّر كل ما تغيّر العدد."
                )
                min_duration = float(_preset.get("min_duration", 20))
                max_duration = float(_eff_max)
            else:
                st.caption("ارفع الفيديو الأول عشان نحسب مدة كل مقطع من عدد المقاطع.")
                min_duration = float(_preset.get("min_duration", 20))
                max_duration = float(_preset.get("max_duration", 60))
        else:
            min_duration = st.slider(
                "أقل مدة (ثانية)",
                5,
                60,
                int(_preset.get("min_duration", 20)),
                key=f"min_duration_{platform_choice}",
            )
            max_duration = st.slider(
                "أقصى مدة (ثانية)",
                20,
                120,
                int(_preset.get("max_duration", 60)),
                key=f"max_duration_{platform_choice}",
            )
        language = st.selectbox(
            "اللغة", options=["auto (تلقائي)", "ar", "en"], index=0
        )
        model_size = st.selectbox(
            "حجم نموذج Whisper",
            options=["tiny", "base", "small", "medium", "large-v3"],
            index=3,
        )
        device = st.selectbox("الجهاز", options=["auto", "cuda", "cpu"], index=0)
        ollama_model = st.text_input("نموذج Ollama", value="qwen3.5:9b")
        ollama_host = st.text_input("عنوان Ollama", value="http://localhost:11434")
        use_smart_reframe = st.checkbox(
            "القص الذكي (تتبع الوجه/المتحدث)",
            value=False,
            help=(
                "بدل القص المركزي الثابت، يتبع الوجه/المتحدث تلقائيًا. "
                "أبطأ بكثير ويحتاج تثبيت requirements-smart-reframe.txt."
            ),
        )
        st.markdown("**تحرير إضافي**")
        burn_captions = st.checkbox(
            "حرق الكابشن تلقائيًا",
            value=False,
            help="ترجمة نصية قصيرة ومتزامنة تظهر أسفل الفيديو.",
        )
        add_title_overlay = st.checkbox(
            "عنوان جذاب في أول 3-5 ثواني",
            value=False,
            help="عنوان يولّده الذكاء الاصطناعي تلقائيًا لكل مقطع.",
        )
        normalize_audio = st.checkbox(
            "توحيد مستوى الصوت (loudnorm)",
            value=True,
            help="يوحّد صوت كل كليب على -16 LUFS حتى تطلع الكليبات كلها بنفس قوة الصوت.",
        )
        fix_lighting = st.checkbox(
            "إصلاح الإضاءة تلقائيًا",
            value=False,
            help=(
                "يقيس سطوع كل مقطع فعليًا من الفيديو الأصلي ثم يصحّح الإضاءة "
                "تلقائيًا: المقاطع المعتمة تفتح، والمقاطع الفاتحة زيادة تهدى، "
                "والمقاطع المعرّضة صح بتتسيب زي ما هي من غير تغيير. "
                "يشتغل مع القص العادي والقص الذكي، ومع الكابشن/العنوان."
            ),
        )
        lighting_strength = st.slider(
            "قوة إصلاح الإضاءة",
            min_value=0.0,
            max_value=2.0,
            value=1.0,
            step=0.1,
            key="lighting_strength",
            disabled=not fix_lighting,
            help=(
                "1.0 = التصحيح الكامل المحسوب من قياس الفيديو (الافتراضي). "
                "صفر = بدون تصحيح، وأعلى من 1 = تصحيح أقوى (بحدود آمنة)."
            ),
        )

    if uploaded_file is not None:
        st.video(uploaded_file)

        if video_duration_seconds:
            minutes = video_duration_seconds / 60
            # In even-split mode the length actually used is the video's
            # share per clip, not whatever the duration sliders say.
            effective_max = (
                video_duration_seconds / max(1, num_clips)
                if split_evenly
                else float(max_duration)
            )
            covered_minutes = (num_clips * effective_max) / 60
            if covered_minutes < minutes * 0.6:
                st.warning(
                    f"🎬 الفيديو مدته حوالي {minutes:.0f} دقيقة. الإعدادات الحالية "
                    f"({num_clips} مقطع × حتى {effective_max:.0f}ث) هتغطي تقريبًا "
                    f"{covered_minutes:.0f} دقيقة بس من أصل {minutes:.0f} — يعني جزء "
                    f"كبير من الفيديو مش هيتفحص. اقترحنا {suggested_clips} مقطع "
                    f"تلقائيًا بناءً على الطول؛ زوّد الرقم من الشريط الجانبي لو "
                    f"عايز تغطية أشمل، أو كمّل بالإعداد الحالي لو ده اللي قاصده."
                )
            else:
                st.info(
                    f"🎬 مدة الفيديو حوالي {minutes:.0f} دقيقة. الإعداد الحالي "
                    f"({num_clips} مقطع × ~{effective_max:.0f}ث) بيغطي تقريبًا {covered_minutes:.0f} دقيقة."
                )

            # Feasibility guard: say plainly when the request exceeds what
            # the source can physically hold, instead of silently producing
            # fewer clips than asked for.
            plan = plan_clip_band(
                video_duration_seconds,
                num_clips,
                float(min_duration),
                float(max_duration),
                Config().max_overlap_ratio,
            )
            if not plan.feasible:
                st.error(
                    f"⚠️ الفيديو مدته {video_duration_seconds:.0f} ثانية بس. "
                    f"حتى بعد ما نصغّر طول المقطع لـ {plan.max_duration:.0f} ثانية "
                    f"(= {video_duration_seconds:.0f} ÷ {num_clips})، أقصى عدد "
                    f"مقاطع من غير تداخل هو {plan.max_feasible_clips} مقطع. "
                    f"يعني {num_clips} مقطع مستحيل يطلعوا من الفيديو ده — قلّل "
                    f"«عدد المقاطع» أو استخدم فيديو أطول. لو كمّلت بالأرقام دي، "
                    f"هنقولك بالظبط إنتاج كام مقطع وليه."
                )
            elif plan.adapted:
                st.info(
                    f"ℹ️ الفيديو قصير بالنسبة لعدد المقاطع المطلوب ({num_clips})، "
                    f"فطول المقطع هيتظبط تلقائيًا على "
                    f"{plan.min_duration:.0f}-{plan.max_duration:.0f} ثانية بدل "
                    f"{min_duration}-{max_duration} ثانية — كده ينفع يطلع "
                    f"{num_clips} مقطع من غير تداخل. (مش بنزوّد عن الأطوال اللي "
                    f"اخترتها، بنقلّل بس.)"
                )

    analyze_clicked = st.button(
        "🔍 حلّل الفيديو", type="primary", disabled=uploaded_file is None
    )

    if analyze_clicked and uploaded_file is not None and video_path is not None:
        analyze_config = Config(
            video_path=str(video_path),
            output_dir=str(Path(st.session_state.work_dir) / "output"),
            whisper_model=model_size,
            language=None if language.startswith("auto") else language,
            device=device,
            min_duration=float(min_duration),
            max_duration=float(max_duration),
            num_clips=num_clips,
            ollama_model=ollama_model,
            ollama_host=ollama_host,
            split_evenly=split_evenly,
            resume=resume_enabled,
        )

        st.subheader("التحليل")
        progress_bar = st.progress(0.0)
        status_placeholder = st.empty()
        with st.expander("سجل التفاصيل", expanded=False):
            log_placeholder = st.empty()

        logger = StreamlitLogger(5, progress_bar, status_placeholder, log_placeholder)

        analyze_stats: dict = {}
        try:
            with st.spinner("جارٍ التحليل... قد يستغرق هذا وقتًا حسب مدة الفيديو"):
                final_clips, segments, _ = analyze_pipeline(
                    analyze_config, logger, stats_out=analyze_stats
                )
            st.session_state.analysis = {
                "final_clips": final_clips,
                "segments": segments,
                "video_path": str(video_path),
                "output_dir": analyze_config.output_dir,
                "stats": analyze_stats,
            }
            if len(final_clips) < num_clips:
                # Say plainly why fewer clips came out, with counts.
                reasons = describe_shortfall(analyze_stats)
                body = "\n".join(_as_bullet(line) for line in reasons) if reasons else (
                    f"- تم إنتاج {len(final_clips)} مقطع من أصل {num_clips} المطلوبة."
                )
                st.warning(f"⚠️ مش كل المقاطع المطلوبة طلعت:\n\n{body}")
            else:
                st.success(f"تم التحليل! {len(final_clips)} مقطع جاهز للمراجعة تحت.")
        except PipelineError as e:
            st.error(e.message)
            if e.hint:
                st.info(e.hint)
        except Exception as e:  # noqa: BLE001
            st.error(f"خطأ غير متوقع: {e}")
            with st.expander("تفاصيل تقنية"):
                st.code(traceback.format_exc())

    analysis = st.session_state.get("analysis")
    if analysis and uploaded_file is not None and analysis["video_path"] == str(video_path):
        st.subheader("راجع المقاطع قبل التصدير")
        st.caption("تقدر تعدّل وقت بداية/نهاية أي مقطع قبل التصدير النهائي.")

        max_time = float(video_duration_seconds or 0.0)
        edited_clips: list[FinalClip] = []
        for clip in analysis["final_clips"]:
            with st.container(border=True):
                st.markdown(f"**{clip.index}. {clip.title or clip.text[:50]}**  ·  الدرجة: {clip.score:.1f}")
                st.caption(clip.text[:200])
                col1, col2 = st.columns(2)
                with col1:
                    new_start = st.number_input(
                        f"بداية المقطع {clip.index} (ث)",
                        min_value=0.0,
                        max_value=max(max_time, clip.end),
                        value=float(clip.start),
                        step=0.5,
                        key=f"start_{clip.index}",
                    )
                with col2:
                    new_end = st.number_input(
                        f"نهاية المقطع {clip.index} (ث)",
                        min_value=0.0,
                        max_value=max(max_time, clip.end),
                        value=float(clip.end),
                        step=0.5,
                        key=f"end_{clip.index}",
                    )
                edited_clips.append(
                    FinalClip(
                        index=clip.index,
                        start=min(new_start, new_end),
                        end=max(new_start, new_end),
                        score=clip.score,
                        reason=clip.reason,
                        text=clip.text,
                        title=clip.title,
                    )
                )

        export_clicked = st.button("🚀 صدّر المقاطع النهائية", type="primary")

        if export_clicked:
            render_config = Config(
                video_path=analysis["video_path"],
                output_dir=analysis["output_dir"],
                use_smart_reframe=use_smart_reframe,
                burn_captions=burn_captions,
                add_title_overlay=add_title_overlay,
                normalize_audio=normalize_audio,
                fix_lighting=fix_lighting,
                lighting_strength=float(lighting_strength),
                resume=resume_enabled,
            )

            st.subheader("التصدير")
            export_status = st.empty()
            with st.expander("سجل تفاصيل التصدير", expanded=False):
                export_log_placeholder = st.empty()

            export_logger = StreamlitLogger(
                1, st.progress(0.0), export_status, export_log_placeholder
            )

            try:
                with st.spinner("جارٍ التصدير..."):
                    rendered = clip_processor.render_clips(
                        edited_clips,
                        analysis["video_path"],
                        render_config,
                        export_logger,
                        analysis["segments"],
                    )
                st.success(f"تم تصدير {len(rendered)} مقطع!")

                for clip in rendered:
                    st.markdown(
                        f"**المقطع {clip.index}** — {clip.duration:.1f}ث "
                        f"— الدرجة: {clip.score:.1f}"
                    )
                    st.caption(clip.text[:200])
                    if clip.output_path and Path(clip.output_path).exists():
                        st.video(clip.output_path)
                        with open(clip.output_path, "rb") as f:
                            st.download_button(
                                f"⬇️ تنزيل المقطع {clip.index}",
                                data=f.read(),
                                file_name=f"clip_{clip.index:02d}.mp4",
                                mime="video/mp4",
                                key=f"dl_{clip.index}",
                            )

                # One-click bulk download: every rendered clip in a ZIP.
                # Built once per export (signature = count + total size)
                # and cached in session_state so reruns don't rebuild it.
                _zip_sig = (
                    f"{len(rendered)}:"
                    + str(
                        sum(
                            Path(c.output_path).stat().st_size
                            for c in rendered
                            if getattr(c, "output_path", None)
                            and Path(c.output_path).exists()
                        )
                    )
                )
                if st.session_state.get("zip_sig") != _zip_sig:
                    try:
                        _zip_path = build_clips_zip(
                            rendered, Path(str(analysis["output_dir"])) / "clips"
                        )
                    except Exception as _ze:  # noqa: BLE001
                        _zip_path = None
                        st.warning(f"تعذر تجهيز ملف ZIP: {_ze}")
                    st.session_state["zip_sig"] = _zip_sig
                    st.session_state["zip_bytes"] = (
                        _zip_path.read_bytes() if _zip_path else None
                    )
                    st.session_state["zip_name"] = (
                        f"{Path(str(analysis['video_path'])).stem}_clips.zip"
                    )
                if st.session_state.get("zip_bytes"):
                    _zb = st.session_state["zip_bytes"]
                    st.download_button(
                        (
                            "⬇️ تحميل كل المقاطع في ملف واحد "
                            f"({len(rendered)} مقطع — {len(_zb) / (1024 * 1024):.0f} MB)"
                        ),
                        data=_zb,
                        file_name=st.session_state.get("zip_name", "all_clips.zip"),
                        mime="application/zip",
                        key="dl_all_clips",
                        type="primary",
                    )
            except PipelineError as e:
                st.error(e.message)
                if e.hint:
                    st.info(e.hint)
            except Exception as e:  # noqa: BLE001
                st.error(f"خطأ غير متوقع: {e}")
                with st.expander("تفاصيل تقنية"):
                    st.code(traceback.format_exc())

    # Independent section, outside the video → clips flow above so neither
    # one can affect the other. The Whisper settings come from the sidebar.
    render_text_extraction_section(
        model_size=model_size, language=language, device=device
    )
    render_audio_split_section()


def cleanup_on_exit() -> None:
    work_dir = st.session_state.get("work_dir")
    if work_dir and Path(work_dir).exists():
        shutil.rmtree(work_dir, ignore_errors=True)


if __name__ == "__main__":
    main()
