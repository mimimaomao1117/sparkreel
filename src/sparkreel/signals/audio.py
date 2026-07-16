"""Audio energy & emotion extractor (local DSP, numpy only).

  audio_excitement — loudness envelope + sudden bursts above a moving baseline
                     (cheers, laughter, hype spikes, impact sounds)
  audio_emotion    — spectral centroid + flux gated by voice presence, a proxy
                     for vocal arousal / emotional intensity

These are intrinsically signal-processing operations, so they run locally even
when other capabilities target AWS. (When backend transcribe=aws, Amazon
Transcribe additionally supplies word-level text used by the speech extractor.)
"""
from __future__ import annotations

from typing import List

import numpy as np

from ..models import SignalTrack
from . import dsp
from .base import AnalysisContext, make_track


def extract(ctx: AnalysisContext) -> List[SignalTrack]:
    grid = ctx.grid
    if ctx.audio is None or ctx.audio_sr <= 0 or ctx.audio.size == 0:
        ctx.warn("[audio] 無音訊軌 → audio_excitement / audio_emotion 為 0。")
        z = np.zeros(grid.n)
        return [
            make_track("audio_excitement", "audio", z, ctx),
            make_track("audio_emotion", "audio", z, ctx),
        ]

    a = ctx.audio.astype(np.float32)
    sr = ctx.audio_sr
    win, hop = 1024, 512
    mag, ftimes, rms = dsp.stft_mag(a, sr, win=win, hop=hop)
    if ftimes.size == 0:
        z = np.zeros(grid.n)
        return [
            make_track("audio_excitement", "audio", z, ctx),
            make_track("audio_emotion", "audio", z, ctx),
        ]

    # ── excitement: loudness + burstiness ───────────────────────────────
    rms_n = dsp.robust_norm(rms)
    baseline_win = max(3, int(4.0 * sr / hop))       # ~4s baseline
    burst = dsp.robust_norm(dsp.baseline_relative(rms, baseline_win))
    excite_frames = np.clip(0.55 * rms_n + 0.7 * burst, 0.0, 1.0)

    # ── emotion / arousal: brightness + flux, gated by voice ────────────
    centroid = dsp.spectral_centroid(mag, sr, win)
    flux = dsp.spectral_flux(mag)
    voice_gate = (rms_n > 0.12).astype(float)
    emotion_frames = (0.5 * dsp.robust_norm(centroid) + 0.5 * dsp.robust_norm(flux)) * (
        0.4 + 0.6 * voice_gate
    )

    excite_g = dsp.robust_norm(dsp.bin_max(ftimes, excite_frames, grid))
    emotion_g = dsp.robust_norm(dsp.bin_mean(ftimes, emotion_frames, grid))

    return [
        make_track("audio_excitement", "audio", excite_g, ctx,
                   meta={"peak_rms": float(rms.max())}),
        make_track("audio_emotion", "audio", emotion_g, ctx),
    ]
