"""Generative B-roll — pluggable provider that synthesises a short cutaway clip
from a text prompt, instead of (or in addition to) the local self-sourced cutaway.

Provider-agnostic on purpose (local-first stays the default). Point it at any
text-to-video service — Replicate, Runway, a local ComfyUI, whatever — via one
HTTP endpoint; SparkReel never bakes in a specific vendor or key.

  SPARKREEL_BROLL_PROVIDER = local | generative     (default: local)
  SPARKREEL_BROLL_ENDPOINT = https://…              (POST {prompt, seconds} → video)
  SPARKREEL_BROLL_AUTH     = Bearer <token>         (optional Authorization header)
  SPARKREEL_BROLL_GEN_MAX  = 3                       (cap generated clips per job)

The endpoint receives JSON {"prompt": str, "seconds": float} and must return
either a video URL (JSON {"video": url} / {"output": url}) or the raw video bytes.
Anything missing / failing → silent fall back to the local cutaway.
"""
from __future__ import annotations

import json
import os
import urllib.request
from pathlib import Path
from typing import Optional

from ..log import get_logger
from ..models import Highlight


def provider() -> str:
    return os.environ.get("SPARKREEL_BROLL_PROVIDER", "local").strip().lower()


def gen_enabled() -> bool:
    return provider() == "generative" and bool(os.environ.get("SPARKREEL_BROLL_ENDPOINT"))


def gen_max() -> int:
    try:
        return max(0, int(os.environ.get("SPARKREEL_BROLL_GEN_MAX", "3")))
    except ValueError:
        return 3


def prompt_for(h: Highlight) -> str:
    """Build a short text-to-video prompt from the highlight's own evidence."""
    topic = "、".join(h.keywords[:4]) or (h.title or h.reason or "現場精彩畫面")
    return f"cinematic b-roll, {topic}, dynamic, high energy, no text, vertical 9:16"


def generate_clip(prompt: str, seconds: float, out_path: Path) -> Optional[str]:
    """Call the configured text-to-video endpoint; save the clip. None on failure."""
    endpoint = os.environ.get("SPARKREEL_BROLL_ENDPOINT")
    if not endpoint:
        return None
    body = json.dumps({"prompt": prompt, "seconds": round(seconds, 2)}).encode("utf-8")
    req = urllib.request.Request(endpoint, data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    auth = os.environ.get("SPARKREEL_BROLL_AUTH")
    if auth:
        req.add_header("Authorization", auth)
    try:
        with urllib.request.urlopen(req, timeout=180) as resp:
            ctype = resp.headers.get("Content-Type", "")
            raw = resp.read()
        if "application/json" in ctype:
            data = json.loads(raw)
            url = data.get("video") or data.get("output") or data.get("url")
            if not url:
                get_logger(__name__).warning("[broll-gen] 端點回應缺少 video/output/url 欄位。")
                return None
            with urllib.request.urlopen(url, timeout=180) as r2:
                raw = r2.read()
        if len(raw) < 1000:
            get_logger(__name__).warning("[broll-gen] 端點回傳 %d bytes(太小)→ 略過。", len(raw))
            return None
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_bytes(raw)
        return str(out_path)
    except Exception as e:
        get_logger(__name__).warning("[broll-gen] 端點呼叫失敗(%s):%s", type(e).__name__, e)
        return None
