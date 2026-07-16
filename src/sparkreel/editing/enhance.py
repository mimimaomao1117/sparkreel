"""Per-highlight edit planning that fills fields the render engine consumes:

  reframe_cx — subject horizontal centre for speaker-tracking crop (landscape
               sources only; vertical sources keep the centred crop)
  broll      — one short video-only cutaway from a visually-dynamic elsewhere-
               moment of the *same* source, dropped over a low-activity span
               (keeps the clip's own audio). Local self-B-roll — no stock/gen.
"""
from __future__ import annotations

from typing import List, Optional

import numpy as np

from ..log import get_logger
from ..models import Highlight, SignalTrack
from ..signals.base import AnalysisContext


def _is_landscape(ctx: AnalysisContext) -> bool:
    m = ctx.media
    return m.width > 0 and m.height > 0 and (m.width / m.height) > (9.0 / 16.0 + 0.02)


def _track(tracks: List[SignalTrack], name: str, n: int) -> Optional[np.ndarray]:
    for t in tracks:
        if t.name == name:
            a = np.asarray(t.samples, dtype=float)
            return np.pad(a, (0, n - a.size)) if a.size < n else a[:n]
    return None


# ── speaker-tracking reframe ─────────────────────────────────────────────────
def plan_reframe(ctx: AnalysisContext, highlights: List[Highlight]) -> None:
    """Set reframe_cx = median face centre over each clip window. Only runs for
    landscape sources — vertical sources need no horizontal crop."""
    if not _is_landscape(ctx):
        return
    from ..signals.visual import _load_detector, _yunet_model
    model = _yunet_model()
    det = _load_detector(model) if model else None
    if det is None:
        ctx.warn("[reframe] 無 YuNet 模型 → 說話者追蹤停用,改用置中裁切。")
        return
    import cv2
    log = get_logger(__name__)
    cap = cv2.VideoCapture(ctx.video_path)
    if not cap.isOpened():
        log.warning("[reframe] 無法開啟影片 %s → 改用置中裁切。", ctx.video_path)
        return
    got = 0
    try:
        for h in highlights:
            cxs = []
            span = max(0.5, h.end - h.start)
            n = 12
            for k in range(n):
                t = h.start + span * (k + 0.5) / n
                cap.set(cv2.CAP_PROP_POS_MSEC, t * 1000.0)
                ok, frame = cap.read()
                if not ok:
                    continue
                fh, fw = frame.shape[:2]
                if fw > 640:
                    frame = cv2.resize(frame, (640, int(640 * fh / fw)))
                    fh, fw = frame.shape[:2]
                try:
                    det.setInputSize((fw, fh))
                    _, faces = det.detect(frame)
                except Exception as e:
                    log.debug("[reframe] detect 失敗 @%.1fs: %s", t, e)
                    continue
                if faces is not None and len(faces):
                    b = faces[int(np.argmax(faces[:, 2] * faces[:, 3]))]
                    cx = (float(b[0]) + float(b[2]) / 2) / fw
                    cxs.append(min(1.0, max(0.0, cx)))
            if cxs:
                h.reframe_cx = round(float(np.median(cxs)), 4)
                got += 1
    finally:
        cap.release()
    log.debug("[reframe] 說話者追蹤 %d/%d 個高光成功定位臉部中心。", got, len(highlights))


# ── local B-roll cutaways ────────────────────────────────────────────────────
def plan_broll(ctx: AnalysisContext, highlights: List[Highlight], tracks: List[SignalTrack],
               out_dir=None) -> None:
    """Drop one cutaway per highlight over its quietest span, sourced from the
    *most visually-dynamic* elsewhere-moment of the same video (relevance-ranked,
    not round-robin — so a cutaway is the strongest available B-roll, and no two
    clips reuse the same one until the pool is exhausted). The low-activity span is
    found with a threshold relative to each clip's own tempo, so lulls are located
    in busy and calm clips alike (a uniformly busy clip whose activity never dips
    still gets skipped — there's nothing to cover). When the generative provider is
    configured, synthesise the top clips' cutaways from a text prompt instead
    (falling back to the local cutaway on any failure)."""
    log = get_logger(__name__)
    g = ctx.grid
    motion = _track(tracks, "visual_motion", g.n)
    scene = _track(tracks, "visual_scene", g.n)
    if motion is None:
        log.debug("[broll] 無 visual_motion 訊號 → 略過 B-roll。")
        return
    interest = motion if scene is None else np.maximum(motion, scene)

    # candidate pool: visually-dynamic bins NOT inside any highlight, ranked by
    # interest (strongest first) and spaced ≥6s apart so cutaways stay varied.
    inside = np.zeros(g.n, dtype=bool)
    for h in highlights:
        inside[g.index(h.start):g.index(h.end) + 1] = True
    cand = []
    for i in range(g.n):
        if not inside[i] and interest[i] > 0.5:
            t = g.time(i)
            if all(abs(t - c[0]) >= 6.0 for c in cand):
                cand.append((t, float(interest[i])))
    cand.sort(key=lambda c: c[1], reverse=True)     # strongest cutaway sources first
    pool = [t for t, _ in cand]
    if not pool:
        log.debug("[broll] 找不到高光以外的動態片段當空鏡素材 → 略過。")
        return

    pad = max(1, int(1.0 / g.dt))
    need = max(1, int(2.5 / g.dt))     # ≥2.5s low-activity run
    used: set = set()
    made_local = 0
    for h in highlights:
        i0, i1 = g.index(h.start), g.index(h.end)
        seg = interest[i0:i1 + 1]
        if seg.size < 3:
            continue
        # adaptive lull threshold: clearly below THIS clip's own median activity
        lowthr = max(0.20, float(np.median(seg)) * 0.6)
        run, run_start, best = 0, None, None      # best = (lull_start_t, run_len_bins)
        for i in range(i0 + pad, max(i0 + pad + 1, i1 - pad)):
            if interest[i] < lowthr:
                if run == 0:
                    run_start = i
                run += 1
                if run >= need and (best is None or run > best[1]):
                    best = (g.time(run_start), run)
            else:
                run = 0
        if best is None:
            continue
        src = next((t for t in pool if t not in used), pool[0])
        used.add(src)
        lull_t, lull_bins = best
        at = round(max(0.6, lull_t - h.start + 0.3), 2)          # start just into the lull
        dur = round(min(2.2, max(1.2, lull_bins * g.dt * 0.7)), 2)  # sized to the lull
        if at + dur < (h.end - h.start) - 0.6:                   # leave room before the tail fade
            h.broll = [{"at": at, "dur": dur, "src_start": round(max(0.0, src - 0.5), 2)}]
            made_local += 1
    log.debug("[broll] 規劃 %d 段本地空鏡(素材池 %d 個候選)。", made_local, len(pool))

    # generative B-roll for the top clips (highlights are virality-sorted → top first)
    from pathlib import Path

    from . import broll_gen
    if out_dir and broll_gen.gen_enabled():
        made = 0
        for h in highlights:
            if not h.broll or made >= broll_gen.gen_max():
                continue
            ins = h.broll[0]
            gp = Path(out_dir) / f"broll_gen_{h.index:02d}.mp4"
            path = broll_gen.generate_clip(broll_gen.prompt_for(h), float(ins["dur"]) + 0.6, gp)
            if path:
                ins["gen_path"] = path
                made += 1
                log.info("[broll] 已生成空鏡 clip #%d → %s", h.index, path)
            else:
                log.warning("[broll] 生成式端點失敗 clip #%d → 改用本地空鏡。", h.index)
        if made:
            log.info("[broll] 共生成 %d 段生成式空鏡。", made)
