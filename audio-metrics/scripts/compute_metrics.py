#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""audio-metrics 命令行入口：读任务 JSON，算指标，输出 JSON。

用法（task 决定输入字段）：
  闭集分类：
    {"task": "classification", "y_true": [...], "y_pred": [...], "labels": [可选]}
  校验型（声纹 verification）：
    {"task": "verification", "pos_scores": [...], "neg_scores": [...], "threshold": 可选}
  开放集：
    {"task": "openset", "y_true": [...], "y_pred": [...], "scores": [...],
     "unknown_label": "unknown"}
  语音识别：
    {"task": "asr", "refs": ["...", ...], "hyps": ["...", ...]}
  声音事件检测：
    {"task": "sed", "y_true_multihot": [[0,1],...], "y_score": [[0.1,0.9],...],
     "classes": [可选], "threshold": 0.5}

输入来源：--input 文件 或 stdin。结果打印到 stdout（--out 可另存文件）。
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import numpy as np  # noqa: E402

import metrics  # noqa: E402


def safe_out(path: str | None, text: str):
    """输出文件名固定为 metrics_result.json 时才落盘，且禁止 '..'。"""
    if not path:
        return
    if ".." in path:
        raise ValueError(f"输出路径不允许包含 '..'：{path!r}")
    target = pathlib.Path(os.path.abspath(path))
    if target.name != "metrics_result.json":
        raise ValueError("输出文件名只允许 metrics_result.json")
    target.write_text(text, encoding="utf-8")


def main():
    ap = argparse.ArgumentParser(description="统一指标计算 CLI")
    ap.add_argument("--input", help="任务 JSON 文件（缺省读 stdin）")
    ap.add_argument("--out", help="结果另存文件（文件名必须为 metrics_result.json）")
    ap.add_argument("--pretty", action="store_true", help="缩进输出")
    args = ap.parse_args()

    raw = open(args.input, encoding="utf-8").read() if args.input else sys.stdin.read()
    spec = json.loads(raw)
    task = spec.get("task")

    if task == "classification":
        labels = spec.get("labels")
        result = metrics.classification_report(spec["y_true"], spec["y_pred"], labels)
    elif task == "verification":
        eer_v, thr = metrics.eer(spec["pos_scores"], spec["neg_scores"])
        result = {"eer": eer_v, "eer_threshold": thr,
                  "auroc": metrics.auroc(spec["pos_scores"], spec["neg_scores"]),
                  "det_points": metrics.det_points(spec["pos_scores"], spec["neg_scores"])}
        if spec.get("threshold") is not None:
            result["at_threshold"] = metrics.far_frr_at(
                spec["pos_scores"], spec["neg_scores"], spec["threshold"])
    elif task == "openset":
        curve = metrics.oscr_curve(spec["y_true"], spec["y_pred"], spec["scores"],
                                   unknown_label=spec.get("unknown_label", "unknown"))
        result = {"oscr_summary": curve.pop("summary"), "oscr_curve": curve}
    elif task == "asr":
        result = metrics.wer(spec["refs"], spec["hyps"])
        result["cer"] = metrics.cer(spec["refs"], spec["hyps"])["CER"]
    elif task == "sed":
        result = metrics.sed_map(spec["y_true_multihot"], spec["y_score"],
                                 spec.get("classes"), spec.get("threshold", 0.5))
    else:
        print(json.dumps({"status": "error",
                          "message": f"未知 task：{task!r}（可选 classification/verification/"
                                     f"openset/asr/sed）"}, ensure_ascii=False))
        sys.exit(2)

    text = json.dumps({"status": "ok", "task": task, "metrics": result},
                      ensure_ascii=False, indent=2 if args.pretty else None)
    print(text)
    safe_out(args.out, text)


if __name__ == "__main__":
    main()
