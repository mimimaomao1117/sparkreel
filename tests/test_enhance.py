"""Unit tests for per-highlight edit planning (speaker-track reframe + B-roll).

These exercise the pure planning logic on synthetic signal tracks — no ffmpeg,
no real video decode — so they're fast and deterministic. The improved B-roll
selection is pinned here: cutaways are sourced from the strongest out-of-highlight
moment (relevance-ranked, not round-robin) and placed over the clip's own lull.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import numpy as np

from sparkreel.aws.clients import get_clients
from sparkreel.config import load_config
from sparkreel.editing.enhance import _is_landscape, _track, plan_broll, plan_reframe
from sparkreel.models import Highlight, MediaInfo, SignalTrack, TimeGrid
from sparkreel.signals.base import AnalysisContext


def _ctx(duration=40.0, w=1280, h=720):
    cfg = load_config()
    grid = TimeGrid.for_duration(duration, cfg.analysis.grid_hz)
    media = MediaInfo(path="x", duration=duration, width=w, height=h, has_audio=True)
    return AnalysisContext(video_path="x", media=media, grid=grid, config=cfg, aws=get_clients())


def _motion_track(ctx, spans):
    """Build a visual_motion track: baseline 0.15, with (start,end,value) overrides."""
    g = ctx.grid
    a = np.full(g.n, 0.15)
    for start, end, val in spans:
        a[g.index(start):g.index(end) + 1] = val
    return SignalTrack(name="visual_motion", modality="visual", hz=g.dt and 1.0 / g.dt,
                       samples=list(a), weight=1.0)


def test_is_landscape():
    assert _is_landscape(_ctx(w=1280, h=720)) is True
    assert _is_landscape(_ctx(w=720, h=1280)) is False


def test_track_pads_and_truncates():
    tracks = [SignalTrack(name="visual_motion", modality="visual", hz=2.0, samples=[0.1, 0.2], weight=1.0)]
    a = _track(tracks, "visual_motion", 5)
    assert a is not None and len(a) == 5 and a[2] == 0.0        # padded with zeros
    b = _track(tracks, "visual_motion", 1)
    assert b is not None and len(b) == 1                        # truncated
    assert _track(tracks, "does_not_exist", 5) is None


def test_plan_broll_inserts_cutaway_over_lull_from_dynamic_source():
    ctx = _ctx(40.0)
    # highlight 10–22s: busy edges, a clear lull 14–18.5s; dynamic pool 28–33s
    track = _motion_track(ctx, [(10.0, 13.0, 0.5), (19.0, 22.0, 0.5),
                                (14.0, 18.5, 0.10), (28.0, 33.0, 0.9)])
    h = Highlight(index=0, start=10.0, end=22.0, peak_t=11.0, score=0.8, duration=12.0)
    plan_broll(ctx, [h], [track])
    assert h.broll, "expected one cutaway placed over the lull"
    ins = h.broll[0]
    assert {"at", "dur", "src_start"} <= set(ins)
    assert 0.0 <= ins["at"] < (h.end - h.start)                # inside the clip timeline
    assert 26.0 <= ins["src_start"] <= 34.0                    # sourced from the dynamic elsewhere-span
    assert 1.2 <= ins["dur"] <= 2.2


def test_plan_broll_two_clips_do_not_reuse_the_same_source():
    ctx = _ctx(60.0)
    # two highlights each with a lull; two distinct dynamic pool moments
    track = _motion_track(ctx, [
        (5.0, 7.0, 0.5), (8.0, 12.5, 0.10), (13.0, 15.0, 0.5),      # highlight A 5–15
        (30.0, 32.0, 0.5), (33.0, 37.5, 0.10), (38.0, 40.0, 0.5),   # highlight B 30–40
        (20.0, 24.0, 0.95),                                         # pool source 1
        (46.0, 50.0, 0.9),                                          # pool source 2
    ])
    ha = Highlight(index=0, start=5.0, end=15.0, peak_t=6.0, score=0.8, duration=10.0)
    hb = Highlight(index=1, start=30.0, end=40.0, peak_t=31.0, score=0.8, duration=10.0)
    plan_broll(ctx, [ha, hb], [track])
    assert ha.broll and hb.broll
    assert ha.broll[0]["src_start"] != hb.broll[0]["src_start"]     # no reuse while pool has spares


def test_plan_broll_noop_when_uniformly_flat():
    ctx = _ctx(30.0)
    track = SignalTrack(name="visual_motion", modality="visual", hz=2.0,
                        samples=[0.12] * ctx.grid.n, weight=1.0)        # no dynamic pool at all
    h = Highlight(index=0, start=5.0, end=15.0, peak_t=7.0, score=0.8, duration=10.0)
    plan_broll(ctx, [h], [track])
    assert h.broll == []


def test_plan_broll_noop_without_motion_track():
    ctx = _ctx(30.0)
    h = Highlight(index=0, start=5.0, end=15.0, peak_t=7.0, score=0.8, duration=10.0)
    plan_broll(ctx, [h], [])            # nothing to work with → must not raise
    assert h.broll == []


def test_plan_reframe_skips_portrait_source():
    ctx = _ctx(20.0, w=720, h=1280)     # vertical → no horizontal crop needed, must be a safe no-op
    h = Highlight(index=0, start=2.0, end=10.0, peak_t=5.0, score=0.8, duration=8.0)
    plan_reframe(ctx, [h])
    assert h.reframe_cx == 0.5
