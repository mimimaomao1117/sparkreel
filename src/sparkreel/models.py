"""SparkReel shared data contracts.

All modules communicate through these pydantic models. Numeric signal arrays are
kept as plain Python lists here (for JSON serialization); heavy numeric work uses
numpy internally inside extractors and converts at the boundary.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional

from pydantic import BaseModel, Field


# ─────────────────────────────────────────────────────────────────────────────
# Time grid (plain helper, not serialized)
# ─────────────────────────────────────────────────────────────────────────────
@dataclass
class TimeGrid:
    """Uniform analysis grid over the media duration."""

    dt: float          # seconds per bin
    n: int             # number of bins

    @classmethod
    def for_duration(cls, duration: float, hz: float) -> "TimeGrid":
        dt = 1.0 / hz
        n = max(1, int(round(duration * hz)))
        return cls(dt=dt, n=n)

    @property
    def duration(self) -> float:
        return self.n * self.dt

    def index(self, t: float) -> int:
        return min(self.n - 1, max(0, int(t / self.dt)))

    def time(self, i: int) -> float:
        return i * self.dt

    def times(self) -> List[float]:
        return [i * self.dt for i in range(self.n)]


# ─────────────────────────────────────────────────────────────────────────────
# Ingested media & chat
# ─────────────────────────────────────────────────────────────────────────────
class MediaInfo(BaseModel):
    path: str
    duration: float = 0.0            # seconds
    fps: float = 0.0
    width: int = 0
    height: int = 0
    has_audio: bool = False
    audio_sample_rate: int = 0
    video_codec: str = ""
    audio_codec: str = ""
    size_bytes: int = 0

    @property
    def aspect_ratio(self) -> float:
        return (self.width / self.height) if self.height else 0.0


class ChatMessage(BaseModel):
    """A single danmaku / live-chat message on the stream timeline."""

    t: float                          # seconds from stream start
    user: str = ""
    text: str = ""
    sentiment: Optional[float] = None  # -1..1 (filled by extractor)
    intensity: Optional[float] = None  # 0..1 emotional intensity
    is_emote_spam: bool = False


class TranscriptSegment(BaseModel):
    start: float
    end: float
    text: str
    speaker: str = ""
    sentiment: Optional[float] = None
    keywords: List[str] = Field(default_factory=list)


# ─────────────────────────────────────────────────────────────────────────────
# Signals & fusion
# ─────────────────────────────────────────────────────────────────────────────
class SignalTrack(BaseModel):
    """A normalized (0..1) signal sampled onto the global time grid."""

    name: str                          # e.g. "audio_excitement"
    modality: str                      # audio | speech | chat | visual
    hz: float                          # sampling rate of `samples`
    samples: List[float]               # normalized 0..1, aligned to grid
    weight: float = 1.0
    backend: str = "local"             # local | aws
    meta: Dict[str, float] = Field(default_factory=dict)


class SignalContribution(BaseModel):
    name: str
    modality: str
    value: float                       # this signal's value at the peak (0..1)
    weighted: float                    # value * normalized weight


class Highlight(BaseModel):
    index: int
    start: float                       # clip window start (with padding)
    end: float                         # clip window end
    peak_t: float                      # time of score peak
    score: float                       # fused peak score 0..1
    duration: float
    components: List[SignalContribution] = Field(default_factory=list)
    dominant_signals: List[str] = Field(default_factory=list)
    title: str = ""
    keywords: List[str] = Field(default_factory=list)
    transcript_excerpt: str = ""
    chat_excerpt: List[str] = Field(default_factory=list)
    reason: str = ""                   # human-readable why-this-is-a-highlight
    virality: int = 0                  # 0..99 predicted social performance
    virality_parts: Dict[str, int] = Field(default_factory=dict)  # hook/emotion/value/trend
    reframe_cx: float = 0.5            # subject horizontal centre 0..1 (speaker-track crop)
    broll: List[Dict[str, float]] = Field(default_factory=list)   # [{at,dur,src_start}] cutaways
    segment_kind: str = ""            # talk | hype | reaction | action | lull (dispatch hint)


# ─────────────────────────────────────────────────────────────────────────────
# Editing & platform output
# ─────────────────────────────────────────────────────────────────────────────
class ClipVariant(BaseModel):
    platform: str                      # tiktok | reels | shorts
    platform_label: str = ""
    aspect: str = "9:16"
    path: str = ""
    thumbnail: str = ""
    duration: float = 0.0
    width: int = 0
    height: int = 0
    size_bytes: int = 0
    caption_style: str = ""


class Clip(BaseModel):
    """A produced highlight short with one or more per-platform variants."""

    highlight_index: int
    narrative_role: str = "standalone"   # hook | build | payoff | standalone
    title: str = ""
    hook: str = ""                        # opening line / on-screen hook
    description: str = ""
    hashtags: List[str] = Field(default_factory=list)
    score: float = 0.0
    quality_score: float = 0.0            # 0..100 predicted quality
    virality: int = 0                     # 0..99 predicted social performance
    virality_parts: Dict[str, int] = Field(default_factory=dict)
    segment_kind: str = ""                # talk | hype | reaction | action | lull
    variants: List[ClipVariant] = Field(default_factory=list)
    moderation_status: str = "pass"       # pass | blurred | flagged | rejected


# ─────────────────────────────────────────────────────────────────────────────
# Moderation / compliance
# ─────────────────────────────────────────────────────────────────────────────
class ModerationFinding(BaseModel):
    t_start: float
    t_end: float
    modality: str                      # visual | audio | text
    label: str
    confidence: float                  # 0..1
    action: str                        # pass | blur | mute | bleep | flag | reject
    source: str = "local"              # local | rekognition | bedrock
    detail: str = ""


class ModerationReport(BaseModel):
    status: str = "pass"               # pass | flagged | rejected | needs_review
    findings: List[ModerationFinding] = Field(default_factory=list)
    needs_human_review: bool = False
    summary: str = ""
    scanned_modalities: List[str] = Field(default_factory=list)


# ─────────────────────────────────────────────────────────────────────────────
# Metrics & result
# ─────────────────────────────────────────────────────────────────────────────
class StageTiming(BaseModel):
    stage: str
    seconds: float


class Metrics(BaseModel):
    source_duration_sec: float = 0.0
    processing_sec: float = 0.0
    realtime_factor: float = 0.0          # source_duration / processing
    highlights_found: int = 0
    clips_produced: int = 0
    total_output_sec: float = 0.0
    compression_ratio: float = 0.0        # source / output duration
    manual_edit_baseline_sec: float = 0.0 # estimated human edit time
    time_saved_sec: float = 0.0
    time_saved_ratio: float = 0.0         # 0..1
    automation_degree: float = 0.0        # 0..1 fraction requiring no human touch
    avg_highlight_score: float = 0.0
    avg_quality_score: float = 0.0        # 0..100
    detection_confidence: float = 0.0     # mean peak prominence
    moderation_flag_rate: float = 0.0


class PipelineResult(BaseModel):
    job_id: str
    product: str = "SparkReel"
    media: MediaInfo
    backends: Dict[str, str] = Field(default_factory=dict)
    grid_hz: float = 2.0
    highlight_threshold: float = 0.55
    score_curve: List[float] = Field(default_factory=list)     # fused 0..1 over grid
    score_times: List[float] = Field(default_factory=list)
    tracks: List[SignalTrack] = Field(default_factory=list)
    highlights: List[Highlight] = Field(default_factory=list)
    clips: List[Clip] = Field(default_factory=list)
    moderation: ModerationReport = Field(default_factory=ModerationReport)
    metrics: Metrics = Field(default_factory=Metrics)
    timings: List[StageTiming] = Field(default_factory=list)
    created_at: str = ""
    warnings: List[str] = Field(default_factory=list)
    stream_profile: Dict[str, float] = Field(default_factory=dict)  # kind → fraction of stream

    def to_json(self, **kw) -> str:
        return self.model_dump_json(**kw)
