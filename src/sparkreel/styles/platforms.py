"""Per-platform delivery specs.

A single detected highlight is rendered into multiple platform-native variants.
Each platform differs in:
  * aspect / resolution   (all default 9:16 1080×1920 vertical)
  * max duration          (TikTok 60s, Reels 90s, Shorts 60s)
  * caption placement     (center vs top — top keeps Shorts' bottom UI clear)
  * hook tone             (punchy / aesthetic / informative)

This module is the single source of truth for turning a PlatformPreset into
concrete render parameters, so the editing engine stays platform-agnostic.
"""
from __future__ import annotations

from typing import Dict, List, Tuple

from ..config import Config, PlatformPreset

ASPECT_RESOLUTION: Dict[str, Tuple[int, int]] = {
    "9:16": (1080, 1920),
    "4:5": (1080, 1350),
    "1:1": (1080, 1080),
    "16:9": (1920, 1080),
}


def target_resolution(preset: PlatformPreset) -> Tuple[int, int]:
    return ASPECT_RESOLUTION.get(preset.aspect, (1080, 1920))


def clamp_duration(start: float, end: float, preset: PlatformPreset) -> Tuple[float, float]:
    """Trim a highlight window to the platform's max duration if needed."""
    if preset.max_sec and (end - start) > preset.max_sec:
        end = start + preset.max_sec
    return start, end


def platform_summary(cfg: Config) -> List[Dict]:
    """Human-readable spec table for docs / UI / reports."""
    rows = []
    for key in cfg.platform_names:
        p = cfg.platform(key)
        w, h = target_resolution(p)
        rows.append({
            "key": key,
            "label": p.label,
            "aspect": p.aspect,
            "resolution": f"{w}x{h}",
            "max_sec": p.max_sec,
            "caption_style": p.caption_style,
            "hook_style": p.hook_style,
        })
    return rows
