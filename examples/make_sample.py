#!/usr/bin/env python3
"""Generate a synthetic SparkReel demo stream (thin wrapper around sparkreel.sample).

Usage:  python examples/make_sample.py [--out DIR]
    or:  sparkreel make-sample --out DIR
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from sparkreel.sample import generate  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser(description="Generate a SparkReel demo stream sample")
    ap.add_argument("--out", default=str(Path(__file__).parent), help="output directory")
    ap.add_argument("--name", default="sample_stream")
    args = ap.parse_args()
    print("[make_sample] rendering demo stream (calm/talk/hype segments)…")
    paths = generate(args.out, args.name)
    for k, v in paths.items():
        print(f"[make_sample] ✓ {k:5s}: {v}")
    print("[make_sample] highlights expected around the 3 'hype' windows.")


if __name__ == "__main__":
    main()
