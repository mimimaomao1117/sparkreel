"""SparkReel web server (stdlib only — no framework dependency).

Endpoints
  GET  /                       single-page UI
  GET  /api/status             backend / AWS availability + platform specs
  GET  /api/samples            server-side demo inputs
  POST /api/make-sample        generate the demo sample on the fly
  POST /api/upload             raw file upload (body=bytes, header X-Filename)
  POST /api/jobs               start a pipeline job → {id}
  GET  /api/jobs/{id}          poll job state/progress/result
  GET  /media?path=...         range-aware media serving (restricted to cwd)

Jobs run in background threads; the UI polls for progress. Range support lets the
browser <video> seek within produced clips and the montage.
"""
from __future__ import annotations

import json
import mimetypes
import re
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from ..aws.clients import aws_status
from ..config import load_config
from ..ingest import probe
from ..pipeline import run_pipeline
from ..sample import generate as gen_sample
from ..styles import platform_summary


def _config_from_plan(plan: dict):
    """Turn an AI/human editing plan into a Config override for the pipeline."""
    cfg = load_config()
    if not plan:
        return cfg
    try:
        cfg.fusion.peak.max_highlights = int(plan.get("max_clips", cfg.fusion.peak.max_highlights))
        cfg.fusion.peak.min_score = float(plan.get("min_score", cfg.fusion.peak.min_score))
        tl = int(plan.get("target_len", cfg.editing.target_clip_sec))
        cfg.editing.target_clip_sec = tl
        cfg.editing.min_clip_sec = min(cfg.editing.min_clip_sec, max(4.0, tl - 6))
        cfg.editing.max_clip_sec = max(cfg.editing.max_clip_sec, tl + 8)
        w = dict(cfg.fusion.weights)
        emph = plan.get("emphasis", "balanced")
        if emph == "reaction":
            # emotion-centric: lean hard on the FER facial-emotion model + vocal emotion
            w["visual_emotion"] = w.get("visual_emotion", 1.2) * 1.7
            w["visual_expression"] = w.get("visual_expression", 1.1) * 1.4
            w["visual_face"] = w.get("visual_face", 0.7) * 1.2
            w["audio_emotion"] = w.get("audio_emotion", 0.8) * 1.2
        elif emph == "action":
            w["visual_motion"] = w.get("visual_motion", 0.9) * 1.6
            w["visual_scene"] = w.get("visual_scene", 0.6) * 1.4
        elif emph == "loud":
            w["audio_excitement"] = w.get("audio_excitement", 1.0) * 1.5
            w["audio_emotion"] = w.get("audio_emotion", 0.8) * 1.3
        cfg.fusion.weights = w
        cfg.editing.broll = bool(plan.get("broll", cfg.editing.broll))
    except Exception as e:
        from ..log import get_logger
        get_logger(__name__).warning("plan→config 轉換失敗(%s):%s → 用預設設定。",
                                     type(e).__name__, e)
    return cfg


def _retrim(video: str, start: float, end: float, platform: str, captions: bool, out_root: str) -> dict:
    """Re-render a single clip from human-adjusted in/out (timeline fine-tuning)."""
    from types import SimpleNamespace

    from ..editing.engine import render_clip
    from ..models import Highlight
    from ..styles import target_resolution
    cfg = load_config()
    platform = platform if platform in cfg.platform_names else (cfg.platform_names[0] if cfg.platform_names else "tiktok")
    preset = cfg.platform(platform)
    start, end = max(0.0, float(start)), float(end)
    end = max(start + 1.0, end)
    ctx = SimpleNamespace(video_path=video, config=cfg, transcript=[])
    out_dir = Path(out_root) / f"retrim-{uuid.uuid4().hex[:8]}"
    h = Highlight(index=0, start=start, end=end, peak_t=(start + end) / 2,
                  score=0.0, duration=round(end - start, 2))
    v = render_clip(ctx, h, platform, preset, {"hook": ""}, str(out_dir),
                    target=target_resolution(preset), captions_on=bool(captions))
    return {"path": v.path, "duration": v.duration, "thumbnail": v.thumbnail}

STATIC = Path(__file__).parent / "static"
JOBS: dict = {}
LOCK = threading.Lock()


def _start_job(video, chat, subtitles, platforms, montage, out_root, captions=True, plan=None):
    jid = uuid.uuid4().hex[:12]
    with LOCK:
        JOBS[jid] = {"state": "running", "pct": 0.0, "stage": "queued",
                     "msg": "排隊中…", "result": None, "error": None}

    def worker():
        def prog(stage, pct, msg):
            with LOCK:
                JOBS[jid].update(stage=stage, pct=pct, msg=msg)
        try:
            result, out_dir = run_pipeline(
                video, chat_path=chat, subtitle_path=subtitles, platforms=platforms,
                out_root=out_root, config=_config_from_plan(plan),
                make_montage=montage, captions=captions, progress=prog)
            montage_path = next((w.split("montage:", 1)[1] for w in result.warnings
                                 if w.startswith("montage:")), None)
            with LOCK:
                JOBS[jid].update(state="done", pct=1.0, stage="done", job_id=result.job_id,
                                 out_dir=str(out_dir), montage=montage_path,
                                 result=json.loads(result.to_json()))
        except Exception as e:
            with LOCK:
                JOBS[jid].update(state="error", error=f"{type(e).__name__}: {e}")

    threading.Thread(target=worker, daemon=True).start()
    return jid


def _safe_under_cwd(path: str) -> Path | None:
    try:
        p = Path(path)
        rp = (Path.cwd() / p if not p.is_absolute() else p).resolve()
        rp.relative_to(Path.cwd().resolve())
        return rp
    except Exception:
        return None


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):  # quiet
        pass

    # ── helpers ─────────────────────────────────────────────────────────
    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self._write(body)

    def _write(self, data: bytes):
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _read_body(self) -> bytes:
        n = int(self.headers.get("Content-Length", 0) or 0)
        return self.rfile.read(n) if n else b""

    def _serve_static(self, rel: str):
        path = (STATIC / rel).resolve()
        if not str(path).startswith(str(STATIC.resolve())) or not path.is_file():
            self.send_error(404)
            return
        self._serve_file(path)

    def _serve_file(self, path: Path):
        if not path.is_file():
            self.send_error(404)
            return
        ctype = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
        size = path.stat().st_size
        rng = self.headers.get("Range")
        if rng and rng.startswith("bytes="):
            m = re.match(r"bytes=(\d*)-(\d*)", rng)
            start = int(m.group(1)) if m and m.group(1) else 0
            end = int(m.group(2)) if m and m.group(2) else size - 1
            end = min(end, size - 1)
            start = min(start, end)
            length = end - start + 1
            self.send_response(206)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Length", str(length))
            self.end_headers()
            with open(path, "rb") as f:
                f.seek(start)
                self._write(f.read(length))
        else:
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(size))
            self.send_header("Accept-Ranges", "bytes")
            self.end_headers()
            with open(path, "rb") as f:
                self._write(f.read())

    # ── GET ─────────────────────────────────────────────────────────────
    def do_GET(self):
        u = urlparse(self.path)
        p = u.path
        if p == "/" or p == "/studio":
            return self._serve_static("studio.html")
        if p == "/pro" or p == "/index.html":
            return self._serve_static("index.html")
        if p.startswith("/static/"):
            return self._serve_static(p[len("/static/"):])
        if p == "/api/status":
            cfg = load_config()
            try:
                from .agent import planner_available
                planner = planner_available()
            except Exception:
                planner = {"available": False, "provider": None, "label": None}
            return self._json({
                "version": "0.1.0",
                "aws": aws_status(cfg.aws.region),
                "backends": cfg.backends,
                "platforms": platform_summary(cfg),
                "planner": planner,
            })
        if p == "/api/samples":
            return self._json({"samples": self._list_samples()})
        if p.startswith("/api/jobs/"):
            jid = p.rsplit("/", 1)[-1]
            with LOCK:
                job = JOBS.get(jid)
            return self._json(job or {"error": "not found"}, 200 if job else 404)
        if p == "/media":
            qs = parse_qs(u.query)
            target = (qs.get("path") or [""])[0]
            rp = _safe_under_cwd(target)
            if not rp:
                return self.send_error(403)
            return self._serve_file(rp)
        self.send_error(404)

    def _list_samples(self):
        out = []
        for d in ["examples", "."]:
            base = Path(d)
            if not base.exists():
                continue
            for mp4 in sorted(base.glob("*.mp4")):
                chat = next((str(c) for c in [mp4.with_suffix(".chat.jsonl"),
                             mp4.parent / "sample_chat.jsonl"] if c.exists()), None)
                srt = mp4.with_suffix(".srt")
                out.append({"video": str(mp4), "name": mp4.name,
                            "chat": chat, "subtitles": str(srt) if srt.exists() else None})
            break
        return out

    # ── POST ────────────────────────────────────────────────────────────
    def do_POST(self):
        u = urlparse(self.path)
        p = u.path
        if p == "/api/make-sample":
            try:
                paths = gen_sample("examples")
                return self._json({"ok": True, **paths})
            except Exception as e:
                return self._json({"ok": False, "error": str(e)}, 500)
        if p == "/api/upload":
            fname = self.headers.get("X-Filename", "upload.mp4")
            fname = re.sub(r"[^A-Za-z0-9._-]", "_", fname)[-60:] or "upload.mp4"
            updir = Path("assets/uploads")
            updir.mkdir(parents=True, exist_ok=True)
            dest = updir / f"{uuid.uuid4().hex[:8]}_{fname}"
            dest.write_bytes(self._read_body())
            return self._json({"path": str(dest)})
        if p == "/api/jobs":
            try:
                cfg = json.loads(self._read_body() or b"{}")
            except Exception:
                return self._json({"error": "bad json"}, 400)
            video = cfg.get("video")
            if not video or not _safe_under_cwd(video):
                return self._json({"error": "invalid video path"}, 400)
            plan = cfg.get("plan") or {}
            platforms = plan.get("platforms") or cfg.get("platforms")
            captions = bool(plan.get("captions", cfg.get("captions", True)))
            jid = _start_job(
                video, cfg.get("chat"), cfg.get("subtitles"),
                platforms, bool(cfg.get("montage", True)),
                self.server.out_root, captions=captions, plan=plan)
            return self._json({"id": jid})
        if p == "/api/plan":
            try:
                body = json.loads(self._read_body() or b"{}")
            except Exception:
                return self._json({"error": "bad json"}, 400)
            rp = _safe_under_cwd(body.get("video") or "")
            if not rp or not rp.exists():
                return self._json({"error": "invalid video path"}, 400)
            try:
                m = probe(str(rp))
                from .agent import plan_chat
                out = plan_chat({"duration": m.duration, "width": m.width, "height": m.height},
                                body.get("messages") or [])
                return self._json(out)
            except Exception as e:
                return self._json({"error": f"{type(e).__name__}: {e}"}, 500)
        if p == "/api/retrim":
            try:
                body = json.loads(self._read_body() or b"{}")
            except Exception:
                return self._json({"error": "bad json"}, 400)
            rp = _safe_under_cwd(body.get("video") or "")
            if not rp or not rp.exists():
                return self._json({"error": "invalid video path"}, 400)
            try:
                out = _retrim(str(rp), float(body.get("start", 0)), float(body.get("end", 0)),
                              str(body.get("platform", "tiktok")), bool(body.get("captions", False)),
                              self.server.out_root)
                return self._json(out)
            except Exception as e:
                return self._json({"error": f"{type(e).__name__}: {e}"}, 500)
        self.send_error(404)


class Server(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, addr, out_root):
        super().__init__(addr, Handler)
        self.out_root = out_root


def run_server(host="127.0.0.1", port=9999, out_root="assets/output"):
    Path(out_root).mkdir(parents=True, exist_ok=True)
    srv = Server((host, port), out_root)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        srv.shutdown()
