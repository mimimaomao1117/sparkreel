"""Speech extractor: transcript → keyword & sentiment signals.

Transcript source priority:
  1. Amazon Transcribe            (backend: transcribe = aws)
  2. sidecar .srt/.vtt            (VOD captions — set by the pipeline)
  3. none                          (local fallback: tracks stay near zero)

  speech_keyword   — density of high-arousal commentary ("這波太神", "五殺"…)
  speech_sentiment — magnitude of emotional polarity (excited or heated moments)
"""
from __future__ import annotations

import json
import time
from typing import List, Optional

import numpy as np

from ..ingest import load_subtitles
from ..models import SignalTrack, TranscriptSegment
from . import dsp
from .base import AnalysisContext, make_track
from .lexicon import score_text


def _transcribe_aws(ctx: AnalysisContext) -> Optional[List[TranscriptSegment]]:
    """Best-effort Amazon Transcribe job (upload → start → poll → parse)."""
    import uuid

    s3 = ctx.aws.client("s3")
    tr = ctx.aws.client("transcribe")
    if s3 is None or tr is None or not ctx.audio_path:
        return None
    bucket = ctx.config.aws.s3_bucket
    key = f"sparkreel/tmp/{uuid.uuid4().hex}.wav"
    job = f"sparkreel-{uuid.uuid4().hex[:12]}"
    try:
        s3.upload_file(ctx.audio_path, bucket, key)
        tr.start_transcription_job(
            TranscriptionJobName=job,
            Media={"MediaFileUri": f"s3://{bucket}/{key}"},
            MediaFormat="wav",
            LanguageCode=ctx.config.aws.transcribe_language,
        )
        for _ in range(60):  # up to ~5 min
            st = tr.get_transcription_job(TranscriptionJobName=job)
            status = st["TranscriptionJob"]["TranscriptionJobStatus"]
            if status in ("COMPLETED", "FAILED"):
                break
            time.sleep(5)
        if status != "COMPLETED":
            return None
        import urllib.request

        uri = st["TranscriptionJob"]["Transcript"]["TranscriptFileUri"]
        data = json.loads(urllib.request.urlopen(uri).read())
        items = data["results"]["items"]
        # group word items into ~4s utterances
        segs: List[TranscriptSegment] = []
        cur_words, cur_start, cur_end = [], None, None
        for it in items:
            if it["type"] != "pronunciation":
                continue
            st_t = float(it["start_time"])
            en_t = float(it["end_time"])
            word = it["alternatives"][0]["content"]
            if cur_start is None:
                cur_start = st_t
            cur_end = en_t
            cur_words.append(word)
            if cur_end - cur_start >= 4.0:
                segs.append(TranscriptSegment(start=cur_start, end=cur_end, text="".join(cur_words)))
                cur_words, cur_start, cur_end = [], None, None
        if cur_words:
            segs.append(TranscriptSegment(start=cur_start or 0.0, end=cur_end or 0.0, text="".join(cur_words)))
        return segs
    except Exception as e:
        ctx.warn(f"[speech] Amazon Transcribe 失敗 ({type(e).__name__}) → 改用字幕/本地。")
        return None


def _get_transcript(ctx: AnalysisContext) -> tuple[List[TranscriptSegment], str]:
    if ctx.use_aws("transcribe"):
        segs = _transcribe_aws(ctx)
        if segs:
            return segs, "aws"
    if ctx.transcript:
        return ctx.transcript, "local"
    if ctx.subtitle_path:
        segs = load_subtitles(ctx.subtitle_path)
        if segs:
            return segs, "local"
    ctx.warn("[speech] 無逐字稿來源（未啟用 Transcribe 且無字幕）→ 語音關鍵語訊號偏弱。")
    return [], "local"


def extract(ctx: AnalysisContext) -> List[SignalTrack]:
    grid = ctx.grid
    transcript, backend = _get_transcript(ctx)
    ctx.transcript = transcript

    kw_arr = np.zeros(grid.n)
    sent_arr = np.zeros(grid.n)
    for seg in transcript:
        s, i, kw = score_text(seg.text)
        seg.sentiment = s
        seg.keywords = kw
        a = grid.index(seg.start)
        b = grid.index(seg.end)
        for j in range(a, b + 1):
            kw_arr[j] = max(kw_arr[j], i if kw else 0.0)
            sent_arr[j] = max(sent_arr[j], abs(s))

    return [
        make_track("speech_keyword", "speech", kw_arr, ctx, backend=backend,
                   meta={"segments": float(len(transcript))}),
        make_track("speech_sentiment", "speech", sent_arr, ctx, backend=backend),
    ]
