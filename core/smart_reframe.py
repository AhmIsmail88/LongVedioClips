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

from utils.ffmpeg import extract_audio, LOUDNORM_FILTER, combine_filters, get_ffmpeg_binaries, trim_clip
from utils.logger import Logger, PipelineError

# How often (in frames) to re-run face detection. Every frame is wasteful
# since faces don't move fast enough to need it - re-using the last known
# box for the skipped frames looks identical and is much faster.
DETECT_EVERY_N_FRAMES = 2

# Progress is logged every N processed frames rather than every frame, to
# avoid flooding the log with noise.
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
        self.target_center_y = video_height / 2

    def update_target(self, box: tuple[int, int, int, int]) -> None:
        x, y, w, h = box
        self.target_center_x = x + w / 2
        self.target_center_y = y + h / 2

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
        y1 = int(max(0, min(self.video_height - self.crop_height,
                            self.target_center_y - self.crop_height / 2)))
        return x1, y1, x2, y1 + self.crop_height


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
        self.known_faces = [face for face in self.known_faces
                            if frame_number - face["last_frame"] <= self.forget_after_frames]
        current_candidates = []
        used_ids = set()
        for face in face_candidates:
            x, y, w, h = face["box"]
            center_x = x + w / 2
            best_match_id = -1
            min_dist = width * 0.15
            for kf in self.known_faces:
                if kf["id"] in used_ids:
                    continue
                if frame_number - kf["last_frame"] > self.forget_after_frames:
                    continue
                dist = abs(center_x - kf["center"])
                if dist < min_dist:
                    min_dist = dist
                    best_match_id = kf["id"]
            if best_match_id == -1:
                best_match_id = self.next_id
                self.next_id += 1
            used_ids.add(best_match_id)
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


def _detect_people_yolo(frame, yolo_model):
    """Confident full person boxes, including seated/profile subjects."""
    height, width = frame.shape[:2]
    people = []
    for result in yolo_model(frame, verbose=False, classes=[0], conf=0.55):
        for box in result.boxes:
            confidence = float(box.conf[0])
            x1, y1, x2, y2 = [int(value) for value in box.xyxy[0]]
            x1, y1 = max(0, x1), max(0, y1)
            x2, y2 = min(width, x2), min(height, y2)
            if confidence >= 0.55 and x2 > x1 and y2-y1 >= height * 0.15:
                people.append({"box": (x1, y1, x2-x1, y2-y1), "confidence": confidence})
    return people


def _choose_person(people, focus_mode="largest"):
    if len(people) == 1:
        return people[0]["box"]
    if len(people) >= 2 and focus_mode in ("left", "right"):
        ordered = sorted(people, key=lambda p: p["box"][0] + p["box"][2]/2)
        return ordered[0 if focus_mode == "left" else -1]["box"]
    # Size alone does not establish who is speaking in a group.
    return None


def _detect_person_yolo(frame, yolo_model):
    return _choose_person(_detect_people_yolo(frame, yolo_model))


def _verified_faces(candidates, people):
    if people is None:
        return candidates
    return [face for face in candidates if any(
        x - w*.05 <= face["box"][0] + face["box"][2]/2 <= x + w*1.05
        and y - h*.05 <= face["box"][1] + face["box"][3]/2 <= y + h
        for person in people for x,y,w,h in [person["box"]])]


def _crop_contains(box, target):
    x1,y1,x2,y2 = box
    x,y,w,h = target
    return x1 <= x and x+w <= x2 and y1 <= y and y+h <= y2


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

    scale = min(output_width / orig_w, output_height / orig_h)
    fg_w = max(1, min(output_width, int(orig_w * scale)))
    fg_h = max(1, min(output_height, int(orig_h * scale)))
    foreground = cv2.resize(frame, (fg_w, fg_h))

    y_offset = (output_height - fg_h) // 2
    final_frame = background.copy()
    if 0 <= y_offset and y_offset + fg_h <= output_height:
        x_offset = (output_width - fg_w) // 2
        final_frame[y_offset : y_offset + fg_h, x_offset:x_offset + fg_w] = foreground
    return final_frame


def _create_stacked_frame(frame, boxes, output_width, output_height, top_share=0.6):
    """Two independent face-centred crops; left source person stays on top."""
    import cv2

    boxes = sorted(boxes, key=lambda box: box[0] + box[2] / 2)
    panels = []
    top_height = int(output_height * max(0.3, min(0.7, top_share)))
    for box, panel_height in zip(boxes[:2], (top_height, output_height - top_height), strict=False):
        x, y, w, h = box
        source_h, source_w = frame.shape[:2]
        crop_h = min(source_h, max(int(h * 3), 1))
        crop_w = max(1, int(crop_h * output_width / panel_height))
        if crop_w > source_w:
            crop_w = source_w
            crop_h = max(1, int(crop_w * panel_height / output_width))
        left = int(max(0, min(source_w - crop_w, x + w / 2 - crop_w / 2)))
        top = int(max(0, min(source_h - crop_h, y + h / 2 - crop_h * 0.35)))
        panels.append(cv2.resize(frame[top:top + crop_h, left:left + crop_w],
                                 (output_width, panel_height)))
    return cv2.vconcat(panels)


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
    layout: str = "single",
    podcast_top_share: float = 0.6,
    focus_mode: str = "largest",
    manual_crop: dict | None = None,
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

        from core.speaker_focus import AudioActivity, MouthFocus, smooth_boxes
        activity = None
        if focus_mode == "auto":
            audio_path = os.path.join(tmp_dir, "focus.wav")
            try:
                extract_audio(trimmed_path, audio_path)
                activity = AudioActivity(audio_path)
                logger.info("Experimental speaker focus uses mouth motion during audible sound.")
            except Exception as exc:
                logger.warn(f"Speaker audio analysis unavailable ({exc}); uncertain focus stays wide.")
        mouth_focus = MouthFocus()
        active_side = None
        panel_share = (0.5 if focus_mode == "auto" else
                       1.0 - podcast_top_share if focus_mode == "right" else podcast_top_share)
        face_detector = _load_face_detector()
        yolo_model = None
        if YOLO is not None:
            try:
                from pathlib import Path
                weights = Path(__file__).resolve().parents[1] / "yolov8n.pt"
                if not weights.is_file():
                    raise FileNotFoundError("Local yolov8n.pt weights missing")
                yolo_model = YOLO(str(weights))
            except Exception as exc:  # noqa: BLE001
                logger.warn(
                    f"Smart reframe: could not load YOLO weights ({exc}); "
                    "continuing with face tracking only."
                )

        # Establish one framing per shot before encoding, including frame zero.
        # Detector box changes must not move a stationary lecture camera.
        import statistics
        locked_crops = []
        probe = cv2.VideoCapture(trimmed_path)
        for scene_start, scene_end in (scenes if layout == "single" else []):
            centers = []
            for offset in (0, .2, .4, .6, .8):
                index = min(scene_end.frame_num - 1, scene_start.frame_num + round(offset * fps))
                probe.set(cv2.CAP_PROP_POS_FRAMES, index)
                ok, sample = probe.read()
                if not ok:
                    continue
                sample_people = _detect_people_yolo(sample, yolo_model) if yolo_model is not None else None
                faces = _verified_faces(_detect_face_candidates(sample, face_detector), sample_people)
                target = _choose_person(sample_people, focus_mode) if sample_people else None
                if target is None and len(faces) == 1:
                    target = faces[0]["box"]
                if target:
                    x, y, w, h = target
                    centers.append((x + w / 2, y + h / 2))
            if centers:
                cx = statistics.median(c[0] for c in centers)
                cy = statistics.median(c[1] for c in centers)
                left = round(max(0, min(video_width-crop_width, cx-crop_width/2)))
                top = round(max(0, min(video_height-crop_height, cy-crop_height/2)))
                locked_crops.append((left, top, left+crop_width, top+crop_height))
            else:
                locked_crops.append(None)
        probe.release()
        if manual_crop:
            center=max(0,min(1,float(manual_crop.get("center",.5))))
            zoom=max(1,min(3,float(manual_crop.get("zoom",1))))
            mw=max(2,round(crop_width/zoom));mh=max(2,round(crop_height/zoom))
            left=round(max(0,min(video_width-mw,center*video_width-mw/2)))
            top=round(max(0,min(video_height-mh,video_height/2-mh/2)))
            locked_crops=[(left,top,left+mw,top+mh) for _ in scenes]

        cameraman = SmoothedCameraman(crop_width, crop_height, video_width, video_height)
        speaker_tracker = SpeakerTracker(switch_cooldown=max(1, round(fps)),
                                         forget_after_frames=max(1, round(fps)))
        scene_boundaries = [(s.frame_num, e.frame_num) for s, e in scenes]

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
        if os.environ.get("LVC_RENDER_THREADS", "0") != "0":
            ffmpeg_cmd += ["-threads", os.environ["LVC_RENDER_THREADS"]]
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
        previous_focus_side = None
        last_detection_frame = -10000
        stacked_boxes = []
        last_pair_frame = -10000
        lost_timeout = max(1, int(fps * 0.35))
        people = None
        last_person_frame = -10000
        general_frames = tracked_frames = person_frames = 0

        try:
            while cap.isOpened():
                ret, frame = cap.read()
                if not ret:
                    break

                if scene_idx < len(scene_boundaries):
                    _, end_f = scene_boundaries[scene_idx]
                    if frame_number >= end_f and scene_idx < len(scene_boundaries) - 1:
                        scene_idx += 1
                        last_face_box = None
                        people = None
                        last_person_frame = -10000
                        previous_focus_side = None
                        stacked_boxes = []
                        last_pair_frame = last_detection_frame = -10000
                        speaker_tracker = SpeakerTracker(switch_cooldown=max(1, round(fps)),
                                                         forget_after_frames=max(1, round(fps)))
                        mouth_focus = MouthFocus()
                        active_side = None
                        panel_share = (0.5 if focus_mode == "auto" else
                                       1.0 - podcast_top_share if focus_mode == "right" else podcast_top_share)

                if yolo_model is not None and (frame_number - last_person_frame >= 6):
                    try:
                        people = _detect_people_yolo(frame, yolo_model)
                    except Exception:
                        people = None
                        logger.warn("YOLO detection failed; continuing with safe face/wide framing.")
                        yolo_model = None
                    last_person_frame = frame_number
                speaker_switched = False

                if layout == "stacked":
                    if frame_number % DETECT_EVERY_N_FRAMES == 0:
                        detected = _verified_faces(_detect_face_candidates(frame, face_detector), people)
                        pair = sorted(detected, key=lambda c: c["score"], reverse=True)[:2]
                        true_faces = len(pair) == 2
                        if not true_faces and people is not None and len(people) == 2:
                            pair = [{"box": (x,y,w,int(h*.45)), "score": w*h}
                                    for person in people for x,y,w,h in [person["box"]]]
                        if len(pair) == 2:
                            raw_boxes = [c["box"] for c in pair]
                            stacked_boxes = smooth_boxes(stacked_boxes, raw_boxes)
                            if focus_mode == "auto" and true_faces:
                                active_side = mouth_focus.update(
                                    frame, raw_boxes, frame_number / fps,
                                    activity.rms_at(frame_number / fps) if activity else 0.0)
                            if focus_mode == "auto" and not true_faces:
                                active_side = None
                            last_pair_frame = frame_number
                        elif focus_mode == "auto":
                            active_side = mouth_focus.update(
                                frame, [c["box"] for c in pair], frame_number / fps, 0.0)
                    if len(stacked_boxes) == 2 and frame_number - last_pair_frame <= lost_timeout:
                        desired_share = podcast_top_share
                        if focus_mode == "right" or (focus_mode == "auto" and active_side == "right"):
                            desired_share = 1.0 - podcast_top_share
                        elif focus_mode == "auto" and active_side is None:
                            desired_share = 0.5
                        panel_share += (desired_share - panel_share) * 0.12
                        output_frame = _create_stacked_frame(
                            frame, stacked_boxes, width, height, panel_share)
                    else:
                        output_frame = _create_general_frame(frame, width, height)
                else:
                    if frame_number % DETECT_EVERY_N_FRAMES == 0:
                        candidates = _verified_faces(_detect_face_candidates(frame, face_detector), people)
                        target_box = speaker_tracker.get_target(candidates, frame_number, video_width)
                        if len(candidates) >= 2 and focus_mode == "largest":
                            target_box = None
                            last_face_box = None
                            last_detection_frame = -10000
                        if len(candidates) >= 2 and focus_mode in ("left", "right", "auto"):
                            pair = sorted(candidates, key=lambda c: c["score"], reverse=True)[:2]
                            pair.sort(key=lambda c: c["box"][0] + c["box"][2] / 2)
                            side = focus_mode
                            if focus_mode == "auto":
                                side = mouth_focus.update(
                                    frame, [c["box"] for c in pair], frame_number / fps,
                                    activity.rms_at(frame_number / fps) if activity else 0.0)
                            target_box = pair[0 if side == "left" else 1]["box"] if side else None
                            if side and side != previous_focus_side:
                                speaker_switched = True
                            previous_focus_side = side
                            if side is None:
                                last_face_box = None
                                last_detection_frame = -10000
                        if focus_mode == "auto" and len(candidates) != 2:
                            mouth_focus.update(frame, [c["box"] for c in candidates], frame_number / fps, 0.0)
                        if target_box:
                            speaker_switched = speaker_switched or last_face_box is None
                            last_detection_frame = frame_number
                            last_face_box = target_box
                            cameraman.update_target(target_box)
                        elif people and not candidates:
                            person_box = _choose_person(people, focus_mode)
                            if person_box:
                                speaker_switched = speaker_switched or last_face_box is None
                                last_detection_frame = frame_number
                                last_face_box = person_box
                                cameraman.update_target(person_box)
                                person_frames += 1
                    elif last_face_box and frame_number - last_detection_frame <= lost_timeout:
                        cameraman.update_target(last_face_box)

                    # A fixed shot uses a fixed crop, never detector-driven pans.
                    # Unknown/group shots keep the complete image for the shot.
                    locked = locked_crops[scene_idx] if scene_idx < len(locked_crops) else None
                    if locked is None:
                        output_frame = _create_general_frame(frame, width, height)
                        general_frames += 1
                    else:
                        x1, y1, x2, y2 = locked
                        cropped = frame[y1:y2, x1:x2]
                        output_frame = cv2.resize(cropped, (width, height))
                        tracked_frames += 1

                ffmpeg_proc.stdin.write(output_frame.tobytes())
                frame_number += 1
                if frame_number % LOG_EVERY_N_FRAMES == 0:
                    logger.info(f"Smart reframe: {frame_number}/{total_frames} frames")
        finally:
            cap.release()
            ffmpeg_proc.stdin.close()
            stderr = ffmpeg_proc.stderr.read().decode(errors="ignore")
            ffmpeg_proc.stderr.close()
            ffmpeg_proc.wait()

        if ffmpeg_proc.returncode != 0:
            raise PipelineError(
                "Smart reframe video encoding failed.",
                hint=stderr.strip()[-800:] if stderr else None,
            )

        logger.info(f"Framing verification: {tracked_frames} tracked frames, "
                    f"{general_frames} safe-wide frames, {person_frames} YOLO acquisitions.")
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
