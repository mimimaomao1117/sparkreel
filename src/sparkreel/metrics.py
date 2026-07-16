"""Effectiveness metrics (成效衡量).

Quantifies the value SparkReel delivers per job:
  * time saved vs. a manual editing baseline
  * highlight detection confidence
  * output quality score
  * automation degree (share of clips needing no human touch)
  * content re-use / compression ratio

The manual baseline is an explicit, defensible model (documented below) rather
than a magic number, so the numbers can be audited and tuned per studio.
"""
from __future__ import annotations

from typing import List, Optional

import numpy as np

from .config import Config
from .models import Clip, Highlight, MediaInfo, Metrics, ModerationReport

# Manual editing baseline model (seconds). A human editor typically:
#   1. reviews the full stream once to locate moments            → REVIEW = duration
#   2. edits each highlight (trim, reframe 9:16, caption, title)  → per highlight
#   3. re-exports each extra per-platform variant                 → per extra variant
REVIEW_FACTOR = 1.0
EDIT_PER_HIGHLIGHT_SEC = 12 * 60
EXTRA_VARIANT_SEC = 3 * 60


def compute(
    media: MediaInfo,
    highlights: List[Highlight],
    clips: List[Clip],
    moderation: ModerationReport,
    processing_sec: float,
    cfg: Config,
    score_curve: Optional[np.ndarray] = None,
) -> Metrics:
    src = max(0.001, media.duration)
    total_variants = sum(len(c.variants) for c in clips)
    extra_variants = max(0, total_variants - len(clips))

    # unique highlight footage (per-highlight durations, not double-counting variants)
    highlight_ids = {c.highlight_index for c in clips}
    total_output = sum(h.duration for h in highlights if h.index in highlight_ids)

    manual = REVIEW_FACTOR * src + len(clips) * EDIT_PER_HIGHLIGHT_SEC + extra_variants * EXTRA_VARIANT_SEC
    time_saved = max(0.0, manual - processing_sec)

    passed = sum(1 for c in clips if c.moderation_status == "pass")
    automation = (passed / len(clips)) if clips else 0.0

    if score_curve is not None and len(score_curve) and highlights:
        med = float(np.median(score_curve))
        detection_conf = float(np.mean([max(0.0, h.score - med) / max(1e-6, 1 - med) for h in highlights]))
    else:
        detection_conf = float(np.mean([h.score for h in highlights])) if highlights else 0.0

    flagged = sum(1 for c in clips if c.moderation_status not in ("pass",))
    flag_rate = (flagged / len(clips)) if clips else 0.0

    return Metrics(
        source_duration_sec=round(src, 2),
        processing_sec=round(processing_sec, 2),
        realtime_factor=round(src / max(0.001, processing_sec), 2),
        highlights_found=len(highlights),
        clips_produced=len(clips),
        total_output_sec=round(total_output, 2),
        compression_ratio=round(src / max(0.001, total_output), 1),
        manual_edit_baseline_sec=round(manual, 1),
        time_saved_sec=round(time_saved, 1),
        time_saved_ratio=round(time_saved / max(0.001, manual), 4),
        automation_degree=round(automation, 4),
        avg_highlight_score=round(float(np.mean([h.score for h in highlights])) if highlights else 0.0, 3),
        avg_quality_score=round(float(np.mean([c.quality_score for c in clips])) if clips else 0.0, 1),
        detection_confidence=round(detection_conf, 3),
        moderation_flag_rate=round(flag_rate, 3),
    )
