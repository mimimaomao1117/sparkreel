"""Smoke tests for the FER+ facial-emotion path (cv2.dnn ONNX).

Skips cleanly when cv2 or the model file is absent, so the suite passes on a
checkout that hasn't downloaded assets/models/emotion_ferplus.onnx.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import numpy as np
import pytest

pytest.importorskip("cv2")

from sparkreel.signals import visual


def test_arousal_weights_are_sane():
    assert visual._FER_AROUSAL.shape == (8,)
    assert float(visual._FER_AROUSAL.min()) >= 0.0 and float(visual._FER_AROUSAL.max()) <= 1.0
    assert len(visual._FER_CLASSES) == 8
    # surprise / happiness / anger are high-arousal; neutral is lowest
    idx = {c: i for i, c in enumerate(visual._FER_CLASSES)}
    assert visual._FER_AROUSAL[idx["neutral"]] == float(visual._FER_AROUSAL.min())
    assert visual._FER_AROUSAL[idx["surprise"]] >= 0.9


def test_fer_runs_and_returns_distribution():
    model = visual._fer_model()
    if not model:
        pytest.skip("emotion_ferplus.onnx not present")
    net = visual._load_fer(model)
    assert net is not None
    face = np.full((80, 80), 128, dtype=np.uint8)
    p = visual._fer_probs(net, face)
    assert p is not None and p.shape == (8,)
    assert abs(float(p.sum()) - 1.0) < 1e-4              # a valid probability distribution
    arousal = float(np.dot(p, visual._FER_AROUSAL))
    assert 0.0 <= arousal <= 1.0


def test_fer_probs_handles_bad_input_gracefully():
    model = visual._fer_model()
    if not model:
        pytest.skip("emotion_ferplus.onnx not present")
    net = visual._load_fer(model)
    assert visual._fer_probs(net, np.zeros((0, 0), dtype=np.uint8)) is None   # empty crop → None, no crash
