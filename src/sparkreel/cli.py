"""SparkReel command-line interface.

    sparkreel make-sample                 # generate a self-contained demo stream
    sparkreel analyze <video|URL> [--chat]# detect highlights → produce clips (URL 自動抓片+字幕+彈幕)
    sparkreel fetch <URL>                 # 只從網址抓 影片+字幕+彈幕（yt-dlp），不分析
    sparkreel batch <dir|glob>            # process many streams concurrently
    sparkreel serve                       # launch the web UI (Live Demo)
    sparkreel console                     # launch the team collaboration console
    sparkreel status                      # show backend / AWS availability
    sparkreel info <result.json>          # pretty-print a previous run
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

import typer

from . import __product_zh__, __version__

app = typer.Typer(add_completion=False, help="SparkReel 亮點秒剪 — AI 直播高光偵測與自動剪輯")


def _c(msg: str, color: str = typer.colors.CYAN, bold: bool = False) -> None:
    typer.secho(msg, fg=color, bold=bold)


def _progress():
    def cb(stage: str, pct: float, msg: str):
        bar = "█" * int(pct * 24) + "░" * (24 - int(pct * 24))
        typer.echo(f"\r  {bar} {pct * 100:5.1f}%  {stage:10s} {msg[:42]:42s}", nl=False)
        if stage == "done":
            typer.echo("")
    return cb


def _fmt_secs(s: float) -> str:
    m, sec = divmod(int(s), 60)
    return f"{m}分{sec:02d}秒" if m else f"{sec}秒"


def _fetch_url(url: str, out_root: str, *, want_chat: bool = True,
               cookies_from_browser: str = "", quiet: bool = False):
    """把平台網址抓成本地 影片+字幕+彈幕，存到 <out_root>/_source/<hash>。回傳 WebSource。"""
    import hashlib

    from .ingest.web import fetch_source

    slug = hashlib.md5(url.encode("utf-8")).hexdigest()[:10]
    dst = Path(out_root) / "_source" / slug
    if not quiet:
        _c(f"▶ 從網址抓取來源（yt-dlp）：{url}", typer.colors.BRIGHT_CYAN, bold=True)
    try:
        src = fetch_source(url, dst, want_chat=want_chat,
                           cookies_from_browser=cookies_from_browser, quiet=quiet)
    except Exception as e:  # noqa: BLE001
        _c(f"  ✗ 抓取失敗：{e}", typer.colors.RED)
        raise typer.Exit(1)
    if not quiet:
        _c(f"  ✓ 影片：{src.video}", typer.colors.GREEN)
        _c(f"  {'✓' if src.subtitles else '·'} 字幕：{src.subtitles or '（無）'}",
           typer.colors.GREEN if src.subtitles else typer.colors.BRIGHT_BLACK)
        _c(f"  {'✓' if src.chat else '·'} 彈幕/聊天：{src.chat or '（無）'}",
           typer.colors.GREEN if src.chat else typer.colors.BRIGHT_BLACK)
        _c(f"  ⓘ 標題「{src.title}」 時長 {_fmt_secs(src.duration)}", typer.colors.WHITE)
    return src


def _print_summary(result, out_dir) -> None:
    m = result.metrics
    typer.echo("")
    _c(f"═══ SparkReel 分析報告　job={result.job_id} ═══", typer.colors.BRIGHT_CYAN, bold=True)
    _c(f"  來源時長 {m.source_duration_sec:.0f}s ｜ 處理 {m.processing_sec:.1f}s "
       f"(即時倍率 {m.realtime_factor}x) ｜ backends={result.backends}", typer.colors.WHITE)
    _c(f"  高光 {m.highlights_found} 個 ｜ 產出短片 {m.clips_produced} 支 "
       f"× {len(result.clips[0].variants) if result.clips and result.clips[0].variants else 0} 平台",
       typer.colors.WHITE)
    _c(f"  ⏱  節省剪輯時間 {_fmt_secs(m.time_saved_sec)}（{m.time_saved_ratio * 100:.1f}%）"
       f" ｜ 自動化程度 {m.automation_degree * 100:.0f}%", typer.colors.GREEN, bold=True)
    _c(f"  ⭐ 平均品質 {m.avg_quality_score}/100 ｜ 偵測信心 {m.detection_confidence} "
       f"｜ 壓縮比 {m.compression_ratio}x", typer.colors.YELLOW)
    status_color = {"pass": typer.colors.GREEN, "flagged": typer.colors.YELLOW,
                    "needs_review": typer.colors.YELLOW, "rejected": typer.colors.RED}
    _c(f"  🛡  審核：{result.moderation.status} — {result.moderation.summary}",
       status_color.get(result.moderation.status, typer.colors.WHITE))
    typer.echo("")
    _c("  高光清單：", typer.colors.BRIGHT_CYAN, bold=True)
    for c in result.clips:
        h = result.highlights[c.highlight_index]
        _c(f"   #{c.highlight_index}  {h.start:5.1f}–{h.end:5.1f}s  🔥{c.virality:2d} "
           f"score={h.score:.2f} q={c.quality_score}  [{c.moderation_status}]  「{c.title}」", typer.colors.WHITE)
        _c(f"        理由：{h.reason} ｜ 關鍵語：{'、'.join(h.keywords[:5])}", typer.colors.BRIGHT_BLACK)
    non_montage_warn = [w for w in result.warnings if not w.startswith("montage:")]
    if non_montage_warn:
        typer.echo("")
        _c("  ⚠  備註：", typer.colors.YELLOW)
        for w in non_montage_warn:
            _c(f"     - {w}", typer.colors.YELLOW)
    typer.echo("")
    _c(f"  📁 輸出目錄：{out_dir}", typer.colors.BRIGHT_CYAN, bold=True)


@app.command()
def analyze(
    video: str = typer.Argument(..., help="輸入直播影片檔"),
    chat: Optional[str] = typer.Option(None, help="彈幕/聊天檔 (jsonl/json/csv)"),
    subtitles: Optional[str] = typer.Option(None, help="字幕檔 (srt/vtt)；未指定會自動偵測 sidecar"),
    platforms: Optional[str] = typer.Option(None, help="逗號分隔：tiktok,reels,shorts"),
    out: str = typer.Option("assets/output", help="輸出根目錄"),
    config: Optional[str] = typer.Option(None, help="設定檔路徑"),
    montage: bool = typer.Option(True, help="是否產出精華合輯"),
    captions: bool = typer.Option(True, "--captions/--no-captions",
                                  help="是否燒錄字幕/標題（--no-captions 產出乾淨純剪片供人為判斷）"),
    broll: bool = typer.Option(False, "--broll", help="自動插入本地 B-roll 空鏡(從同片其他精彩片段)"),
    json_out: bool = typer.Option(False, "--json", help="輸出 result.json 到 stdout"),
):
    """分析單一直播影片，偵測高光並自動剪出多平台短片。

    video 可為本地檔，或直播/VOD **網址**（YouTube/Twitch/… 由 yt-dlp 抓影片+字幕+彈幕）。
    """
    import os

    from .pipeline import run_pipeline

    plist = [p.strip() for p in platforms.split(",")] if platforms else None
    if broll:
        os.environ["SPARKREEL_BROLL"] = "1"

    # video 是網址 → 先抓下影片/字幕/彈幕，再走既有本地流程
    from .ingest.web import is_url
    if is_url(video):
        src = _fetch_url(video, out, want_chat=chat is None, quiet=json_out)
        video = src.video
        chat = chat or src.chat
        subtitles = subtitles or src.subtitles

    if not json_out:
        _c(f"▶ SparkReel {__product_zh__} 分析中：{video}", typer.colors.BRIGHT_CYAN, bold=True)
    result, out_dir = run_pipeline(
        video, chat_path=chat, subtitle_path=subtitles, platforms=plist,
        out_root=out, config_path=config, make_montage=montage, captions=captions,
        progress=None if json_out else _progress(),
    )
    if json_out:
        typer.echo(result.to_json(indent=2))
    else:
        _print_summary(result, out_dir)


@app.command()
def fetch(
    url: str = typer.Argument(..., help="直播/VOD 網址（YouTube/Twitch/…）"),
    out: str = typer.Option("assets/output", help="來源下載根目錄"),
    no_chat: bool = typer.Option(False, "--no-chat", help="不抓直播聊天（彈幕）"),
    cookies_from_browser: str = typer.Option(
        "", "--cookies-from-browser",
        help="帶瀏覽器登入 cookie（chrome/firefox…）以抓會員/私人內容或過反爬"),
):
    """只從網址抓下 影片+字幕+彈幕（不分析）。之後可 `sparkreel analyze <抓下的影片>`。"""
    src = _fetch_url(url, out, want_chat=not no_chat,
                     cookies_from_browser=cookies_from_browser, quiet=False)
    _c(f"\n完成。可接著分析：\n  sparkreel analyze '{src.video}'"
       f"{' --chat ' + repr(src.chat) if src.chat else ''}"
       f"{' --subtitles ' + repr(src.subtitles) if src.subtitles else ''}",
       typer.colors.BRIGHT_CYAN, bold=True)


@app.command()
def batch(
    path: str = typer.Argument(..., help="影片目錄或 glob，如 vods/ 或 'vods/*.mp4'"),
    platforms: Optional[str] = typer.Option(None, help="逗號分隔平台"),
    out: str = typer.Option("assets/output", help="輸出根目錄"),
    workers: int = typer.Option(2, help="並行處理數"),
    config: Optional[str] = typer.Option(None),
):
    """批量處理多個直播檔（自動偵測 sidecar 彈幕/字幕）。"""
    from .batch import discover, process
    from .config import load_config

    items = discover(path)
    if not items:
        _c(f"找不到任何影片：{path}", typer.colors.RED)
        raise typer.Exit(1)
    _c(f"▶ 批量處理 {len(items)} 個檔案（workers={workers}）…", typer.colors.BRIGHT_CYAN, bold=True)
    plist = [p.strip() for p in platforms.split(",")] if platforms else None
    done = {"n": 0}

    def on_done(o):
        done["n"] += 1
        if o.ok:
            _c(f"  [{done['n']}/{len(items)}] ✓ {Path(o.video).name} → "
               f"{o.highlights} 高光 / {o.clips} 短片（省時 {o.time_saved_ratio * 100:.0f}%）", typer.colors.GREEN)
        else:
            _c(f"  [{done['n']}/{len(items)}] ✗ {Path(o.video).name} — {o.error}", typer.colors.RED)

    outcomes = process(items, out_root=out, platforms=plist, workers=workers,
                       config=load_config(config), on_done=on_done)
    ok = sum(1 for o in outcomes if o.ok)
    clips = sum(o.clips for o in outcomes if o.ok)
    _c(f"\n完成：{ok}/{len(outcomes)} 成功，共產出 {clips} 支短片。", typer.colors.BRIGHT_CYAN, bold=True)


@app.command()
def serve(
    host: str = typer.Option("127.0.0.1", help="綁定位址"),
    port: int = typer.Option(9999, help="連接埠"),
    out: str = typer.Option("assets/output", help="輸出根目錄"),
):
    """啟動網頁介面（Live Demo）。"""
    from .web.app import run_server

    _c(f"▶ SparkReel Web UI → http://{host}:{port}", typer.colors.BRIGHT_CYAN, bold=True)
    run_server(host=host, port=port, out_root=out)


@app.command()
def mcp():
    """啟動 MCP server（讓 Claude Code / Cursor 任何 agent 驅動剪輯）。

    註冊到 Claude Code：  claude mcp add sparkreel -- sparkreel mcp
    工具：create_clips / list_jobs / get_job / make_sample
    """
    try:
        from .mcp_server import run
    except ImportError:
        _c("需要安裝 MCP 套件：pip install 'sparkreel[mcp]'（或 pip install mcp）", typer.colors.RED)
        raise typer.Exit(1)
    run()


@app.command()
def console(
    host: str = typer.Option("127.0.0.1", help="綁定位址"),
    port: int = typer.Option(9998, help="連接埠"),
    store: str = typer.Option("assets/console/chat.jsonl", help="團隊對話保存檔"),
    enable_agent: bool = typer.Option(
        False, "--enable-agent",
        help="開啟 SparkAgent（AI 開發代理，可讀寫本目錄檔案並跑指令）"),
    agent_backend: str = typer.Option(
        "auto", "--agent-backend",
        help="cli＝本機已登入的 claude/codex（訂閱、免金鑰）；api＝API 金鑰；"
             "bedrock＝Amazon Bedrock（EC2 IAM role、免金鑰）；auto＝自動"),
):
    """啟動團隊協作主控台（密碼登入 + 共享對話室，一起開發此 Agent）。"""
    import os
    import shutil

    from .web.console import PASSWORD_IS_GENERATED, TEAM_PASSWORD, run_console

    _c(f"▶ SparkReel 團隊主控台 → http://{host}:{port}", typer.colors.BRIGHT_CYAN, bold=True)
    if PASSWORD_IS_GENERATED:
        _c(f"  🔒 本次隨機密碼：{TEAM_PASSWORD}", typer.colors.YELLOW, bold=True)
        _c("     ⚠ 未設定 SPARKREEL_CONSOLE_PASSWORD——此密碼隨機產生、重啟即失效；"
           "正式或對外部署請改設固定強密碼。", typer.colors.YELLOW)
    else:
        _c("  🔒 團隊密碼：已由環境變數 SPARKREEL_CONSOLE_PASSWORD 設定。", typer.colors.GREEN)
    _c("  💬 團隊成員以密碼登入後即可在共享對話室協作；輸入 /help 呼叫 SparkBot。",
       typer.colors.WHITE)
    if agent_backend in ("cli", "api", "bedrock"):
        os.environ["SPARKREEL_AGENT_BACKEND"] = agent_backend
    if enable_agent:
        os.environ["SPARKREEL_AGENT_ENABLED"] = "1"
        backend = os.environ.get("SPARKREEL_AGENT_BACKEND", "auto")
        use_cli = backend == "cli" or (backend == "auto"
                                       and not os.environ.get("ANTHROPIC_API_KEY")
                                       and shutil.which("claude"))
        _c("  🧠 SparkAgent 已啟用——AI 可在此目錄讀寫檔案並執行指令。",
           typer.colors.BRIGHT_MAGENTA, bold=True)
        _c("     ⚠ 這對『拿到密碼的所有人』開放了在本機操控檔案/指令的能力——請用強密碼且只在可信任內網使用。",
           typer.colors.YELLOW)
        if backend == "bedrock":
            model = os.environ.get("SPARKREEL_BEDROCK_MODEL", "anthropic.claude-3-5-sonnet-20241022-v2:0")
            region = (os.environ.get("SPARKREEL_AWS_REGION") or os.environ.get("AWS_REGION")
                      or os.environ.get("AWS_DEFAULT_REGION") or "us-east-1")
            _c(f"     🔗 後端＝bedrock：Amazon Bedrock（{region}／{model}）——免 API 金鑰，"
               "憑證取自 EC2 instance role 或環境變數。", typer.colors.GREEN)
            _c("     ⓘ 需 IAM 具 bedrock:InvokeModel 權限，且該模型已在此區域開通。", typer.colors.WHITE)
        elif use_cli:
            has_claude = bool(shutil.which("claude"))
            _c(f"     🔗 後端＝cli：使用本機已登入的 claude{' / codex' if shutil.which('codex') else ''}"
               f"（訂閱額度、免 API 金鑰）。權限模式＝{os.environ.get('SPARKREEL_CLAUDE_PERMISSION','acceptEdits')}。",
               typer.colors.GREEN)
            if not has_claude:
                _c("     ⓘ 找不到 `claude` CLI——請先在此機器 `claude` 登入。", typer.colors.WHITE)
        else:
            has_key = bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("OPENAI_API_KEY"))
            _c("     🔗 後端＝api：需 ANTHROPIC_API_KEY / OPENAI_API_KEY。", typer.colors.GREEN)
            if not has_key:
                _c("     ⓘ 尚未偵測到金鑰——設定後 AI 才會回應（或改用 --agent-backend cli 走登入）。",
                   typer.colors.WHITE)
    run_console(host=host, port=port, store=store)


@app.command(name="make-sample")
def make_sample(out: str = typer.Option("examples", help="輸出目錄")):
    """產生一個自足的示範直播樣本（影片 + 彈幕 + 字幕）。"""
    from .sample import generate

    _c("▶ 產生示範直播樣本…", typer.colors.BRIGHT_CYAN, bold=True)
    paths = generate(out)
    for k, v in paths.items():
        _c(f"  ✓ {k}: {v}", typer.colors.GREEN)


@app.command()
def status(config: Optional[str] = typer.Option(None)):
    """顯示後端設定與 AWS 可用性。"""
    import os

    from .aws.clients import aws_status
    from .config import load_config

    cfg = load_config(config)
    st = aws_status(cfg.aws.region)
    _c(f"SparkReel v{__version__}", typer.colors.BRIGHT_CYAN, bold=True)
    _c(f"  AWS: boto3={st['boto3']} credentials={st['credentials']} "
       f"region={st['region']} → mode={st['mode']}",
       typer.colors.GREEN if st["credentials"] else typer.colors.YELLOW)
    _c(f"  Bedrock 模型：{cfg.aws.bedrock_model}", typer.colors.WHITE)
    if os.environ.get("SPARKREEL_CLOUD"):
        _c("  雲端模式：SPARKREEL_CLOUD=1 → 所有能力預設走 aws", typer.colors.GREEN)
    _c("  能力後端：", typer.colors.WHITE)
    for cap, be in cfg.backends.items():
        effective = be if (be == "local" or st["credentials"]) else "local(降級)"
        _c(f"    {cap:16s} = {be:6s}  →  {effective}", typer.colors.WHITE)
    _c("  平台輸出：", typer.colors.WHITE)
    from .styles import platform_summary
    for row in platform_summary(cfg):
        _c(f"    {row['label']:16s} {row['resolution']} ≤{row['max_sec']}s "
           f"caption={row['caption_style']}", typer.colors.WHITE)


@app.command()
def info(result_json: str = typer.Argument(..., help="result.json 路徑")):
    """讀取並漂亮列印先前的分析結果。"""
    from .models import PipelineResult

    data = Path(result_json).read_text(encoding="utf-8")
    result = PipelineResult.model_validate_json(data)
    _print_summary(result, Path(result_json).parent)


@app.callback(invoke_without_command=True)
def _root(ctx: typer.Context,
          version: bool = typer.Option(False, "--version", help="顯示版本")):
    if version:
        _c(f"SparkReel {__product_zh__} v{__version__}", typer.colors.BRIGHT_CYAN, bold=True)
        raise typer.Exit()
    if ctx.invoked_subcommand is None:
        typer.echo(ctx.get_help())


if __name__ == "__main__":
    app()
