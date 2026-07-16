"""SparkReel MCP server — expose clipping to any agent (Claude Code / Cursor / …).

Like Palmier Pro exposes its editor over MCP, this exposes SparkReel's
highlight-clipping pipeline as agent-native tools. An agent can then say
"clip this stream into the 3 most viral TikToks" and drive it end-to-end.

    sparkreel mcp                                  # run (stdio transport)
    claude mcp add sparkreel -- sparkreel mcp      # register with Claude Code

Requires the optional `mcp` dependency:  pip install "sparkreel[mcp]"
"""
from __future__ import annotations

import contextlib
import json
import sys
from pathlib import Path

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("sparkreel")


@mcp.tool()
def create_clips(video: str, platforms: str = "tiktok,reels,shorts", max_clips: int = 8,
                 emphasis: str = "balanced", captions: bool = False, broll: bool = False,
                 min_score: float = 0.55, target_len: int = 20,
                 out_root: str = "assets/output") -> dict:
    """Detect highlights in a local video **or URL** and auto-cut ranked short-form clips.

    Args:
        video: path to a video file, or a YouTube/Twitch/VOD URL.
        platforms: comma-separated of tiktok,reels,shorts.
        max_clips: how many clips to produce (1–12).
        emphasis: reaction | action | loud | balanced (biases highlight selection).
        captions: burn animated captions onto the clips.
        broll: auto-insert B-roll cutaways.
        min_score: highlight threshold 0.4–0.7 (higher = more selective).
        target_len: target clip length in seconds (6–45).

    Returns a summary with clips ranked by predicted virality (0–99).
    """
    from .pipeline import run_pipeline
    from .web.app import _config_from_plan
    plats = [p.strip() for p in platforms.split(",") if p.strip()] or ["tiktok", "reels", "shorts"]
    plan = {"platforms": plats, "captions": bool(captions), "max_clips": int(max_clips),
            "emphasis": emphasis, "min_score": float(min_score), "target_len": int(target_len),
            "broll": bool(broll)}
    cfg = _config_from_plan(plan)
    # keep stdout clean for the JSON-RPC transport; pipeline chatter → stderr
    with contextlib.redirect_stdout(sys.stderr):
        result, out_dir = run_pipeline(video, platforms=plats, out_root=out_root,
                                       config=cfg, captions=bool(captions), make_montage=True)
    d = json.loads(result.to_json())
    clips = [{"rank": i, "title": c["title"], "virality": c["virality"],
              "virality_parts": c.get("virality_parts", {}), "kind": c.get("segment_kind", ""),
              "duration": round(c["variants"][0]["duration"], 1) if c["variants"] else 0,
              "path": c["variants"][0]["path"] if c["variants"] else "",
              "platforms": {v["platform"]: v["path"] for v in c["variants"]}}
             for i, c in enumerate(d["clips"])]
    montage = next((w.split("montage:", 1)[1] for w in d["warnings"] if w.startswith("montage:")), None)
    return {"job_id": result.job_id, "out_dir": str(out_dir),
            "highlights": d["metrics"]["highlights_found"], "clips": clips, "montage": montage,
            "stream_profile": {k: v for k, v in d.get("stream_profile", {}).items() if v > 0},
            "avg_virality": round(sum(c["virality"] for c in clips) / max(1, len(clips))),
            "time_saved_sec": round(d["metrics"]["time_saved_sec"])}


@mcp.tool()
def list_jobs(out_root: str = "assets/output", limit: int = 10) -> list:
    """List recent SparkReel jobs (output folders that contain a result)."""
    base = Path(out_root)
    if not base.exists():
        return []
    jobs = sorted([p for p in base.iterdir() if (p / "result.json").exists()],
                  key=lambda p: p.stat().st_mtime, reverse=True)[:limit]
    out = []
    for j in jobs:
        try:
            r = json.loads((j / "result.json").read_text(encoding="utf-8"))
            cl = r.get("clips", [])
            out.append({"job_id": r["job_id"], "dir": str(j), "clips": len(cl),
                        "avg_virality": round(sum(c.get("virality", 0) for c in cl) / max(1, len(cl)))})
        except Exception:
            pass
    return out


@mcp.tool()
def get_job(job_dir: str) -> dict:
    """Get the full result summary (ranked clips + metrics) for a job directory."""
    p = Path(job_dir) / "result.json"
    if not p.exists():
        return {"error": f"no result.json under {job_dir}"}
    r = json.loads(p.read_text(encoding="utf-8"))
    return {"job_id": r["job_id"], "metrics": r["metrics"],
            "clips": [{"rank": i, "title": c["title"], "virality": c["virality"],
                       "virality_parts": c.get("virality_parts", {}),
                       "path": c["variants"][0]["path"] if c["variants"] else ""}
                      for i, c in enumerate(r["clips"])]}


@mcp.tool()
def make_sample(out: str = "examples") -> dict:
    """Generate a self-contained demo stream (video + chat + subtitles) to test clipping."""
    from .sample import generate
    with contextlib.redirect_stdout(sys.stderr):
        return generate(out)


def run() -> None:
    mcp.run()
