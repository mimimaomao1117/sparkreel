"""Ingestion: media probing/decoding, chat/danmaku, subtitles, and web/URL sources."""
from .media import probe, extract_audio, read_audio_samples, iter_frames, FFmpegError, run_ff  # noqa: F401
from .chat import load_chat  # noqa: F401
from .subtitles import load_subtitles, find_sidecar  # noqa: F401
from .web import fetch_source, is_url, WebSource  # noqa: F401
