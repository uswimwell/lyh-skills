#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""扫描结果目录：生成 Markdown 对比表 + 泄漏审计。

用法：
  python3 summarize.py --results-dir ./results [--out SUMMARY.md] [--check]

--check 逐条结果审计：
  - 缺切分/数据指纹 → 无法追溯，标黄
  - 同名实验多个结果但指纹不同 → 提示（可能是数据或切分变了）
  - git dirty 的结果 → 数字可能不可复现
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import sys


def load_results(root: str) -> list[dict]:
    out = []
    for fn in sorted(os.listdir(root)):
        if not fn.endswith(".json") or fn.startswith(("SUMMARY", "metrics_result")):
            continue
        p = os.path.join(root, fn)
        try:
            r = json.load(open(p, encoding="utf-8"))
            if isinstance(r, dict) and "metrics" in r:
                r["_file"] = fn
                out.append(r)
        except (json.JSONDecodeError, OSError):
            continue
    return out


def metric_keys(results) -> list[str]:
    keys: dict = {}
    for r in results:
        for k in r.get("metrics", {}):
            keys.setdefault(k, 0)
            keys[k] += 1
    return sorted(keys, key=lambda k: -keys[k])[:8]


def main():
    ap = argparse.ArgumentParser(description="实验结果汇总与审计")
    ap.add_argument("--results-dir", required=True)
    ap.add_argument("--out", help="写 SUMMARY.md（缺省打印到 stdout）")
    ap.add_argument("--check", action="store_true", help="附泄漏/可追溯审计")
    args = ap.parse_args()

    if ".." in args.results_dir:
        print(json.dumps({"status": "error", "message": "results-dir 不允许 '..'"},
                         ensure_ascii=False))
        sys.exit(2)
    root = os.path.realpath(os.path.abspath(args.results_dir))
    results = load_results(root)
    if not results:
        print(json.dumps({"status": "ok", "n_results": 0,
                          "message": "目录中没有可识别的结果 JSON"},
                         ensure_ascii=False))
        sys.exit(0)

    mkeys = metric_keys(results)
    lines = ["| 实验 | 时间 | " + " | ".join(mkeys) + " | 配置摘要 |",
             "|" + "---|" * (len(mkeys) + 3)]
    for r in sorted(results, key=lambda x: x.get("timestamp", "")):
        mvals = []
        for k in mkeys:
            v = r.get("metrics", {}).get(k)
            mvals.append(f"{v:.4f}" if isinstance(v, float) else str(v if v is not None else ""))
        cfg = r.get("config", {})
        cfg_short = ", ".join(f"{k}={v}" for k, v in list(cfg.items())[:4])
        lines.append(f"| {r.get('name','?')} | {r.get('timestamp','?')[:16]} | "
                     + " | ".join(mvals) + f" | {cfg_short} |")

    audit_lines = []
    if args.check:
        audit_lines.append("\n## 可追溯性审计\n")
        issues = []
        seen = {}
        for r in results:
            name = r.get("name", "?")
            if not r.get("split_fingerprint"):
                issues.append(f"- ⚠️ `{name}`（{r['_file']}）缺少切分指纹，数字无法追溯到具体切分")
            if not r.get("data_fingerprint"):
                issues.append(f"- ⚠️ `{name}`（{r['_file']}）缺少数据指纹")
            g = r.get("git") or {}
            if g.get("dirty"):
                issues.append(f"- ⚠️ `{name}` 登记时 git 工作区不干净，复现性存疑")
            key = (name, r.get("split_fingerprint", {}).get("sha256"))
            seen.setdefault(key, []).append(r)
        for (name, fp), group in seen.items():
            if len(group) > 1:
                vals = [g2.get("metrics") for g2 in group]
                if len({json.dumps(v, sort_keys=True) for v in vals}) > 1:
                    audit_lines.append(
                        f"- ℹ️ `{name}` 同名同切分有 {len(group)} 次结果且指标不同——"
                        "确认是否为多种子/多折（应把折号写进 config 或 notes）")
        audit_lines += (issues or ["- ✅ 未发现可追溯性问题"])

    text = (f"# 实验结果汇总（{len(results)} 条，{mkeys and '核心指标：' + ', '.join(mkeys)}）\n\n"
            + "\n".join(lines) + "\n" + "\n".join(audit_lines) + "\n")
    if args.out:
        if ".." in args.out:
            print(json.dumps({"status": "error", "message": "out 不允许 '..'"},
                             ensure_ascii=False))
            sys.exit(2)
        target = pathlib.Path(os.path.abspath(args.out))
        if target.name != "SUMMARY.md":
            print(json.dumps({"status": "error",
                              "message": "输出文件名只允许 SUMMARY.md"},
                             ensure_ascii=False))
            sys.exit(2)
        target.write_text(text, encoding="utf-8")
        print(json.dumps({"status": "ok", "n_results": len(results),
                          "summary_file": str(target)}, ensure_ascii=False))
    else:
        print(text)
        print(json.dumps({"status": "ok", "n_results": len(results)},
                         ensure_ascii=False))


if __name__ == "__main__":
    main()
