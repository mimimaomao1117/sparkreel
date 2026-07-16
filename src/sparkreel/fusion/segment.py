"""Timeline classification — label every grid bin with a segment *kind*, so the
editor can dispatch a fitting strategy per region instead of one-size-fits-all.
This is the "先對整個影片分類" step: understand what *kind* of moment each part
of the stream is before deciding how to cut it.

Kinds (by dominant multimodal evidence):
  talk     — steady voice, calm visuals (someone speaking to camera / commentary)
  hype     — loudness / chat surge (cheers, big plays, crowd going off)
  reaction — facial-emotion or micro-expression spike (laugh, shock, anger close-up)
  action   — high visual motion or scene cutting (gameplay, movement, replays)
  lull     — everything quiet (dead air, transitions, waiting)

Also returns a stream `profile` (fraction of each kind) so the planner can reason
about the whole video ("70% talk → longer lead-ins, emphasise reactions").
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Tuple

import numpy as np

from ..signals.base import AnalysisContext

KINDS = ["talk", "hype", "reaction", "action", "lull"]
KIND_LABELS = {"talk": "談話", "hype": "爆點", "reaction": "反應", "action": "動作", "lull": "冷場"}
_LULL_FLOOR = 0.22   # below this, nothing is really happening → lull


@dataclass
class Segment:
    start: float
    end: float
    kind: str
    intensity: float   # 0..1 peak evidence for this span's kind


def _get(per: Dict[str, np.ndarray], name: str, n: int) -> np.ndarray:
    a = per.get(name)
    if a is None:
        return np.zeros(n)
    a = np.asarray(a, dtype=float)
    return np.pad(a, (0, n - a.size)) if a.size < n else a[:n]


def _bin_scores(per: Dict[str, np.ndarray], ctx: AnalysisContext) -> Dict[str, np.ndarray]:
    """Per-bin evidence for each active kind (talk/hype/reaction/action)."""
    g = ctx.grid
    n = g.n
    exc = _get(per, "audio_excitement", n)
    aemo = _get(per, "audio_emotion", n)
    chat = _get(per, "chat_volume", n)
    kw = _get(per, "speech_keyword", n)
    ssent = _get(per, "speech_sentiment", n)
    motion = _get(per, "visual_motion", n)
    scene = _get(per, "visual_scene", n)
    vemo = _get(per, "visual_emotion", n)
    vexpr = _get(per, "visual_expression", n)
    face = _get(per, "visual_face", n)

    # voice presence: transcript coverage OR a vocal-arousal proxy
    voice = np.zeros(n)
    for seg in ctx.transcript:
        voice[g.index(seg.start):g.index(seg.end) + 1] = 1.0
    voice = np.maximum(voice, (aemo > 0.15).astype(float))

    return {
        # talking to camera: voice present, visuals calm, nothing exploding
        "talk": voice * (1.0 - 0.6 * motion) * (0.4 + 0.6 * (1.0 - exc)),
        # crowd/loudness surge
        "hype": np.clip(np.maximum(exc, 0.9 * chat) + 0.3 * kw, 0.0, 1.0),
        # facial emotion / expression close-up
        "reaction": np.clip(np.maximum(vemo, 0.8 * vexpr) * (0.5 + 0.5 * face) + 0.3 * ssent, 0.0, 1.0),
        # visual movement / cutting
        "action": np.maximum(motion, 0.9 * scene),
    }


def _smooth(labels: List[str], win: int) -> List[str]:
    """Majority filter to kill single-bin flicker."""
    if win <= 1:
        return labels
    n = len(labels)
    out = list(labels)
    half = win // 2
    for i in range(n):
        lo, hi = max(0, i - half), min(n, i + half + 1)
        window = labels[lo:hi]
        out[i] = max(set(window), key=window.count)
    return out


def _merge(labels: List[str], inten: List[float], ctx: AnalysisContext,
           min_sec: float = 1.5) -> List[Segment]:
    g = ctx.grid
    segs: List[Segment] = []
    i = 0
    n = len(labels)
    while i < n:
        j = i
        while j < n and labels[j] == labels[i]:
            j += 1
        start, end = g.time(i), min(g.duration, g.time(j))
        peak = max(inten[i:j]) if j > i else 0.0
        segs.append(Segment(start=round(start, 2), end=round(end, 2), kind=labels[i],
                            intensity=round(float(peak), 3)))
        i = j
    # absorb too-short spans into the stronger neighbour (keeps the map readable)
    return _absorb_short(segs, min_sec)


def _absorb_short(segs: List[Segment], min_sec: float) -> List[Segment]:
    if len(segs) <= 1:
        return segs
    changed = True
    while changed and len(segs) > 1:
        changed = False
        for i, s in enumerate(segs):
            if (s.end - s.start) >= min_sec:
                continue
            # merge into the neighbour with the higher intensity, inherit its kind
            left = segs[i - 1] if i > 0 else None
            right = segs[i + 1] if i < len(segs) - 1 else None
            keep = left if (left and (not right or left.intensity >= right.intensity)) else right
            if keep is None:
                continue
            keep.start = min(keep.start, s.start)
            keep.end = max(keep.end, s.end)
            keep.intensity = max(keep.intensity, s.intensity)
            segs.pop(i)
            changed = True
            break
    return segs


def _profile(labels: List[str]) -> Dict[str, float]:
    n = len(labels) or 1
    return {k: round(labels.count(k) / n, 3) for k in KINDS}


def classify_segments(per: Dict[str, np.ndarray], ctx: AnalysisContext) -> Tuple[List[Segment], Dict[str, float]]:
    """Label the timeline and summarise the stream. Returns (segments, profile)."""
    g = ctx.grid
    n = g.n
    if n == 0:
        return [], {k: 0.0 for k in KINDS}
    scores = _bin_scores(per, ctx)
    labels: List[str] = []
    inten: List[float] = []
    for i in range(n):
        col = {k: float(scores[k][i]) for k in scores}
        k = max(col, key=col.get)
        best = col[k]
        if best < _LULL_FLOOR:
            k, best = "lull", 0.0
        labels.append(k)
        inten.append(min(1.0, best))
    labels = _smooth(labels, win=max(1, int(1.5 / g.dt)))
    segs = _merge(labels, inten, ctx)
    return segs, _profile(labels)


def kind_at(segments: List[Segment], t: float, default: str = "talk") -> str:
    """The segment kind covering time `t` (for per-highlight dispatch)."""
    for s in segments:
        if s.start <= t < s.end:
            return s.kind
    return default


def dominant_kind(profile: Dict[str, float]) -> str:
    """The stream's headline kind, ignoring lull unless it truly dominates."""
    ranked = sorted(((k, profile.get(k, 0.0)) for k in KINDS), key=lambda kv: kv[1], reverse=True)
    for k, frac in ranked:
        if k != "lull":
            return k
    return "talk"
