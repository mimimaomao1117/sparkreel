"""SparkReel（亮點秒剪）

AI live-stream highlight detection & automatic short-clip generation.

從直播串流中辨識高光時刻並自動產出多平台精華短片。
多模態訊號：語音情緒、彈幕情感、視覺動態、逐字稿關鍵語。
"""

__version__ = "0.1.0"
__product__ = "SparkReel"
__product_zh__ = "亮點秒剪"

from .models import (  # noqa: E402
    MediaInfo,
    ChatMessage,
    SignalTrack,
    Highlight,
    Clip,
    ClipVariant,
    ModerationReport,
    PipelineResult,
)

__all__ = [
    "__version__",
    "MediaInfo",
    "ChatMessage",
    "SignalTrack",
    "Highlight",
    "Clip",
    "ClipVariant",
    "ModerationReport",
    "PipelineResult",
]
