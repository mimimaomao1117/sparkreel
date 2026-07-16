"""Synthetic demo stream generator (shared by CLI `make-sample` and examples/).

Builds a self-contained live-stream sample from ffmpeg lavfi sources — no
external footage needed. Emits video + danmaku (.jsonl) + transcript (.srt) with
calm / talk / hype segments; the hype windows are where highlights should fire.
"""
from __future__ import annotations

import json
import random
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Dict, List, Tuple

W, H, FPS = 1280, 720, 30
TIMELINE: List[Tuple[str, int]] = [
    ("calm", 8), ("hype", 6), ("talk", 10), ("calm", 6),
    ("hype", 7), ("talk", 9), ("calm", 6), ("hype", 8), ("calm", 10),
]

HYPE_MSGS = ["太扯了吧！！", "這波超猛 6666", "神啊 GGGG", "笑死 哈哈哈哈", "臥槽這操作",
             "太強了太強了", "amazing!!!", "clip it clip it", "重播重播", "666666",
             "這也太神", "我起雞皮疙瘩了", "封神了", "教科書等級", "醒醒這是直播欸",
             "破防了啦", "史詩級", "🔥🔥🔥🔥", "讚啦讚啦", "這段一定要剪"]
TALK_MSGS = ["主播今天狀態不錯", "這張地圖我熟", "等等要喝水", "剛剛那句好好笑",
             "solo 一下啦", "版本答案", "你各位安安", "訂閱一下謝謝"]
CALM_MSGS = ["安", "路過", "hi", "第一次看", "簽到", "喔喔", "在幹嘛"]
HYPE_SPEECH = ["喔喔喔這波太神了！", "五殺！這操作直接封神！", "太扯了吧我起雞皮疙瘩", "重播重播這一定要剪！"]
TALK_SPEECH = ["接下來我們看一下這個版本的打法", "這張地圖的節奏其實蠻重要的", "大家記得訂閱一下謝謝", "剛剛那波其實可以再穩一點"]
CALM_SPEECH = ["嗯讓我想一下", "喝口水先", "我看看喔"]


def _run(cmd: List[str]) -> None:
    p = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    if p.returncode != 0:
        raise RuntimeError(p.stderr.decode("utf-8", "ignore")[-1200:])


def _video_source(kind: str, dur: int) -> Tuple[str, List[str]]:
    if kind == "hype":
        return f"testsrc2=s={W}x{H}:r={FPS}:d={dur}", ["hue=s=2"]
    if kind == "talk":
        src = f"color=c=0x0d1b2a:s={W}x{H}:r={FPS}:d={dur}"
        return src, [f"drawbox=x='mod(t*160\\,{W})':y={H // 2 - 130}:w=180:h=260:color=teal@0.95:t=fill"]
    return f"color=c=0x10233a:s={W}x{H}:r={FPS}:d={dur}", []


def _audio_source(kind: str, dur: int) -> Tuple[str, List[str]]:
    if kind == "hype":
        return f"anoisesrc=color=pink:d={dur}:amplitude=1", ["volume=0.7"]
    if kind == "talk":
        return f"sine=frequency=330:d={dur}", ["tremolo=f=5:d=0.8", "volume=0.22"]
    return f"sine=frequency=210:d={dur}", ["volume=0.05"]


def _render_segment(kind: str, dur: int, out: Path) -> None:
    vsrc, vf = _video_source(kind, dur)
    asrc, af = _audio_source(kind, dur)
    cmd = ["ffmpeg", "-y", "-f", "lavfi", "-i", vsrc, "-f", "lavfi", "-i", asrc]
    if vf:
        cmd += ["-vf", ",".join(vf)]
    if af:
        cmd += ["-af", ",".join(af)]
    cmd += ["-t", str(dur), "-r", str(FPS), "-c:v", "libx264", "-preset", "veryfast",
            "-pix_fmt", "yuv420p", "-c:a", "aac", "-ar", "44100", "-ac", "2", "-shortest", str(out)]
    _run(cmd)


def _overlay(src: Path, dst: Path) -> bool:
    font = next((c for c in [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    ] if Path(c).exists()), None)
    if not font:
        return False
    title = (f"drawtext=fontfile={font}:text='SparkReel DEMO STREAM':x=24:y=24:"
             "fontsize=30:fontcolor=white:box=1:boxcolor=black@0.45:boxborderw=10")
    clock = (f"drawtext=fontfile={font}:text='%{{pts\\:hms}}':x=w-tw-24:y=24:"
             "fontsize=30:fontcolor=yellow:box=1:boxcolor=black@0.45:boxborderw=10")
    try:
        _run(["ffmpeg", "-y", "-i", str(src), "-vf", f"{title},{clock}",
              "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p", "-c:a", "copy", str(dst)])
        return True
    except Exception:
        return False


def _make_chat(path: Path, seed: int = 7) -> None:
    rng = random.Random(seed)
    msgs, t = [], 0.0
    for kind, dur in TIMELINE:
        pool, n = ({"hype": (HYPE_MSGS, rng.randint(22, 34)),
                    "talk": (TALK_MSGS, rng.randint(6, 10)),
                    "calm": (CALM_MSGS, rng.randint(2, 4))})[kind]
        for _ in range(n):
            off = rng.uniform(0.5, dur) if kind == "hype" else rng.uniform(0, dur)
            msgs.append({"t": round(t + off, 2), "user": f"viewer{rng.randint(1000, 9999)}", "text": rng.choice(pool)})
        t += dur
    msgs.sort(key=lambda m: m["t"])
    with open(path, "w", encoding="utf-8") as f:
        for m in msgs:
            f.write(json.dumps(m, ensure_ascii=False) + "\n")


def _srt_ts(sec: float) -> str:
    h, m, s = int(sec // 3600), int((sec % 3600) // 60), int(sec % 60)
    return f"{h:02d}:{m:02d}:{s:02d},{int(round((sec - int(sec)) * 1000)):03d}"


def _make_srt(path: Path, seed: int = 8) -> None:
    rng = random.Random(seed)
    cues, t = [], 0.0
    for kind, dur in TIMELINE:
        pool = {"hype": HYPE_SPEECH, "talk": TALK_SPEECH, "calm": CALM_SPEECH}[kind]
        n = 2 if kind in ("hype", "talk") else 1
        for j in range(n):
            start = t + (j + 0.3) * dur / (n + 0.5)
            end = min(t + dur, start + rng.uniform(2.0, 3.2))
            cues.append((start, end, rng.choice(pool)))
        t += dur
    path.write_text("\n".join(
        f"{i}\n{_srt_ts(a)} --> {_srt_ts(b)}\n{txt}\n" for i, (a, b, txt) in enumerate(cues, 1)
    ), encoding="utf-8")


def generate(out_dir: str, name: str = "sample_stream") -> Dict[str, str]:
    """Generate the sample; returns paths {video, chat, srt}."""
    if not shutil.which("ffmpeg"):
        raise RuntimeError("需要 ffmpeg，請先安裝。")
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    video_out = out / f"{name}.mp4"
    chat_out = out / "sample_chat.jsonl"
    srt_out = video_out.with_suffix(".srt")

    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        segs = []
        for i, (kind, dur) in enumerate(TIMELINE):
            seg = td / f"seg_{i:02d}.mp4"
            _render_segment(kind, dur, seg)
            segs.append(seg)
        listf = td / "list.txt"
        listf.write_text("".join(f"file '{p}'\n" for p in segs))
        raw = td / "raw.mp4"
        _run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(listf), "-c", "copy", str(raw)])
        if not _overlay(raw, video_out):
            shutil.copy(raw, video_out)

    _make_chat(chat_out)
    _make_srt(srt_out)
    return {"video": str(video_out), "chat": str(chat_out), "srt": str(srt_out)}
