"""Small numeric helpers shared by the signal extractors.

Deliberately dependency-light (numpy only) so the whole analysis stack runs
without scipy/librosa.
"""
from __future__ import annotations

from typing import Iterable, Tuple

import numpy as np

from ..models import TimeGrid


def moving_average(x: np.ndarray, win: int) -> np.ndarray:
    if win <= 1 or x.size == 0:
        return x
    win = min(win, x.size)
    k = np.ones(win, dtype=float) / win
    return np.convolve(x, k, mode="same")


def robust_norm(x: np.ndarray, lo: float = 5.0, hi: float = 95.0) -> np.ndarray:
    """Percentile-clipped min-max to 0..1 (resistant to single outliers)."""
    x = np.asarray(x, dtype=float)
    if x.size == 0:
        return x
    a, b = np.percentile(x, lo), np.percentile(x, hi)
    if b - a < 1e-9:
        m = float(x.max())
        return np.zeros_like(x) if m < 1e-9 else np.clip(x / m, 0.0, 1.0)
    return np.clip((x - a) / (b - a), 0.0, 1.0)


def minmax(x: np.ndarray) -> np.ndarray:
    x = np.asarray(x, dtype=float)
    if x.size == 0:
        return x
    mn, mx = float(x.min()), float(x.max())
    return np.zeros_like(x) if mx - mn < 1e-9 else (x - mn) / (mx - mn)


def baseline_relative(x: np.ndarray, win: int) -> np.ndarray:
    """Positive deviation above a slow moving baseline — isolates *bursts*."""
    if x.size == 0:
        return x
    base = moving_average(x, max(3, win))
    return np.clip(x - base, 0.0, None)


def _agg_to_grid(times: Iterable[float], values: Iterable[float], grid: TimeGrid, how: str) -> np.ndarray:
    if how == "max":
        out = np.zeros(grid.n)
        for t, v in zip(times, values):
            i = grid.index(t)
            if v > out[i]:
                out[i] = v
        return out
    if how == "sum":
        out = np.zeros(grid.n)
        for t, v in zip(times, values):
            out[grid.index(t)] += v
        return out
    # mean
    s = np.zeros(grid.n)
    c = np.zeros(grid.n)
    for t, v in zip(times, values):
        i = grid.index(t)
        s[i] += v
        c[i] += 1
    c[c == 0] = 1
    return s / c


def bin_max(times, values, grid) -> np.ndarray:
    return _agg_to_grid(times, values, grid, "max")


def bin_sum(times, values, grid) -> np.ndarray:
    return _agg_to_grid(times, values, grid, "sum")


def bin_mean(times, values, grid) -> np.ndarray:
    return _agg_to_grid(times, values, grid, "mean")


def stft_mag(a: np.ndarray, sr: int, win: int = 1024, hop: int = 512) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Return (magnitude[n_frames, n_bins], frame_times, rms[n_frames])."""
    if a.size < win:
        return np.zeros((0, win // 2 + 1)), np.zeros(0), np.zeros(0)
    n = 1 + (a.size - win) // hop
    window = np.hanning(win).astype(np.float32)
    # strided frames without copying the whole signal twice
    frames = np.stack([a[i * hop : i * hop + win] * window for i in range(n)])
    mag = np.abs(np.fft.rfft(frames, axis=1))
    rms = np.sqrt(np.mean(frames.astype(np.float64) ** 2, axis=1))
    times = (np.arange(n) * hop + win / 2) / sr
    return mag, times, rms


def spectral_centroid(mag: np.ndarray, sr: int, win: int) -> np.ndarray:
    if mag.shape[0] == 0:
        return np.zeros(0)
    freqs = np.fft.rfftfreq(win, d=1.0 / sr)
    denom = mag.sum(axis=1)
    denom[denom < 1e-9] = 1e-9
    return (mag * freqs).sum(axis=1) / denom


def spectral_flux(mag: np.ndarray) -> np.ndarray:
    if mag.shape[0] == 0:
        return np.zeros(0)
    diff = np.diff(mag, axis=0)
    flux = np.clip(diff, 0, None).sum(axis=1)
    return np.concatenate([[0.0], flux])
