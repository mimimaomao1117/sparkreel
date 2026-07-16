"""Content moderation & compliance.

Scans each highlight window across three modalities:
  * visual — Amazon Rekognition DetectModerationLabels (backend: moderation=aws),
             mapped to block / blur actions; local backend skips with a notice
  * audio  — spoken profanity from the transcript → mute / bleep
  * text   — transcript + chat scanned for profanity / hate / personal info (PII)

Produces a ModerationReport (auditable findings + status) and an action plan the
editor applies: reject a clip, blur a window, or mute a span. Anything above the
human-review confidence threshold is routed to a reviewer (needs_review) — the
system never silently publishes borderline content.
"""
from __future__ import annotations

import re
from typing import Dict, List, Optional, Tuple

from ..models import Highlight, ModerationFinding, ModerationReport
from ..signals.base import AnalysisContext

# Local lexicons (intentionally small & interpretable; production uses Bedrock
# Guardrails + Rekognition). None of these appear in the demo sample → it passes.
_PROFANITY = ["幹你", "他媽", "靠北", "去死", "fuck", "shit", "bitch", "asshole", "混帳東西"]
_HATE = ["賤畜", "支那", "n1gger", "faggot"]
_PII = [
    (re.compile(r"09\d{2}[- ]?\d{3}[- ]?\d{3}"), "phone"),
    (re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"), "email"),
    (re.compile(r"\b[A-Z][12]\d{8}\b"), "national_id"),
]


def _contains(text: str, words: List[str]) -> Optional[str]:
    low = text.lower()
    for w in words:
        if w.lower() in low:
            return w
    return None


def _scan_text(ctx: AnalysisContext, h: Highlight) -> List[ModerationFinding]:
    findings: List[ModerationFinding] = []
    action = ctx.config.moderation.audio_profanity_action

    for seg in ctx.transcript:
        if seg.start >= h.end or seg.end <= h.start:
            continue
        hit = _contains(seg.text, _PROFANITY)
        if hit:
            findings.append(ModerationFinding(
                t_start=seg.start, t_end=seg.end, modality="audio", label="profanity",
                confidence=0.9, action=action, source="local",
                detail=f"逐字稿出現不雅字詞：{hit}"))
        hate = _contains(seg.text, _HATE)
        if hate:
            findings.append(ModerationFinding(
                t_start=seg.start, t_end=seg.end, modality="text", label="hate_speech",
                confidence=0.95, action="flag", source="local",
                detail=f"疑似仇恨言論：{hate}"))

    for m in ctx.chat:
        if not (h.start <= m.t <= h.end):
            continue
        hit = _contains(m.text, _PROFANITY + _HATE)
        if hit:
            findings.append(ModerationFinding(
                t_start=m.t, t_end=m.t + 1.0, modality="text", label="chat_profanity",
                confidence=0.8, action="flag", source="local",
                detail=f"彈幕不當內容：{hit}"))
        for rx, label in _PII:
            if rx.search(m.text):
                findings.append(ModerationFinding(
                    t_start=m.t, t_end=m.t + 1.0, modality="text", label=f"pii_{label}",
                    confidence=0.85, action="flag", source="local",
                    detail=f"彈幕含個資（{label}）"))
    return findings


def _rekognition_visual(ctx: AnalysisContext, h: Highlight) -> Optional[List[ModerationFinding]]:
    import cv2

    from ..ingest import iter_frames

    client = ctx.aws.client("rekognition")
    if client is None:
        return None
    cfg = ctx.config.moderation
    min_conf = ctx.config.aws.rekognition_min_confidence
    findings: List[ModerationFinding] = []
    next_t = h.start
    try:
        for t, frame in iter_frames(ctx.video_path, target_fps=1.0, max_width=640):
            if t < h.start or t > h.end:
                continue
            if t < next_t:
                continue
            next_t = t + 2.0
            ok, buf = cv2.imencode(".jpg", frame)
            if not ok:
                continue
            resp = client.detect_moderation_labels(
                Image={"Bytes": buf.tobytes()}, MinConfidence=min_conf)
            for lab in resp.get("ModerationLabels", []):
                name = (lab.get("Name") or "").lower().replace(" ", "_")
                conf = float(lab.get("Confidence", 0)) / 100.0
                action = "flag"
                if any(k in name for k in cfg.block_labels):
                    action = "reject"
                elif any(k in name for k in cfg.blur_labels):
                    action = "blur"
                findings.append(ModerationFinding(
                    t_start=t, t_end=t + 2.0, modality="visual", label=name,
                    confidence=conf, action=action, source="rekognition",
                    detail=lab.get("ParentName", "")))
        return findings
    except Exception as e:
        ctx.warn(f"[moderation] Rekognition 審核失敗 ({type(e).__name__}) → 僅套用本地文字審核。")
        return None


def scan(ctx: AnalysisContext, highlights: List[Highlight]) -> ModerationReport:
    findings: List[ModerationFinding] = []
    scanned = ["text", "audio"]
    use_visual_aws = ctx.use_aws("moderation")

    for h in highlights:
        findings += _scan_text(ctx, h)
        if use_visual_aws:
            vf = _rekognition_visual(ctx, h)
            if vf is not None:
                findings += vf
                if "visual" not in scanned:
                    scanned.append("visual")
    if not use_visual_aws:
        ctx.warn("[moderation] 視覺審核需 AWS Rekognition（backend=aws）；目前僅本地文字/語音審核。")

    threshold = ctx.config.moderation.require_human_review_above
    has_reject = any(f.action == "reject" for f in findings)
    needs_review = any(f.confidence >= threshold and f.action in ("flag", "reject") for f in findings)

    if has_reject:
        status = "rejected"
    elif needs_review:
        status = "needs_review"
    elif findings:
        status = "flagged"
    else:
        status = "pass"

    summary = {
        "pass": "全部通過，未偵測到違規內容。",
        "flagged": f"偵測到 {len(findings)} 項需注意內容，已標記但可發布。",
        "needs_review": f"偵測到 {len(findings)} 項內容需人工複審後發布。",
        "rejected": f"偵測到違規內容（{len(findings)} 項），已阻擋發布並送人工處理。",
    }[status]

    return ModerationReport(
        status=status, findings=findings,
        needs_human_review=(status in ("needs_review", "rejected")),
        summary=summary, scanned_modalities=scanned,
    )


def plan_for_window(report: ModerationReport, start: float, end: float) -> Dict:
    """Return an action plan for a specific clip window."""
    overlap = [f for f in report.findings if f.t_start < end and f.t_end > start]
    status = "pass"
    mute_ranges: List[Tuple[float, float]] = []
    for f in overlap:
        if f.action == "reject":
            status = "rejected"
        elif f.action == "blur" and status != "rejected":
            status = "blurred"
        elif f.action in ("mute", "bleep"):
            if status == "pass":
                status = "muted"
            mute_ranges.append((max(0.0, f.t_start - start), max(0.0, f.t_end - start)))
        elif f.action == "flag" and status == "pass":
            status = "flagged"
    return {"status": status, "mute_ranges": mute_ranges,
            "needs_review": any(f.action in ("flag", "reject") for f in overlap)}


def build_mute_filter(mute_ranges: List[Tuple[float, float]]) -> Optional[str]:
    """ffmpeg -af value muting the given clip-relative spans (profanity bleep)."""
    if not mute_ranges:
        return None
    conds = "+".join(f"between(t,{a:.2f},{b:.2f})" for a, b in mute_ranges)
    return f"volume=0:enable='{conds}'"
