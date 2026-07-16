"""SparkReel dev-agent engine — a multi-provider coding agent for the team console.

The team can ask an AI to actually develop SparkReel: it reads, writes, and edits
files under the project root and runs shell commands, in a manual tool-use loop.
Two providers are supported and the caller (or a light router) picks the model:

  • Anthropic Claude  (default: claude-opus-4-8, adaptive thinking, effort=high)
  • OpenAI ChatGPT     (default: env OPENAI_MODEL or gpt-5)

API keys are read from the environment (ANTHROPIC_API_KEY / OPENAI_API_KEY) and
never leave the server. The official SDKs (`anthropic`, `openai`) are optional
imports — the console runs fine without them; the agent simply reports itself
unavailable until the relevant key + SDK is present.

Safety (this exposes file-write + shell to whoever holds the team password):
  • every file op is confined to the project root (path-traversal rejected)
  • run_bash blocks a denylist of catastrophic commands, runs with a timeout,
    caps output, and can be disabled entirely (SPARKREEL_AGENT_SHELL=0)
  • every tool call is appended to assets/console/agent_audit.jsonl
It is a guardrail, not a sandbox — keep the console on a trusted network and use
a strong SPARKREEL_CONSOLE_PASSWORD.
"""
from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import subprocess
import time
from pathlib import Path

from ..log import get_logger

# ── configuration ────────────────────────────────────────────────────────────
ROOT = Path.cwd().resolve()
CLAUDE_MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-opus-4-8")
OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "gpt-5")
#: Amazon Bedrock — Claude with no API key: auth comes from the environment /
#: shared config / EC2 instance IAM role via boto3. This is the backend the agent
#: uses once SparkReel is deployed on EC2 (no logged-in CLI, no API key there).
AWS_REGION = (os.environ.get("SPARKREEL_AWS_REGION") or os.environ.get("AWS_REGION")
              or os.environ.get("AWS_DEFAULT_REGION") or "us-east-1")
#: Bedrock model id, or a cross-region inference-profile id for newer Claude
#: (e.g. "us.anthropic.claude-sonnet-4-...-v1:0"). Override per-deployment.
BEDROCK_MODEL = os.environ.get("SPARKREEL_BEDROCK_MODEL",
                               "anthropic.claude-3-5-sonnet-20241022-v2:0")
#: backend: "cli" drives the user's own logged-in `claude`/`codex` CLIs (no API
#: key, uses their subscription); "api" uses the SDKs + API keys; "bedrock" uses
#: Amazon Bedrock via the instance role; "auto" prefers cli → api → bedrock.
CLAUDE_BIN, CODEX_BIN = "claude", "codex"
CLI_PERMISSION = os.environ.get("SPARKREEL_CLAUDE_PERMISSION", "acceptEdits")
CLI_TIMEOUT = int(os.environ.get("SPARKREEL_AGENT_CLI_TIMEOUT", "900"))
SHELL_ENABLED = os.environ.get("SPARKREEL_AGENT_SHELL", "1") != "0"
MAX_STEPS = int(os.environ.get("SPARKREEL_AGENT_MAX_STEPS", "40"))
BASH_TIMEOUT = int(os.environ.get("SPARKREEL_AGENT_BASH_TIMEOUT", "120"))
_OUT_CAP = 8000            # per tool-result char cap
_READ_CAP = 60_000         # read_file char cap
_AUDIT = ROOT / "assets" / "console" / "agent_audit.jsonl"

SYSTEM_PROMPT = (
    "You are SparkAgent, the in-console coding agent for the SparkReel project "
    "(亮點秒剪 — AI live-stream highlight detection & auto-editing, a stdlib-first "
    "Python codebase). A team collaborates with you through a shared web console to "
    "develop this agent. You can read, write, and edit files under the project root "
    f"({ROOT}) and run shell commands there.\n\n"
    "Work like a careful senior engineer:\n"
    "• Open with a one-line plan so the team knows your intent "
    "before you start touching anything.\n"
    "• Investigate before editing — read the relevant files first.\n"
    "• Make the smallest change that fully solves the task; match surrounding style "
    "(this project favours dependency-free stdlib code).\n"
    "• After changing product code, verify it (run it / a quick test) when practical.\n"
    "• Keep replies concise: lead with the outcome, then only essential detail. The "
    "whole team reads your messages in a shared room.\n"
    "• Never run destructive or outward-facing commands (deleting data, pushing, "
    "sending network requests) without being explicitly asked.\n"
    "Reply in the language the user used (usually Traditional Chinese)."
)

# ── tool definitions (provider-agnostic JSON schema) ─────────────────────────
_TOOLS = [
    {"name": "list_dir", "description": "List files and subdirectories of a directory (relative to the project root).",
     "schema": {"type": "object", "properties": {"path": {"type": "string", "description": "Directory, default '.'"}}, "required": []}},
    {"name": "read_file", "description": "Read a UTF-8 text file under the project root.",
     "schema": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}},
    {"name": "write_file", "description": "Create or overwrite a text file under the project root with the given content.",
     "schema": {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}}, "required": ["path", "content"]}},
    {"name": "edit_file", "description": "Replace the single unique occurrence of old_string with new_string in a file.",
     "schema": {"type": "object", "properties": {"path": {"type": "string"}, "old_string": {"type": "string"}, "new_string": {"type": "string"}}, "required": ["path", "old_string", "new_string"]}},
    {"name": "run_bash", "description": "Run a bash command in the project root and return stdout+stderr. Use for tests, grep/find, git status, running the CLI, etc.",
     "schema": {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]}},
]

# Commands that are refused outright regardless of who asks.
_DENY = [
    r"\brm\s+-[a-z]*r[a-z]*f?\s+(/|~|\*|\.\.)", r"\brm\s+-[a-z]*f[a-z]*r[a-z]*\s+(/|~|\*)",
    r"\bmkfs\b", r"\bdd\b[^\n]*\bof=/dev/", r">\s*/dev/sd", r":\(\)\s*\{\s*:\s*\|\s*:",
    r"\bshutdown\b", r"\breboot\b", r"\bhalt\b", r"\bpoweroff\b",
    r"\bchmod\s+-R\s+0*777\s+/", r"\bsudo\b", r"\bsu\s+-",
    r"\bgit\s+push\b", r"\bcurl\b[^\n]*\|\s*(sudo\s+)?(bash|sh)\b", r"\bwget\b[^\n]*\|\s*(bash|sh)\b",
    r"\bmv\s+[^\n]*\s+/dev/null", r"/dev/sda", r"\bfdisk\b",
]
_DENY_RE = [re.compile(p, re.IGNORECASE) for p in _DENY]


# ── master switch ────────────────────────────────────────────────────────────
def _enabled() -> bool:
    """The dev agent stays fully dormant until this is turned on. Read at call
    time (not import) so `sparkreel console --enable-agent` works regardless of
    import order. Kept off by default so the API path exists but is not enabled."""
    return os.environ.get("SPARKREEL_AGENT_ENABLED", "0") == "1"


def _backend() -> str:
    """'cli' (logged-in claude/codex CLIs), 'api' (SDK + API keys), or 'bedrock'
    (Claude via the AWS instance IAM role — the EC2 default)."""
    b = os.environ.get("SPARKREEL_AGENT_BACKEND", "auto")
    if b in ("cli", "api", "bedrock"):
        return b
    # auto: subscription-login CLI → API keys → Bedrock (IAM role, e.g. on EC2)
    if not os.environ.get("ANTHROPIC_API_KEY") and shutil.which(CLAUDE_BIN):
        return "cli"
    if os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("OPENAI_API_KEY"):
        return "api"
    if _bedrock_ready():
        return "bedrock"
    return "api"


def _bedrock_client():
    """A boto3 bedrock-runtime client via the shared AWS factory (resolves creds
    from env / shared config / EC2 instance role), or None if unavailable."""
    try:
        from ..aws.clients import get_clients
        return get_clients(AWS_REGION).client("bedrock-runtime")
    except Exception:
        return None


def _bedrock_ready() -> bool:
    """True only if boto3 is importable, credentials resolve, and the
    bedrock-runtime client builds — so 'auto' won't pick a dead backend."""
    try:
        from ..aws.clients import get_clients
        c = get_clients(AWS_REGION)
        return c.credentials_available() and c.client("bedrock-runtime") is not None
    except Exception:
        return False


# ── safety helpers ───────────────────────────────────────────────────────────
def _safe(path: str) -> Path:
    """Resolve `path` under ROOT; raise ValueError if it escapes."""
    p = Path(path)
    rp = (ROOT / p if not p.is_absolute() else p).resolve()
    rp.relative_to(ROOT)  # raises ValueError if outside the project root
    return rp


def _audit(actor: str, tool: str, inp: dict, ok: bool, preview: str) -> None:
    try:
        _AUDIT.parent.mkdir(parents=True, exist_ok=True)
        with _AUDIT.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"ts": time.time(), "actor": actor, "tool": tool,
                                "input": inp, "ok": ok, "preview": preview[:400]},
                               ensure_ascii=False) + "\n")
    except Exception as e:
        # the audit trail is a safety feature — a silent gap in it is itself a problem
        get_logger(__name__).warning("稽核寫入失敗(%s):%s", type(e).__name__, e)


# ── tool execution ───────────────────────────────────────────────────────────
def _execute(name: str, inp: dict, actor: str) -> tuple[str, bool]:
    """Run a tool; return (result_text, is_error)."""
    try:
        if name == "list_dir":
            d = _safe(inp.get("path", "."))
            if not d.is_dir():
                return f"Not a directory: {inp.get('path')}", True
            rows = []
            for c in sorted(d.iterdir()):
                rows.append(("[DIR] " if c.is_dir() else "[FILE] ") + c.name +
                             (f"  ({c.stat().st_size}B)" if c.is_file() else ""))
            return "\n".join(rows) or "(empty)", False
        if name == "read_file":
            f = _safe(inp["path"])
            if not f.is_file():
                return f"No such file: {inp['path']}", True
            txt = f.read_text(encoding="utf-8", errors="replace")
            if len(txt) > _READ_CAP:
                txt = txt[:_READ_CAP] + f"\n… [truncated at {_READ_CAP} chars]"
            return txt, False
        if name == "write_file":
            f = _safe(inp["path"])
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text(inp["content"], encoding="utf-8")
            return f"Wrote {len(inp['content'])} chars to {inp['path']}", False
        if name == "edit_file":
            f = _safe(inp["path"])
            if not f.is_file():
                return f"No such file: {inp['path']}", True
            txt = f.read_text(encoding="utf-8")
            old = inp["old_string"]
            n = txt.count(old)
            if n == 0:
                return "old_string not found; read the file and match exactly.", True
            if n > 1:
                return f"old_string is not unique ({n} matches); add surrounding context.", True
            f.write_text(txt.replace(old, inp["new_string"], 1), encoding="utf-8")
            return f"Edited {inp['path']}", False
        if name == "run_bash":
            if not SHELL_ENABLED:
                return "Shell is disabled on this console (SPARKREEL_AGENT_SHELL=0).", True
            cmd = inp["command"]
            for rx in _DENY_RE:
                if rx.search(cmd):
                    return f"Refused: command matches a blocked-destructive pattern ({rx.pattern}).", True
            try:
                r = subprocess.run(cmd, shell=True, cwd=str(ROOT), capture_output=True,
                                   text=True, timeout=BASH_TIMEOUT)
            except subprocess.TimeoutExpired:
                return f"Command timed out after {BASH_TIMEOUT}s.", True
            out = (r.stdout or "") + (("\n[stderr]\n" + r.stderr) if r.stderr else "")
            if len(out) > _OUT_CAP:
                out = out[:_OUT_CAP] + f"\n… [truncated at {_OUT_CAP} chars]"
            return f"(exit {r.returncode})\n{out}".strip(), r.returncode != 0
        return f"Unknown tool: {name}", True
    except ValueError:
        return f"Path escapes the project root — refused: {inp.get('path')}", True
    except Exception as e:
        return f"{type(e).__name__}: {e}", True


# ── providers ────────────────────────────────────────────────────────────────
def available() -> dict:
    """What SparkAgent can use right now, given enable-switch + backend."""
    backend = _backend()
    out = {"enabled": _enabled(), "backend": backend, "anthropic": False, "openai": False,
           "shell": SHELL_ENABLED, "models": {"anthropic": CLAUDE_MODEL, "openai": OPENAI_MODEL}}
    if not _enabled():
        return out  # master switch off → dormant regardless of keys/logins
    if backend == "cli":
        # uses the user's own logged-in CLIs (subscription); no API key needed
        out["anthropic"] = bool(shutil.which(CLAUDE_BIN))
        out["openai"] = bool(shutil.which(CODEX_BIN))
        out["models"] = {"anthropic": "claude CLI（登入）", "openai": "codex CLI（登入）"}
        return out
    if backend == "bedrock":
        # Claude via Amazon Bedrock (instance IAM role); OpenAI has no Bedrock path
        out["anthropic"] = _bedrock_ready()
        out["models"] = {"anthropic": f"Bedrock {BEDROCK_MODEL}", "openai": None}
        return out
    if os.environ.get("ANTHROPIC_API_KEY"):
        try:
            import anthropic  # noqa: F401
            out["anthropic"] = True
        except Exception:
            pass
    if os.environ.get("OPENAI_API_KEY"):
        try:
            import openai  # noqa: F401
            out["openai"] = True
        except Exception:
            pass
    return out


def route(task: str, avail: dict) -> str:
    """Pick a provider. Explicit @claude/@gpt wins; else prefer Claude for coding."""
    t = task.lower()
    if any(k in t for k in ("@gpt", "@openai", "@chatgpt")) and avail["openai"]:
        return "openai"
    if any(k in t for k in ("@claude", "@anthropic")) and avail["anthropic"]:
        return "anthropic"
    if avail["anthropic"]:
        return "anthropic"
    if avail["openai"]:
        return "openai"
    return ""


def _strip_mentions(task: str) -> str:
    return re.sub(r"@(claude|anthropic|gpt|openai|chatgpt)\b", "", task, flags=re.IGNORECASE).strip()


def _run_anthropic(task: str, actor: str, progress, actions: list) -> str:
    import anthropic
    client = anthropic.Anthropic()
    tools = [{"name": t["name"], "description": t["description"], "input_schema": t["schema"]} for t in _TOOLS]
    messages = [{"role": "user", "content": task}]
    final = ""
    for _ in range(MAX_STEPS):
        resp = client.messages.create(
            model=CLAUDE_MODEL, max_tokens=16000, system=SYSTEM_PROMPT,
            thinking={"type": "adaptive"}, output_config={"effort": "high"},
            tools=tools, messages=messages)
        # surface any text the model produced this step
        for b in resp.content:
            if b.type == "text" and b.text.strip():
                progress("text", b.text.strip())
                final = b.text.strip()
        if resp.stop_reason != "tool_use":
            break
        messages.append({"role": "assistant", "content": resp.content})
        results = []
        for b in resp.content:
            if b.type == "tool_use":
                progress("tool", _tool_line(b.name, b.input))
                out, err = _execute(b.name, b.input, actor)
                _audit(actor, b.name, b.input, not err, out)
                actions.append(("✗ " if err else "") + _tool_line(b.name, b.input))
                results.append({"type": "tool_result", "tool_use_id": b.id,
                                "content": out, "is_error": err})
        messages.append({"role": "user", "content": results})
    else:
        progress("text", "（已達最大步數上限，先停在這裡）")
    return final


def _run_openai(task: str, actor: str, progress, actions: list) -> str:
    import openai
    client = openai.OpenAI()
    tools = [{"type": "function", "function": {"name": t["name"], "description": t["description"],
              "parameters": t["schema"]}} for t in _TOOLS]
    messages = [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": task}]
    final = ""
    for _ in range(MAX_STEPS):
        resp = client.chat.completions.create(model=OPENAI_MODEL, messages=messages, tools=tools)
        msg = resp.choices[0].message
        if msg.content and msg.content.strip():
            progress("text", msg.content.strip())
            final = msg.content.strip()
        if not msg.tool_calls:
            break
        messages.append({"role": "assistant", "content": msg.content or "",
                         "tool_calls": [{"id": c.id, "type": "function",
                                         "function": {"name": c.function.name, "arguments": c.function.arguments}}
                                        for c in msg.tool_calls]})
        for c in msg.tool_calls:
            try:
                args = json.loads(c.function.arguments or "{}")
            except Exception:
                args = {}
            progress("tool", _tool_line(c.function.name, args))
            out, err = _execute(c.function.name, args, actor)
            _audit(actor, c.function.name, args, not err, out)
            actions.append(("✗ " if err else "") + _tool_line(c.function.name, args))
            messages.append({"role": "tool", "tool_call_id": c.id, "content": out})
    else:
        progress("text", "（已達最大步數上限，先停在這裡）")
    return final


# ── Bedrock backend: Claude via boto3 bedrock-runtime (no API key, IAM role) ──
def _bedrock_invoke(client, body: dict, max_tokens: int) -> dict:
    """One InvokeModel call against a Claude model on Bedrock → parsed JSON.
    Body is the Anthropic Messages API shape minus `model` (that's `modelId`)."""
    body = {"anthropic_version": "bedrock-2023-05-31", "max_tokens": max_tokens, **body}
    resp = client.invoke_model(modelId=BEDROCK_MODEL, body=json.dumps(body))
    return json.loads(resp["body"].read())


def _run_bedrock(task: str, actor: str, progress, actions: list) -> str:
    """Manual tool-use loop against Amazon Bedrock (Claude), reusing the same
    provider-agnostic tools + executor as the API path. Auth is the instance IAM
    role — no API key. Returns the final assistant text."""
    client = _bedrock_client()
    if client is None:
        raise RuntimeError("Bedrock 不可用（boto3 / 憑證 / IAM 角色未就緒）。")
    tools = [{"name": t["name"], "description": t["description"], "input_schema": t["schema"]} for t in _TOOLS]
    messages = [{"role": "user", "content": [{"type": "text", "text": task}]}]
    final = ""
    for _ in range(MAX_STEPS):
        payload = _bedrock_invoke(client, {"system": SYSTEM_PROMPT, "tools": tools, "messages": messages}, 8000)
        content = payload.get("content", []) or []
        for b in content:
            if b.get("type") == "text" and b.get("text", "").strip():
                progress("text", b["text"].strip())
                final = b["text"].strip()
        if payload.get("stop_reason") != "tool_use":
            break
        messages.append({"role": "assistant", "content": content})
        results = []
        for b in content:
            if b.get("type") == "tool_use":
                name, inp = b.get("name", ""), b.get("input", {}) or {}
                progress("tool", _tool_line(name, inp))
                out, err = _execute(name, inp, actor)
                _audit(actor, name, inp, not err, out)
                actions.append(("✗ " if err else "") + _tool_line(name, inp))
                results.append({"type": "tool_result", "tool_use_id": b.get("id"),
                                "content": out, "is_error": err})
        messages.append({"role": "user", "content": results})
    else:
        progress("text", "（已達最大步數上限，先停在這裡）")
    return final


# ── CLI-login backend: drive the user's own logged-in claude / codex ─────────
def _cli_tool_line(name: str, inp: dict) -> str:
    if name == "Bash":
        return "$ " + str(inp.get("command", ""))[:200]
    if name in ("Read", "Write", "Edit", "MultiEdit", "NotebookEdit"):
        return f"{name} {inp.get('file_path', inp.get('notebook_path', ''))}"
    if name == "Grep":
        return f"grep {inp.get('pattern', '')}"
    if name == "Glob":
        return f"glob {inp.get('pattern', '')}"
    if name in ("TodoWrite", "Task"):
        return name
    return f"{name} {json.dumps(inp, ensure_ascii=False)[:120]}"


def _run_cli_claude(task: str, actor: str, progress, actions: list, resume_sid: Optional[str] = None):
    """Run the user's logged-in Claude Code headlessly; stream its steps.
    Returns (final_text, meta) where meta carries session_id (for resume),
    denied tools, and captured test output."""
    cmd = ["timeout", str(CLI_TIMEOUT), CLAUDE_BIN, "-p", task,
           "--output-format", "stream-json", "--verbose",
           "--permission-mode", CLI_PERMISSION]
    if resume_sid:
        cmd += ["--resume", resume_sid]
    proc = subprocess.Popen(cmd, cwd=str(ROOT), stdin=subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, bufsize=1)
    final, sid, denials, tests, pending = "", None, [], [], {}
    for line in proc.stdout:
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except Exception:
            continue
        if sid is None and ev.get("session_id"):
            sid = ev["session_id"]
        t = ev.get("type")
        if t == "assistant":
            for b in ev.get("message", {}).get("content", []):
                if b.get("type") == "text" and b.get("text", "").strip():
                    progress("text", b["text"].strip())
                elif b.get("type") == "tool_use":
                    name, inp = b.get("name", ""), b.get("input", {})
                    ln = _cli_tool_line(name, inp)
                    progress("tool", ln)
                    actions.append(ln)
                    _audit(actor, "claude:" + name, inp, True, "")
                    if name == "Bash" and _is_test_cmd(inp.get("command", "")):
                        pending[b.get("id")] = inp.get("command", "")
        elif t == "user":
            for b in ev.get("message", {}).get("content", []):
                if b.get("type") == "tool_result" and b.get("tool_use_id") in pending:
                    c = b.get("content", "")
                    if isinstance(c, list):
                        c = " ".join(x.get("text", "") for x in c if isinstance(x, dict))
                    tests.append(f"$ {pending.pop(b['tool_use_id'])}\n{str(c)[:400]}")
        elif t == "result":
            final = ev.get("result", "") or final
            for d in ev.get("permission_denials", []) or []:
                denials.append(d.get("tool_name") or str(d)[:60])
    proc.wait()
    return final, {"session_id": sid, "denials": denials, "test_results": "\n".join(tests)[:1500]}


def _run_cli_codex(task: str, actor: str, progress, actions: list) -> str:
    """Run the user's logged-in Codex CLI headlessly (best-effort streaming)."""
    out_file = str(ROOT / "assets" / "console" / "_codex_last.txt")
    Path(out_file).parent.mkdir(parents=True, exist_ok=True)
    cmd = ["timeout", str(CLI_TIMEOUT), CODEX_BIN, "exec", "--skip-git-repo-check",
           "-C", str(ROOT), "-o", out_file, task]
    proc = subprocess.Popen(cmd, cwd=str(ROOT), stdin=subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, bufsize=1)
    for line in proc.stdout:
        line = line.strip()
        if line:
            progress("tool", line[:300])
    proc.wait()
    _audit(actor, "codex:exec", {"task": task[:200]}, proc.returncode == 0, "")
    actions.append("codex exec")
    try:
        final = Path(out_file).read_text(encoding="utf-8").strip()
    except Exception:
        final = ""
    return final, {"session_id": None, "denials": [], "test_results": ""}


# ── team-facing auto-summary ─────────────────────────────────────────────────
def _plain_complete(provider: str, system: str, user: str, max_tokens: int = 500) -> str:
    """One non-tool completion, used to write the team summary."""
    if provider == "bedrock":
        client = _bedrock_client()
        if client is None:
            raise RuntimeError("Bedrock 不可用。")
        payload = _bedrock_invoke(client, {"system": system,
            "messages": [{"role": "user", "content": [{"type": "text", "text": user}]}]}, max_tokens)
        return "".join(b.get("text", "") for b in payload.get("content", []) if b.get("type") == "text").strip()
    if provider == "anthropic":
        import anthropic
        r = anthropic.Anthropic().messages.create(
            model=CLAUDE_MODEL, max_tokens=max_tokens, system=system,
            messages=[{"role": "user", "content": user}])
        return "".join(b.text for b in r.content if b.type == "text").strip()
    import openai
    r = openai.OpenAI().chat.completions.create(
        model=OPENAI_MODEL, messages=[{"role": "system", "content": system},
                                       {"role": "user", "content": user}])
    return (r.choices[0].message.content or "").strip()


def _fallback_summary(actions: list) -> str:
    """Deterministic recap if the model summary call fails — never blank."""
    wrote = [a.split(" ", 1)[1] for a in actions if a.startswith("write_file ")]
    edited = [a.split(" ", 1)[1] for a in actions if a.startswith("edit_file ")]
    cmds = [a[2:] for a in actions if a.startswith("$ ")]
    parts = []
    if wrote:  parts.append("新增/覆寫 " + "、".join(dict.fromkeys(wrote)))
    if edited: parts.append("修改 " + "、".join(dict.fromkeys(edited)))
    if cmds:   parts.append("執行了 " + str(len(cmds)) + " 個指令")
    return "本次動作：" + ("；".join(parts) if parts else "只做了讀取/查詢,未變更檔案") + "。"


def _summarize(provider: str, task: str, actions: list, final_text: str) -> str:
    """Plain-language, team-facing recap of what the agent just did."""
    bullets = "\n".join(f"- {a}" for a in actions[:40]) or "（無工具動作）"
    prompt = (f"你剛完成一個開發任務。\n任務：{task}\n\n你採取的動作：\n{bullets}\n\n"
              f"你的最終回覆：\n{final_text[:1500]}\n\n"
              "請用繁體中文寫一段『給團隊看的摘要』（2–4 句），說明：你做了什麼、動了哪些檔案、"
              "結果或是否已驗證。用平實的話讓非技術成員也看得懂，直接輸出摘要,不要前言或標題。")
    try:
        s = _plain_complete(provider, "你為 SparkReel 開發團隊寫簡潔清楚的工作摘要。", prompt, 500)
        return s or _fallback_summary(actions)
    except Exception:
        return _fallback_summary(actions)


def _tool_line(name: str, inp: dict) -> str:
    if name == "run_bash":
        return f"$ {inp.get('command', '')}"
    if name in ("read_file", "write_file", "edit_file", "list_dir"):
        return f"{name} {inp.get('path', '')}"
    return f"{name} {json.dumps(inp, ensure_ascii=False)[:120]}"


# ── AI editing planner (Studio) — chat + structured plan, no file edits ──────
PLAN_EMPHASIS = {"reaction", "action", "loud", "balanced"}
PLAN_PLATFORMS = {"tiktok", "reels", "shorts"}

_PLAN_SYSTEM = (
    "你是 SparkReel 的短影音剪輯助手,和使用者一起討論怎麼把他上傳的影片剪成精華短片。"
    "影片資訊:時長約 {dur} 秒、解析度 {w}x{h}。\n"
    "每次回覆都『只』輸出一個 JSON 物件(不要使用任何工具、不要讀取任何檔案、不要多餘文字),格式:\n"
    '{{"reply":"給使用者的繁體中文回覆——說明你的剪輯想法或回應他的調整,語氣自然像夥伴,2-4 句",'
    '"plan":{{"platforms":["tiktok","reels","shorts"],"captions":false,"max_clips":8,'
    '"target_len":20,"emphasis":"reaction","min_score":0.55,"broll":false}}}}\n'
    "欄位:platforms 輸出平台(tiktok/reels/shorts 可多選);captions 是否燒錄字幕;"
    "max_clips 產出幾支(1-12);target_len 每支約幾秒(6-45);"
    "emphasis 取向 reaction=情緒/表情反應(用 FER+ 臉部情緒模型抓笑/驚/怒) / action=畫面動態 / "
    "loud=音量爆點 / balanced=平衡;"
    "min_score 高光門檻(0.4-0.7,越高越精選);broll 是否自動插入 B-roll 空鏡(增加畫面變化)。"
    "依使用者的話更新 plan,並在 reply 說明你為何這樣調。"
    "系統會先把整片分類成 談話/爆點/反應/動作 段型,並以『鋪陳→爆點→餘韻』的節奏剪輯"
    "(爆點落在後段營造期待感),所有短片自動計算 0-99 病毒潛力分並排序。"
    "若使用者想要更有情緒張力/期待感,建議 emphasis=reaction。"
)


def _planner_provider() -> Optional[str]:
    # respect an explicit Bedrock backend (the EC2 deployment sets this)
    if os.environ.get("SPARKREEL_AGENT_BACKEND") == "bedrock" and _bedrock_ready():
        return "bedrock"
    if os.environ.get("ANTHROPIC_API_KEY"):
        try:
            import anthropic  # noqa: F401
            return "api-anthropic"
        except Exception:
            pass
    if os.environ.get("OPENAI_API_KEY"):
        try:
            import openai  # noqa: F401
            return "api-openai"
        except Exception:
            pass
    if shutil.which(CLAUDE_BIN):
        return "cli-claude"
    if _bedrock_ready():           # e.g. on EC2: no logged-in CLI / API key → Bedrock
        return "bedrock"
    return None


def planner_available() -> dict:
    p = _planner_provider()
    label = {"api-anthropic": "Claude API", "api-openai": "OpenAI API",
             "cli-claude": "claude 登入", "bedrock": f"Bedrock {BEDROCK_MODEL}"}.get(p)
    return {"available": p is not None, "provider": p, "label": label}


def _extract_json(text: str) -> dict:
    t = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.MULTILINE).strip()
    m = re.search(r"\{.*\}", t, re.S)
    if not m:
        raise ValueError("no json in reply")
    return json.loads(m.group(0))


def _norm_plan(plan: dict, meta: dict) -> dict:
    def ci(v, lo, hi, d):
        try:
            return max(lo, min(hi, int(v)))
        except Exception:
            return d

    def cf(v, lo, hi, d):
        try:
            return max(lo, min(hi, round(float(v), 2)))
        except Exception:
            return d
    plats = [p for p in (plan.get("platforms") or []) if p in PLAN_PLATFORMS] or ["tiktok", "reels", "shorts"]
    emph = plan.get("emphasis") if plan.get("emphasis") in PLAN_EMPHASIS else "balanced"
    return {"platforms": plats, "captions": bool(plan.get("captions", False)),
            "max_clips": ci(plan.get("max_clips"), 1, 12, 8), "target_len": ci(plan.get("target_len"), 6, 45, 20),
            "emphasis": emph, "min_score": cf(plan.get("min_score"), 0.4, 0.72, 0.55),
            "broll": bool(plan.get("broll", False))}


def _rule_plan(meta: dict, history: list) -> dict:
    last = next((str(m.get("content", "")) for m in reversed(history or []) if m.get("role") == "user"), "")
    plan = {"platforms": ["tiktok", "reels", "shorts"], "captions": False, "max_clips": 8,
            "target_len": 20, "emphasis": "reaction", "min_score": 0.55, "broll": False}
    if any(k in last.lower() for k in ["空鏡", "b-roll", "broll", "b roll"]):
        plan["broll"] = True
    if any(k in last for k in ["字幕", "上字"]) and not any(k in last for k in ["不要", "無", "去", "不上"]):
        plan["captions"] = True
    if any(k in last for k in ["短", "精簡"]):
        plan["target_len"] = 12
    if any(k in last for k in ["長", "完整"]):
        plan["target_len"] = 30
    if any(k in last for k in ["多", "更多", "再找"]):
        plan["max_clips"] = 12
    if any(k in last for k in ["少", "精選", "幾支"]):
        plan["max_clips"] = 5
    if any(k in last for k in ["反應", "表情", "微表情", "笑"]):
        plan["emphasis"] = "reaction"
    if any(k in last for k in ["動作", "畫面", "動態"]):
        plan["emphasis"] = "action"
    if any(k in last for k in ["大聲", "爆點", "音量", "高潮"]):
        plan["emphasis"] = "loud"
    em = {"reaction": "臉部表情反應", "action": "畫面動態", "loud": "音量爆點", "balanced": "多模態平衡"}
    reply = (f"我的剪輯方案:以「{em[plan['emphasis']]}」為主軸,產出最多 {plan['max_clips']} 支、"
             f"每支約 {plan['target_len']} 秒,平台 {'/'.join(plan['platforms'])},"
             f"{'含' if plan['captions'] else '不含'}字幕。想改都跟我說,或直接按「就這樣剪」。")
    return {"reply": reply, "plan": plan}


def _claude_cli_text(prompt: str) -> str:
    r = subprocess.run(["timeout", "90", CLAUDE_BIN, "-p", prompt, "--output-format", "json"],
                       cwd=str(ROOT), stdin=subprocess.DEVNULL, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError((r.stderr or "claude cli failed")[:200])
    return json.loads(r.stdout).get("result", "")


def plan_chat(video_meta: dict, history: list) -> dict:
    """Discuss an editing plan. Returns {provider, reply, plan}. Chat + params
    only — never edits files, so it's safe regardless of the agent master switch."""
    prov = _planner_provider()
    if prov is None:
        return {"provider": None, **_rule_plan(video_meta, history)}
    sysmsg = _PLAN_SYSTEM.format(dur=int(video_meta.get("duration", 0)),
                                 w=video_meta.get("width", 0), h=video_meta.get("height", 0))
    try:
        if prov == "cli-claude":
            convo = "\n".join(f"{'使用者' if m['role'] == 'user' else '助手'}:{m['content']}" for m in history)
            text = _claude_cli_text(sysmsg + "\n\n── 對話 ──\n" + convo + "\n\n請輸出更新後的 JSON。")
        elif prov == "bedrock":
            user = "\n".join(f"{m['role']}: {m['content']}" for m in history)
            text = _plain_complete("bedrock", sysmsg, user, 700)
        else:
            user = "\n".join(f"{m['role']}: {m['content']}" for m in history)
            text = _plain_complete("anthropic" if prov == "api-anthropic" else "openai", sysmsg, user, 700)
        data = _extract_json(text)
        return {"provider": prov, "reply": str(data.get("reply", "")).strip() or "(方案已更新)",
                "plan": _norm_plan(data.get("plan", {}), video_meta)}
    except Exception as e:
        fb = _rule_plan(video_meta, history)
        fb["reply"] += f"（AI 連線暫時失敗:{type(e).__name__},先給你預設方案）"
        return {"provider": prov, **fb}


# ── resumable task context ───────────────────────────────────────────────────
# Each run is persisted so the next task can pick up where the last left off:
# prior request/summary/changed-files/tests, live git state, and the provider
# session id (for native `claude --resume`).
_RUNS_FILE = ROOT / "assets" / "console" / "agent_runs.jsonl"
_THREAD_FILE = ROOT / "assets" / "console" / "agent_thread.json"
_TEST_RE = re.compile(r"\b(pytest|unittest|python\s+-m\s+pytest|go\s+test|cargo\s+test|npm\s+(run\s+)?test|tox|sparkreel\s+analyze)\b", re.I)
_RESET_RE = re.compile(r"(/reset|重新開始|忘掉之前|忘記之前|從頭開始|清空脈絡|clear context)", re.I)


def _git(*args) -> str:
    try:
        r = subprocess.run(["git", *args], cwd=str(ROOT), capture_output=True, text=True, timeout=15)
        return (r.stdout or "").strip()
    except Exception:
        return ""


def _git_state() -> dict:
    return {"branch": _git("rev-parse", "--abbrev-ref", "HEAD"),
            "status": _git("status", "--porcelain")[:2000],
            "stat": _git("diff", "--stat", "HEAD")[:2000]}


def _is_test_cmd(cmd: str) -> bool:
    return bool(_TEST_RE.search(cmd or ""))


def _wants_reset(task: str) -> bool:
    return bool(_RESET_RE.search(task or ""))


def _load_runs(n: int = 3) -> list:
    if not _RUNS_FILE.exists():
        return []
    out = []
    for line in _RUNS_FILE.read_text(encoding="utf-8").splitlines()[-n:]:
        try:
            out.append(json.loads(line))
        except Exception:
            pass
    return out


def _run_count() -> int:
    try:
        return sum(1 for _ in _RUNS_FILE.open(encoding="utf-8")) if _RUNS_FILE.exists() else 0
    except Exception:
        return 0


def _save_run(rec: dict) -> None:
    try:
        _RUNS_FILE.parent.mkdir(parents=True, exist_ok=True)
        with _RUNS_FILE.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception as e:
        get_logger(__name__).warning("任務紀錄存檔失敗(%s):%s → 下一輪脈絡可能接不上。",
                                     type(e).__name__, e)


def _thread() -> dict:
    try:
        return json.loads(_THREAD_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _set_thread(d: dict) -> None:
    try:
        _THREAD_FILE.parent.mkdir(parents=True, exist_ok=True)
        _THREAD_FILE.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass


def reset_context() -> None:
    _set_thread({})


def _changed_files(actions: list, before: dict, after: dict) -> list:
    files = set()
    for a in actions:
        m = re.match(r"(?:Write|Edit|MultiEdit|NotebookEdit|write_file|edit_file)\s+(\S+)", a)
        if m:
            files.add(m.group(1))
    def paths(status: str) -> set:
        return {ln[3:].strip() for ln in status.splitlines() if ln.strip()}
    files |= (paths(after.get("status", "")) - paths(before.get("status", "")))
    return sorted(f for f in files if f)[:20]


def _preamble(git_before: dict) -> str:
    """Context block injected into a fresh task so the agent sees prior work."""
    runs = _load_runs(3)
    if not runs and not git_before.get("status"):
        return ""
    lines = ["── 專案脈絡（供你接續;已完成的事不要重做）──"]
    for r in runs:
        lines.append(f"• 前一輪 run {str(r.get('run_id', ''))[:8]}（{r.get('actor', '')}）要求:「{str(r.get('request', ''))[:90]}」")
        if r.get("summary"):
            lines.append(f"    結果:{str(r['summary'])[:200]}")
        if r.get("changed_files"):
            lines.append(f"    變更檔案:{', '.join(r['changed_files'][:8])}")
        if r.get("test_results"):
            lines.append(f"    測試:{str(r['test_results'])[:120]}")
        denied = (r.get("approvals") or {}).get("denied") or []
        if denied:
            lines.append(f"    被拒動作:{', '.join(str(x) for x in denied[:5])}")
    if git_before.get("branch"):
        lines.append(f"• 目前分支:{git_before['branch']}")
    if git_before.get("status"):
        lines.append(f"• 目前工作樹（git status）:\n{git_before['status'][:600]}")
    return "\n".join(lines) + "\n\n── 本輪任務 ──\n"


def context_summary() -> dict:
    """What resumable context is currently carried (for the console UI/API)."""
    runs = _load_runs(1)
    th = _thread()
    last = runs[0] if runs else None
    return {
        "has_context": bool(last or th.get("session_id")),
        "run_count": _run_count(),
        "session_id": th.get("session_id"),
        "last_run": ({"run_id": last.get("run_id"), "actor": last.get("actor"),
                      "request": str(last.get("request", ""))[:100],
                      "summary": str(last.get("summary", ""))[:200],
                      "changed_files": last.get("changed_files", []),
                      "ok": last.get("ok")} if last else None),
    }


def run_task(task: str, actor: str, progress) -> dict:
    """Run one agent task. `progress(kind, text)` streams steps to the room.
    Returns {provider, model, ok, error}."""
    if not _enabled():
        return {"ok": False, "provider": None, "model": None,
                "error": "SparkAgent 未啟用（用 --enable-agent 或設 SPARKREEL_AGENT_ENABLED=1,並提供 API 金鑰後才會開啟）。"}
    backend = _backend()
    avail = available()
    provider = route(task, avail)
    if not provider:
        if backend == "cli":
            err = "找不到已登入的 claude / codex CLI——請先在此機器 `claude`（或 `codex`）登入。"
        elif backend == "bedrock":
            err = ("Bedrock 後端未就緒——請確認此機器有可用的 AWS 憑證（EC2 instance role 或環境變數），"
                   "且 IAM 有 bedrock:InvokeModel 權限、目標模型已在該區域開通。")
        else:
            err = "沒有可用的 AI 供應商——請設定 ANTHROPIC_API_KEY 或 OPENAI_API_KEY 並安裝對應 SDK。"
        return {"ok": False, "error": err, "provider": None, "model": None}
    clean = _strip_mentions(task)
    actions: list = []
    if backend == "cli":
        model = "claude CLI（登入）" if provider == "anthropic" else "codex CLI（登入）"
    elif backend == "bedrock":
        model = f"Bedrock {BEDROCK_MODEL}"
    else:
        model = CLAUDE_MODEL if provider == "anthropic" else OPENAI_MODEL

    # ── resumable context ──────────────────────────────────────────────
    run_id = secrets.token_hex(6)
    started = time.time()
    git_before = _git_state()
    reset = _wants_reset(clean)
    if reset:
        _set_thread({})
    thread = {} if reset else _thread()
    # native Claude Code session resume (CLI + Claude only)
    resume_sid = thread.get("session_id") if (backend == "cli" and provider == "anthropic") else None
    preamble = _preamble(git_before)

    meta = {"session_id": None, "denials": [], "test_results": ""}
    final, ok, err = "", True, None
    try:
        if backend == "cli":
            if resume_sid:  # resumed session already holds the history natively
                prompt = clean + ("\n\n（接續你上一輪的工作;目前 git status:\n"
                                  + (git_before["status"][:400] or "（乾淨）") + "）")
            else:
                prompt = preamble + clean
            if provider == "anthropic":
                final, meta = _run_cli_claude(prompt, actor, progress, actions, resume_sid)
            else:
                final, meta = _run_cli_codex(prompt, actor, progress, actions)
            if final:
                progress("summary", final)
        elif backend == "bedrock":
            final = _run_bedrock(preamble + clean, actor, progress, actions)
            if actions:
                progress("summary", _summarize("bedrock", clean, actions, final))
        else:
            final = (_run_anthropic if provider == "anthropic" else _run_openai)(
                preamble + clean, actor, progress, actions)
            if actions:
                progress("summary", _summarize(provider, clean, actions, final))
    except Exception as e:
        ok, err = False, f"{type(e).__name__}: {e}"
        if backend in ("api", "bedrock") and actions:
            progress("summary", _fallback_summary(actions))

    # ── persist this run so the next task can pick up where we left off ─
    git_after = _git_state()
    _save_run({
        "run_id": run_id, "ts": started, "actor": actor, "request": clean,
        "backend": backend, "provider": provider, "model": model,
        "session_id": meta.get("session_id"), "resumed": bool(resume_sid),
        "summary": (final or "")[:2000],
        "changed_files": _changed_files(actions, git_before, git_after),
        "actions": actions[:60], "test_results": meta.get("test_results", ""),
        "approvals": {"denied": meta.get("denials", [])},
        "git_status": git_after["status"], "git_stat": git_after["stat"], "ok": ok, "error": err,
    })
    if backend == "cli" and provider == "anthropic" and meta.get("session_id"):
        _set_thread({"session_id": meta["session_id"], "run_id": run_id, "updated": time.time()})

    return {"ok": ok, "provider": provider, "model": model, "error": err,
            "run_id": run_id, "resumed": bool(resume_sid)}
