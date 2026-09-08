#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""传统声学参数批量提取（深度特征之外的"经典"特征，论文对照/基线常用）。

每文件输出一行 CSV，含：
  基础：时长、采样率、RMS 能量(dB)
  频率：峰频（能量加权主频）、带宽(90%能量范围)、谱质心、谱滚降(85%)
  音高：F0 均值/中位数/范围（自相关法，可选）
  MFCC：前 13 维均值（可选）
  谱通量/过零率（可选）

用法：
  python3 extract_features.py --input /data/wavs --outdir ./feat \
      [--features duration,f0,peak_freq,bandwidth,spectral,mfcc,zcr] [--light]

仅依赖 numpy/scipy（MFCC 的 mel 滤波与 DCT 内置实现，无需 librosa）。
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# audio_common 与 audio-io-toolkit 共用（WAV 读取/枚举）
_IO_TOOLKIT = ("/home/liyonghao/.agents/skills/audio-io-toolkit/scripts")
if _IO_TOOLKIT not in sys.path:
    sys.path.append(_IO_TOOLKIT)

import numpy as np  # noqa: E402

from audio_common import list_audio, read_any, to_mono  # noqa: E402

ALL_FEATURES = ["duration", "f0", "peak_freq", "bandwidth", "spectral",
                "mfcc", "zcr"]

_OUTPUT_NAMES = ("acoustic_features.csv", "acoustic_features_summary.json")


def prepare_outdir(outdir: str) -> str:
    if not outdir or ".." in outdir:
        raise ValueError(f"outdir 不允许为空或包含 '..'：{outdir!r}")
    root = os.path.realpath(os.path.abspath(outdir))
    for name in _OUTPUT_NAMES:
        target = os.path.realpath(os.path.join(root, name))
        if os.path.dirname(target) != root:
            raise ValueError(f"输出路径越界：{target!r}")
    os.makedirs(root, exist_ok=True)
    return root


# ---------------- DSP 基元（numpy/scipy 实现） ----------------

def stft_power(x: np.ndarray, sr: int, n_fft: int = 1024, hop: int = 256):
    """简易 STFT 功率谱 (freq, time)，Hann 窗。"""
    x = x.astype(np.float32)
    if len(x) < n_fft:
        x = np.pad(x, (0, n_fft - len(x)))
    n = (len(x) - n_fft) // hop + 1
    win = np.hanning(n_fft).astype(np.float32)
    idx = np.arange(n_fft)[None, :] + hop * np.arange(n)[:, None]
    frames = x[idx] * win
    spec = np.abs(np.fft.rfft(frames, axis=1)) ** 2
    freqs = np.fft.rfftfreq(n_fft, 1.0 / sr)
    return freqs, spec.T  # (freq, time)


def spectral_features(freqs, power):
    tot = power.sum(axis=0) + 1e-12
    centroid = (freqs[:, None] * power).sum(axis=0) / tot
    cum = np.cumsum(power, axis=0)
    rolloff85 = freqs[np.argmax(cum >= 0.85 * cum[-1], axis=0)]
    peak = freqs[np.argmax(power, axis=0)]
    e90 = freqs[np.argmax(cum >= 0.05 * cum[-1], axis=0)]
    e95 = freqs[np.argmax(cum >= 0.95 * cum[-1], axis=0)]
    return centroid, rolloff85, peak, e95 - e90


def hz2mel(f):
    return 2595.0 * np.log10(1.0 + np.asarray(f) / 700.0)


def mel2hz(m):
    return 700.0 * (10 ** (np.asarray(m) / 2595.0) - 1.0)


def mel_filterbank(n_mels, n_fft, sr, fmin=0.0, fmax=None):
    fmax = fmax or sr / 2
    mpts = np.linspace(hz2mel(fmin), hz2mel(fmax), n_mels + 2)
    hzpts = mel2hz(mpts)
    bins = np.floor((n_fft + 1) * hzpts / sr).astype(int)
    fb = np.zeros((n_mels, n_fft // 2 + 1), dtype=np.float32)
    for i in range(n_mels):
        a, b, c = bins[i], bins[i + 1], bins[i + 2]
        if b > a:
            fb[i, a:b] = (np.arange(a, b) - a) / (b - a)
        if c > b:
            fb[i, b:c] = (c - np.arange(b, c)) / (c - b)
    return fb


def dct2(x, keep_first=False):
    """DCT-II（沿最后一维），scipy 1.1 兼容实现。"""
    import scipy.fftpack as fp
    y = fp.dct(x, type=2, axis=-1, norm="ortho")
    return y if keep_first else y[..., 1:]


def mfcc_mean(x, sr, n_mfcc=13, n_mels=26, n_fft=1024, hop=256):
    freqs, power = stft_power(x, sr, n_fft, hop)
    fb = mel_filterbank(n_mels, n_fft, sr, 50.0, min(7800.0, sr / 2))
    mel = (fb @ power) + 1e-12
    logmel = np.log(mel)
    cc = dct2(logmel.T)  # (time, n_mels-1)
    cc = cc[:, :n_mfcc]
    return cc.mean(axis=0)


def f0_autocorr(x, sr, fmin=50.0, fmax=2000.0, frame_ms=40.0, hop_ms=20.0):
    """自相关法基频：逐帧找滞后峰。返回有效帧的 F0 序列。"""
    fl = int(sr * frame_ms / 1000)
    hp = int(sr * hop_ms / 1000)
    lag_min = max(int(sr / fmax), 2)
    lag_max = min(int(sr / fmin), fl // 2)
    if len(x) < fl or lag_max <= lag_min:
        return np.array([])
    f0s = []
    for i in range(0, len(x) - fl, hp):
        fr = x[i:i + fl]
        if np.sqrt(np.mean(fr ** 2)) < 1e-4:
            continue
        fr = fr - fr.mean()
        ac = np.correlate(fr, fr, "full")[fl - 1:]
        if ac[0] <= 0:
            continue
        ac /= ac[0]
        seg = ac[lag_min:lag_max]
        j = int(np.argmax(seg))
        if seg[j] > 0.3:  # 周期性足够强才算有声调
            f0s.append(sr / (lag_min + j))
    return np.array(f0s)


# ---------------- 主流程 ----------------

def main():
    ap = argparse.ArgumentParser(description="传统声学参数批量提取")
    ap.add_argument("--input", required=True, help="音频目录或单个文件")
    ap.add_argument("--outdir", required=True, help="输出目录")
    ap.add_argument("--features", default="duration,peak_freq,bandwidth,spectral,f0",
                    help=f"逗号分隔，可选 {','.join(ALL_FEATURES)}")
    ap.add_argument("--pcm-sr", type=int, default=None, help="目录含 .pcm 时必须提供采样率")
    ap.add_argument("--pcm-dtype", default="int16", help="PCM 位深：int16/int32")
    args = ap.parse_args()

    feats = [f.strip() for f in args.features.split(",") if f.strip()]
    bad = [f for f in feats if f not in ALL_FEATURES]
    if bad:
        print(json.dumps({"status": "error",
                          "message": f"未知特征：{bad}（可选 {ALL_FEATURES}）"},
                         ensure_ascii=False))
        sys.exit(2)
    try:
        root = prepare_outdir(args.outdir)
    except ValueError as e:
        print(json.dumps({"status": "error", "message": str(e)}, ensure_ascii=False))
        sys.exit(2)
    if ".." in args.input:
        print(json.dumps({"status": "error", "message": "input 不允许包含 '..'"},
                         ensure_ascii=False))
        sys.exit(2)

    audio, _unsupported = list_audio(args.input)
    rows, errors = [], []
    header = ["file"]
    for p in audio:
        try:
            sr, x = read_any(p, args.pcm_sr, args.pcm_dtype)
            xm = to_mono(x)
            row = {"file": os.path.basename(p)}
            freqs = power = None
            if "duration" in feats:
                row["duration_s"] = round(len(xm) / sr, 3)
                r = float(np.sqrt(np.mean(xm ** 2))) if len(xm) else 0.0
                row["rms_db"] = round(20 * np.log10(r + 1e-12), 2)
            if any(f in feats for f in ("peak_freq", "bandwidth", "spectral", "mfcc")) \
                    and freqs is None:
                freqs, power = stft_power(xm, sr)
            if "peak_freq" in feats:
                _, _, peak, _ = spectral_features(freqs, power)
                row["peak_freq_hz"] = round(float(np.median(peak)), 1)
            if "bandwidth" in feats:
                _, _, _, bw = spectral_features(freqs, power)
                row["bandwidth_90_hz"] = round(float(np.median(bw)), 1)
            if "spectral" in feats:
                c, ro, _, _ = spectral_features(freqs, power)
                row["spec_centroid_hz"] = round(float(np.mean(c)), 1)
                row["spec_rolloff85_hz"] = round(float(np.mean(ro)), 1)
            if "f0" in feats:
                f0 = f0_autocorr(xm, sr)
                if len(f0):
                    row["f0_mean_hz"] = round(float(np.mean(f0)), 1)
                    row["f0_median_hz"] = round(float(np.median(f0)), 1)
                    row["f0_range_hz"] = round(float(np.percentile(f0, 95)
                                                     - np.percentile(f0, 5)), 1)
                else:
                    row["f0_mean_hz"] = ""
            if "mfcc" in feats:
                cc = mfcc_mean(xm, sr)
                for i, v in enumerate(cc):
                    row[f"mfcc_{i + 1:02d}"] = round(float(v), 4)
            if "zcr" in feats:
                zcr = float(np.mean(np.abs(np.diff(np.sign(xm)))) ) if len(xm) else 0.0
                row["zcr"] = round(zcr, 5)
            if not header:
                header = ["file"] + [k for k in row if k != "file"]
            rows.append(row)
        except Exception as e:
            errors.append({"file": os.path.basename(p),
                           "error": f"{type(e).__name__}: {e}"})

    # 统一列
    all_keys = list(header)
    for r in rows:
        for k in r:
            if k not in all_keys:
                all_keys.append(k)
    lines = [",".join(all_keys)]
    for r in rows:
        lines.append(",".join(str(r.get(k, "")) for k in all_keys))
    pathlib.Path(root, "acoustic_features.csv").write_text(
        "\n".join(lines) + "\n", encoding="utf-8")

    summary = {"status": "ok", "outdir": root, "n_files": len(rows),
               "n_errors": len(errors), "features": feats,
               "errors": errors[:10]}
    pathlib.Path(root, "acoustic_features_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False))


if __name__ == "__main__":
    main()
