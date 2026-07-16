"""Unit tests for narrative-anchored windowing (the 期待感 fix in scorer.py).

Pins the structure that gives a clip a *point*: the payoff lands late, there's a
real build-up before it, leading dead air is trimmed, and length bounds hold.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import numpy as np

from sparkreel.aws.clients import get_clients
from sparkreel.config import load_config
from sparkreel.fusion import scorer
from sparkreel.fusion.scorer import _emotion_curve, _narrative_window, _payoff_index
from sparkreel.models import MediaInfo, TimeGrid
from sparkreel.signals.base import AnalysisContext


def _ctx(dur=60.0):
    cfg = load_config()
    g = TimeGrid.for_duration(dur, cfg.analysis.grid_hz)
    media = MediaInfo(path="x", duration=dur, width=1280, height=720, has_audio=True)
    return AnalysisContext(video_path="x", media=media, grid=g, config=cfg, aws=get_clients())


def _buildup_curve(ctx):
    """Dead air 0–19s, a rising build 20–30, a peak at 30, quick drop after."""
    g = ctx.grid
    n = g.n
    score = np.zeros(n)
    for t in range(g.index(20.0), g.index(30.0)):
        score[t] = 0.35 + 0.06 * (t - g.index(20.0))
    score[g.index(30.0)] = 0.95
    for t in range(g.index(30.0) + 1, g.index(34.0)):
        score[t] = 0.9 - 0.15 * (t - g.index(30.0))
    emotion = np.zeros(n)
    emotion[g.index(29.0):g.index(32.0)] = 0.9
    return score, emotion, g.index(30.0)


def test_payoff_is_late_and_deadair_trimmed():
    ctx = _ctx(60.0)
    score, emotion, peak = _buildup_curve(ctx)
    s, e = _narrative_window(score, peak, ctx, "talk", emotion)
    assert s > 15.0                                   # leading dead air (0–19s) trimmed
    pt = ctx.grid.time(_payoff_index(score, emotion, peak, ctx))
    frac = (pt - s) / (e - s)
    assert 0.72 <= frac <= 0.90                       # payoff sits in the last ~20–28% (talk)
    assert (pt - s) > 3.0                             # and there is a genuine build-up before it


def test_talk_payoff_later_than_action():
    ctx = _ctx(60.0)
    score, emotion, peak = _buildup_curve(ctx)
    pt = ctx.grid.time(_payoff_index(score, emotion, peak, ctx))
    st, et = _narrative_window(score, peak, ctx, "talk", emotion)
    sa, ea = _narrative_window(score, peak, ctx, "action", emotion)
    assert (pt - st) / (et - st) > (pt - sa) / (ea - sa)   # talk builds longer; action leaves aftermath


def test_length_bounds_respected():
    ctx = _ctx(120.0)
    g = ctx.grid
    score = np.full(g.n, 0.7)
    emotion = np.full(g.n, 0.5)
    s, e = _narrative_window(score, g.index(60.0), ctx, "talk", emotion)
    assert (e - s) <= ctx.config.editing.max_clip_sec + 0.1
    assert (e - s) >= ctx.config.editing.min_clip_sec - 0.1


def test_emotion_curve_takes_weighted_max():
    ctx = _ctx(20.0)
    n = ctx.grid.n
    per = {"visual_emotion": np.linspace(0.0, 1.0, n), "audio_emotion": np.zeros(n)}
    c = _emotion_curve(per, n)
    assert c.shape == (n,) and float(c.max()) <= 1.0 and c[-1] > 0.9


def test_payoff_index_prefers_emotional_crest():
    ctx = _ctx(40.0)
    g = ctx.grid
    n = g.n
    score = np.zeros(n)
    score[g.index(20.0)] = 0.9                         # energy peak at 20
    emotion = np.zeros(n)
    emotion[g.index(21.5)] = 1.0                       # emotional crest slightly later
    pay = _payoff_index(score, emotion, g.index(20.0), ctx)
    # payoff pulled toward the emotional crest (between the two, not before 20)
    assert g.index(20.0) <= pay <= g.index(22.0)


def test_detect_highlights_sets_segment_kind():
    from sparkreel.models import SignalTrack
    ctx = _ctx(40.0)
    g = ctx.grid
    n = g.n
    bump = np.zeros(n)
    c = g.index(20.0)
    bump[c - 3:c + 2] = [0.4, 0.7, 0.95, 0.8, 0.5]
    tracks = [SignalTrack(name="audio_excitement", modality="audio", hz=2.0, samples=list(bump), weight=1.0),
              SignalTrack(name="chat_volume", modality="chat", hz=2.0, samples=list(bump), weight=1.2)]
    _, highlights = scorer.detect_highlights(tracks, ctx)
    assert highlights and all(h.segment_kind for h in highlights)   # every clip is dispatched a kind
    assert ctx.stream_profile                                       # profile stashed on the context
