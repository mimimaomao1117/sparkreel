"""Chat / danmaku (彈幕) loader.

Accepts jsonl / json / csv exported from live platforms (Twitch, YouTube Live,
抖音/TikTok Live, 斗魚, etc.). Field names are flexible; timestamps may be numeric
seconds or hh:mm:ss strings. Output is a time-sorted list of ChatMessage.
"""
from __future__ import annotations

import csv
import json
import re
from pathlib import Path
from typing import Any, List

from ..models import ChatMessage

_NUM_RE = re.compile(r"^-?\d+(\.\d+)?$")


def _parse_time(v: Any) -> float:
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip()
    if not s:
        return 0.0
    if _NUM_RE.match(s):
        return float(s)
    if ":" in s:  # hh:mm:ss or mm:ss
        try:
            parts = [float(p) for p in s.split(":")]
        except ValueError:
            return 0.0
        sec = 0.0
        for p in parts:
            sec = sec * 60 + p
        return sec
    return 0.0


def _first(row: dict, *keys: str, default: str = "") -> str:
    for k in keys:
        if k in row and row[k] is not None:
            return str(row[k])
    return default


def load_chat(path: str | Path) -> List[ChatMessage]:
    path = Path(path)
    if not path.exists():
        return []
    ext = path.suffix.lower()
    rows: List[dict] = []

    if ext in (".jsonl", ".ndjson"):
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    elif ext == ".json":
        data = json.loads(path.read_text(encoding="utf-8"))
        rows = data if isinstance(data, list) else data.get("messages", data.get("comments", []))
    elif ext == ".csv":
        with open(path, newline="", encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
    else:
        return []

    msgs: List[ChatMessage] = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        t = r.get("t", r.get("time", r.get("timestamp", r.get("offset", r.get("time_in_seconds", 0)))))
        msgs.append(
            ChatMessage(
                t=_parse_time(t),
                user=_first(r, "user", "author", "nick", "username", "name"),
                text=_first(r, "text", "message", "msg", "content", "comment", "body"),
            )
        )
    msgs.sort(key=lambda m: m.t)
    return msgs
