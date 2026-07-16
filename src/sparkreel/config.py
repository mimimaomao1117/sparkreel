"""Configuration loading for SparkReel.

Resolution order (later overrides earlier):
    1. Built-in DEFAULTS (this file — guarantees the app always runs)
    2. config/default.yaml  (repo-root, human-facing documented config)
    3. explicit override file (path arg or $SPARKREEL_CONFIG)
    4. environment variables (SPARKREEL_BACKEND_<CAP>, SPARKREEL_AWS_REGION, ...)
"""
from __future__ import annotations

import copy
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml
from pydantic import BaseModel, Field

# Built-in defaults — mirror of config/default.yaml so the pipeline runs even if
# the YAML is missing. The YAML overlays these values.
DEFAULTS: Dict[str, Any] = {
    "project": "sparkreel",
    "version": "0.1.0",
    "backends": {
        "transcribe": "local",
        "chat_sentiment": "local",
        "visual": "local",
        "moderation": "local",
        "narrative": "local",
    },
    "aws": {
        "region": "us-east-1",
        "s3_bucket": "sparkreel-media",
        "bedrock_model": "anthropic.claude-3-5-sonnet-20241022-v2:0",
        "transcribe_language": "zh-TW",
        "rekognition_min_confidence": 80,
        "mediaconvert_endpoint": "",
    },
    "analysis": {"grid_hz": 2.0, "visual_sample_fps": 4.0, "smoothing_sec": 2.0},
    "fusion": {
        "weights": {
            "audio_excitement": 1.0,
            "audio_emotion": 0.8,
            "speech_keyword": 0.9,
            "speech_sentiment": 0.7,
            "chat_volume": 1.2,
            "chat_sentiment": 1.0,
            "visual_motion": 0.9,
            "visual_scene": 0.6,
            "visual_face": 0.7,
            "visual_expression": 1.1,
            "visual_emotion": 1.2,
        },
        "peak": {"min_score": 0.55, "min_gap_sec": 8.0, "max_highlights": 8},
    },
    "editing": {
        "pad_before_sec": 1.5,
        "pad_after_sec": 1.2,
        "min_clip_sec": 6.0,
        "max_clip_sec": 45.0,
        "target_clip_sec": 22.0,
        "crossfade_sec": 0.25,
        "broll": False,          # auto-insert local B-roll cutaways (opt-in)
        "speaker_track": True,   # face-centred crop for landscape sources
        "narrative": True,       # anchor clips on the payoff (build-up → climax → tail)
    },
    "platforms": {
        "tiktok": {"label": "TikTok", "aspect": "9:16", "max_sec": 60,
                   "caption_style": "bold_center", "hook_style": "punchy"},
        "reels": {"label": "IG Reels", "aspect": "9:16", "max_sec": 90,
                  "caption_style": "bold_center", "hook_style": "aesthetic"},
        "shorts": {"label": "YouTube Shorts", "aspect": "9:16", "max_sec": 60,
                   "caption_style": "bold_top", "hook_style": "informative"},
    },
    "moderation": {
        "block_labels": ["explicit_nudity", "graphic_violence", "hate_symbol", "self_harm"],
        "blur_labels": ["weapon", "alcohol", "tobacco", "gambling"],
        "flag_labels": ["profanity", "sensitive_political", "personal_info"],
        "audio_profanity_action": "mute",
        "require_human_review_above": 0.85,
    },
}


# ── typed views ──────────────────────────────────────────────────────────────
class AwsConfig(BaseModel):
    region: str = "us-east-1"
    s3_bucket: str = "sparkreel-media"
    bedrock_model: str = ""
    transcribe_language: str = "zh-TW"
    rekognition_min_confidence: int = 80
    mediaconvert_endpoint: str = ""


class AnalysisConfig(BaseModel):
    grid_hz: float = 2.0
    visual_sample_fps: float = 4.0
    smoothing_sec: float = 2.0


class PeakConfig(BaseModel):
    min_score: float = 0.55
    min_gap_sec: float = 8.0
    max_highlights: int = 8


class FusionConfig(BaseModel):
    weights: Dict[str, float] = Field(default_factory=dict)
    peak: PeakConfig = Field(default_factory=PeakConfig)


class EditingConfig(BaseModel):
    pad_before_sec: float = 1.5
    pad_after_sec: float = 1.2
    min_clip_sec: float = 6.0
    max_clip_sec: float = 45.0
    target_clip_sec: float = 22.0
    crossfade_sec: float = 0.25
    broll: bool = False
    speaker_track: bool = True
    narrative: bool = True


class PlatformPreset(BaseModel):
    label: str = ""
    aspect: str = "9:16"
    max_sec: int = 60
    caption_style: str = "bold_center"
    hook_style: str = "punchy"


class ModerationConfig(BaseModel):
    block_labels: List[str] = Field(default_factory=list)
    blur_labels: List[str] = Field(default_factory=list)
    flag_labels: List[str] = Field(default_factory=list)
    audio_profanity_action: str = "mute"
    require_human_review_above: float = 0.85


class Config(BaseModel):
    project: str = "sparkreel"
    version: str = "0.1.0"
    backends: Dict[str, str] = Field(default_factory=dict)
    aws: AwsConfig = Field(default_factory=AwsConfig)
    analysis: AnalysisConfig = Field(default_factory=AnalysisConfig)
    fusion: FusionConfig = Field(default_factory=FusionConfig)
    editing: EditingConfig = Field(default_factory=EditingConfig)
    platforms: Dict[str, PlatformPreset] = Field(default_factory=dict)
    moderation: ModerationConfig = Field(default_factory=ModerationConfig)

    # convenience accessors
    def backend(self, capability: str) -> str:
        return self.backends.get(capability, "local")

    def platform(self, name: str) -> PlatformPreset:
        return self.platforms.get(name, PlatformPreset())

    @property
    def platform_names(self) -> List[str]:
        return list(self.platforms.keys())


# ── loading helpers ──────────────────────────────────────────────────────────
def _deep_merge(base: Dict[str, Any], overlay: Dict[str, Any]) -> Dict[str, Any]:
    out = copy.deepcopy(base)
    for k, v in (overlay or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def _repo_default_yaml() -> Optional[Path]:
    # src/sparkreel/config.py -> parents[2] == repo root
    candidate = Path(__file__).resolve().parents[2] / "config" / "default.yaml"
    if candidate.exists():
        return candidate
    cwd = Path.cwd() / "config" / "default.yaml"
    return cwd if cwd.exists() else None


def _truthy(v: str) -> bool:
    return v.strip().lower() in ("1", "true", "yes", "on")


def _apply_env(data: Dict[str, Any]) -> Dict[str, Any]:
    # Per-capability override: SPARKREEL_BACKEND_VISUAL=aws  etc.
    for cap in list(data.get("backends", {}).keys()):
        env = os.environ.get(f"SPARKREEL_BACKEND_{cap.upper()}")
        if env:
            data["backends"][cap] = env
    # SPARKREEL_CLOUD=1 → flip *every* capability to aws (full cloud mode, e.g. on
    # EC2 with an instance role), except ones pinned explicitly by SPARKREEL_BACKEND_*.
    if _truthy(os.environ.get("SPARKREEL_CLOUD", "")):
        for cap in list(data.get("backends", {}).keys()):
            if not os.environ.get(f"SPARKREEL_BACKEND_{cap.upper()}"):
                data["backends"][cap] = "aws"
    if os.environ.get("SPARKREEL_BROLL"):
        data.setdefault("editing", {})["broll"] = os.environ["SPARKREEL_BROLL"] not in ("0", "false", "False", "")
    # Region: explicit SPARKREEL_AWS_REGION wins, else honour the standard AWS_* env
    # (so a deployment that only sets AWS_REGION still targets the right region).
    region_env = (os.environ.get("SPARKREEL_AWS_REGION") or os.environ.get("AWS_REGION")
                  or os.environ.get("AWS_DEFAULT_REGION"))
    if region_env:
        data.setdefault("aws", {})["region"] = region_env
    if os.environ.get("SPARKREEL_S3_BUCKET"):
        data.setdefault("aws", {})["s3_bucket"] = os.environ["SPARKREEL_S3_BUCKET"]
    # Bedrock model / inference-profile id, shared by narrative + chat_sentiment + agent.
    if os.environ.get("SPARKREEL_BEDROCK_MODEL"):
        data.setdefault("aws", {})["bedrock_model"] = os.environ["SPARKREEL_BEDROCK_MODEL"]
    return data


def load_config(path: Optional[str] = None) -> Config:
    """Load configuration with the documented resolution order."""
    data = copy.deepcopy(DEFAULTS)

    repo_yaml = _repo_default_yaml()
    if repo_yaml:
        with open(repo_yaml, "r", encoding="utf-8") as f:
            data = _deep_merge(data, yaml.safe_load(f) or {})

    override = path or os.environ.get("SPARKREEL_CONFIG")
    if override and Path(override).exists():
        with open(override, "r", encoding="utf-8") as f:
            data = _deep_merge(data, yaml.safe_load(f) or {})

    data = _apply_env(data)
    return Config.model_validate(data)
