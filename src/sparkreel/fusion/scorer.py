"""Multimodal fusion → highlight detection.

Strategy
--------
1. Fuse:   fused(t) = Σ wᵢ·sᵢ(t) / Σ wᵢ  over *active* signals, then smooth.
           Each sᵢ is already per-stream normalized, so `fused` is comparable
           across the timeline and thresholdable in absolute terms.
2. Peaks:  local maxima ≥ min_score, greedily spaced ≥ min_gap_sec, capped.
3. Window: expand each peak across its contiguous above-threshold region, pad,
           and clamp to [min_clip, max_clip]; trim overlaps.
4. Explain: attribute each highlight to its dominant signals and attach the
           transcript / chat evidence that fired — this is what makes the
           output auditable rather than a black box.
"""
from __future__ import annotations

from typing import Dict, List, Tuple

import numpy as np

from ..models import Highlight, SignalContribution, SignalTrack
from ..signals import dsp
from ..signals.base import AnalysisContext
from .segment import classify_segments, kind_at

SIGNAL_LABELS = {
    "audio_excitement": "音量爆點",
    "audio_emotion": "語音情緒",
    "speech_keyword": "關鍵語",
    "speech_sentiment": "語音情感",
    "chat_volume": "彈幕爆量",
    "chat_sentiment": "彈幕情緒",
    "visual_motion": "畫面動態",
    "visual_scene": "場景切換",
    "visual_face": "特寫反應",
    "visual_expression": "微表情",
    "visual_emotion": "臉部情緒",
}


def fuse(tracks: List[SignalTrack], ctx: AnalysisContext) -> Tuple[np.ndarray, Dict[str, np.ndarray]]:
    grid = ctx.grid
    n = grid.n
    fused = np.zeros(n)
    wsum = 0.0
    per: Dict[str, np.ndarray] = {}
    for tr in tracks:
        a = np.asarray(tr.samples, dtype=float)
        if a.size < n:
            a = np.pad(a, (0, n - a.size))
        else:
            a = a[:n]
        per[tr.name] = a
        if float(a.max()) > 0.02:            # ignore dead modalities in the denominator
            fused += tr.weight * a
            wsum += tr.weight
    if wsum > 0:
        fused /= wsum
    win = max(1, int(ctx.config.analysis.smoothing_sec / grid.dt))
    fused = dsp.moving_average(fused, win)
    return np.clip(fused, 0.0, 1.0), per


def _find_peaks(score: np.ndarray, ctx: AnalysisContext) -> List[int]:
    p = ctx.config.fusion.peak
    grid = ctx.grid
    n = len(score)
    cands = [
        i for i in range(n)
        if score[i] >= p.min_score
        and score[i] >= (score[i - 1] if i > 0 else -1)
        and score[i] >= (score[i + 1] if i < n - 1 else -1)
    ]
    cands.sort(key=lambda i: score[i], reverse=True)
    accepted: List[int] = []
    for i in cands:
        if all(abs(grid.time(i) - grid.time(j)) >= p.min_gap_sec for j in accepted):
            accepted.append(i)
        if len(accepted) >= p.max_highlights:
            break
    accepted.sort()
    return accepted


def _window(score: np.ndarray, peak: int, ctx: AnalysisContext) -> Tuple[float, float]:
    cfg = ctx.config
    grid = ctx.grid
    n = len(score)
    thr = max(0.30, cfg.fusion.peak.min_score * 0.6)
    half_max = int(cfg.editing.max_clip_sec / grid.dt) // 2
    l = r = peak
    while l > 0 and score[l - 1] >= thr and (peak - l) < half_max:
        l -= 1
    while r < n - 1 and score[r + 1] >= thr and (r - peak) < half_max:
        r += 1
    start = max(0.0, grid.time(l) - cfg.editing.pad_before_sec)
    end = min(grid.duration, grid.time(r) + cfg.editing.pad_after_sec)
    if end - start < cfg.editing.min_clip_sec:
        grow = (cfg.editing.min_clip_sec - (end - start)) / 2
        start = max(0.0, start - grow)
        end = min(grid.duration, end + grow)
    if end - start > cfg.editing.max_clip_sec:
        pt = grid.time(peak)
        start = max(0.0, pt - cfg.editing.max_clip_sec / 2)
        end = min(grid.duration, start + cfg.editing.max_clip_sec)
    return round(start, 2), round(end, 2)


def _evidence(ctx: AnalysisContext, start: float, end: float) -> Tuple[str, List[str], List[str]]:
    transcript_bits = [s.text for s in ctx.transcript if s.start < end and s.end > start]
    kw = []
    for s in ctx.transcript:
        if s.start < end and s.end > start:
            kw += s.keywords
    chat_in = [m for m in ctx.chat if start <= m.t <= end]
    chat_in.sort(key=lambda m: (m.intensity or 0.0), reverse=True)
    chat_excerpt = [m.text for m in chat_in[:4]]
    excerpt = " ".join(transcript_bits)[:120]
    # de-dup keywords
    seen = set()
    kw = [k for k in kw if not (k in seen or seen.add(k))]
    return excerpt, kw[:8], chat_excerpt


# ── narrative windowing (期待感) ──────────────────────────────────────────────
# Where the payoff (climax) sits within the clip. Placing it late leaves a build-up
# before it and a short resolution after — that structure is what creates
# anticipation. Talk needs the most lead-in (setup → punchline); action/hype hit
# sooner. This is the fix for "highlights with no build, no point".
_PAYOFF_POS = {"talk": 0.80, "reaction": 0.74, "hype": 0.70, "action": 0.66, "lull": 0.74}
_MIN_LEAD_SEC = {"talk": 5.0, "reaction": 3.0, "hype": 2.5, "action": 2.0, "lull": 3.0}
_EMO_CURVE = (("visual_emotion", 1.0), ("audio_emotion", 0.9), ("visual_expression", 0.8),
              ("speech_sentiment", 0.7), ("chat_sentiment", 0.6))


def _emotion_curve(per: Dict[str, np.ndarray], n: int) -> np.ndarray:
    """Composite 'emotional intensity over time' from the emotion-bearing signals."""
    curve = np.zeros(n)
    for name, w in _EMO_CURVE:
        a = per.get(name)
        if a is None:
            continue
        a = np.asarray(a, dtype=float)
        a = np.pad(a, (0, n - a.size)) if a.size < n else a[:n]
        curve = np.maximum(curve, w * a)
    return curve


def _payoff_index(score: np.ndarray, emotion: np.ndarray, peak: int, ctx: AnalysisContext) -> int:
    """The real climax = the energy+emotion crest near the fused peak (so the clip's
    high point is where the *feeling* peaks, not merely where it's loudest)."""
    g = ctx.grid
    w = max(1, int(3.0 / g.dt))
    lo, hi = max(0, peak - w), min(len(score), peak + w + 1)
    seg = 0.55 * np.asarray(score[lo:hi], dtype=float) + 0.45 * np.asarray(emotion[lo:hi], dtype=float)
    return lo + int(np.argmax(seg)) if seg.size else peak


def _narrative_window(score: np.ndarray, peak: int, ctx: AnalysisContext,
                      kind: str, emotion: np.ndarray) -> Tuple[float, float]:
    """Build a clip around the payoff so it *builds* to a point:
      • payoff placed at ~70–80% (by segment kind) → lead-in before, short tail after
      • leading dead air trimmed so the clip opens on the rising edge (a hook)
      • a minimum lead-in guaranteed by kind (talk keeps its setup)."""
    cfg = ctx.config
    g = ctx.grid
    pay_i = _payoff_index(score, emotion, peak, ctx)
    pt = g.time(pay_i)
    target = cfg.editing.target_clip_sec
    pos = _PAYOFF_POS.get(kind, 0.74)
    lead = target * pos                       # the *most* lead-in we'd consider

    # open on the build's rising edge: skip leading dead air, but keep ≥ min-lead
    thr = max(0.30, cfg.fusion.peak.min_score * 0.55)
    si = g.index(max(0.0, pt - lead))
    min_lead_i = max(0, pay_i - max(1, int(_MIN_LEAD_SEC.get(kind, 3.0) / g.dt)))
    while si < min_lead_i and score[si] < thr:
        si += 1
    start = max(0.0, g.time(si) - 0.3)                        # 0.3s pre-roll onto the onset

    # tail = a short resolution, sized so the payoff lands at `pos` of the ACTUAL clip
    # (proportional to the real lead-in, not the nominal target) → payoff stays late
    actual_lead = max(0.5, pt - start)
    tail = min(6.0, max(1.5, actual_lead * (1.0 - pos) / pos))
    end = min(g.duration, pt + tail)

    # enforce clip-length bounds while keeping the payoff late
    if end - start < cfg.editing.min_clip_sec:
        start = max(0.0, end - cfg.editing.min_clip_sec)
    if end - start > cfg.editing.max_clip_sec:
        start = max(0.0, pt - cfg.editing.max_clip_sec * pos)
        end = min(g.duration, start + cfg.editing.max_clip_sec)
    return round(start, 2), round(end, 2)


def detect_highlights(
    tracks: List[SignalTrack], ctx: AnalysisContext
) -> Tuple[np.ndarray, List[Highlight]]:
    score, per = fuse(tracks, ctx)
    peaks = _find_peaks(score, ctx)
    grid = ctx.grid

    # ── classify the whole timeline first, then dispatch a per-kind clip window ──
    emotion = _emotion_curve(per, grid.n)
    segments, profile = classify_segments(per, ctx)
    ctx.segments = segments
    ctx.stream_profile = profile
    use_narrative = ctx.config.editing.narrative

    total_w = sum(t.weight for t in tracks) or 1.0
    highlights: List[Highlight] = []
    prev_end = -1.0
    for idx, peak in enumerate(peaks):
        kind = kind_at(segments, grid.time(peak))
        if use_narrative:
            start, end = _narrative_window(score, peak, ctx, kind, emotion)
        else:
            start, end = _window(score, peak, ctx)
        if start < prev_end:                       # avoid reusing footage
            start = prev_end
            if end - start < ctx.config.editing.min_clip_sec:
                continue
        prev_end = end

        # per-signal contribution at the peak
        comps: List[SignalContribution] = []
        for tr in tracks:
            val = float(per[tr.name][peak])
            comps.append(SignalContribution(
                name=tr.name, modality=tr.modality, value=round(val, 3),
                weighted=round(val * tr.weight / total_w, 4),
            ))
        comps.sort(key=lambda c: c.weighted, reverse=True)
        dominant = [SIGNAL_LABELS.get(c.name, c.name) for c in comps if c.value > 0.15][:3]
        reason = " + ".join(dominant) if dominant else "綜合訊號"

        excerpt, kw, chat_excerpt = _evidence(ctx, start, end)

        highlights.append(Highlight(
            index=len(highlights),
            start=start, end=end, duration=round(end - start, 2),
            peak_t=round(grid.time(peak), 2),
            score=round(float(score[peak]), 3),
            components=comps,
            dominant_signals=dominant,
            keywords=kw,
            transcript_excerpt=excerpt,
            chat_excerpt=chat_excerpt,
            reason=reason,
            segment_kind=kind,
        ))

    # predicted virality (0..99) → rank so the most shareable clip is #0
    from .virality import score_virality
    for h in highlights:
        h.virality, h.virality_parts = score_virality(h, score, per, ctx)
    highlights.sort(key=lambda h: (h.virality, h.score), reverse=True)
    for i, h in enumerate(highlights):
        h.index = i
    return score, highlights
