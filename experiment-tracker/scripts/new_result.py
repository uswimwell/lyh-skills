#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""实验结果登记：把一次实验写成带指纹的规范 JSON（结果唯一事实源）。

每个结果 JSON 包含：实验名、时间戳、指标、配置、git commit、数据指纹、
切分指纹、备注。指纹让"这个数字是哪份数据哪个切分跑出来的"永远可追溯。

用法：
  python3 new_result.py --results-dir ./results --name cos25_crossdate \
      --metrics '{"rank1": 0.609, "eer": 0.1758}' \
      --config '{"backbone": "BEATs_iter3", "loss": "SupCon", "kfold": 5}' \
      [--split-file ./splits/crossdate.json] [--data-dir /data/wavs16k] \
      [--notes "五折多切分均值"]

输出：写入 <results-dir>/<name>_<时间戳>.json，stdout 返回该路径。
"""

from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import os
import pathlib
import re
import sys

SCHEMA_VERSION = 1


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def data_dir_fingerprint(data_dir: str) -> dict:
    """目录指纹：遍历 (相对路径, 大小, mtime) 做哈希——快，不读内容。"""
    entries = []
    for dirpath, _dirs, files in os.walk(data_dir):
        for fn in sorted(files):
            p = os.path.join(dirpath, fn)
            try:
                st = os.stat(p)
                entries.append(f"{os.path.relpath(p, data_dir)}|{st.st_size}|{int(st.st_mtime)}")
            except OSError:
                continue
    h = hashlib.sha256("\n".join(sorted(entries)).encode("utf-8")).hexdigest()
    return {"sha256": h[:16], "n_files": len(entries), "mode": "stat-fingerprint"}


def git_state(cwd: str) -> dict:
    import subprocess
    out = {"commit": None, "dirty": None}
    try:
        commit = subprocess.run(["git", "rev-parse", "--short", "HEAD"],
                                cwd=cwd, capture_output=True, timeout=10)
        if commit.returncode == 0:
            out["commit"] = commit.stdout.decode().strip()
        status = subprocess.run(["git", "status", "--porcelain"],
                                cwd=cwd, capture_output=True, timeout=10)
        if status.returncode == 0:
            out["dirty"] = bool(status.stdout.strip())
    except Exception:
        pass
    return out


def sanitize(name: str) -> str:
    s = re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("._-")
    return s[:60] or "result"


def main():
    ap = argparse.ArgumentParser(description="登记一次实验结果（带指纹）")
    ap.add_argument("--results-dir", required=True, help="结果 JSON 存放目录")
    ap.add_argument("--name", required=True, help="实验名，如 cos25_crossdate")
    ap.add_argument("--metrics", required=True,
                    help="指标 JSON，如 '{\"rank1\": 0.609}'")
    ap.add_argument("--config", default="{}", help="配置 JSON（模型/损失/切分等）")
    ap.add_argument("--split-file", help="切分文件路径（记内容哈希）")
    ap.add_argument("--data-dir", help="数据目录（记结构指纹）")
    ap.add_argument("--notes", default="", help="备注")
    ap.add_argument("--workdir", default=".", help="git 仓库位置（默认当前目录）")
    args = ap.parse_args()

    if ".." in args.results_dir:
        print(json.dumps({"status": "error", "message": "results-dir 不允许 '..'"},
                         ensure_ascii=False))
        sys.exit(2)
    try:
        metrics = json.loads(args.metrics)
        config = json.loads(args.config)
    except json.JSONDecodeError as e:
        print(json.dumps({"status": "error", "message": f"JSON 解析失败：{e}"},
                         ensure_ascii=False))
        sys.exit(2)
    if not isinstance(metrics, dict) or not metrics:
        print(json.dumps({"status": "error",
                          "message": "metrics 必须是非空 JSON 对象"},
                         ensure_ascii=False))
        sys.exit(2)

    root = os.path.realpath(os.path.abspath(args.results_dir))
    os.makedirs(root, exist_ok=True)

    record = {
        "schema_version": SCHEMA_VERSION,
        "name": sanitize(args.name),
        "timestamp": datetime.datetime.now().isoformat(timespec="seconds"),
        "metrics": metrics,
        "config": config,
        "git": git_state(args.workdir),
        "notes": args.notes,
    }
    if args.split_file:
        record["split_fingerprint"] = {
            "file": os.path.abspath(args.split_file),
            "sha256": sha256_file(args.split_file)}
    if args.data_dir:
        record["data_fingerprint"] = data_dir_fingerprint(args.data_dir)

    us = datetime.datetime.now().strftime("%f")
    fname = f"{record['name']}_{record['timestamp'].replace(':', '')}{us[:3]}.json"
    target = pathlib.Path(root) / fname
    if os.path.dirname(os.path.realpath(str(target))) != root:
        print(json.dumps({"status": "error", "message": "输出路径越界"},
                         ensure_ascii=False))
        sys.exit(2)
    target.write_text(json.dumps(record, ensure_ascii=False, indent=2),
                      encoding="utf-8")
    print(json.dumps({"status": "ok", "path": str(target),
                      "fingerprints": {k: record[k] for k in
                                       ("split_fingerprint", "data_fingerprint")
                                       if k in record}},
                     ensure_ascii=False))


if __name__ == "__main__":
    main()
