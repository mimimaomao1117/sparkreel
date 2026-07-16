"""Virality score (0–99) — predicted social performance per highlight.

Reuses the multimodal signals already extracted, mapped onto the four drivers
that clip tools (Opus Clip et al.) score on:

  hook    — a sharp, early rise into the peak (does it grab in the first seconds)
  emotion — intensity *and* dynamic range of emotional signals over the clip
            (audio emotion, speech sentiment, facial micro-expression, chat mood)
  value   — overall strength × multimodal breadth × length fit (is it "worth it")
  trend   — local proxy from audience/keyword signals (weak without chat/subs)

No cloud call — it's a weighted read of local features, so it's honest about what
it can and can't know (trend alignment degrades gracefully to a neutral estimate).
"""
from __future__ import annotations

from typing import Dict, Tuple

import numpy as np

from ..models import Highlight
from ..signals.base import AnalysisContext

_EMO = ["audio_emotion", "speech_sentiment", "visual_emotion", "visual_expression", "visual_face", "chat_sentiment"]
_TREND = ["chat_volume", "chat_sentiment", "speech_keyword"]


def _clip01(x: float) -> float:
    return max(0.0, min(1.0, float(x)))


def score_virality(h: Highlight, score: np.ndarray, per: Dict[str, np.ndarray],
                   ctx: AnalysisContext) -> Tuple[int, Dict[str, int]]:
    g = ctx.grid
    i0 = max(0, g.index(h.start))
    i1 = max(i0 + 1, min(g.n, g.index(h.end) + 1))
    pk = g.index(h.peak_t)
    win = np.asarray(score[i0:i1], dtype=float)
    peak_score = float(score[pk]) if 0 <= pk < len(score) else float(win.max() if win.size else 0.0)
    dur = max(1e-3, h.end - h.start)

    # hook — sharp early rise into the peak
    rise = _clip01((peak_score - float(win.min() if win.size else 0.0)) / 0.5)
    early = _clip01(1.0 - (h.peak_t - h.start) / dur)
    hook = _clip01(0.5 * peak_score + 0.3 * rise + 0.2 * early)

    # emotion — intensity + dynamic range of emotional signals over the clip
    means, ranges = [], []
    for name in _EMO:
        a = per.get(name)
        if a is None:
            continue
        seg = np.asarray(a[i0:i1], dtype=float)
        if seg.size and float(seg.max()) > 0.02:
            means.append(float(seg.mean()))
            ranges.append(float(seg.max() - seg.min()))
    emotion = _clip01(0.6 * (float(np.mean(means)) if means else 0.0)
                      + 0.4 * (float(np.mean(ranges)) if ranges else 0.0))

    # value — strength × multimodal breadth × length fit
    breadth = _clip01(len([c for c in h.components if c.value > 0.20]) / 4.0)
    target = ctx.config.editing.target_clip_sec
    length_fit = _clip01(1.0 - abs(dur - target) / max(target, 1.0))
    value = _clip01(0.5 * peak_score + 0.3 * breadth + 0.2 * length_fit)

    # trend — audience/keyword proxy; neutral-ish fallback when no chat/subs
    tvals = []
    for name in _TREND:
        a = per.get(name)
        if a is None:
            continue
        seg = np.asarray(a[i0:i1], dtype=float)
        if seg.size and float(seg.max()) > 0.02:
            tvals.append(float(seg.max()))
    trend = _clip01(max(tvals) if tvals else 0.35 * peak_score)

    parts = {"hook": int(round(hook * 99)), "emotion": int(round(emotion * 99)),
             "value": int(round(value * 99)), "trend": int(round(trend * 99))}
    total = 0.30 * hook + 0.28 * emotion + 0.27 * value + 0.15 * trend
    return int(round(_clip01(total) * 99)), parts
