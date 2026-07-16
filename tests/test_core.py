"""Fast, dependency-light unit tests for SparkReel core logic.

Runs under pytest OR as a plain script (python tests/test_core.py). The heavy
end-to-end ffmpeg pipeline is exercised by test_integration.py (opt-in).
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import numpy as np

from sparkreel.config import load_config
from sparkreel.models import TimeGrid, Highlight, MediaInfo, ChatMessage, TranscriptSegment
from sparkreel.signals import dsp
from sparkreel.signals.lexicon import score_text
from sparkreel.signals.base import AnalysisContext
from sparkreel.aws.clients import get_clients
from sparkreel.fusion.scorer import fuse, detect_highlights
from sparkreel.styles import target_resolution, platform_summary
from sparkreel import moderation as mod
from sparkreel.models import SignalTrack


def test_config_loads():
    cfg = load_config()
    assert cfg.platform_names == ["tiktok", "reels", "shorts"]
    assert 0 < cfg.fusion.peak.min_score < 1
    assert len(cfg.fusion.weights) == 11   # incl. visual_expression + visual_emotion (FER+)


def test_dsp_norm_and_bins():
    x = np.array([0, 1, 2, 3, 100.0])
    n = dsp.robust_norm(x)
    assert n.min() >= 0 and n.max() <= 1
    grid = TimeGrid.for_duration(10, 2.0)
    b = dsp.bin_max([0.1, 0.2, 5.0], [0.3, 0.9, 0.5], grid)
    assert b[0] == 0.9 and len(b) == grid.n


def test_lexicon_hype_vs_neutral():
    s_hi, i_hi, kw = score_text("這波太神了 666 封神")
    s_lo, i_lo, _ = score_text("嗯好喔")
    assert i_hi > i_lo and s_hi > 0 and "封神" in kw
    s_neg, _, _ = score_text("這主播好爛 垃圾")
    assert s_neg < 0


def test_platform_resolution():
    cfg = load_config()
    assert target_resolution(cfg.platform("tiktok")) == (1080, 1920)
    assert len(platform_summary(cfg)) == 3


def _ctx(duration=30.0):
    cfg = load_config()
    grid = TimeGrid.for_duration(duration, cfg.analysis.grid_hz)
    media = MediaInfo(path="x", duration=duration, width=1280, height=720, has_audio=True)
    return AnalysisContext(video_path="x", media=media, grid=grid, config=cfg, aws=get_clients())


def test_fusion_detects_synthetic_peak():
    ctx = _ctx(30.0)
    n = ctx.grid.n
    # two synthetic co-firing signals with a bump around t=15s (bin 30)
    bump = np.zeros(n)
    c = ctx.grid.index(15.0)
    bump[c - 2:c + 3] = [0.6, 0.85, 1.0, 0.85, 0.6]
    tracks = [
        SignalTrack(name="audio_excitement", modality="audio", hz=2.0, samples=list(bump), weight=1.0),
        SignalTrack(name="chat_volume", modality="chat", hz=2.0, samples=list(bump), weight=1.2),
    ]
    score, highlights = detect_highlights(tracks, ctx)
    assert len(highlights) == 1
    assert 12 <= highlights[0].peak_t <= 18


def test_moderation_catches_profanity_and_pii():
    ctx = _ctx(20.0)
    ctx.chat = [ChatMessage(t=5.0, user="a", text="他媽的爛"),
                ChatMessage(t=6.0, user="b", text="打 0912-345-678 給我")]
    ctx.transcript = [TranscriptSegment(start=4.0, end=7.0, text="這波太神")]
    h = Highlight(index=0, start=3.0, end=9.0, peak_t=6.0, score=0.8, duration=6.0)
    report = mod.scan(ctx, [h])
    labels = {f.label for f in report.findings}
    assert "chat_profanity" in labels and "pii_phone" in labels
    assert report.needs_human_review


def test_moderation_clean_passes():
    ctx = _ctx(20.0)
    ctx.chat = [ChatMessage(t=5.0, user="a", text="666 太神了")]
    ctx.transcript = [TranscriptSegment(start=4.0, end=7.0, text="這波超猛")]
    h = Highlight(index=0, start=3.0, end=9.0, peak_t=6.0, score=0.8, duration=6.0)
    report = mod.scan(ctx, [h])
    assert report.status == "pass" and not report.needs_human_review


def _run_all():
    fns = [v for k, v in globals().items() if k.startswith("test_") and callable(v)]
    passed = 0
    for fn in fns:
        fn()
        print(f"  ✓ {fn.__name__}")
        passed += 1
    print(f"\n{passed}/{len(fns)} tests passed.")


if __name__ == "__main__":
    _run_all()
