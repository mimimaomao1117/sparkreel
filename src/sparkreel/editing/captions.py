"""Caption/subtitle generation as styled ASS (burned in via libass).

Two platform-safe styles:
  bold_center — big bold captions in the lower-middle (TikTok / Reels)
  bold_top    — captions pinned to the top zone (YouTube Shorts, keeps the
                bottom UI-safe area clear)
The default CJK font is WenQuanYi Zen Hei (verified present in the environment).
"""
from __future__ import annotations

from typing import List, Tuple

from ..models import TranscriptSegment

CJK_FONT = "WenQuanYi Zen Hei"

# (Alignment, MarginV) per style. Alignment: 2=bottom-center, 8=top-center.
_STYLES = {
    "bold_center": (2, 720),
    "bold_top": (8, 220),
}


def ass_time(sec: float) -> str:
    sec = max(0.0, sec)
    h = int(sec // 3600)
    m = int((sec % 3600) // 60)
    s = int(sec % 60)
    cs = int(round((sec - int(sec)) * 100))
    if cs == 100:
        cs = 0
        s += 1
    return f"{h:d}:{m:02d}:{s:02d}.{cs:02d}"


def _escape(text: str) -> str:
    return text.replace("{", "(").replace("}", ")").replace("\n", r"\N").strip()


def build_ass(
    segments: List[TranscriptSegment],
    win_start: float,
    win_end: float,
    style: str = "bold_center",
    playres: Tuple[int, int] = (1080, 1920),
) -> str:
    align, margin_v = _STYLES.get(style, _STYLES["bold_center"])
    W, H = playres
    fontsize = 76 if style == "bold_center" else 64
    dur = win_end - win_start

    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {W}
PlayResY: {H}
WrapStyle: 2
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,{CJK_FONT},{fontsize},&H00FFFFFF,&H000000FF,&H00202020,&H80000000,1,0,0,0,100,100,1,0,1,5,2,{align},70,70,{margin_v},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    lines = []
    for seg in segments:
        if seg.start >= win_end or seg.end <= win_start:
            continue
        s0 = max(0.0, seg.start - win_start)
        s1 = min(dur, seg.end - win_start)
        if s1 - s0 < 0.2:
            s1 = min(dur, s0 + 0.8)
        text = _escape(seg.text)
        if not text:
            continue
        lines.append(
            f"Dialogue: 0,{ass_time(s0)},{ass_time(s1)},Default,,0,0,0,,{text}"
        )
    return header + "\n".join(lines) + "\n"


def write_ass(path: str, *args, **kwargs) -> str:
    content = build_ass(*args, **kwargs)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    return path
