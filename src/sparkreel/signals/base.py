"""Analysis context shared by every signal extractor."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional

import numpy as np

from ..aws.clients import AwsClients
from ..config import Config
from ..models import ChatMessage, MediaInfo, SignalTrack, TimeGrid, TranscriptSegment


def make_track(name, modality, arr, ctx, backend="local", meta=None) -> SignalTrack:
    """Build a normalized SignalTrack aligned to the context grid."""
    samples = [float(max(0.0, min(1.0, x))) for x in list(arr)]
    return SignalTrack(
        name=name,
        modality=modality,
        hz=1.0 / ctx.grid.dt,
        samples=samples,
        weight=float(ctx.config.fusion.weights.get(name, 1.0)),
        backend=backend,
        meta=meta or {},
    )


@dataclass
class AnalysisContext:
    video_path: str
    media: MediaInfo
    grid: TimeGrid
    config: Config
    aws: AwsClients
    audio_path: Optional[str] = None
    audio_sr: int = 0
    audio: Optional[np.ndarray] = None
    chat: List[ChatMessage] = field(default_factory=list)
    subtitle_path: Optional[str] = None
    transcript: List[TranscriptSegment] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    segments: list = field(default_factory=list)        # timeline classification (fusion.segment)
    stream_profile: dict = field(default_factory=dict)  # kind → fraction of stream

    def warn(self, msg: str) -> None:
        if msg not in self.warnings:
            self.warnings.append(msg)

    def use_aws(self, capability: str) -> bool:
        """True only if this capability is configured for aws AND creds resolve."""
        if self.config.backend(capability) != "aws":
            return False
        if not self.aws.credentials_available():
            self.warn(
                f"[{capability}] 設定為 aws，但未偵測到 AWS 憑證 → 自動降級為本地備援。"
            )
            return False
        return True
