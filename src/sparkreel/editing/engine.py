"""ffmpeg render engine: cut → 9:16 reframe → burn captions → hook overlay.

Design notes:
  * Accurate seek (`-ss` as an *output* option after `-i`) so cut points and
    caption timing stay frame-accurate regardless of GOP size.
  * A filter fallback ladder guarantees a clip is always produced: if caption
    burn-in or the hook overlay fails on some environment, it degrades to a
    plain reframed cut instead of crashing the whole job.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from ..ingest import probe
from ..ingest.media import FFmpegError, run_ff
from ..models import Clip, ClipVariant, Highlight
from ..signals.base import AnalysisContext
from ..styles import clamp_duration, target_resolution
from . import captions, narrative

CJK_FONT_FILE = "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc"

# Color emoji have no glyph in the CJK font → they burn in as tofu boxes.
# Strip them from on-screen text (they stay in the social-caption metadata).
_EMOJI = re.compile(
    "[\U0001F000-\U0001FAFF\U00002600-\U000027BF\U00002B00-\U00002BFF"
    "\U0001F1E6-\U0001F1FF\U0000FE00-\U0000FE0F\U00002190-\U000021FF]",
    flags=re.UNICODE,
)


def _strip_emoji(text: str) -> str:
    return _EMOJI.sub("", text).strip()


def _reframe(W: int, H: int, cx: float = 0.5) -> str:
    # crop to 9:16 then scale; cx (0..1) shifts the crop horizontally to follow
    # the speaker (cx=0.5 → centred, identical to the old behaviour)
    x = (rf"clip({cx:.4f}*iw-ow/2\,0\,iw-ow)" if abs(cx - 0.5) > 1e-3 else r"(iw-ow)/2")
    return (
        rf"crop=min(iw\,ih*9/16):min(ih\,iw*16/9):{x}:(ih-oh)/2,"
        f"scale={W}:{H},setsar=1"
    )


def _render_broll(ctx, h, out: Path, reframe: str, vfade: str, af_full: str, dur: float, ins: dict) -> None:
    """Overlay a short video-only cutaway (from elsewhere in the source) over the
    clip, keeping the clip's own audio. Raises FFmpegError on failure (caller falls
    back to the plain render)."""
    at = float(ins.get("at", 1.0))
    bd = float(ins.get("dur", 1.4))
    gen = ins.get("gen_path")
    if gen and Path(gen).exists():          # generative B-roll: loop the synth clip
        second_in = ["-stream_loop", "-1", "-t", f"{bd:.2f}", "-i", str(gen)]
    else:                                   # local B-roll: cutaway from the source
        second_in = ["-ss", f"{float(ins.get('src_start', 0.0))}", "-t", f"{bd:.2f}", "-i", ctx.video_path]
    fc = (f"[0:v]{reframe},setpts=PTS-STARTPTS,{vfade}[mainv];"
          f"[1:v]{reframe},setpts=PTS-STARTPTS+{at}/TB[bv];"
          f"[mainv][bv]overlay=enable='between(t,{at:.2f},{at + bd:.2f})':eof_action=pass[outv]")
    cmd = (["ffmpeg", "-y", "-ss", f"{h.start}", "-t", f"{dur}", "-i", ctx.video_path]
           + second_in + ["-filter_complex", fc, "-map", "[outv]", "-map", "0:a:0?"])
    if af_full:
        cmd += ["-af", af_full]
    cmd += _encode(out)
    run_ff(cmd)


def _hook_filter(hook_txt: Path) -> str:
    font = CJK_FONT_FILE if Path(CJK_FONT_FILE).exists() else ""
    fontarg = f"fontfile={font}:" if font else ""
    return (
        f"drawtext={fontarg}textfile={hook_txt}:x=(w-tw)/2:y=h*0.10:"
        "fontsize=56:fontcolor=white:borderw=6:bordercolor=black:"
        "box=1:boxcolor=black@0.35:boxborderw=18:line_spacing=6:"
        r"enable=lt(t\,2.6)"
    )


def _encode(out: Path) -> List[str]:
    # CRF-based visually-lossless-ish encode (crisper than the old default bitrate),
    # social-standard stereo AAC, +faststart for instant web playback.
    return [
        "-c:v", "libx264", "-preset", "fast", "-crf", "20", "-pix_fmt", "yuv420p",
        "-profile:v", "high", "-r", "30",
        "-c:a", "aac", "-b:a", "160k", "-ar", "48000", "-ac", "2",
        "-movflags", "+faststart", str(out),
    ]


def render_clip(
    ctx: AnalysisContext,
    h: Highlight,
    platform_key: str,
    preset,
    meta: Dict,
    out_dir: str,
    target: Tuple[int, int] = (1080, 1920),
    audio_filter: Optional[str] = None,
    captions_on: bool = True,
) -> ClipVariant:
    W, Ht = target
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    base = f"clip{h.index:02d}_{platform_key}"
    out = out_dir / f"{base}.mp4"
    thumb = out_dir / f"{base}.jpg"
    ass = out_dir / f"{base}.ass"
    hook_txt = out_dir / f"{base}.hook.txt"

    reframe = _reframe(W, Ht, getattr(h, "reframe_cx", 0.5))  # speaker-tracking crop
    # PTS reset so that clip-relative caption times & hook enable-expr line up.
    # Combined with input-side seek (-ss before -i) this is frame-accurate.
    reset = "setpts=PTS-STARTPTS"
    dur = round(h.end - h.start, 2)
    fd = min(0.4, max(0.15, ctx.config.editing.crossfade_sec))
    fout = max(0.0, dur - fd)
    # gentle fade in/out top-and-tail every clip so it never hard-cuts on/off
    vfade = f"fade=t=in:st=0:d={fd:.2f},fade=t=out:st={fout:.2f}:d={fd:.2f}"
    # audio: [moderation mute →] normalise loudness to the −14 LUFS social target
    # → matching gentle fades, so clips play back at a consistent, punchy level
    af_full = ",".join(([audio_filter] if audio_filter else []) + [
        "loudnorm=I=-14:TP=-1.5:LRA=11",
        f"afade=t=in:st=0:d={fd:.2f}", f"afade=t=out:st={fout:.2f}:d={fd:.2f}",
    ])

    if captions_on:
        captions.write_ass(str(ass), ctx.transcript, h.start, h.end, preset.caption_style, (W, Ht))
        hook_txt.write_text(_strip_emoji(meta.get("hook", "")), encoding="utf-8")
        subs = f"subtitles=filename={ass}"
        hook = _hook_filter(hook_txt)
        vbase = [[reframe, reset, subs, hook], [reframe, reset, subs], [reframe, reset, hook]]
    else:
        # clean cut: reframed only, no burned-in text (for human review)
        vbase = [[reframe, reset]]

    # each attempt = (video chain, audio chain); ladder degrades to a bare
    # reframed cut so a clip is always produced even if a fancy filter fails.
    attempts = [(",".join(v + [vfade]), af_full) for v in vbase]
    if captions_on:
        attempts.append((",".join([reframe, reset, vfade]), af_full))
    attempts.append((reframe, audio_filter))

    # optional B-roll cutaway (video-only, keeps the clip's audio) — best-effort,
    # falls straight through to the plain render ladder if it fails.
    produced = False
    broll = getattr(h, "broll", None)
    if broll and not captions_on:
        try:
            _render_broll(ctx, h, out, reframe, vfade, af_full, dur, broll[0])
            produced = True
        except FFmpegError:
            produced = False

    last_err: Optional[Exception] = None
    if not produced:
        for vf, af in attempts:
            cmd = ["ffmpeg", "-y", "-ss", f"{h.start}", "-i", ctx.video_path, "-t", f"{dur}",
                   "-vf", vf, "-map", "0:v:0", "-map", "0:a:0?"]
            if af:
                cmd += ["-af", af]
            cmd += _encode(out)
            try:
                run_ff(cmd)
                last_err = None
                break
            except FFmpegError as e:
                last_err = e
        if last_err is not None:
            raise last_err

    tpeak = max(h.start, min(h.end - 0.1, h.peak_t))
    thumb_path = ""
    try:
        run_ff(["ffmpeg", "-y", "-ss", f"{tpeak}", "-i", ctx.video_path, "-frames:v", "1",
                "-update", "1", "-vf", reframe, str(thumb)])
        thumb_path = str(thumb)
    except FFmpegError:
        pass

    info = probe(out)
    return ClipVariant(
        platform=platform_key, platform_label=preset.label, aspect=preset.aspect,
        path=str(out), thumbnail=thumb_path, duration=round(info.duration, 2),
        width=info.width, height=info.height, size_bytes=info.size_bytes,
        caption_style=preset.caption_style,
    )


def produce_clip(
    ctx: AnalysisContext,
    h: Highlight,
    platform_keys: List[str],
    out_dir: str,
    audio_filter: Optional[str] = None,
    captions_on: bool = True,
) -> Clip:
    base = narrative.generate_base(ctx, h, platform_keys[0])
    first_preset = ctx.config.platform(platform_keys[0])
    variants: List[ClipVariant] = []
    for pk in platform_keys:
        preset = ctx.config.platform(pk)
        target = target_resolution(preset)
        s, e = clamp_duration(h.start, h.end, preset)
        hh = h if (s == h.start and e == h.end) else h.model_copy(
            update={"start": s, "end": e, "duration": round(e - s, 2)}
        )
        meta = {
            "title": base["title"],
            "hook": narrative.hook_for(h, preset.hook_style),
            "hashtags": base.get("hashtags", []),
        }
        variants.append(
            render_clip(ctx, hh, pk, preset, meta, out_dir, target=target,
                        audio_filter=audio_filter, captions_on=captions_on)
        )

    return Clip(
        highlight_index=h.index, narrative_role="standalone",
        title=base["title"], hook=narrative.hook_for(h, first_preset.hook_style),
        description=base.get("description", ""), hashtags=base.get("hashtags", []),
        score=h.score, quality_score=narrative.predict_quality(h, ctx.config),
        variants=variants,
    )


def render_montage(variant_paths: List[str], out_path: str, crossfade: float = 0.4) -> Optional[str]:
    """Splice per-highlight clips into one 精華合輯 with smooth crossfades."""
    paths = [p for p in variant_paths if p and Path(p).exists()]
    if not paths:
        return None
    out_path = Path(out_path)

    def _hardcut() -> str:
        listf = out_path.with_suffix(".concat.txt")
        listf.write_text("".join(f"file '{Path(p).resolve()}'\n" for p in paths), encoding="utf-8")
        try:
            run_ff(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(listf),
                    "-c", "copy", str(out_path)])
        except FFmpegError:
            run_ff(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(listf)] + _encode(out_path))
        return str(out_path)

    if len(paths) < 2:
        return _hardcut()
    try:
        durs = [probe(p).duration for p in paths]
        d = crossfade
        inputs: List[str] = []
        for p in paths:
            inputs += ["-i", p]
        fc: List[str] = []
        vlab, alab, cum = "[0:v]", "[0:a]", 0.0
        for i in range(1, len(paths)):
            cum += durs[i - 1]
            off = max(0.0, cum - i * d)
            vout, aout = f"[v{i}]", f"[a{i}]"
            fc.append(f"{vlab}[{i}:v]xfade=transition=fade:duration={d}:offset={off:.2f}{vout}")
            fc.append(f"{alab}[{i}:a]acrossfade=d={d}{aout}")
            vlab, alab = vout, aout
        run_ff(["ffmpeg", "-y"] + inputs + ["-filter_complex", ";".join(fc),
                "-map", vlab, "-map", alab] + _encode(out_path))
        return str(out_path)
    except FFmpegError:
        return _hardcut()
