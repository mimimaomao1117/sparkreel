"""Web / 平台 ingest：從直播/VOD 網址抓 **影片 + 字幕 + 直播聊天(彈幕)** → 餵進既有 pipeline。

原本 SparkReel 只吃本地檔；本模組把「給網址」也接上（README 流程圖的 🎥直播串流/VOD+彈幕+字幕 門面）：
  - **yt-dlp**（標準工具）：下載影片(mp4) + 字幕(srt) + 直播聊天(live_chat.json) + 中繼資料。涵蓋 YouTube/Twitch/抖音/TikTok…。
  - **browser-act**（雲端 stealth，可選）：yt-dlp 拿不到中繼資料時（JS 渲染/反爬頁面）補抓頁面文字。

產出的 chat.json（`[{t,user,text}]`）與 srt 直接被 `load_chat` / `load_subtitles` 讀取，無需改 pipeline。
不下載影片、只給本地檔的既有流程完全不受影響。
"""
from __future__ import annotations

import glob
import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


def is_url(s: str | os.PathLike) -> bool:
    return str(s).lower().startswith(("http://", "https://"))


@dataclass
class WebSource:
    video: str
    subtitles: Optional[str]
    chat: Optional[str]
    title: str
    duration: float
    url: str


def _ytdlp() -> str:
    return shutil.which("yt-dlp") or os.path.expanduser("~/.local/bin/yt-dlp")


def _run(cmd: list[str], timeout: int = 3600) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


# ---------------------------------------------------------------- YouTube live_chat.json → SparkReel chat
def _parse_live_chat(path: Path, out: Path) -> Optional[str]:
    """把 yt-dlp 的 `*.live_chat.json`(JSONL) 轉成 SparkReel chat json `[{t,user,text}]`。"""
    msgs = []
    try:
        for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            action = (obj.get("replayChatItemAction") or {})
            off = action.get("videoOffsetTimeMsec")
            for act in action.get("actions", []):
                item = ((act.get("addChatItemAction") or {}).get("item") or {})
                r = (item.get("liveChatTextMessageRenderer")
                     or item.get("liveChatPaidMessageRenderer") or {})
                if not r:
                    continue
                runs = (r.get("message") or {}).get("runs", [])
                text = "".join(run.get("text", "") or run.get("emoji", {}).get("shortcuts", [""])[0] for run in runs)
                user = ((r.get("authorName") or {}).get("simpleText", ""))
                t = float(off) / 1000.0 if off is not None else float(r.get("timestampUsec", 0) or 0) / 1e6
                if text:
                    msgs.append({"t": round(t, 3), "user": user, "text": text})
    except OSError:
        return None
    if not msgs:
        return None
    msgs.sort(key=lambda m: m["t"])
    cf = out / "chat.json"
    cf.write_text(json.dumps(msgs, ensure_ascii=False), encoding="utf-8")
    return str(cf)


# ---------------------------------------------------------------- browser-act fallback（頁面中繼資料）
def _browser_extract(url: str) -> str:
    ba = shutil.which("browser-act") or os.path.expanduser("~/.local/bin/browser-act")
    if not os.path.exists(ba):
        return ""
    try:
        r = _run([ba, "stealth-extract", url, "--content-type", "markdown"], timeout=120)
        return (r.stdout or "")[:4000] if r.returncode == 0 else ""
    except Exception:  # noqa: BLE001
        return ""


# ---------------------------------------------------------------- 主入口
def fetch_source(url: str, out_dir: str | Path, *, sub_langs: str = "zh.*,zh-Hant,zh-Hans,en.*,ja.*",
                 want_chat: bool = True, cookies_from_browser: str = "", quiet: bool = True) -> WebSource:
    """從平台 URL 抓 影片+字幕+直播聊天。回傳 WebSource（路徑可直接餵 analyze）。

    cookies_from_browser：如 'chrome'/'firefox'，讓 yt-dlp 帶登入 cookie（會員/私人內容/過反爬）。
    """
    yt = _ytdlp()
    if not (shutil.which("yt-dlp") or os.path.exists(yt)):
        raise RuntimeError("需要 yt-dlp（pip install yt-dlp）才能從網址抓輸入。")
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    base = out / "source"
    # --retries/--extractor-retries 緩解 YouTube 429；三個產物各自獨立抓，互不拖累
    common = ["--no-warnings", "--no-playlist", "--retries", "5", "--extractor-retries", "3"]
    if cookies_from_browser:
        common += ["--cookies-from-browser", cookies_from_browser]

    def _warn(tag: str, cp: subprocess.CompletedProcess) -> None:
        if cp.returncode != 0 and not quiet:
            print(f"[web] yt-dlp {tag} 警告：{(cp.stderr or '').strip().splitlines()[-1:] or ['']}"[:220])

    # 1) 中繼資料
    title, duration = "source", 0.0
    r = _run([yt, "-J", *common, url], timeout=180)
    if r.returncode == 0 and r.stdout.strip():
        try:
            meta = json.loads(r.stdout)
            title = meta.get("title") or title
            duration = float(meta.get("duration") or 0.0)
        except (json.JSONDecodeError, ValueError):
            pass
    if title == "source":  # yt-dlp 拿不到 → browser-act 補（bot-protected 頁）
        txt = _browser_extract(url)
        if txt:
            title = txt.splitlines()[0].lstrip("# ").strip()[:120] or title

    # 2) 影片（獨立一趟：字幕/彈幕的 429 不該拖垮影片）
    rv = _run([yt, "-f", "bv*+ba/b", "--merge-output-format", "mp4",
               "-o", f"{base}.%(ext)s", *common, url])
    _warn("video", rv)
    video = next(iter(sorted(glob.glob(f"{base}.mp4") + glob.glob(f"{base}.mkv")
                             + glob.glob(f"{base}.webm"))), "")

    # 3) 字幕（best-effort，獨立一趟；load_subtitles 同時吃 vtt/srt，故不強制轉檔）
    rs = _run([yt, "--skip-download", "--write-subs", "--write-auto-subs",
               "--sub-langs", sub_langs, "-o", f"{base}.%(ext)s", *common, url])
    _warn("subs", rs)
    subs = next(iter(sorted(glob.glob(f"{base}*.srt") + glob.glob(f"{base}*.vtt"))), None)

    # 4) 直播聊天（彈幕，best-effort，獨立一趟）
    chat = None
    if want_chat:
        rc = _run([yt, "--skip-download", "--write-subs", "--sub-langs", "live_chat",
                   "-o", f"{base}.%(ext)s", *common, url])
        _warn("chat", rc)
        lc = next(iter(sorted(glob.glob(f"{base}*.live_chat.json"))), None)
        if lc:
            chat = _parse_live_chat(Path(lc), out)

    if not video:
        raise RuntimeError(f"yt-dlp 未能下載影片：{url}\n{(rv.stderr or '')[:300]}")
    return WebSource(video=video, subtitles=subs, chat=chat, title=title, duration=duration, url=url)
