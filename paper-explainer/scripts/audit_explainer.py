#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""讲解文档交付自查（只读，不修改任何文件）。

对 paper-explainer 产出的 讲解.md 做 four 类检查：
  1. 结构完整：必选章节是否齐全（概览级文档自动放宽"逐图解析"）；
  2. 图片引用：所有相对路径图片是否存在；
  3. 术语覆盖：正文出现的英文专业词是否都进了文末术语表（启发式，
     带默认白名单，报告需人工判断）；
  4. 内联解释：术语表中的术语在正文首次出现处，是否带有
     "**术语（English）**：解释"式的内联解释（启发式，供人工复核）。

用法：
  python3 audit_explainer.py <讲解.md 路径> [更多路径...]
  python3 audit_explainer.py paper_notes/foo/讲解.md

stdout 输出人类可读报告，最后一行 JSON（便于程序解析）。
退出码：0 通过 / 1 存在硬性问题（缺章节、图片失效、缺状态说明）。
"""

from __future__ import annotations

import json
import os
import re
import sys

# 常见非术语词（平台名、文件格式、元词汇等）
DEFAULT_WHITELIST = {
    "pdf", "doi", "png", "jpg", "jpeg", "gif", "md", "url", "api", "http",
    "https", "www", "org", "com", "cn", "html", "arxiv", "peerj", "biorxiv",
    "github", "zenodo", "figshare", "crossref", "openalex", "unpaywall",
    "europepmc", "pmc", "ncbi", "cdn", "google", "scholar", "et", "al",
    "etc", "vs", "web", "os", "linux", "python", "r", "who", "faq", "cc",
    "by", "nc", "nd", "gps", "the", "and", "for", "a",
}

REQUIRED_SECTIONS = ["一、", "三、", "四、"]
FIGURE_SECTION = "二、"


def strip_noise(text: str) -> str:
    """去掉 URL、图片链接、行内代码，减少误报。"""
    text = re.sub(r"https?://\S+", " ", text)
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)", " ", text)
    text = re.sub(r"\[[^\]]*\]\([^)]*\)", " ", text)  # markdown 链接
    text = re.sub(r"`[^`]*`", " ", text)
    return text


def split_doc(text: str):
    """按术语表标题切分 (body, glossary_text)；找不到则返回 (全文, '')。"""
    m = re.search(r"^##\s*三、.*$", text, re.M)
    if not m:
        return text, ""
    return text[:m.start()], text[m.start():]


def glossary_terms(glossary_text: str) -> list[str]:
    """从术语表 markdown 表格提取术语（首列，按 / 拆分复合行）。"""
    terms = []
    for line in glossary_text.splitlines():
        line = line.strip()
        if not line.startswith("|") or line.startswith("|---") \
                or re.match(r"^\|[\s:\-|]+\|$", line) or "英文术语" in line:
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if not cells or not cells[0]:
            continue
        for part in re.split(r"[/、]", cells[0]):
            part = part.strip()
            part = re.sub(r"^✅|^\*", "", part).strip()
            if part:
                terms.append(part)
    return terms


def audit(path: str) -> dict:
    text = open(path, encoding="utf-8").read()
    d = os.path.dirname(os.path.abspath(path))
    report = {"file": path, "hard": [], "info": []}
    overview = "概览级" in text

    # 1. 结构
    for sec in REQUIRED_SECTIONS:
        if sec not in text:
            report["hard"].append(f"缺少必选章节：{sec}")
    if not overview and FIGURE_SECTION not in text:
        report["hard"].append(f"缺少必选章节：{FIGURE_SECTION}（逐图解析）")
    if "讲解状态说明" not in text:
        report["hard"].append("缺少『讲解状态说明』（来源/完整度披露）")

    # 2. 图片引用
    refs = re.findall(r"!\[[^\]]*\]\(([^)]+)\)", text)
    for ref in refs:
        if ref.startswith("http"):
            continue
        if not os.path.exists(os.path.join(d, ref)):
            report["hard"].append(f"图片引用失效：{ref}")
    report["info"].append(f"图片引用 {len(refs)} 个")

    # 3. 术语覆盖
    body, gloss = split_doc(text)
    terms = glossary_terms(gloss)
    report["info"].append(f"术语表收录 {len(terms)} 个术语")
    term_lowers = {t.lower() for t in terms}
    body_clean = strip_noise(body)
    tokens = re.findall(r"[A-Za-z][A-Za-z0-9\-']{2,}", body_clean)
    from collections import Counter
    counts = Counter(t.lower() for t in tokens)
    whitelist = set(DEFAULT_WHITELIST)
    missing = {}
    for tok, n in counts.items():
        if tok in whitelist:
            continue
        if any(tok in t or t in tok for t in term_lowers):
            continue
        missing[tok] = n
    if missing:
        top = sorted(missing.items(), key=lambda kv: -kv[1])[:15]
        report["info"].append("疑似未收录术语（需人工判断，可加白名单或补术语表）："
                              + ", ".join(f"{k}×{v}" for k, v in top))

    # 4. 内联解释抽查
    no_inline = []
    for t in terms:
        if not re.search(r"[A-Za-z]", t):
            continue
        m = re.search(re.escape(t), body, re.I)
        if not m:
            continue
        seg_start = body.rfind("**", 0, m.start())
        seg_end = body.find("**", m.start())
        if 0 <= seg_start < m.start() < seg_end:
            seg = body[seg_start:seg_end + 2]
            if "（" in seg and "）" in seg:
                continue
        no_inline.append(t)
    if no_inline:
        report["info"].append(f"首次出现疑似无内联解释（人工复核）：{', '.join(no_inline[:12])}"
                              + ("…" if len(no_inline) > 12 else ""))

    report["pass"] = not report["hard"]
    return report


def main():
    paths = [p for p in sys.argv[1:] if p.endswith(".md")]
    if not paths:
        print("用法：audit_explainer.py <讲解.md> [更多...]")
        sys.exit(2)
    results = [audit(p) for p in paths]
    for r in results:
        status = "PASS" if r["pass"] else "FAIL"
        print(f"\n=== [{status}] {r['file']} ===")
        for h in r["hard"]:
            print(f"  [硬性] {h}")
        for i in r["info"]:
            print(f"  [提示] {i}")
    all_pass = all(r["pass"] for r in results)
    print(json.dumps({"all_pass": all_pass,
                      "files": [{k: r[k] for k in ("file", "pass", "hard")}
                                for r in results]}, ensure_ascii=False))
    sys.exit(0 if all_pass else 1)


if __name__ == "__main__":
    main()
