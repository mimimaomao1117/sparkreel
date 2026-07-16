"""Narrative & copy generation for each clip.

Local backend: interpretable templates driven by the highlight's own evidence
(keywords, dominant signals). AWS backend (narrative = aws): Amazon Bedrock
(Claude) writes the title / hook / description / hashtags from the same evidence.

Also provides:
  - predict_quality(): a 0..100 heuristic quality score per highlight
  - montage_order(): hook → build → payoff ordering for the optional 精華合輯
"""
from __future__ import annotations

import json
from typing import Dict, List

from ..config import Config
from ..models import Highlight
from ..signals.base import AnalysisContext

_HOOKS = {
    "punchy": ["別眨眼👀 這波直接封神", "全場暴動的一波🔥", "這操作我直接跪了…",
               "⚠️高能預警 3秒後炸裂", "你敢信這是直播？"],
    "aesthetic": ["這一刻我起雞皮疙瘩了✨", "值得重播一百次的瞬間", "美到想截圖收藏📸",
                  "直播裡最讓人安靜的一秒"],
    "informative": ["為什麼這波讓全場暴動？", "一次看懂這個神操作", "高手都是這樣打的",
                    "重點在最後3秒👇"],
}
_TITLES = ["{kw}！這段太神了", "這波{kw}直接炸裂", "全場暴動：{kw}",
           "封神時刻：{kw}", "直播高光：{kw}"]

_BASE_TAGS = ["#直播精華", "#SparkReel", "#亮點秒剪"]
_PLATFORM_TAGS = {
    "tiktok": ["#tiktok", "#fyp", "#foryou"],
    "reels": ["#reels", "#instagram", "#viral"],
    "shorts": ["#shorts", "#youtubeshorts"],
}


def _top_keyword(h: Highlight) -> str:
    if h.keywords:
        return h.keywords[0]
    if h.dominant_signals:
        return h.dominant_signals[0]
    return "精華"


def hook_for(h: Highlight, hook_style: str) -> str:
    pool = _HOOKS.get(hook_style, _HOOKS["punchy"])
    return pool[h.index % len(pool)]


def _clean_tag(word: str) -> str:
    w = "".join(ch for ch in word if ch.isalnum() or "一" <= ch <= "鿿")
    return f"#{w}" if w else ""


def hashtags_for(h: Highlight, platform_key: str) -> List[str]:
    tags = list(_BASE_TAGS) + _PLATFORM_TAGS.get(platform_key, [])
    for kw in h.keywords[:3]:
        t = _clean_tag(kw)
        if t and t not in tags:
            tags.append(t)
    return tags[:10]


def predict_quality(h: Highlight, cfg: Config) -> float:
    """0..100 heuristic: fused strength + multimodal breadth + duration fit."""
    q = h.score * 55.0
    mods = {c.modality for c in h.components if c.value > 0.3}
    q += min(4, len(mods)) * 6.5
    tgt = cfg.editing.target_clip_sec
    fit = 1.0 - min(1.0, abs(h.duration - tgt) / tgt)
    q += fit * 16.0
    return round(min(100.0, q), 1)


def _bedrock_narrative(ctx: AnalysisContext, h: Highlight, platform_key: str) -> Dict | None:
    client = ctx.aws.client("bedrock-runtime")
    if client is None:
        return None
    try:
        prompt = (
            f"你是短影音社群小編。根據以下直播高光片段資訊，為 {platform_key} 平台"
            "生成吸睛文案，輸出 JSON："
            '{"title":..., "hook":..., "description":..., "hashtags":[...]}。只輸出 JSON。\n'
            f"高光理由：{h.reason}\n關鍵語：{h.keywords}\n逐字稿：{h.transcript_excerpt}\n"
            f"熱門彈幕：{h.chat_excerpt}"
        )
        body = json.dumps({
            "anthropic_version": "bedrock-2023-05-31",
            "max_tokens": 600,
            "messages": [{"role": "user", "content": prompt}],
        })
        resp = client.invoke_model(modelId=ctx.config.aws.bedrock_model, body=body)
        payload = json.loads(resp["body"].read())
        # newer Claude models may return thinking blocks first — take the text block
        text = next((b.get("text", "") for b in payload.get("content", [])
                     if b.get("type") == "text"), "")
        return json.loads(text[text.index("{"): text.rindex("}") + 1])
    except Exception as e:
        ctx.warn(f"[narrative] Bedrock 文案生成失敗 ({type(e).__name__}) → 使用本地樣板。")
        return None


def generate_base(ctx: AnalysisContext, h: Highlight, platform_key: str = "tiktok") -> Dict:
    """Return {title, hook, description, hashtags} for a highlight."""
    if ctx.use_aws("narrative"):
        got = _bedrock_narrative(ctx, h, platform_key)
        if got:
            got.setdefault("hashtags", hashtags_for(h, platform_key))
            got.setdefault("hook", hook_for(h, ctx.config.platform(platform_key).hook_style))
            return got

    kw = _top_keyword(h)
    title = _TITLES[h.index % len(_TITLES)].format(kw=kw)
    hook = hook_for(h, ctx.config.platform(platform_key).hook_style)
    tags = hashtags_for(h, platform_key)
    desc = f"{title}｜直播高光時刻，由 SparkReel 亮點秒剪 AI 自動偵測與剪輯。 " + " ".join(tags[:5])
    return {"title": title, "hook": hook, "description": desc, "hashtags": tags}


def montage_order(highlights: List[Highlight]) -> List[Highlight]:
    """hook (best, teased) → chronological build → payoff (best, full)."""
    if not highlights:
        return []
    best = max(highlights, key=lambda h: h.score)
    body = sorted(highlights, key=lambda h: h.start)
    return [best] + body  # first entry rendered as a short cold-open teaser
