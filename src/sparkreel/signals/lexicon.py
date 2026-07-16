"""Lightweight bilingual (繁中 + 英文 + 遊戲/直播俚語) sentiment & hype lexicon.

Used as the *local fallback* for both chat sentiment and speech keyword/sentiment.
When the Bedrock backend is enabled, an LLM replaces this — but the lexicon keeps
the demo fully offline and interpretable (you can see exactly which words fired).
"""
from __future__ import annotations

import re
from typing import List, Tuple

# High-arousal excitement / "this is a highlight" markers → weight 0..1
HYPE = {
    "太扯": 1.0, "扯": 0.5, "誇張": 0.7, "猛": 0.8, "超猛": 0.95, "神": 0.85,
    "封神": 1.0, "史詩": 1.0, "教科書": 0.9, "神操作": 1.0, "太神": 0.95,
    "太強": 0.9, "強": 0.4, "屌": 0.7, "帥": 0.6, "炸裂": 0.9, "炸": 0.6,
    "破防": 0.7, "雞皮疙瘩": 0.9, "起雞皮": 0.9, "醒醒": 0.6, "笑死": 0.7,
    "笑爛": 0.7, "重播": 0.8, "剪": 0.6, "clip": 0.85, "highlight": 0.9,
    "amazing": 0.9, "insane": 0.95, "crazy": 0.85, "poggers": 0.9, "pog": 0.85,
    "這波": 0.7, "這操作": 0.8, "這反應": 0.7, "讚啦": 0.7, "絕": 0.7,
    "頂": 0.6, "王": 0.5, "無解": 0.7, "秒殺": 0.8, "翻盤": 0.9, "逆轉": 0.9,
    "五殺": 1.0, "團滅": 0.9, "極限": 0.8, "壓線": 0.8,
}

POSITIVE = {
    "讚": 0.6, "喜歡": 0.5, "愛": 0.6, "好看": 0.5, "厲害": 0.7, "棒": 0.6,
    "可愛": 0.5, "感動": 0.6, "溫馨": 0.5, "good": 0.5, "nice": 0.5, "love": 0.6,
    "gg": 0.6, "ggwp": 0.7, "讚啦": 0.7, "推": 0.5, "訂閱": 0.3,
}

NEGATIVE = {
    "爛": -0.7, "無聊": -0.6, "雷": -0.6, "垃圾": -0.8, "划水": -0.5, "演": -0.4,
    "煩": -0.5, "難看": -0.6, "退訂": -0.6, "菜": -0.5, "送頭": -0.5, "崩": -0.5,
    "boring": -0.6, "trash": -0.8, "bad": -0.5, "cringe": -0.6,
}

EMOJI_POS = set("🔥😂🤣👏💪❤️❤😍🎉👍✨🥳😮😱🤯💯🙌⚡")
EMOJI_NEG = set("😡🤬👎😴💤🙄😒")

_RE_LAUGH = re.compile(r"(哈{2,}|ha{2,}|w+|w{2,}|草{2,}|lol|lmao)", re.IGNORECASE)
_RE_SIX = re.compile(r"6{3,}")            # 666 洗版 = 讚/猛
_RE_REPEAT_CHAR = re.compile(r"(.)\1{2,}")  # any char repeated 3+
_RE_EXCLAIM = re.compile(r"[!！]{2,}")


def _emoji_score(text: str) -> Tuple[float, float]:
    pos = sum(1 for ch in text if ch in EMOJI_POS)
    neg = sum(1 for ch in text if ch in EMOJI_NEG)
    intensity = min(1.0, 0.35 * (pos + neg))
    sentiment = 0.5 * pos - 0.6 * neg
    return sentiment, intensity


def score_text(text: str) -> Tuple[float, float, List[str]]:
    """Return (sentiment[-1..1], intensity[0..1], matched_keywords)."""
    if not text:
        return 0.0, 0.0, []
    low = text.lower()
    sentiment = 0.0
    intensity = 0.0
    keywords: List[str] = []

    for word, w in HYPE.items():
        if word in low:
            intensity += w
            sentiment += min(0.6, w)
            keywords.append(word)
    for word, w in POSITIVE.items():
        if word in low:
            sentiment += w
            intensity += 0.3 * w
    for word, w in NEGATIVE.items():
        if word in low:
            sentiment += w              # w already negative
            intensity += 0.3 * abs(w)
            keywords.append(word)

    es, ei = _emoji_score(text)
    sentiment += es
    intensity += ei

    # arousal from typographic emphasis / spam
    if _RE_SIX.search(text):
        intensity += 0.7
        sentiment += 0.4
        keywords.append("666")
    if _RE_LAUGH.search(low):
        intensity += 0.5
        sentiment += 0.3
    if _RE_REPEAT_CHAR.search(text):
        intensity += 0.3
    if _RE_EXCLAIM.search(text):
        intensity += 0.25

    sentiment = max(-1.0, min(1.0, sentiment))
    intensity = max(0.0, min(1.0, intensity))
    # de-dup keywords, keep order
    seen = set()
    kw = [k for k in keywords if not (k in seen or seen.add(k))]
    return sentiment, intensity, kw
