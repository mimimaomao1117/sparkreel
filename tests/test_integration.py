"""End-to-end integration test (opt-in — needs ffmpeg; generates a sample).

Run explicitly:  python tests/test_integration.py
Under pytest it is skipped unless SPARKREEL_E2E=1 to keep the default suite fast.
"""
import os
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


@pytest.mark.skipif(os.environ.get("SPARKREEL_E2E") != "1", reason="set SPARKREEL_E2E=1 to run")
def test_end_to_end():
    _run()


def _run():
    from sparkreel.sample import generate
    from sparkreel.pipeline import run_pipeline

    with tempfile.TemporaryDirectory() as td:
        paths = generate(td, "e2e")
        result, out_dir = run_pipeline(
            paths["video"], chat_path=paths["chat"], subtitle_path=paths["srt"],
            out_root=str(Path(td) / "out"), make_montage=True,
        )
        assert result.metrics.highlights_found >= 2, "should detect the hype windows"
        assert result.metrics.clips_produced >= 2
        for c in result.clips:
            for v in c.variants:
                assert Path(v.path).exists() and v.width == 1080 and v.height == 1920
        assert result.moderation.status == "pass"
        print(f"  ✓ e2e: {result.metrics.highlights_found} highlights, "
              f"{result.metrics.clips_produced} clips, saved "
              f"{result.metrics.time_saved_ratio * 100:.1f}%")


if __name__ == "__main__":
    _run()
