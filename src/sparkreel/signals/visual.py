"""Visual dynamics extractor.

Local (OpenCV):
  visual_motion      — inter-frame absolute difference (action / movement)
  visual_scene       — colour-histogram divergence (scene cuts / kill-cams / replays)
  visual_face        — face area fraction (close-ups / reaction shots), via the
                       YuNet DNN face detector when its model is present
  visual_expression  — micro-expression intensity: frame-to-frame change *within*
                       the detected face region (a smile breaking, eyes widening,
                       a laugh) — size-normalised so it captures subtle facial
                       motion, not gross camera movement. This is what makes the
                       highlight picker reach for reaction moments, not just
                       volume spikes. Falls back to a centre-region residual-motion
                       proxy when no face model is available.

AWS (Amazon Rekognition, backend: visual = aws):
  visual_face is replaced by DetectFaces emotion confidence (happy/surprised/…).
"""
from __future__ import annotations

from pathlib import Path
from typing import Dict, List, Optional

import numpy as np

from ..ingest import iter_frames
from ..log import get_logger
from ..models import SignalTrack
from . import dsp
from .base import AnalysisContext, make_track

_FACE_CROP = 48   # face is resized to this square before diffing (expression proxy)


def _yunet_model() -> Optional[str]:
    """Locate the bundled YuNet face-detection model, if present."""
    roots = [Path.cwd(), Path(__file__).resolve().parents[3]]
    for r in roots:
        p = r / "assets" / "models" / "face_detection_yunet.onnx"
        if p.exists() and p.stat().st_size > 50_000:
            return str(p)
    return None


def _load_detector(model: str):
    import cv2
    try:
        det = cv2.FaceDetectorYN.create(model, "", (320, 320), score_threshold=0.6)
        return det
    except Exception as e:
        get_logger(__name__).warning("[visual] YuNet 載入失敗(%s):%s → 改用中央殘差代理。",
                                     type(e).__name__, e)
        return None


# ── FER+ facial emotion (real model, run on YuNet face crops via cv2.dnn) ─────
# FER+ 8-class order (ONNX model zoo): neutral, happiness, surprise, sadness,
# anger, disgust, fear, contempt. Arousal weights turn the class distribution
# into one "emotional intensity" scalar — surprise/laughter/anger score highest,
# neutral lowest — which is exactly what a highlight/reaction moment looks like.
_FER_CLASSES = ["neutral", "happy", "surprise", "sad", "anger", "disgust", "fear", "contempt"]
_FER_AROUSAL = np.array([0.05, 0.95, 1.00, 0.35, 0.90, 0.55, 0.85, 0.45], dtype=np.float32)


def _fer_model() -> Optional[str]:
    """Locate the bundled FER+ emotion model, if present."""
    roots = [Path.cwd(), Path(__file__).resolve().parents[3]]
    for r in roots:
        p = r / "assets" / "models" / "emotion_ferplus.onnx"
        if p.exists() and p.stat().st_size > 1_000_000:
            return str(p)
    return None


def _load_fer(model: Optional[str]):
    if not model:
        return None
    import cv2
    try:
        # cv2 5.0's new graph engine logs a harmless per-forward WARN → quiet it
        try:
            cv2.utils.logging.setLogLevel(cv2.utils.logging.LOG_LEVEL_ERROR)
        except Exception:
            pass
        return cv2.dnn.readNetFromONNX(model)
    except Exception as e:
        get_logger(__name__).warning("[visual] FER+ 載入失敗(%s):%s → 情緒訊號改用微表情代理。",
                                     type(e).__name__, e)
        return None


def _fer_probs(net, face_gray) -> Optional[np.ndarray]:
    """Run FER+ on a grayscale face crop → softmax probabilities over 8 classes."""
    import cv2
    try:
        f = cv2.resize(face_gray, (64, 64)).astype(np.float32)   # FER+ wants raw 0–255
        net.setInput(f.reshape(1, 1, 64, 64))
        logits = net.forward().reshape(-1)
        e = np.exp(logits - float(logits.max()))
        return e / (float(e.sum()) or 1.0)
    except Exception:
        return None


def _local_visual(ctx: AnalysisContext) -> Dict[str, np.ndarray]:
    import cv2

    fps = ctx.config.analysis.visual_sample_fps
    times: List[float] = []
    motion: List[float] = []
    scene: List[float] = []
    face: List[float] = []
    expr: List[float] = []
    emo: List[float] = []        # FER+ facial arousal (0 when no face / no model)

    detector = None
    model = _yunet_model()
    if model:
        detector = _load_detector(model)
    if detector is None:
        ctx.warn("[visual] 無 YuNet 臉部模型 → 微表情以中央殘差動態代理估計。")
    fer = _load_fer(_fer_model()) if detector is not None else None
    if detector is not None and fer is None:
        ctx.warn("[visual] 無 FER+ 情緒模型 → visual_emotion 停用(改看微表情變化)。")

    prev_gray: Optional[np.ndarray] = None
    prev_hist: Optional[np.ndarray] = None
    prev_face: Optional[np.ndarray] = None   # last face crop (for expression diff)
    det_fail = 0                              # frames where face detection raised
    # FER is decimated: emotion changes slowly, so run it at ≤2fps and carry the
    # last value forward — a big cost cut on long videos (YuNet still runs each frame).
    fer_every = max(1, round(ctx.config.analysis.visual_sample_fps / 2.0))
    last_emo = 0.0
    for fidx, (t, frame) in enumerate(iter_frames(ctx.video_path, target_fps=fps, max_width=320)):
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        H, W = gray.shape
        # ── motion ──
        if prev_gray is not None and prev_gray.shape == gray.shape:
            m = float(np.mean(np.abs(gray.astype(np.int16) - prev_gray.astype(np.int16)))) / 255.0
        else:
            m = 0.0
        # ── scene change (grayscale histogram correlation) ──
        hist = cv2.calcHist([gray], [0], None, [32], [0, 256])
        cv2.normalize(hist, hist)
        s = max(0.0, min(1.0, 1.0 - float(cv2.compareHist(prev_hist, hist, cv2.HISTCMP_CORREL)))) if prev_hist is not None else 0.0

        # ── face presence + micro-expression + FER emotion ──
        f_val = 0.0
        e_val = 0.0
        emo_val = 0.0
        cur_face: Optional[np.ndarray] = None
        if detector is not None:
            try:
                detector.setInputSize((W, H))
                _, faces = detector.detect(frame)
            except Exception:
                faces = None
                det_fail += 1
            if faces is not None and len(faces):
                # largest face drives close-up + expression + emotion
                boxes = faces[:, :4]
                areas = boxes[:, 2] * boxes[:, 3]
                f_val = min(1.0, float(areas.sum()) / float(W * H) * 2.2)
                bx, by, bw, bh = boxes[int(np.argmax(areas))].astype(int)
                bx, by = max(0, bx), max(0, by)
                crop = gray[by:by + max(1, bh), bx:bx + max(1, bw)]
                if crop.size:
                    cur_face = cv2.resize(crop, (_FACE_CROP, _FACE_CROP))
                    if fer is not None:
                        if fidx % fer_every == 0:      # decimated FER inference
                            p = _fer_probs(fer, crop)
                            if p is not None:
                                last_emo = float(np.dot(p, _FER_AROUSAL))
                        # emotional intensity, weighted up for close-up reactions
                        emo_val = last_emo * min(1.0, 0.6 + f_val)
            else:
                last_emo = 0.0                          # no face this frame → drop stale emotion
        else:
            # proxy: residual motion in the centre 46% (where a talking head sits),
            # above the global motion floor → subtle subject-level activity
            cy0, cy1 = int(H * 0.27), int(H * 0.73)
            cx0, cx1 = int(W * 0.27), int(W * 0.73)
            c = gray[cy0:cy1, cx0:cx1]
            if prev_gray is not None and prev_gray.shape == gray.shape:
                pc = prev_gray[cy0:cy1, cx0:cx1]
                cm = float(np.mean(np.abs(c.astype(np.int16) - pc.astype(np.int16)))) / 255.0
                e_val = max(0.0, cm - 0.6 * m)
            f_val = 0.35 * m

        if cur_face is not None:
            if prev_face is not None and prev_face.shape == cur_face.shape:
                e_val = float(np.mean(np.abs(cur_face.astype(np.int16) - prev_face.astype(np.int16)))) / 255.0
            prev_face = cur_face
        elif detector is not None:
            prev_face = None  # face lost → don't diff across the gap

        times.append(t)
        motion.append(m)
        scene.append(s)
        face.append(f_val)
        expr.append(e_val)
        emo.append(emo_val)
        prev_gray, prev_hist = gray, hist

    if det_fail:
        get_logger(__name__).debug("[visual] %d 幀臉部偵測失敗(已略過)。", det_fail)
    grid = ctx.grid
    motion_g = dsp.robust_norm(dsp.bin_max(times, motion, grid))
    scene_g = dsp.robust_norm(dsp.bin_max(times, scene, grid))
    face_g = dsp.robust_norm(dsp.bin_mean(times, face, grid)) if any(face) else 0.4 * motion_g
    expr_g = dsp.robust_norm(dsp.bin_max(times, expr, grid)) if any(expr) else np.zeros(grid.n)
    emo_g = dsp.robust_norm(dsp.bin_max(times, emo, grid)) if any(emo) else np.zeros(grid.n)
    return {"visual_motion": motion_g, "visual_scene": scene_g, "visual_face": face_g,
            "visual_expression": expr_g, "visual_emotion": emo_g}


def _rekognition_face_track(ctx: AnalysisContext) -> Optional[np.ndarray]:
    """DetectFaces emotion confidence on sparsely-sampled frames."""
    import cv2

    client = ctx.aws.client("rekognition")
    if client is None:
        ctx.warn("[visual] 無法建立 Rekognition client → 使用本地人臉偵測。")
        return None
    grid = ctx.grid
    arousal = {"HAPPY", "SURPRISED", "ANGRY", "FEAR"}
    times: List[float] = []
    vals: List[float] = []
    next_t = 0.0
    step = 2.0  # sample one frame every 2s to bound cost
    try:
        for t, frame in iter_frames(ctx.video_path, target_fps=1.0, max_width=640):
            if t < next_t:
                continue
            next_t = t + step
            ok, buf = cv2.imencode(".jpg", frame)
            if not ok:
                continue
            resp = client.detect_faces(Image={"Bytes": buf.tobytes()}, Attributes=["ALL"])
            best = 0.0
            for fd in resp.get("FaceDetails", []):
                for emo in fd.get("Emotions", []):
                    if emo.get("Type") in arousal:
                        best = max(best, float(emo.get("Confidence", 0)) / 100.0)
                if not fd.get("Emotions"):
                    best = max(best, 0.4)  # face present but neutral
            times.append(t)
            vals.append(best)
    except Exception as e:  # credentials/permission/throttling → fall back
        ctx.warn(f"[visual] Rekognition 呼叫失敗 ({type(e).__name__}) → 使用本地人臉偵測。")
        return None
    if not times:
        return None
    return dsp.robust_norm(dsp.bin_max(times, vals, grid))


def extract(ctx: AnalysisContext) -> List[SignalTrack]:
    local = _local_visual(ctx)
    face_backend = "local"
    if ctx.use_aws("visual"):
        face_aws = _rekognition_face_track(ctx)
        if face_aws is not None:
            local["visual_face"] = face_aws
            face_backend = "aws"

    emo_backend = "fer+" if any(local["visual_emotion"]) else "local"
    return [
        make_track("visual_motion", "visual", local["visual_motion"], ctx),
        make_track("visual_scene", "visual", local["visual_scene"], ctx),
        make_track("visual_face", "visual", local["visual_face"], ctx, backend=face_backend),
        make_track("visual_expression", "visual", local["visual_expression"], ctx),
        make_track("visual_emotion", "visual", local["visual_emotion"], ctx, backend=emo_backend),
    ]
