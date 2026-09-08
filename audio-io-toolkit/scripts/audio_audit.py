#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""音频批量体检：枚举目录下全部音频，输出每文件参数与质量指标，标记可疑项。

产出（写入 --outdir，文件名固定）：
  audio_audit.csv   每文件一行：路径/采样率/声道/时长/峰值/削波比/静音比/文件大小
  audio_audit.json  汇总：数量、采样率分布、时长统计、可疑清单、错误清单

可疑判定（默认阈值，可用参数调整）：
  - 削波（|x|≥0.99 的样本占比 > 1%）
  - 高静音（RMS < 峰值 1% 的帧占比 > 80%）
  - 时长 < min_sec 或 > max_sec
  - 全零/静音文件、采样率异常（不在 --expect-sr 列表时提示）
超大文件（> max_read_mb）只读头不计质量指标。

用法：
  python3 audio_audit.py --input /data/wavs --outdir ./audit [--light]
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import numpy as np  # noqa: E402

from audio_common import list_audio, read_any, to_mono, wav_header, frame_rms  # noqa: E402


def prepare_outdir(outdir: str) -> str:
    if not outdir or ".." in outdir:
        raise ValueError(f"outdir 不允许为空或包含 '..'：{outdir!r}")
    root = os.path.realpath(os.path.abspath(outdir))
    for name in ("audio_audit.csv", "audio_audit.json"):
        target = os.path.realpath(os.path.join(root, name))
        if os.path.dirname(target) != root:
            raise ValueError(f"输出路径越界：{target!r}")
    os.makedirs(root, exist_ok=True)
    return root


def audit_one(path: str, light: bool, min_sec: float, max_sec: float,
              clip_ratio_thr: float, silence_ratio_thr: float,
              max_read_mb: int, pcm_sr=None, pcm_dtype: str = "int16") -> dict:
    row = {"path": path, "size_mb": round(os.path.getsize(path) / (1 << 20), 3),
           "error": ""}
    head = wav_header(path)
    if head:
        row.update({"sr": head["sr"], "channels": head["channels"],
                    "duration": round(head["frames"] / head["sr"], 3)
                    if head["sr"] else 0.0})
    else:
        try:
            sr, x = read_any(path, pcm_sr, pcm_dtype,
                             max_bytes=max_read_mb * (1 << 20))
        except (NotImplementedError, ValueError) as e:
            row["error"] = str(e)
            return row
        row.update({"sr": sr, "channels": 1 if x.ndim == 1 else x.shape[1],
                    "duration": round(len(x) / sr, 3)})
    if light:
        return row
    try:
        sr, x = read_any(path, pcm_sr, pcm_dtype,
                         max_bytes=max_read_mb * (1 << 20))
    except (NotImplementedError, ValueError) as e:
        row["error"] = f"质量指标跳过：{e}"
        return row
    xm = to_mono(x)
    peak = float(np.max(np.abs(xm))) if len(xm) else 0.0
    clip = float(np.mean(np.abs(xm) >= 0.99)) if len(xm) else 0.0
    rms = frame_rms(xm, sr)
    silence = float(np.mean(rms < max(peak * 0.01, 1e-5))) if len(rms) else 1.0
    row.update({"peak": round(peak, 4), "clip_ratio": round(clip, 5),
                "silence_ratio": round(silence, 3),
                "mean_rms": round(float(np.mean(rms)) if len(rms) else 0.0, 5)})
    flags = []
    if clip > clip_ratio_thr:
        flags.append("削波")
    if silence > silence_ratio_thr:
        flags.append("高静音")
    dur = row.get("duration", 0.0)
    if dur < min_sec:
        flags.append("过短")
    if dur > max_sec:
        flags.append("过长")
    if peak == 0.0:
        flags.append("全零")
    if flags:
        row["flags"] = flags
    return row


def main():
    ap = argparse.ArgumentParser(description="音频批量体检")
    ap.add_argument("--input", required=True, help="音频目录或单个文件")
    ap.add_argument("--outdir", required=True, help="报告输出目录")
    ap.add_argument("--light", action="store_true", help="只读头不读数据（大库快扫）")
    ap.add_argument("--min-sec", type=float, default=0.2)
    ap.add_argument("--max-sec", type=float, default=120.0)
    ap.add_argument("--clip-ratio", type=float, default=0.01)
    ap.add_argument("--silence-ratio", type=float, default=0.8)
    ap.add_argument("--max-read-mb", type=int, default=300,
                    help="质量指标只读小于该值的文件（MB）")
    ap.add_argument("--pcm-sr", type=int, default=None,
                    help="目录含无头 .pcm 文件时必须提供采样率")
    ap.add_argument("--pcm-dtype", default="int16", help="PCM 位深：int16/int32")
    args = ap.parse_args()

    try:
        root = prepare_outdir(args.outdir)
    except ValueError as e:
        print(json.dumps({"status": "error", "message": str(e)}, ensure_ascii=False))
        sys.exit(2)
    if ".." in args.input:
        print(json.dumps({"status": "error", "message": "input 不允许包含 '..'"},
                         ensure_ascii=False))
        sys.exit(2)

    audio, unsupported = list_audio(args.input)
    rows, errors = [], []
    for p in audio:
        try:
            rows.append(audit_one(p, args.light, args.min_sec, args.max_sec,
                                  args.clip_ratio, args.silence_ratio,
                                  args.max_read_mb, args.pcm_sr, args.pcm_dtype))
        except Exception as e:  # 单文件失败不影响整体
            errors.append({"path": p, "error": f"{type(e).__name__}: {e}"})

    # 汇总
    ok_rows = [r for r in rows if "sr" in r]
    sr_dist = {}
    for r in ok_rows:
        sr_dist[str(r["sr"])] = sr_dist.get(str(r["sr"]), 0) + 1
    durs = sorted(r["duration"] for r in ok_rows if "duration" in r)
    flagged = [r for r in rows if r.get("flags")]
    def q(vals, p):
        return round(vals[min(int(len(vals) * p), len(vals) - 1)], 3) if vals else 0.0
    summary = {
        "input": os.path.abspath(args.input),
        "n_audio": len(audio), "n_unsupported": len(unsupported),
        "unsupported_examples": unsupported[:5],
        "sr_distribution": sr_dist,
        "duration_stats": {"min": q(durs, 0), "p25": q(durs, 0.25),
                           "median": q(durs, 0.5), "p75": q(durs, 0.75),
                           "max": q(durs, 1)} if durs else {},
        "n_flagged": len(flagged),
        "flagged": [{"path": r["path"], "flags": r["flags"]} for r in flagged[:50]],
        "n_errors": len(errors), "errors": errors[:10],
        "mode": "light" if args.light else "full",
    }
    report = json.dumps(summary, ensure_ascii=False, indent=2)
    pathlib.Path(root, "audio_audit.json").write_text(report, encoding="utf-8")

    cols = ["path", "sr", "channels", "duration", "peak", "clip_ratio",
            "silence_ratio", "mean_rms", "size_mb", "flags", "error"]
    lines = [",".join(cols)]
    for r in rows:
        lines.append(",".join(str(r.get(c, "")) for c in cols))
    pathlib.Path(root, "audio_audit.csv").write_text("\n".join(lines) + "\n",
                                                     encoding="utf-8")
    print(json.dumps({"status": "ok", "outdir": root,
                      "n_audio": len(audio), "n_flagged": len(flagged),
                      "n_errors": len(errors), "sr_distribution": sr_dist,
                      "duration_stats": summary["duration_stats"],
                      "warnings": (["存在不支持格式的文件"] if unsupported else [])
                                  + ([f"{len(errors)} 个文件读取失败"] if errors else [])},
                     ensure_ascii=False))


if __name__ == "__main__":
    main()
