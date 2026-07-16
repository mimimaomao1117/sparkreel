"""End-to-end pipeline orchestrator.

ingest → multimodal signals → fusion/highlight detection → moderation →
auto-edit (per-platform, moderation-gated) → montage → metrics → result.

Every stage is timed; a `progress(stage, pct, msg)` callback drives the CLI
progress bar and the web UI. The whole thing runs locally with zero AWS
credentials; flipping any backend to `aws` in config swaps in the cloud service.
"""
from __future__ import annotations

import time
import uuid
from pathlib import Path
from typing import Callable, List, Optional

from . import metrics as metrics_mod
from . import moderation as mod
from .aws.clients import aws_status, get_clients
from .config import Config, load_config
from .editing import produce_clip, render_montage
from .fusion import detect_highlights
from .ingest import extract_audio, find_sidecar, load_chat, probe, read_audio_samples
from .models import Clip, PipelineResult, StageTiming, TimeGrid
from .signals import EXTRACTORS
from .signals.base import AnalysisContext

ProgressCB = Optional[Callable[[str, float, str], None]]


def _emit(cb: ProgressCB, stage: str, pct: float, msg: str) -> None:
    if cb:
        try:
            cb(stage, pct, msg)
        except Exception:
            from .log import get_logger
            get_logger(__name__).debug("進度回呼於 stage=%s 拋出例外(已忽略)", stage, exc_info=True)


class _Stage:
    def __init__(self, name: str, sink: List[StageTiming]):
        self.name, self.sink = name, sink

    def __enter__(self):
        self.t = time.time()
        return self

    def __exit__(self, *a):
        self.sink.append(StageTiming(stage=self.name, seconds=round(time.time() - self.t, 2)))


def _auto_chat(video_path: str) -> Optional[str]:
    p = Path(video_path)
    for cand in [p.with_suffix(".chat.jsonl"), p.with_name(p.stem + "_chat.jsonl")]:
        if cand.exists():
            return str(cand)
    return None


def run_pipeline(
    video_path: str,
    chat_path: Optional[str] = None,
    subtitle_path: Optional[str] = None,
    platforms: Optional[List[str]] = None,
    out_root: str = "assets/output",
    job_id: Optional[str] = None,
    config: Optional[Config] = None,
    config_path: Optional[str] = None,
    make_montage: bool = True,
    captions: bool = True,
    progress: ProgressCB = None,
) -> PipelineResult:
    t0 = time.time()
    cfg = config or load_config(config_path)
    platforms = platforms or cfg.platform_names
    video_path = str(video_path)
    if not Path(video_path).exists():
        raise FileNotFoundError(f"找不到影片檔：{video_path}")

    job_id = job_id or f"{Path(video_path).stem[:24]}-{uuid.uuid4().hex[:8]}"
    out_dir = Path(out_root) / job_id
    out_dir.mkdir(parents=True, exist_ok=True)
    timings: List[StageTiming] = []
    aws = get_clients(cfg.aws.region)

    # ── ingest ──────────────────────────────────────────────────────────
    _emit(progress, "ingest", 0.05, "讀取媒體與多模態輸入…")
    with _Stage("ingest", timings):
        media = probe(video_path)
        grid = TimeGrid.for_duration(media.duration, cfg.analysis.grid_hz)
        audio_path = extract_audio(video_path, out_dir / "audio.wav") if media.has_audio else None
        sr, audio = read_audio_samples(audio_path) if audio_path else (0, None)
        chat = load_chat(chat_path or _auto_chat(video_path) or "")
        sub = subtitle_path or find_sidecar(video_path)

    ctx = AnalysisContext(
        video_path=video_path, media=media, grid=grid, config=cfg, aws=aws,
        audio_path=audio_path, audio_sr=sr, audio=audio, chat=chat, subtitle_path=sub,
    )

    # ── multimodal signals ──────────────────────────────────────────────
    tracks = []
    with _Stage("signals", timings):
        for i, (label, fn) in enumerate(EXTRACTORS):
            _emit(progress, "signals", 0.15 + 0.30 * i / len(EXTRACTORS), f"分析訊號：{label}")
            tracks += fn(ctx)

    # ── fusion / highlight detection ────────────────────────────────────
    _emit(progress, "fusion", 0.50, "多模態融合與高光偵測…")
    with _Stage("fusion", timings):
        score, highlights = detect_highlights(tracks, ctx)
        # speaker-tracking reframe (landscape) + optional local B-roll planning
        from .editing.enhance import plan_broll, plan_reframe
        if cfg.editing.speaker_track:
            plan_reframe(ctx, highlights)
        if cfg.editing.broll:
            plan_broll(ctx, highlights, tracks, out_dir=out_dir)

    # ── moderation ──────────────────────────────────────────────────────
    _emit(progress, "moderation", 0.58, "內容審核與合規檢查…")
    with _Stage("moderation", timings):
        report = mod.scan(ctx, highlights)

    # ── editing (moderation-gated, per platform) ────────────────────────
    clips: List[Clip] = []
    with _Stage("editing", timings):
        for i, h in enumerate(highlights):
            _emit(progress, "editing", 0.62 + 0.30 * i / max(1, len(highlights)),
                  f"剪輯高光 {i + 1}/{len(highlights)}：{h.reason}")
            plan = mod.plan_for_window(report, h.start, h.end)
            if plan["status"] == "rejected":
                clips.append(Clip(highlight_index=h.index, title=h.reason,
                                  score=h.score, moderation_status="rejected"))
                continue
            mute = mod.build_mute_filter(plan["mute_ranges"])
            clip = produce_clip(ctx, h, platforms, str(out_dir), audio_filter=mute, captions_on=captions)
            clip.moderation_status = plan["status"]
            clip.virality = h.virality
            clip.virality_parts = h.virality_parts
            clip.segment_kind = h.segment_kind
            clips.append(clip)

    # ── optional montage (精華合輯) ─────────────────────────────────────
    montage_path = None
    if make_montage:
        with _Stage("montage", timings):
            first = platforms[0]
            paths = [v.path for c in clips for v in c.variants if v.platform == first]
            if len(paths) >= 2:
                _emit(progress, "montage", 0.94, "拼接精華合輯…")
                montage_path = render_montage(paths, str(out_dir / f"montage_{first}.mp4"))

    # ── metrics & result ────────────────────────────────────────────────
    processing = time.time() - t0
    published = [c for c in clips if c.moderation_status != "rejected"]
    metrics = metrics_mod.compute(media, highlights, published, report, processing, cfg, score)

    result = PipelineResult(
        job_id=job_id, media=media, backends=dict(cfg.backends),
        grid_hz=cfg.analysis.grid_hz, highlight_threshold=cfg.fusion.peak.min_score,
        score_curve=[round(float(x), 4) for x in score],
        score_times=[round(t, 2) for t in grid.times()],
        tracks=tracks, highlights=highlights, clips=clips, moderation=report,
        metrics=metrics, timings=timings,
        created_at=time.strftime("%Y-%m-%dT%H:%M:%S"),
        warnings=list(ctx.warnings),
        stream_profile=dict(ctx.stream_profile),
    )
    if montage_path:
        result.warnings.append(f"montage:{montage_path}")

    (out_dir / "result.json").write_text(result.to_json(indent=2), encoding="utf-8")
    _emit(progress, "done", 1.0, f"完成：{len(published)} 支短片、{len(highlights)} 個高光。")
    return result, out_dir


def aws_report(region: str = "us-east-1") -> dict:
    return aws_status(region)
