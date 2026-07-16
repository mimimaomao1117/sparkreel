"""Multimodal signal extractors.

Each extractor returns a list of normalized SignalTrack objects aligned to the
analysis grid, and internally chooses a local engine or an AWS adapter based on
config + credential availability (see AnalysisContext.use_aws).
"""
from .base import AnalysisContext, make_track  # noqa: F401
from . import audio, chat_sentiment, speech, visual  # noqa: F401

# (modality-label, extractor) — pipeline runs these in order; speech populates
# the transcript that the editing stage later reuses.
EXTRACTORS = [
    ("visual", visual.extract),
    ("audio", audio.extract),
    ("speech", speech.extract),
    ("chat", chat_sentiment.extract),
]

__all__ = ["AnalysisContext", "make_track", "EXTRACTORS",
           "audio", "visual", "speech", "chat_sentiment"]
