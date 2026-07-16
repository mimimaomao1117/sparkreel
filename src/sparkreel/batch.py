"""Batch processing over many streams/VODs.

Discovers video files (+ auto-detected chat/subtitle sidecars) and runs the
pipeline concurrently with a bounded worker pool. Designed for the "creator
uploads a week of VODs / a studio processes many channels" workflow.
"""
from __future__ import annotations

import glob as globlib
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Callable, Dict, List, Optional

from pydantic import BaseModel

from .config import Config, load_config
from .ingest import find_sidecar
from .pipeline import run_pipeline

VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".flv", ".ts", ".webm", ".m4v"}


class BatchItem(BaseModel):
    video: str
    chat: Optional[str] = None
    subtitles: Optional[str] = None


class BatchOutcome(BaseModel):
    video: str
    ok: bool
    job_id: str = ""
    out_dir: str = ""
    clips: int = 0
    highlights: int = 0
    time_saved_ratio: float = 0.0
    error: str = ""


def _find_chat(video: Path) -> Optional[str]:
    for cand in [video.with_suffix(".chat.jsonl"), video.with_name(video.stem + "_chat.jsonl"),
                 video.with_suffix(".jsonl")]:
        if cand.exists():
            return str(cand)
    return None


def discover(path_or_glob: str) -> List[BatchItem]:
    """Expand a directory or glob into BatchItems with sidecars attached."""
    p = Path(path_or_glob)
    if p.is_dir():
        files = [f for f in sorted(p.iterdir()) if f.suffix.lower() in VIDEO_EXTS]
    else:
        files = [Path(f) for f in sorted(globlib.glob(path_or_glob)) if Path(f).suffix.lower() in VIDEO_EXTS]
    items = []
    for f in files:
        items.append(BatchItem(video=str(f), chat=_find_chat(f), subtitles=find_sidecar(f)))
    return items


def process(
    items: List[BatchItem],
    out_root: str = "assets/output",
    platforms: Optional[List[str]] = None,
    workers: int = 2,
    config: Optional[Config] = None,
    on_done: Optional[Callable[[BatchOutcome], None]] = None,
) -> List[BatchOutcome]:
    cfg = config or load_config()
    outcomes: List[BatchOutcome] = []

    def _one(item: BatchItem) -> BatchOutcome:
        try:
            result, out_dir = run_pipeline(
                item.video, chat_path=item.chat, subtitle_path=item.subtitles,
                platforms=platforms, out_root=out_root, config=cfg, progress=None,
            )
            return BatchOutcome(
                video=item.video, ok=True, job_id=result.job_id, out_dir=str(out_dir),
                clips=result.metrics.clips_produced, highlights=result.metrics.highlights_found,
                time_saved_ratio=result.metrics.time_saved_ratio,
            )
        except Exception as e:
            return BatchOutcome(video=item.video, ok=False, error=f"{type(e).__name__}: {e}")

    with ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
        futs = {ex.submit(_one, it): it for it in items}
        for fut in as_completed(futs):
            outcome = fut.result()
            outcomes.append(outcome)
            if on_done:
                on_done(outcome)
    # preserve input order
    order = {it.video: i for i, it in enumerate(items)}
    outcomes.sort(key=lambda o: order.get(o.video, 0))
    return outcomes
