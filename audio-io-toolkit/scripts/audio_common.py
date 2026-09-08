#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""audio-io-toolkit 共用模块：音频枚举与读取。

依赖：scipy（WAV 读取/重采样）+ 标准库 wave（仅读头，不读数据）。
若安装了 soundfile 则自动支持 mp3/flac/ogg/m4a（当前环境未装，WAV 全支持）。
"""

from __future__ import annotations

import os
import wave

import numpy as np

AUDIO_EXTS = (".wav", ".flac", ".mp3", ".ogg", ".m4a", ".aif", ".aiff", ".pcm")

_PCM_DTYPES = {"int16": ("<i2", 32768.0), "int32": ("<i4", 2147483648.0)}


def read_pcm(path: str, sr: int, dtype: str = "int16") -> tuple[int, np.ndarray]:
    """读无头原始 PCM（默认 16-bit 小端单声道）。调用方必须显式提供采样率。"""
    if dtype not in _PCM_DTYPES:
        raise ValueError(f"不支持的 PCM dtype：{dtype}（可选 {list(_PCM_DTYPES)}）")
    code, norm = _PCM_DTYPES[dtype]
    x = np.fromfile(path, dtype=code).astype(np.float32) / norm
    return int(sr), x


def read_any(path: str, pcm_sr: int | None = None, pcm_dtype: str = "int16",
             max_bytes: int = 300 * 1024 * 1024) -> tuple[int, np.ndarray]:
    """WAV 走 read_wav；.pcm 且提供了采样率时走 read_pcm。"""
    if path.lower().endswith(".pcm"):
        if not pcm_sr:
            raise ValueError(
                f"PCM 文件需要显式采样率（--pcm-sr）：{path}")
        return read_pcm(path, pcm_sr, pcm_dtype)
    return read_wav(path, max_bytes=max_bytes)


def list_audio(root: str, recursive: bool = True) -> tuple[list, list]:
    """枚举目录下音频文件。返回 (audio_files, unsupported_files)。"""
    audio, unsupported = [], []
    if os.path.isfile(root):
        roots = [root]
    else:
        roots = []
        for dirpath, _dirnames, filenames in os.walk(root):
            for fn in filenames:
                roots.append(os.path.join(dirpath, fn))
            if not recursive:
                break
    for p in sorted(roots):
        ext = os.path.splitext(p)[1].lower()
        if ext in AUDIO_EXTS:
            audio.append(p)
        elif ext in (".txt", ".json", ".csv", ".md", ".png", ".jpg", ".py",
                     ".xlsx", ".docx", ".pdf"):
            continue
        elif os.path.isfile(p):
            unsupported.append(p)
    return audio, unsupported


def wav_header(path: str) -> dict:
    """只读 WAV 头，不读数据（大文件友好）。非 WAV 返回 {}。"""
    if not path.lower().endswith(".wav"):
        return {}
    try:
        with wave.open(path, "rb") as w:
            return {"sr": w.getframerate(), "channels": w.getnchannels(),
                    "frames": w.getnframes(), "sampwidth": w.getsampwidth()}
    except Exception:
        return {}


def read_wav(path: str, max_bytes: int = 300 * 1024 * 1024) -> tuple[int, np.ndarray]:
    """读 WAV 为 float32 数组，shape=(n,) 单声道混音 或 (n, ch) 多声道。

    超过 max_bytes 的文件抛 ValueError（调用方可降级为仅读头）。
    非 WAV 格式抛 NotImplementedError（提示安装 soundfile）。
    """
    if not path.lower().endswith(".wav"):
        raise NotImplementedError(
            f"当前环境无 soundfile，仅支持 WAV：{path}（安装 soundfile 后可读 "
            f"mp3/flac/ogg）")
    if os.path.getsize(path) > max_bytes:
        raise ValueError(f"文件过大（>{max_bytes // (1 << 20)}MB）：{path}")
    import warnings
    from scipy.io import wavfile
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        sr, data = wavfile.read(path)
    data = np.asarray(data)
    if data.dtype == np.int16:
        x = data.astype(np.float32) / 32768.0
    elif data.dtype == np.int32:
        x = data.astype(np.float32) / 2147483648.0
    elif data.dtype == np.uint8:
        x = (data.astype(np.float32) - 128.0) / 128.0
    else:
        x = data.astype(np.float32)
    if x.ndim == 1:
        return int(sr), x
    return int(sr), x


def to_mono(x: np.ndarray) -> np.ndarray:
    return x if x.ndim == 1 else x.mean(axis=1)


def frame_rms(x: np.ndarray, sr: int, frame_ms: float = 50.0) -> np.ndarray:
    """分帧 RMS，用于静音比例估计。"""
    n = max(int(sr * frame_ms / 1000), 1)
    if len(x) < n:
        return np.array([np.sqrt(np.mean(x ** 2)) if len(x) else 0.0])
    m = len(x) // n * n
    frames = x[:m].reshape(-1, n)
    return np.sqrt(np.mean(frames ** 2, axis=1))
