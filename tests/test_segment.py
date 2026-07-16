"""Unit tests for timeline classification (fusion/segment.py)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import numpy as np

from sparkreel.aws.clients import get_clients
from sparkreel.config import load_config
from sparkreel.fusion.segment import (KINDS, Segment, _absorb_short, classify_segments,
                                       dominant_kind, kind_at)
from sparkreel.models import MediaInfo, TimeGrid, TranscriptSegment
from sparkreel.signals.base import AnalysisContext


def _ctx(dur=40.0):
    cfg = load_config()
    g = TimeGrid.for_duration(dur, cfg.analysis.grid_hz)
    media = MediaInfo(path="x", duration=dur, width=1280, height=720, has_audio=True)
    return AnalysisContext(video_path="x", media=media, grid=g, config=cfg, aws=get_clients())


def _band(g, a, b, v):
    x = np.zeros(g.n)
    x[g.index(a):g.index(b) + 1] = v
    return x


def test_classify_identifies_each_kind():
    ctx = _ctx(40.0)
    g = ctx.grid
    ctx.transcript = [TranscriptSegment(start=0.0, end=12.0, text="在講話")]
    per = {
        "audio_emotion": _band(g, 0, 12, 0.4),                       # talk 0–12
        "visual_emotion": _band(g, 14, 18, 0.9), "visual_face": _band(g, 14, 18, 0.8),  # reaction
        "audio_excitement": _band(g, 24, 28, 0.95), "chat_volume": _band(g, 24, 28, 0.8),  # hype
        "visual_motion": _band(g, 32, 38, 0.9),                     # action
    }
    segs, prof = classify_segments(per, ctx)
    kinds = {s.kind for s in segs}
    assert {"talk", "reaction", "hype", "action"} <= kinds
    assert kind_at(segs, 16.0) == "reaction"
    assert kind_at(segs, 26.0) == "hype"
    assert kind_at(segs, 35.0) == "action"
    assert set(prof) == set(KINDS)
    assert abs(sum(prof.values()) - 1.0) < 0.02   # fractions are rounded to 3dp for display


def test_dominant_kind_prefers_real_kind_over_lull():
    prof = {"talk": 0.1, "hype": 0.05, "reaction": 0.0, "action": 0.0, "lull": 0.85}
    assert dominant_kind(prof) == "talk"


def test_dominant_kind_picks_majority():
    ctx = _ctx(20.0)
    g = ctx.grid
    segs, prof = classify_segments({"audio_excitement": _band(g, 0, 18, 0.9)}, ctx)
    assert dominant_kind(prof) == "hype"


def test_kind_at_default_outside_segments():
    segs = [Segment(0.0, 10.0, "talk", 0.8)]
    assert kind_at(segs, 50.0, default="hype") == "hype"


def test_absorb_short_removes_tiny_spans():
    segs = [Segment(0.0, 10.0, "talk", 0.8),
            Segment(10.0, 10.5, "action", 0.3),      # 0.5s flicker → absorbed
            Segment(10.5, 20.0, "talk", 0.7)]
    out = _absorb_short(segs, min_sec=1.5)
    assert all((s.end - s.start) >= 1.5 for s in out)
    assert "action" not in {s.kind for s in out}     # the flicker is gone
