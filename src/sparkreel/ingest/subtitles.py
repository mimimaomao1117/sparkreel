"""SRT / VTT subtitle parsing → TranscriptSegment list.

In production the transcript comes from Amazon Transcribe; a sidecar .srt/.vtt
(common on VODs) or this parser provides the same structure offline.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import List

from ..models import TranscriptSegment

_TC = re.compile(
    r"(\d{1,2}):(\d{2}):(\d{2})[,.](\d{1,3})\s*-->\s*(\d{1,2}):(\d{2}):(\d{2})[,.](\d{1,3})"
)


def _to_sec(h: str, m: str, s: str, ms: str) -> float:
    return int(h) * 3600 + int(m) * 60 + int(s) + int(ms.ljust(3, "0")) / 1000.0


def load_subtitles(path: str | Path) -> List[TranscriptSegment]:
    path = Path(path)
    if not path.exists():
        return []
    raw = path.read_text(encoding="utf-8", errors="ignore")
    raw = raw.replace("\r\n", "\n").replace("\r", "\n")
    blocks = re.split(r"\n\s*\n", raw)
    segments: List[TranscriptSegment] = []
    for block in blocks:
        lines = [ln for ln in block.split("\n") if ln.strip()]
        if not lines:
            continue
        tc_line = next((ln for ln in lines if "-->" in ln), None)
        if not tc_line:
            continue
        m = _TC.search(tc_line)
        if not m:
            continue
        start = _to_sec(*m.group(1, 2, 3, 4))
        end = _to_sec(*m.group(5, 6, 7, 8))
        text_lines = [ln for ln in lines if "-->" not in ln and ln.strip().upper() != "WEBVTT"]
        # drop a leading pure-number index line (SRT)
        if text_lines and text_lines[0].strip().isdigit():
            text_lines = text_lines[1:]
        text = " ".join(text_lines).strip()
        if text:
            segments.append(TranscriptSegment(start=start, end=end, text=text))
    segments.sort(key=lambda s: s.start)
    return segments


def find_sidecar(video_path: str | Path) -> str | None:
    """Look for <video>.srt / .vtt next to the media file."""
    p = Path(video_path)
    for ext in (".srt", ".vtt"):
        cand = p.with_suffix(ext)
        if cand.exists():
            return str(cand)
    return None
