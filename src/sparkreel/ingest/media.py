"""Media ingestion via ffmpeg / ffprobe / OpenCV.

Handles both real recorded streams (.mp4/.flv/.mkv/.ts) and the synthetic demo
sample. Everything downstream reads through these helpers so the rest of the
pipeline never shells out to ffmpeg directly (except the render stage).
"""
from __future__ import annotations

import json
import shutil
import subprocess
import wave
from pathlib import Path
from typing import Iterator, Optional, Tuple

import numpy as np

from ..models import MediaInfo


class FFmpegError(RuntimeError):
    pass


def _require(binary: str) -> str:
    path = shutil.which(binary)
    if not path:
        raise FFmpegError(
            f"'{binary}' 未安裝或不在 PATH。SparkReel 需要 ffmpeg/ffprobe，請先安裝。"
        )
    return path


def run_ff(cmd: list[str], timeout: int = 600) -> subprocess.CompletedProcess:
    proc = subprocess.run(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout
    )
    if proc.returncode != 0:
        tail = proc.stderr.decode("utf-8", "ignore")[-1000:]
        raise FFmpegError(f"{cmd[0]} 失敗:\n{tail}")
    return proc


def _parse_fps(fr: str) -> float:
    try:
        num, den = fr.split("/")
        den = float(den)
        return float(num) / den if den else 0.0
    except Exception:
        return 0.0


def probe(path: str | Path) -> MediaInfo:
    """Return media metadata using ffprobe."""
    _require("ffprobe")
    cmd = [
        "ffprobe", "-v", "error", "-print_format", "json",
        "-show_format", "-show_streams", str(path),
    ]
    data = json.loads(run_ff(cmd).stdout.decode("utf-8", "ignore") or "{}")
    info = MediaInfo(path=str(path))
    fmt = data.get("format", {})
    info.duration = float(fmt.get("duration", 0) or 0)
    info.size_bytes = int(fmt.get("size", 0) or 0)
    for s in data.get("streams", []):
        kind = s.get("codec_type")
        if kind == "video" and info.width == 0:
            info.width = int(s.get("width", 0) or 0)
            info.height = int(s.get("height", 0) or 0)
            info.video_codec = s.get("codec_name", "")
            info.fps = _parse_fps(s.get("avg_frame_rate") or s.get("r_frame_rate") or "0/1")
            if not info.duration:
                info.duration = float(s.get("duration", 0) or 0)
        elif kind == "audio" and not info.has_audio:
            info.has_audio = True
            info.audio_codec = s.get("codec_name", "")
            info.audio_sample_rate = int(s.get("sample_rate", 0) or 0)
    return info


def extract_audio(path: str | Path, out_wav: str | Path, sr: int = 16000) -> Optional[str]:
    """Extract mono PCM16 wav. Returns path or None if the source has no audio."""
    _require("ffmpeg")
    out_wav = str(out_wav)
    cmd = [
        "ffmpeg", "-y", "-i", str(path), "-vn", "-ac", "1", "-ar", str(sr),
        "-acodec", "pcm_s16le", out_wav,
    ]
    try:
        run_ff(cmd)
    except FFmpegError:
        return None
    p = Path(out_wav)
    return out_wav if p.exists() and p.stat().st_size > 44 else None


def read_audio_samples(wav_path: str | Path) -> Tuple[int, np.ndarray]:
    """Read a PCM wav into a mono float32 array in [-1, 1]."""
    with wave.open(str(wav_path), "rb") as w:
        sr = w.getframerate()
        n = w.getnframes()
        ch = w.getnchannels()
        sw = w.getsampwidth()
        raw = w.readframes(n)
    dtype = {1: np.uint8, 2: np.int16, 4: np.int32}.get(sw, np.int16)
    a = np.frombuffer(raw, dtype=dtype).astype(np.float32)
    if ch > 1:
        a = a.reshape(-1, ch).mean(axis=1)
    if sw == 1:  # unsigned 8-bit
        a = (a - 128.0) / 128.0
    else:
        a = a / float(np.iinfo(dtype).max)
    return sr, a


def iter_frames(
    path: str | Path, target_fps: float = 4.0, max_width: int = 320
) -> Iterator[Tuple[float, np.ndarray]]:
    """Yield (timestamp_sec, BGR frame) subsampled to ~target_fps and downscaled."""
    import cv2

    cap = cv2.VideoCapture(str(path))
    if not cap.isOpened():
        return
    native = cap.get(cv2.CAP_PROP_FPS) or 0.0
    if native <= 0:
        native = 30.0
    stride = max(1, int(round(native / max(0.1, target_fps))))
    idx = 0
    try:
        while True:
            if not cap.grab():
                break
            if idx % stride == 0:
                ok, frame = cap.retrieve()
                if not ok:
                    break
                if max_width and frame.shape[1] > max_width:
                    scale = max_width / frame.shape[1]
                    frame = cv2.resize(
                        frame, (max_width, max(1, int(frame.shape[0] * scale)))
                    )
                yield idx / native, frame
            idx += 1
    finally:
        cap.release()
