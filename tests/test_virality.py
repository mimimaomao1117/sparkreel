"""Unit tests for the virality score (0–99) and its use to rank highlights.

The score is an honest heuristic read of local signals (no ground-truth labels),
so these tests pin down its *contract* — bounded output, the four named drivers,
sensible monotonicity, and that detect_highlights sorts/relabels by it — rather
than asserting exact magic numbers.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import numpy as np

from sparkreel.aws.clients import get_clients
from sparkreel.config import load_config
from sparkreel.fusion.scorer import detect_highlights
from sparkreel.fusion.virality import score_virality
from sparkreel.models import Highlight, MediaInfo, SignalContribution, SignalTrack, TimeGrid
from sparkreel.signals.base import AnalysisContext


def _ctx(duration=40.0, w=1280, h=720):
    cfg = load_config()
    grid = TimeGrid.for_duration(duration, cfg.analysis.grid_hz)
    media = MediaInfo(path="x", duration=duration, width=w, height=h, has_audio=True)
    return AnalysisContext(video_path="x", media=media, grid=grid, config=cfg, aws=get_clients())


def _comps(names, val=0.7):
    return [SignalContribution(name=n, modality="audio", value=val, weighted=val * 0.25) for n in names]


def test_score_bounded_and_named_parts():
    ctx = _ctx(30.0)
    g = ctx.grid
    n = g.n
    score = np.zeros(n)
    c = g.index(10.0)
    score[c - 2:c + 3] = [0.5, 0.8, 0.95, 0.7, 0.5]
    per = {"audio_emotion": score.copy(), "visual_expression": score.copy()}
    h = Highlight(index=0, start=8.0, end=20.0, peak_t=10.0, score=0.95, duration=12.0,
                  components=_comps(["audio_emotion", "audio_excitement", "visual_expression"]))
    v, parts = score_virality(h, score, per, ctx)
    assert isinstance(v, int) and 0 <= v <= 99
    assert set(parts) == {"hook", "emotion", "value", "trend"}
    assert all(0 <= parts[k] <= 99 for k in parts)


def test_strong_highlight_beats_weak():
    ctx = _ctx(40.0)
    g = ctx.grid
    n = g.n
    # strong: sharp early peak, rich + broad emotional signals, good length fit
    strong = np.zeros(n)
    cs = g.index(6.0)
    strong[cs - 1:cs + 3] = [0.6, 0.97, 0.9, 0.7]
    per_s = {"audio_emotion": strong.copy(), "visual_expression": strong.copy(),
             "speech_sentiment": strong.copy(), "chat_volume": strong.copy()}
    hs = Highlight(index=0, start=4.0, end=24.0, peak_t=6.0, score=0.97, duration=20.0,
                   components=_comps(["audio_emotion", "audio_excitement", "visual_expression", "chat_volume"], 0.8))
    # weak: late, low, no emotional breadth
    weak = np.zeros(n)
    cw = g.index(30.0)
    weak[cw:cw + 2] = [0.56, 0.55]
    per_w = {"audio_emotion": np.zeros(n)}
    hw = Highlight(index=1, start=20.0, end=40.0, peak_t=39.0, score=0.56, duration=20.0,
                   components=_comps(["audio_excitement"], 0.3))
    vs, _ = score_virality(hs, strong, per_s, ctx)
    vw, _ = score_virality(hw, weak, per_w, ctx)
    assert vs > vw


def test_detect_highlights_ranks_and_reindexes_by_virality():
    ctx = _ctx(40.0)
    g = ctx.grid
    n = g.n
    c1, c2 = g.index(8.0), g.index(30.0)
    exc = np.zeros(n)
    exc[c1 - 2:c1 + 3] = [0.6, 0.85, 1.0, 0.85, 0.6]
    exc[c2 - 2:c2 + 3] = [0.6, 0.85, 0.95, 0.85, 0.6]
    emo = np.zeros(n)
    emo[c1 - 2:c1 + 3] = [0.6, 0.85, 1.0, 0.85, 0.6]     # emotion only near the first peak
    tracks = [
        SignalTrack(name="audio_excitement", modality="audio", hz=2.0, samples=list(exc), weight=1.0),
        SignalTrack(name="audio_emotion", modality="audio", hz=2.0, samples=list(emo), weight=1.0),
        SignalTrack(name="chat_volume", modality="chat", hz=2.0, samples=list(exc), weight=1.2),
    ]
    score, highlights = detect_highlights(tracks, ctx)
    assert len(highlights) >= 2
    # virality is non-increasing across the returned order …
    vs = [h.virality for h in highlights]
    assert vs == sorted(vs, reverse=True)
    # … and indices are re-labelled 0..k-1 to match the new order
    assert [h.index for h in highlights] == list(range(len(highlights)))
    assert all(0 <= h.virality <= 99 for h in highlights)
