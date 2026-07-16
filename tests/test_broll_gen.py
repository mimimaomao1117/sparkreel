"""Unit tests for the generative B-roll provider.

No network: the provider's HTTP call is monkeypatched, so we test the contract
(env-driven config, prompt building, JSON-url following, and graceful None on any
failure) deterministically and offline.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from sparkreel.editing import broll_gen
from sparkreel.models import Highlight


def test_provider_and_gen_flags(monkeypatch):
    monkeypatch.delenv("SPARKREEL_BROLL_PROVIDER", raising=False)
    monkeypatch.delenv("SPARKREEL_BROLL_ENDPOINT", raising=False)
    assert broll_gen.provider() == "local"
    assert broll_gen.gen_enabled() is False
    monkeypatch.setenv("SPARKREEL_BROLL_PROVIDER", "generative")
    assert broll_gen.gen_enabled() is False            # provider set but no endpoint yet
    monkeypatch.setenv("SPARKREEL_BROLL_ENDPOINT", "http://example.test/gen")
    assert broll_gen.gen_enabled() is True


def test_gen_max_parsing(monkeypatch):
    monkeypatch.setenv("SPARKREEL_BROLL_GEN_MAX", "5")
    assert broll_gen.gen_max() == 5
    monkeypatch.setenv("SPARKREEL_BROLL_GEN_MAX", "not-a-number")
    assert broll_gen.gen_max() == 3                     # falls back to default
    monkeypatch.setenv("SPARKREEL_BROLL_GEN_MAX", "-2")
    assert broll_gen.gen_max() == 0                     # clamped to >= 0


def test_prompt_for_uses_keywords():
    h = Highlight(index=0, start=0.0, end=5.0, peak_t=1.0, score=0.8, duration=5.0,
                  keywords=["逆轉", "五殺"])
    p = broll_gen.prompt_for(h)
    assert "逆轉" in p and "9:16" in p


def test_generate_clip_none_without_endpoint(monkeypatch, tmp_path):
    monkeypatch.delenv("SPARKREEL_BROLL_ENDPOINT", raising=False)
    assert broll_gen.generate_clip("x", 2.0, tmp_path / "g.mp4") is None


def test_generate_clip_none_on_network_failure(monkeypatch, tmp_path):
    monkeypatch.setenv("SPARKREEL_BROLL_ENDPOINT", "http://example.test/gen")

    def boom(*a, **k):
        raise OSError("no network")

    monkeypatch.setattr(broll_gen.urllib.request, "urlopen", boom)
    assert broll_gen.generate_clip("x", 2.0, tmp_path / "g.mp4") is None


def _resp(ctype, body):
    class R:
        headers = {"Content-Type": ctype}

        def read(self):
            return body

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    return R()


def test_generate_clip_saves_raw_video_bytes(monkeypatch, tmp_path):
    monkeypatch.setenv("SPARKREEL_BROLL_ENDPOINT", "http://example.test/gen")
    monkeypatch.setattr(broll_gen.urllib.request, "urlopen",
                        lambda *a, **k: _resp("video/mp4", b"\x00" * 2048))
    out = broll_gen.generate_clip("prompt", 2.0, tmp_path / "g.mp4")
    assert out and Path(out).exists() and Path(out).stat().st_size == 2048


def test_generate_clip_follows_json_url(monkeypatch, tmp_path):
    monkeypatch.setenv("SPARKREEL_BROLL_ENDPOINT", "http://example.test/gen")
    calls = {"n": 0}

    def fake(url, *a, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            return _resp("application/json", json.dumps({"video": "http://cdn.test/clip.mp4"}).encode())
        return _resp("video/mp4", b"\x00" * 3000)      # the followed URL returns bytes

    monkeypatch.setattr(broll_gen.urllib.request, "urlopen", fake)
    out = broll_gen.generate_clip("prompt", 2.0, tmp_path / "g.mp4")
    assert out and Path(out).exists() and calls["n"] == 2


def test_generate_clip_none_when_json_has_no_url(monkeypatch, tmp_path):
    monkeypatch.setenv("SPARKREEL_BROLL_ENDPOINT", "http://example.test/gen")
    monkeypatch.setattr(broll_gen.urllib.request, "urlopen",
                        lambda *a, **k: _resp("application/json", json.dumps({"status": "ok"}).encode()))
    assert broll_gen.generate_clip("prompt", 2.0, tmp_path / "g.mp4") is None


def test_generate_clip_none_when_too_small(monkeypatch, tmp_path):
    monkeypatch.setenv("SPARKREEL_BROLL_ENDPOINT", "http://example.test/gen")
    monkeypatch.setattr(broll_gen.urllib.request, "urlopen",
                        lambda *a, **k: _resp("video/mp4", b"tiny"))
    assert broll_gen.generate_clip("prompt", 2.0, tmp_path / "g.mp4") is None
