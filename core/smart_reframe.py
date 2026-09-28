"""
core/smart_reframe.py

Optional "Smart Auto-Reframe" rendering path: instead of a fixed center
crop, this tracks faces/speakers across each clip and pans the vertical
crop window to follow them, falling back to a blurred-background "general
shot" for group scenes or when nobody is detected.

This is adapted from the face-tracking/speaker-tracking approach used by
the MIT-licensed mutonby/openshorts project (itself forked from
kamilstanuch/Autocrop-vertical): https://github.com/mutonby/openshorts
Reimplemented here against our own Config/Logger/PipelineError
conventions and scoped to a single trimmed clip rather than a whole
video. Original project MIT-licensed; attribution preserved per license
terms.

This path is opt-in (`config.use_smart_reframe = True`) because it needs
extra dependencies (opencv-python and scenedetect are required;
ultralytics is optional - it only powers the person-detection fallback
used when no face is visible, so face tracking still works without it)
and it is
is noticeably slower than the default single-pass ffmpeg center crop in
utils/ffmpeg.render_vertical_clip, since it walks the clip frame-by-frame
in Python instead of a single ffmpeg filter.

Face detection uses OpenCV's bundled Haar Cascade rather than mediapipe's
face detector (which the upstream project uses): mediapipe 0.10+ dropped
the old `solutions` API this relied on in favor of a Tasks API that needs
a model file downloaded from Google Cloud Storage at runtime. Haar
Cascade is slightly less accurate on hard angles/lighting, but ships
inside opencv-python with nothing extra to download or track.
"""

from __future__ import annotations

import os
import subprocess
import tempfile

from utils.ffmpeg import LOUDNORM_FILTER, combine_filters, get_ffmpeg_binaries, trim_clip
from utils.logger import Logger, PipelineError

# How often (in frames) to re-run face detection. Every frame is wasteful
# since faces don't move fast enough to need it - re-using the last known
# box for the skipped frames looks identical and is much faster.
DETECT_EVERY_N_FRAMES = 2

# Progress is logged every N processed frames rather than every frame, to
# avoid flooding the log (and the Streamlit UI) with noise.
LOG_EVERY_N_FRAMES = 150


def _check_dependencies() -> None:
    missing = []
    try:
        import cv2  # noqa: F401
    except ImportError:
        missing.append("opencv-python")
    try:
        import scenedetect  # noqa: F401
    except ImportError:
        missing.append("scenedetect")

    # ultralytics/YOLO is deliberately NOT required: it is only a
    # fallback for frames with no visible face, and on some machines
    # (e.g. Windows Application Control policies blocking torch's
    # unsigned DLLs) it cannot load at all. The feature degrades to
    # face tracking instead of failing.
    if missing:
        raise PipelineError(
            "Smart reframe requires extra dependencies that aren't installed: "
            + ", ".join(missing),
            hint="Install them with: pip install -r requirements-smart-reframe.txt",
        )


def _load_face_detector():
    """Loads OpenCV's bundled Haar Cascade face detector.

    Note: earlier versions of this module used mediapipe's face detector,
    matching the upstream project this was adapted from. mediapipe 0.10+
    removed that legacy `solutions` API in favor of a new Tasks API that
    needs a model file downloaded at runtime from Google Cloud Storage.
    Haar Cascade is a bit less accurate on hard angles/lighting, but it
    ships inside opencv-python with no extra download or API to track,
    which is a better fit for a small, self-contained local tool.
    """
    import cv2

    cascade_path = cv2.data.haarcascades + "haarcascade_frontalface_default.xml"
    detector = cv2.CascadeClassifier(cascade_path)
    if detector.empty():
        raise PipelineError(
            "Could not load OpenCV's bundled face detector.",
            hint="Try reinstalling opencv-python: pip install --force-reinstall opencv-python",
        )
    return detector


class SmoothedCameraman:
    """Pans a vertical crop window smoothly to follow a moving target,
    instead of snapping frame-to-frame (which looks jittery).

    Stays still while the target is inside a "safe zone" near the crop
    center; only pans once the target drifts outside it, and pans faster
    for large jumps (e.g. a scene cut) than for small ones.
    """

    def __init__(self, crop_width: int, crop_height: int, video_width: int, video_height: int):
        self.crop_width = crop_width
        self.crop_height = crop_height
        self.video_width = video_width
        self.video_height = video_height
        self.current_center_x = video_width / 2
        self.target_center_x = video_width / 2
        self.safe_zone_radius = crop_width * 0.25

    def update_target(self, box: tuple[int, int, int, int]) -> None:
        x, y, w, h = box
        self.target_center_x = x + w / 2

    def get_crop_box(self, force_snap: bool = False) -> tuple[int, int, int, int]:
        if force_snap:
            self.current_center_x = self.target_center_x
        else:
            diff = self.target_center_x - self.current_center_x
            if abs(diff) > self.safe_zone_radius:
                direction = 1 if diff > 0 else -1
                speed = 15.0 if abs(diff) > self.crop_width * 0.5 else 3.0
                self.current_center_x += direction * speed
                new_diff = self.target_center_x - self.current_center_x
                if (direction == 1 and new_diff < 0) or (direction == -1 and new_diff > 0):
                    self.current_center_x = self.target_center_x

        half_crop = self.crop_width / 2
        self.current_center_x = max(
            half_crop, min(self.current_center_x, self.video_width - half_crop)
        )

        x1 = int(self.current_center_x - half_crop)
        x2 = int(self.current_center_x + half_crop)
        x1 = max(0, x1)
        x2 = min(self.video_width, x2)
        return x1, 0, x2, self.video_height


class SpeakerTracker:
    """Tracks which detected face is "the active speaker" across frames,
    with hysteresis so it doesn't flicker between people when several
    faces are briefly visible at once.
    """

    def __init__(self, switch_cooldown: int = 30, forget_after_frames: int = 30):
        self.active_speaker_id: int | None = None
        self.speaker_scores: dict[int, float] = {}
        self.last_switch_frame = -10_000
        self.switch_cooldown = switch_cooldown
        self.forget_after_frames = forget_after_frames
        self.next_id = 0
        self.known_faces: list[dict] = []

    def get_target(self, face_candidates: list[dict], frame_number: int, width: int):
        current_candidates = []
        for face in face_candidates:
            x, y, w, h = face["box"]
            center_x = x + w / 2
            best_match_id = -1
            min_dist = width * 0.15
            for kf in self.known_faces:
                if frame_number - kf["last_frame"] > self.forget_after_frames:
                    continue
                dist = abs(center_x - kf["center"])
                if dist < min_dist:
                    min_dist = dist
                    best_match_id = kf["id"]
            if best_match_id == -1:
                best_match_id = self.next_id
                self.next_id += 1
            self.known_faces = [kf for kf in self.known_faces if kf["id"] != best_match_id]
            self.known_faces.append(
                {"id": best_match_id, "center": center_x, "last_frame": frame_number}
            )
            current_candidates.append(
                {"id": best_match_id, "box": face["box"], "score": face["score"]}
            )

        for pid in list(self.speaker_scores.keys()):
            self.speaker_scores[pid] *= 0.85
            if self.speaker_scores[pid] < 0.1:
                del self.speaker_scores[pid]

        for cand in current_candidates:
            pid = cand["id"]
            raw_score = cand["score"] / (width * width * 0.05)
            self.speaker_scores[pid] = self.speaker_scores.get(pid, 0) + raw_score

        if not current_candidates:
            return None

        best_candidate = None
        max_score = -1.0
        for cand in current_candidates:
            pid = cand["id"]
            total_score = self.speaker_scores.get(pid, 0)
            if pid == self.active_speaker_id:
                total_score *= 3.0  # Sticky bonus: prefer staying on current speaker.
            if total_score > max_score:
                max_score = total_score
                best_candidate = cand

        if best_candidate is None:
            return None

        target_id = best_candidate["id"]
        if target_id == self.active_speaker_id:
            return best_candidate["box"]

        if frame_number - self.last_switch_frame < self.switch_cooldown:
            old_cand = next(
                (c for c in current_candidates if c["id"] == self.active_speaker_id), None
            )
            if old_cand:
                return old_cand["box"]

        self.active_speaker_id = target_id
        self.last_switch_frame = frame_number
        return best_candidate["box"]


def _detect_face_candidates(frame, face_detector) -> list[dict]:
    import cv2

    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    faces = face_detector.detectMultiScale(
        gray, scaleFactor=1.1, minNeighbors=5, minSize=(40, 40)
    )
    return [{"box": [int(x), int(y), int(w), int(h)], "score": int(w * h)} for x, y, w, h in faces]


def _detect_person_yolo(frame, yolo_model) -> tuple[int, int, int, int] | None:
    """Fallback when no face is detected: locate the largest person and
    approximate their head/chest region from the top of their bounding box.
    """
    results = yolo_model(frame, verbose=False, classes=[0])  # class 0 = person
    best_box = None
    max_area = 0
    for result in results:
        for box in result.boxes:
            x1, y1, x2, y2 = [int(i) for i in box.xyxy[0]]
            w, h = x2 - x1, y2 - y1
            area = w * h
            if area > max_area:
                max_area = area
                face_h = int(h * 0.4)
                best_box = (x1, y1, w, face_h)
    return best_box


def _create_general_frame(frame, output_width: int, output_height: int):
    """Group/wide-shot fallback: blurred, zoomed background filling the
    frame, with the original shot fit to width and centered on top -
    avoids an awkward crop when there's no single subject to track.
    """
    import cv2

    orig_h, orig_w = frame.shape[:2]

    bg_scale = output_height / orig_h
    bg_w = int(orig_w * bg_scale)
    bg_resized = cv2.resize(frame, (bg_w, output_height))
    start_x = max(0, (bg_w - output_width) // 2)
    background = bg_resized[:, start_x : start_x + output_width]
    if background.shape[1] != output_width:
        background = cv2.resize(background, (output_width, output_height))
    background = cv2.GaussianBlur(background, (51, 51), 0)

    scale = output_width / orig_w
    fg_h = int(orig_h * scale)
    foreground = cv2.resize(frame, (output_width, fg_h))

    y_offset = (output_height - fg_h) // 2
    final_frame = background.copy()
    if 0 <= y_offset and y_offset + fg_h <= output_height:
        final_frame[y_offset : y_offset + fg_h, :] = foreground
    return final_frame


def _analyze_scene_strategies(video_path: str, scenes, face_detector, logger: Logger) -> list[str]:
    """Classify each detected scene as TRACK (roughly one person - follow
    them) or GENERAL (nobody, or a group - use the blurred wide shot)."""
    import cv2

    cap = cv2.VideoCapture(video_path)
    strategies = []
    if not cap.isOpened():
        return ["TRACK"] * len(scenes)

    for start, end in scenes:
        sample_frames = [
            start.get_frames() + 5,
            int((start.get_frames() + end.get_frames()) / 2),
            max(start.get_frames() + 5, end.get_frames() - 5),
        ]
        face_counts = []
        for f_idx in sample_frames:
            cap.set(cv2.CAP_PROP_POS_FRAMES, f_idx)
            ret, frame = cap.read()
            if not ret:
                continue
            face_counts.append(len(_detect_face_candidates(frame, face_detector)))

        avg_faces = sum(face_counts) / len(face_counts) if face_counts else 0
        strategies.append("GENERAL" if (avg_faces > 1.2 or avg_faces < 0.5) else "TRACK")

    cap.release()
    return strategies


def _detect_scenes(video_path: str):
    from scenedetect import SceneManager, open_video
    from scenedetect.detectors import ContentDetector

    video = open_video(video_path)
    scene_manager = SceneManager()
    scene_manager.add_detector(ContentDetector())
    scene_manager.detect_scenes(video=video)
    scenes = scene_manager.get_scene_list()
    return scenes, video.frame_rate


def render_vertical_clip_smart(
    video_path: str,
    output_path: str,
    start: float,
    end: float,
    width: int,
    height: int,
    audio_codec: str,
    logger: Logger,
    normalize_audio: bool = False,
    extra_video_filter: str | None = None,
) -> None:
    """Face/speaker-tracking alternative to utils.ffmpeg.render_vertical_clip.

    Cuts [start, end], then instead of a fixed center crop, pans the
    vertical crop window to follow whoever is speaking (or falls back to
    a blurred wide shot for group scenes), before muxing the original
    audio back in.

    `extra_video_filter` (e.g. the lighting fix) is applied by the encoder
    that receives the cropped frames, so both render paths apply the same
    correction at the same point in the pipeline.
    """
    _check_dependencies()

    import cv2
    from scenedetect import FrameTimecode

    # Optional: see _check_dependencies. A load failure (missing
    # package, or an OS policy blocking torch's DLLs) only disables
    # the person fallback.
    YOLO = None
    try:
        from ultralytics import YOLO  # type: ignore[no-redef]
    except Exception as exc:  # noqa: BLE001 - includes OSError from blocked DLLs
        logger.warn(
            f"Smart reframe: person-detection fallback unavailable ({exc}); "
            "continuing with face tracking only."
        )

    aspect_ratio = width / height
    ffmpeg_bin, _ = get_ffmpeg_binaries()

    with tempfile.TemporaryDirectory() as tmp_dir:
        trimmed_path = os.path.join(tmp_dir, "trimmed.mp4")
        raw_video_path = os.path.join(tmp_dir, "raw_video.mp4")
        raw_audio_path = os.path.join(tmp_dir, "raw_audio.aac")

        logger.info("Smart reframe: trimming source clip...")
        trim_clip(video_path, trimmed_path, start, end)

        logger.info("Smart reframe: detecting scenes...")
        scenes, fps = _detect_scenes(trimmed_path)
        if not scenes:
            cap = cv2.VideoCapture(trimmed_path)
            total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
            cap.release()
            scenes = [(FrameTimecode(0, fps), FrameTimecode(total_frames, fps))]
        logger.info(f"Smart reframe: found {len(scenes)} scene(s)")

        cap = cv2.VideoCapture(trimmed_path)
        if not cap.isOpened():
            raise PipelineError(f"Could not open trimmed clip '{trimmed_path}' for reading.")
        video_width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        video_height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        cap.release()

        crop_height = video_height
        crop_width = int(crop_height * aspect_ratio)
        if crop_width > video_width:
            crop_width = video_width
            crop_height = int(crop_width / aspect_ratio)

        face_detector = _load_face_detector()
        yolo_model = None
        if YOLO is not None:
            try:
                yolo_model = YOLO("yolov8n.pt")
            except Exception as exc:  # noqa: BLE001
                logger.warn(
                    f"Smart reframe: could not load YOLO weights ({exc}); "
                    "continuing with face tracking only."
                )

        logger.info("Smart reframe: classifying scenes (single speaker vs group)...")
        scene_strategies = _analyze_scene_strategies(trimmed_path, scenes, face_detector, logger)

        cameraman = SmoothedCameraman(crop_width, crop_height, video_width, video_height)
        speaker_tracker = SpeakerTracker()
        scene_boundaries = [(s.get_frames(), e.get_frames()) for s, e in scenes]

        ffmpeg_cmd = [
            ffmpeg_bin,
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "rawvideo",
            "-vcodec",
            "rawvideo",
            "-s",
            f"{width}x{height}",
            "-pix_fmt",
            "bgr24",
            "-r",
            str(fps),
            "-i",
            "-",
        ]
        vf = combine_filters(extra_video_filter)
        if vf:
            ffmpeg_cmd += ["-vf", vf]
        ffmpeg_cmd += [
            "-c:v",
            "libx264",
            "-preset",
            "fast",
            "-crf",
            "23",
            "-an",
            raw_video_path,
        ]
        ffmpeg_proc = subprocess.Popen(
            ffmpeg_cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE
        )

        cap = cv2.VideoCapture(trimmed_path)
        frame_number = 0
        scene_idx = 0
        last_face_box = None

        try:
            while cap.isOpened():
                ret, frame = cap.read()
                if not ret:
                    break

                if scene_idx < len(scene_boundaries):
                    _, end_f = scene_boundaries[scene_idx]
                    if frame_number >= end_f and scene_idx < len(scene_boundaries) - 1:
                        scene_idx += 1

                strategy = scene_strategies[scene_idx] if scene_idx < len(scene_strategies) else "TRACK"

                if strategy == "GENERAL":
                    output_frame = _create_general_frame(frame, width, height)
                    cameraman.current_center_x = video_width / 2
                    cameraman.target_center_x = video_width / 2
                else:
                    if frame_number % DETECT_EVERY_N_FRAMES == 0:
                        candidates = _detect_face_candidates(frame, face_detector)
                        target_box = speaker_tracker.get_target(candidates, frame_number, video_width)
                        if target_box:
                            last_face_box = target_box
                            cameraman.update_target(target_box)
                        elif yolo_model is not None:
                            person_box = _detect_person_yolo(frame, yolo_model)
                            if person_box:
                                last_face_box = person_box
                                cameraman.update_target(person_box)
                    elif last_face_box:
                        cameraman.update_target(last_face_box)

                    is_scene_start = (
                        scene_idx < len(scene_boundaries)
                        and frame_number == scene_boundaries[scene_idx][0]
                    )
                    x1, y1, x2, y2 = cameraman.get_crop_box(force_snap=is_scene_start)
                    if x2 > x1 and y2 > y1:
                        cropped = frame[y1:y2, x1:x2]
                        output_frame = cv2.resize(cropped, (width, height))
                    else:
                        output_frame = cv2.resize(frame, (width, height))

                ffmpeg_proc.stdin.write(output_frame.tobytes())
                frame_number += 1
                if frame_number % LOG_EVERY_N_FRAMES == 0:
                    logger.info(f"Smart reframe: {frame_number}/{total_frames} frames")
        finally:
            cap.release()
            ffmpeg_proc.stdin.close()
            stderr = ffmpeg_proc.stderr.read().decode(errors="ignore")
            ffmpeg_proc.wait()

        if ffmpeg_proc.returncode != 0:
            raise PipelineError(
                "Smart reframe video encoding failed.",
                hint=stderr.strip()[-800:] if stderr else None,
            )

        # Re-attach the original audio, extracted from the trimmed source.
        extract_result = subprocess.run(
            [
                ffmpeg_bin, "-y", "-hide_banner", "-loglevel", "error",
                "-i", trimmed_path, "-vn", "-acodec", "copy", raw_audio_path,
            ],
            capture_output=True,
            text=True,
        )
        has_audio = extract_result.returncode == 0 and os.path.exists(raw_audio_path)

        if has_audio:
            mux_cmd = [
                ffmpeg_bin, "-y", "-hide_banner", "-loglevel", "error",
                "-i", raw_video_path, "-i", raw_audio_path,
                "-c:v", "copy", "-c:a", audio_codec,
                "-movflags", "+faststart", output_path,
            ]
            if normalize_audio:
                mux_cmd[mux_cmd.index(output_path):mux_cmd.index(output_path)] = ["-af", LOUDNORM_FILTER]
        else:
            logger.warn("Smart reframe: no audio track found or extraction failed; video-only output.")
            mux_cmd = [
                ffmpeg_bin, "-y", "-hide_banner", "-loglevel", "error",
                "-i", raw_video_path, "-c:v", "copy",
                "-movflags", "+faststart", output_path,
            ]

        mux_result = subprocess.run(mux_cmd, capture_output=True, text=True)
        if mux_result.returncode != 0:
            raise PipelineError(
                f"Failed to merge audio/video for '{output_path}'.",
                hint=mux_result.stderr.strip()[-800:] if mux_result.stderr else None,
            )
