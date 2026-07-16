"""Chat / danmaku (彈幕) extractor.

  chat_volume    — message rate +洗版 bursts above a moving baseline
  chat_sentiment — crowd emotional intensity (excitement / hype), volume-weighted

Local backend uses the interpretable lexicon; AWS backend (chat_sentiment = aws)
classifies messages in batches with Amazon Bedrock (Claude) for nuanced,
context-aware sentiment. Both write sentiment/intensity back onto each message.
"""
from __future__ import annotations

import json
from typing import Dict, List, Optional, Tuple

import numpy as np

from ..models import ChatMessage, SignalTrack
from . import dsp
from .base import AnalysisContext, make_track
from .lexicon import score_text


def _score_local(messages: List[ChatMessage]) -> None:
    for m in messages:
        s, i, kw = score_text(m.text)
        m.sentiment = s
        m.intensity = i
        m.is_emote_spam = "666" in kw or i >= 0.8


def _score_bedrock(ctx: AnalysisContext, messages: List[ChatMessage]) -> bool:
    """Classify messages with Bedrock. Returns True on success, else caller falls back."""
    client = ctx.aws.client("bedrock-runtime")
    if client is None:
        return False
    model = ctx.config.aws.bedrock_model
    try:
        for start in range(0, len(messages), 40):
            batch = messages[start : start + 40]
            listing = "\n".join(f"{i}: {m.text}" for i, m in enumerate(batch))
            prompt = (
                "你是直播彈幕情緒分析器。針對每一則訊息，輸出 JSON 陣列，"
                "每個元素為 {\"i\":序號, \"s\":情感(-1~1), \"a\":情緒強度(0~1)}。"
                "只輸出 JSON。\n\n" + listing
            )
            body = json.dumps({
                "anthropic_version": "bedrock-2023-05-31",
                "max_tokens": 2000,
                "messages": [{"role": "user", "content": prompt}],
            })
            resp = client.invoke_model(modelId=model, body=body)
            payload = json.loads(resp["body"].read())
            # newer Claude models may return thinking blocks first — take the text block
            text = next((b.get("text", "") for b in payload.get("content", [])
                         if b.get("type") == "text"), "")
            arr = json.loads(text[text.index("[") : text.rindex("]") + 1])
            for rec in arr:
                idx = int(rec["i"])
                if 0 <= idx < len(batch):
                    batch[idx].sentiment = float(rec.get("s", 0))
                    batch[idx].intensity = float(rec.get("a", 0))
        return True
    except Exception as e:
        ctx.warn(f"[chat] Bedrock 情緒分析失敗 ({type(e).__name__}) → 改用本地詞典。")
        return False


def extract(ctx: AnalysisContext) -> List[SignalTrack]:
    grid = ctx.grid
    msgs = ctx.chat
    if not msgs:
        ctx.warn("[chat] 無彈幕/聊天資料 → chat_volume / chat_sentiment 為 0。")
        z = np.zeros(grid.n)
        return [
            make_track("chat_volume", "chat", z, ctx),
            make_track("chat_sentiment", "chat", z, ctx),
        ]

    backend = "local"
    if ctx.use_aws("chat_sentiment") and _score_bedrock(ctx, msgs):
        backend = "aws"
    else:
        _score_local(msgs)

    times = [m.t for m in msgs]
    counts = dsp.bin_sum(times, [1.0] * len(msgs), grid)
    rate_n = dsp.robust_norm(counts)
    burst = dsp.robust_norm(dsp.baseline_relative(counts, max(3, int(6.0 / grid.dt))))
    volume_g = np.clip(0.5 * rate_n + 0.7 * burst, 0.0, 1.0)

    intensities = [float(m.intensity or 0.0) for m in msgs]
    intensity_bin = dsp.bin_mean(times, intensities, grid)
    sentiment_g = dsp.robust_norm(intensity_bin * (0.5 + 0.5 * volume_g))

    return [
        make_track("chat_volume", "chat", volume_g, ctx, backend=backend,
                   meta={"messages": float(len(msgs))}),
        make_track("chat_sentiment", "chat", sentiment_g, ctx, backend=backend),
    ]
