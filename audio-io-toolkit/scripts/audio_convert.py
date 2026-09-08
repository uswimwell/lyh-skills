#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""音频转换：重采样 / 混单声道 / 裁剪 / 峰值归一化，输出 16-bit PCM WAV。

用法：
  python3 audio_convert.py --input /data/wavs --outdir /data/wavs_16k \
      --sr 16000 --mono [--trim 0:5] [--normalize] [--pattern "*.wav"]

注意：--outdir 必须与 --input 不同目录（防止覆盖原始数据）。
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import os
import pathlib
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import numpy as np  # noqa: E402
from scipy.signal import resample_poly  # noqa: E402

from audio_common import list_audio, read_any, to_mono  # noqa: E402


def prepare_outdir(outdir: str, indir: str) -> str:
    if not outdir or ".." in outdir:
        raise ValueError(f"outdir 不允许为空或包含 '..'：{outdir!r}")
    root = os.path.realpath(os.path.abspath(outdir))
    in_real = os.path.realpath(os.path.abspath(indir))
    if root == in_real or (in_real + os.sep) in root:
        raise ValueError("输出目录必须与输入目录不同（防止覆盖原始数据）")
    os.makedirs(root, exist_ok=True)
    return root


def parse_trim(s: str | None):
    if not s:
        return None
    def sec(x):
        if ":" in x:
            a, b = x.split(":")
            return int(a) + float(b) / 60 if int(b) < 60 else float(a) + float(b)
        return float(x)
    parts = s.split("-")
    if len(parts) != 2:
        raise ValueError(f"--trim 格式应为 起秒-止秒 或 分:秒-分:秒：{s!r}")
    return sec(parts[0]), sec(parts[1])


def resample(x: np.ndarray, sr_in: int, sr_out: int) -> np.ndarray:
    if sr_in == sr_out:
        return x
    import math
    g = math.gcd(sr_out, sr_in)
    return resample_poly(x, sr_out // g, sr_in // g).astype(np.float32)


def write_wav16(path: pathlib.Path, sr: int, x: np.ndarray):
    y = np.clip(x, -1.0, 1.0)
    y = (y * 32767.0).astype(np.int16)
    import wave
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1 if y.ndim == 1 else y.shape[1])
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(y.tobytes())


def main():
    ap = argparse.ArgumentParser(description="音频批量转换（输出 16-bit PCM WAV）")
    ap.add_argument("--input", required=True, help="音频目录或单个文件")
    ap.add_argument("--outdir", required=True, help="输出目录（必须不同于输入）")
    ap.add_argument("--sr", type=int, default=16000, help="目标采样率")
    ap.add_argument("--mono", action="store_true", help="混为单声道")
    ap.add_argument("--trim", help="裁剪区间，如 0-5（秒）或 0:30-1:00")
    ap.add_argument("--normalize", action="store_true", help="峰值归一化到 0.99")
    ap.add_argument("--pattern", default="*", help="文件名过滤，如 only_a_*.wav")
    ap.add_argument("--pcm-sr", type=int, default=None, help="目录含 .pcm 时必须提供采样率")
    ap.add_argument("--pcm-dtype", default="int16", help="PCM 位深：int16/int32")
    args = ap.parse_args()

    trim = parse_trim(args.trim)
    try:
        root = prepare_outdir(args.outdir, args.input)
    except ValueError as e:
        print(json.dumps({"status": "error", "message": str(e)}, ensure_ascii=False))
        sys.exit(2)
    if ".." in args.input:
        print(json.dumps({"status": "error", "message": "input 不允许包含 '..'"},
                         ensure_ascii=False))
        sys.exit(2)

    audio, unsupported = list_audio(args.input)
    done, failed = 0, []
    for p in audio:
        name = os.path.basename(p)
        if not fnmatch.fnmatch(name, args.pattern):
            continue
        try:
            sr, x = read_any(p, args.pcm_sr, args.pcm_dtype)
            if args.mono:
                x = to_mono(x)
            if trim:
                t0, t1 = trim
                x = x[int(t0 * sr):int(t1 * sr)]
            x = resample(x, sr, args.sr)
            if args.normalize:
                peak = float(np.max(np.abs(x))) if x.size else 0.0
                if peak > 0:
                    x = x / peak * 0.99
            stem = os.path.splitext(name)[0]
            out_name = f"{stem}_{args.sr}hz{'_mono' if args.mono else ''}.wav"
            write_wav16(pathlib.Path(root) / out_name, args.sr, x)
            done += 1
        except Exception as e:
            failed.append({"file": name, "error": f"{type(e).__name__}: {e}"})

    print(json.dumps({"status": "ok", "outdir": root, "converted": done,
                      "failed": len(failed), "failures": failed[:10],
                      "target_sr": args.sr,
                      "warnings": (["存在未支持格式的文件"] if unsupported else [])},
                     ensure_ascii=False))


if __name__ == "__main__":
    main()
