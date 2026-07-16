"""SparkReel Team Console — a password-gated collaboration hub (stdlib only).

A lightweight web console for the SparkReel team to co-develop the agent:
a shared, persisted chat room plus a built-in project bot that answers live
questions about backends / modules straight from the local config — so the
whole team shares the same context while building.

Auth model
  A single shared team password (see TEAM_PASSWORD / SPARKREEL_CONSOLE_PASSWORD)
  is verified *server-side*; a random opaque session token is issued on success
  and sent back as a Bearer token. No token → no API access. This is a
  team-internal tool, not a public service — keep it on a trusted network.

Endpoints
  GET  /                       single-page console UI
  POST /api/login              {password, name} → {token, name}         (public)
  POST /api/logout             invalidate the current session
  GET  /api/session            validate token → {name}
  GET  /api/context            backends / AWS / modules / online members
  GET  /api/messages?since=N   poll chat (long-ish; returns messages id > N)
  POST /api/messages           {text} → append a message (+ maybe a bot reply)

Chat history persists to a JSONL file so the team shares one timeline that
survives restarts. Everything runs with zero cloud credentials.
"""
from __future__ import annotations

import hmac
import json
import os
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

STATIC = Path(__file__).parent / "static"

#: Shared team password. Set it via env SPARKREEL_CONSOLE_PASSWORD — required for
#: any shared or internet-reachable deployment, because the console can drive a
#: shell (see agent.py). When unset, a random one-time password is generated at
#: startup and printed once, so the published source ships NO usable default.
_ENV_PASSWORD = os.environ.get("SPARKREEL_CONSOLE_PASSWORD")
PASSWORD_IS_GENERATED = not _ENV_PASSWORD
TEAM_PASSWORD = _ENV_PASSWORD or secrets.token_urlsafe(12)

MAX_TEXT = 2000        # per-message character cap
MAX_TASK = 8000        # per agent-task character cap
MAX_NAME = 24          # display-name character cap
ONLINE_WINDOW = 30.0   # seconds since last activity to count as "online"
BOT_NAME = "SparkBot"
AGENT_NAME = "SparkAgent"

# ── shared, thread-guarded state ────────────────────────────────────────────
LOCK = threading.Lock()
SESSIONS: dict[str, dict] = {}   # token -> {name, joined, last_seen}
MESSAGES: list[dict] = []        # [{id, ts, name, kind, text}, ...]
_SEQ = 0                         # monotonic message id
_STORE: Path | None = None       # JSONL persistence path
#: single-flight dev-agent run state (only one task runs at a time)
AGENT: dict = {"busy": False, "actor": None, "task": None, "started": 0.0, "model": None}


# ── persistence ─────────────────────────────────────────────────────────────
def _load_history(store: Path) -> None:
    """Load prior chat history so the team timeline survives restarts."""
    global _SEQ
    if not store.exists():
        return
    for line in store.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            m = json.loads(line)
        except Exception:
            continue
        MESSAGES.append(m)
        _SEQ = max(_SEQ, int(m.get("id", 0)))


def _append(name: str, text: str, kind: str = "user") -> dict:
    """Create a message, persist it, return it. Caller must hold LOCK."""
    global _SEQ
    _SEQ += 1
    msg = {"id": _SEQ, "ts": time.time(), "name": name, "kind": kind, "text": text}
    MESSAGES.append(msg)
    if _STORE is not None:
        try:
            with _STORE.open("a", encoding="utf-8") as f:
                f.write(json.dumps(msg, ensure_ascii=False) + "\n")
        except Exception:
            pass  # a chat log write must never take the server down
    return msg


def _online() -> list[str]:
    now = time.time()
    seen: dict[str, float] = {}
    for s in SESSIONS.values():
        seen[s["name"]] = max(seen.get(s["name"], 0.0), s["last_seen"])
    return sorted(n for n, t in seen.items() if now - t <= ONLINE_WINDOW)


# ── the in-room project bot ─────────────────────────────────────────────────
_MODULES = [
    ("ingest",     "媒體探測、抽幀、抽音、彈幕 / 字幕載入"),
    ("signals",    "四大多模態訊號抽取（音訊 / 語音 / 彈幕 / 視覺）＋本地與 AWS 轉接"),
    ("fusion",     "多模態融合、峰值偵測、可解釋歸因、病毒潛力評分 0–99 自動排序"),
    ("editing",    "剪輯引擎、淡入淡出、響度正規化、說話者追蹤裁切、B-roll 空鏡、交叉淡化合輯"),
    ("styles",     "平台版型規格（TikTok / Reels / Shorts）"),
    ("moderation", "三模態內容審核與合規行為規劃"),
    ("aws",        "boto3 client 工廠、可用性偵測、優雅降級"),
    ("web",        "Live Demo UI + 本團隊主控台"),
]


def _bot_reply(cmd: str, arg: str) -> str | None:
    """Answer a slash-command from local project state. None = stay silent."""
    cmd = cmd.lower()
    if cmd in ("/help", "/?", "/h"):
        return (
            "可用指令：\n"
            "  /status   — 後端與 AWS 可用性\n"
            "  /modules  — SparkReel 模組地圖\n"
            "  /who      — 目前在線成員\n"
            "  /whoami   — 你的顯示名稱\n"
            "  /roll     — 擲一個決策骰（例：/roll 誰負責剪輯引擎）\n"
            "  /help     — 顯示這份說明\n"
            "非指令訊息只會發給團隊，SparkBot 不會插話。")
    if cmd == "/status":
        try:
            from ..aws.clients import aws_status
            from ..config import load_config
            cfg = load_config()
            st = aws_status(cfg.aws.region)
            lines = [f"AWS：boto3={st['boto3']} · credentials={st['credentials']} · "
                     f"region={st['region']} → mode={st['mode']}", "能力後端："]
            for cap, be in cfg.backends.items():
                eff = be if (be == "local" or st["credentials"]) else "local(降級)"
                lines.append(f"  {cap:16s}= {be:6s} → {eff}")
            return "\n".join(lines)
        except Exception as e:  # never crash the room over a status probe
            return f"讀取狀態失敗：{type(e).__name__}: {e}"
    if cmd == "/modules":
        return "SparkReel 模組地圖（src/sparkreel/）：\n" + "\n".join(
            f"  {name:11s}— {desc}" for name, desc in _MODULES)
    if cmd == "/who":
        online = _online()
        return "目前在線：" + ("、".join(online) if online else "（無）")
    if cmd == "/whoami":
        return None  # handled by caller (needs the requester's name)
    if cmd == "/roll":
        opts = [o for o in arg.replace("，", ",").replace("、", ",").split(",") if o.strip()]
        if len(opts) >= 2:
            pick = opts[secrets.randbelow(len(opts))].strip()
            return f"決定：{pick}"
        n = secrets.randbelow(6) + 1
        return f"擲出 {n}" + (f"（{arg.strip()}）" if arg.strip() else "")
    return f"未知指令：{cmd}　輸入 /help 看看有哪些指令。"


# ── dev-agent runner ─────────────────────────────────────────────────────────
def _start_agent(task: str, actor: str) -> None:
    """Run one agent task in a background thread, streaming steps into the room.
    AGENT['busy'] must already be set True by the caller (under LOCK)."""
    def prog(kind: str, text: str) -> None:
        with LOCK:
            _append(AGENT_NAME, text, kind=kind if kind in ("tool", "summary") else "agent")

    def worker() -> None:
        with LOCK:
            _append(actor, task, kind="user")
        try:
            from .agent import context_summary
            ctx = context_summary()
        except Exception:
            ctx = {"has_context": False}
        with LOCK:
            lr = ctx.get("last_run") if ctx.get("has_context") else None
            if lr:
                _append("系統", f"帶著脈絡開始（已存 {ctx.get('run_count', 0)} 輪·上一輪「{lr.get('request', '')[:40]}」）", kind="system")
            _append("系統", f"{AGENT_NAME} 開始處理…", kind="system")
        try:
            from .agent import run_task
            result = run_task(task, actor, prog)
        except Exception as e:  # importing/starting the agent must not wedge the room
            result = {"ok": False, "error": f"{type(e).__name__}: {e}", "provider": None, "model": None}
        with LOCK:
            rid = str(result.get("run_id", ""))[:8]
            tag = "接續 " if result.get("resumed") else ""
            if result.get("ok"):
                _append("系統", f"{AGENT_NAME} 完成 · {tag}run {rid}（{result.get('provider')} · {result.get('model')}）", kind="system")
            else:
                _append("系統", f"{AGENT_NAME} 中止：{result.get('error')}", kind="system")
            AGENT.update(busy=False, actor=None, task=None, model=None)

    threading.Thread(target=worker, daemon=True).start()


# ── HTTP handler ─────────────────────────────────────────────────────────────
class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):  # quiet
        pass

    # helpers ----------------------------------------------------------------
    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _read_json(self) -> dict:
        n = int(self.headers.get("Content-Length", 0) or 0)
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(n) or b"{}")
        except Exception:
            return {}

    def _auth(self) -> dict | None:
        """Return the live session for a valid Bearer token, refreshing last_seen."""
        h = self.headers.get("Authorization", "")
        token = h[7:] if h.startswith("Bearer ") else ""
        if not token:
            return None
        with LOCK:
            s = SESSIONS.get(token)
            if s:
                s["last_seen"] = time.time()
        return s

    def _serve_console(self):
        path = STATIC / "console.html"
        if not path.is_file():
            return self.send_error(404)
        body = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    # GET --------------------------------------------------------------------
    def do_GET(self):
        u = urlparse(self.path)
        p = u.path
        if p in ("/", "/index.html", "/console"):
            return self._serve_console()
        if p == "/api/session":
            s = self._auth()
            return self._json({"name": s["name"]} if s else {"error": "unauthorized"},
                              200 if s else 401)
        if p == "/api/context":
            if not self._auth():
                return self._json({"error": "unauthorized"}, 401)
            return self._json(self._context())
        if p == "/api/messages":
            if not self._auth():
                return self._json({"error": "unauthorized"}, 401)
            since = 0
            try:
                since = int((parse_qs(u.query).get("since") or ["0"])[0])
            except ValueError:
                pass
            with LOCK:
                msgs = [m for m in MESSAGES if m["id"] > since]
                online = _online()
                agent = {"busy": AGENT["busy"], "actor": AGENT["actor"], "model": AGENT["model"]}
            return self._json({"messages": msgs, "online": online, "last": _SEQ, "agent": agent})
        return self.send_error(404)

    def _context(self) -> dict:
        aws = {"credentials": False, "mode": "local", "region": None, "boto3": False}
        backends: dict = {}
        try:
            from ..aws.clients import aws_status
            from ..config import load_config
            cfg = load_config()
            aws = aws_status(cfg.aws.region)
            backends = cfg.backends
        except Exception:
            pass
        try:
            from .agent import available as agent_available, context_summary
            agent = agent_available()
            agent["context"] = context_summary()
        except Exception:
            agent = {"anthropic": False, "openai": False, "shell": False, "models": {}}
        with LOCK:
            online = _online()
            total = len(MESSAGES)
        return {"aws": aws, "backends": backends,
                "modules": [{"name": n, "desc": d} for n, d in _MODULES],
                "online": online, "message_count": total, "agent": agent}

    # POST -------------------------------------------------------------------
    def do_POST(self):
        p = urlparse(self.path).path
        if p == "/api/login":
            return self._login()
        if p == "/api/logout":
            h = self.headers.get("Authorization", "")
            token = h[7:] if h.startswith("Bearer ") else ""
            with LOCK:
                SESSIONS.pop(token, None)
            return self._json({"ok": True})
        if p == "/api/messages":
            return self._post_message()
        if p == "/api/agent":
            return self._post_agent()
        return self.send_error(404)

    def _login(self):
        data = self._read_json()
        password = str(data.get("password", ""))
        name = str(data.get("name", "")).strip()[:MAX_NAME] or "訪客"
        # constant-time compare so a wrong password can't be timed out char-by-char
        if not hmac.compare_digest(password, TEAM_PASSWORD):
            return self._json({"error": "密碼錯誤"}, 401)
        token = secrets.token_urlsafe(24)
        now = time.time()
        with LOCK:
            SESSIONS[token] = {"name": name, "joined": now, "last_seen": now}
            join = _append("系統", f"{name} 加入了主控台", kind="system")
        return self._json({"token": token, "name": name, "joined_id": join["id"]})

    def _post_message(self):
        s = self._auth()
        if not s:
            return self._json({"error": "unauthorized"}, 401)
        data = self._read_json()
        text = str(data.get("text", "")).strip()
        if not text:
            return self._json({"error": "empty"}, 400)
        text = text[:MAX_TEXT]
        with LOCK:
            msg = _append(s["name"], text, kind="user")
            replies = []
            if text.startswith("/"):
                parts = text[1:].split(None, 1)
                cmd = "/" + (parts[0] if parts else "")
                arg = parts[1] if len(parts) > 1 else ""
                if cmd.lower() == "/whoami":
                    reply = f"你是 {s['name']}"
                else:
                    reply = _bot_reply(cmd, arg)
                if reply is not None:
                    replies.append(_append(BOT_NAME, reply, kind="bot"))
        return self._json({"message": msg, "replies": replies})

    def _post_agent(self):
        s = self._auth()
        if not s:
            return self._json({"error": "unauthorized"}, 401)
        data = self._read_json()
        task = str(data.get("text", "")).strip()[:MAX_TASK]
        if not task:
            return self._json({"error": "empty"}, 400)
        with LOCK:
            if AGENT["busy"]:
                return self._json({"error": f"{AGENT_NAME} 正忙著處理 {AGENT['actor']} 的任務，請稍候。",
                                   "busy": True}, 409)
            AGENT.update(busy=True, actor=s["name"], task=task, started=time.time(), model=None)
        _start_agent(task, s["name"])
        return self._json({"ok": True})


class Server(ThreadingHTTPServer):
    daemon_threads = True


def run_console(host: str = "127.0.0.1", port: int = 9998,
                store: str = "assets/console/chat.jsonl") -> None:
    """Launch the team console server (blocking)."""
    global _STORE
    _STORE = Path(store)
    _STORE.parent.mkdir(parents=True, exist_ok=True)
    with LOCK:
        _load_history(_STORE)
    srv = Server((host, port), Handler)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        srv.shutdown()
