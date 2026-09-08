#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""PMC 全文救援模式：当论文 PDF 被反爬拦截（NCBI/PeerJ/ScienceDirect 等），
但论文在 PubMed Central 有开放全文时，绕过 PDF 直接获取全文文本与插图。

数据来源（全部为官方开放渠道）：
  - Europe PMC 全文 XML：https://www.ebi.ac.uk/europepmc/webservices/rest/{PMCID}/fullTextXML
  - PMC 文章落地页（不做 JS 反爬拦截）中引用的 CDN 图片直链
    （https://cdn.ncbi.nlm.nih.gov/pmc/blobs/...）

产出（写入 --outdir，文件名固定，杜绝路径注入）：
  paper_text.txt   全文（章节结构保留，图/表位置插入题注）
  figures/<原名>.jpg/png   论文插图（以出版方原始文件名保存）
  metadata.json    论文元数据（标题/作者/期刊/年份/许可/来源）

安全约束：与 download_paper.py 相同——仅 http/https，逐跳拒绝非公网地址；
输出路径 pathlib 写入 + realpath 包含校验。

用法：
  python3 pmc_fetch.py --doi 10.7717/peerj.20655 --outdir paper_notes/foo/
  python3 pmc_fetch.py --pmcid PMC12962132 --outdir paper_notes/foo/

stdout 输出一行 JSON 摘要；退出码：0 成功 / 1 失败（无 PMC 全文等）/ 2 参数错误。
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from download_paper import (  # noqa: E402
    DownloadError,
    UA,
    fetch_json,
    safe_get,
    strip_tags,
    validate_url,
)

try:
    import requests  # noqa: F401  (safe_get 依赖；提前探测以便重解释器切换)
except ImportError:
    print(json.dumps({"status": "error",
                      "message": "缺少 requests 库：请用 /home/liyonghao/anaconda3/bin/python3 运行"},
                     ensure_ascii=False))
    sys.exit(2)

FIG_DIR = "figures"
TEXT_NAME = "paper_text.txt"
META_NAME = "metadata.json"
IMG_EXT = (".jpg", ".jpeg", ".png", ".gif")
BLOB_RE = re.compile(r"https://cdn\.ncbi\.nlm\.nih\.gov/pmc/blobs/[^\"]+")


# ---------------- 路径安全 ----------------

def prepare_outdir(outdir: str) -> str:
    if not outdir or ".." in outdir:
        raise ValueError(f"outdir 不允许为空或包含 '..'：{outdir!r}")
    root = os.path.realpath(os.path.abspath(outdir))
    os.makedirs(os.path.join(root, FIG_DIR), exist_ok=True)
    return root


def contained(root: str, target: pathlib.Path) -> pathlib.Path:
    real = os.path.realpath(str(target))
    if not real.startswith(root + os.sep):
        raise ValueError(f"输出路径越界，已拒绝：{target!r}")
    return pathlib.Path(real)


def write_text_in(root: str, name: str, text: str) -> str:
    target = contained(root, pathlib.Path(root) / name)
    target.write_text(text, encoding="utf-8")
    return str(target)


def write_bytes_in(root: str, rel: str, data: bytes) -> str:
    if ".." in rel or "\\" in rel or not rel:
        raise ValueError(f"非法文件名：{rel!r}")
    parts = [p for p in rel.split("/") if p]
    if len(parts) != 2 or parts[0] != FIG_DIR or any(p.startswith(".") for p in parts):
        raise ValueError(f"非法文件名：{rel!r}")
    target = contained(root, pathlib.Path(root).joinpath(*parts))
    target.write_bytes(data)
    return str(target)


# ---------------- 获取 ----------------

def find_pmcid(doi: str, proxy):
    """Europe PMC 检索 DOI，返回 (pmcid, 核心元数据)。"""
    q = urllib_quote(f'DOI:"{doi}"')
    data = fetch_json("https://www.ebi.ac.uk/europepmc/webservices/rest/search"
                      f"?query={q}&format=json&resultType=core", proxy)
    hits = ((data or {}).get("resultList") or {}).get("result") or []
    for h in hits:
        if h.get("pmcid"):
            return h["pmcid"], h
    return None, {}


def urllib_quote(s: str) -> str:
    import urllib.parse
    return urllib.parse.quote(s)


def fetch_fulltext_xml(pmcid: str, proxy) -> str:
    url = ("https://www.ebi.ac.uk/europepmc/webservices/rest/"
           f"{pmcid}/fullTextXML")
    validate_url(url)
    resp = safe_get(url, proxy=proxy, stream=False)
    try:
        if resp.status_code != 200:
            raise DownloadError(f"fullTextXML HTTP {resp.status_code}")
        return resp.text
    finally:
        resp.close()


def fetch_landing_html(pmcid: str, proxy) -> str:
    url = f"https://pmc.ncbi.nlm.nih.gov/articles/{pmcid}/"
    validate_url(url)
    resp = safe_get(url, proxy=proxy, stream=False)
    try:
        if resp.status_code != 200:
            raise DownloadError(f"PMC 落地页 HTTP {resp.status_code}")
        return resp.text
    finally:
        resp.close()


# ---------------- XML 解析 ----------------

def xml_to_text(xml: str) -> tuple[str, list[dict]]:
    """全文 XML → 纯文本（保留章节标题与图表题注）。返回 (text, figure_captions)。"""
    t = xml
    t = re.sub(r"<title>(.*?)</title>", r"\n\n## \1\n", t, flags=re.S)
    # 图：label + caption + graphic href
    figures = []
    def _fig(m):
        body = m.group(1)
        label = re.search(r"<label>(.*?)</label>", body, re.S)
        cap = re.search(r"<caption>(.*?)</caption>", body, re.S)
        href = re.search(r'xlink:href="([^"]+)"', body)
        text = f"\n[图 {label.group(1) if label else '?'}] " \
               f"{re.sub(r'<[^>]+>', ' ', cap.group(1)).strip() if cap else ''}\n"
        if href:
            figures.append({"href": href.group(1),
                            "label": (label.group(1) if label else "")})
        return text
    t = re.sub(r"<fig[^>]*>(.*?)</fig>", _fig, t, flags=re.S)
    # 表：label + caption
    def _tab(m):
        body = m.group(1)
        label = re.search(r"<label>(.*?)</label>", body, re.S)
        cap = re.search(r"<caption>(.*?)</caption>", body, re.S)
        return (f"\n[表 {label.group(1) if label else '?'}] "
                f"{re.sub(r'<[^>]+>', ' ', cap.group(1)).strip() if cap else ''}\n")
    t = re.sub(r"<table-wrap[^>]*>(.*?)</table-wrap>", _tab, t, flags=re.S)
    t = re.sub(r"<xref[^>]*>(.*?)</xref>", r"\1", t)
    t = re.sub(r"<[^>]+>", "", t)
    t = re.sub(r"\n{3,}", "\n\n", t)
    return t, figures


def parse_meta(pmcid: str, core: dict, doi: str | None) -> dict:
    ji = core.get("journalInfo") or {}
    journal = (ji.get("journal") or {}).get("title")
    return {
        "doi": doi or core.get("doi"),
        "title": strip_tags(core.get("title")) or core.get("title"),
        "authors": [a.strip() for a in (core.get("authorString") or "").split(",")
                    if a.strip()],
        "journal": journal,
        "year": core.get("pubYear") or ji.get("yearOfPublication"),
        "volume": core.get("journalVolume"),
        "pages": core.get("pageInfo"),
        "pmcid": pmcid,
        "pmid": core.get("pmid") or None,
        "license": core.get("license"),
        "is_preprint": False,
        "download_source": f"EuropePMC fullTextXML + PMC CDN images ({pmcid})",
    }


# ---------------- 主流程 ----------------

def main():
    ap = argparse.ArgumentParser(description="PMC 全文救援：获取全文文本与插图（无需 PDF）")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--doi", help="论文 DOI")
    g.add_argument("--pmcid", help="PubMed Central 编号（PMCxxxxxxx）")
    ap.add_argument("--outdir", required=True, help="输出目录")
    ap.add_argument("--proxy", default=os.environ.get("PAPER_EXPLAINER_PROXY"),
                    help="直连失败时使用的代理")
    args = ap.parse_args()

    def fail(msg, code=1):
        print(json.dumps({"status": "failure", "message": msg}, ensure_ascii=False))
        sys.exit(code)

    try:
        root = prepare_outdir(args.outdir)
    except ValueError as e:
        print(json.dumps({"status": "error", "message": str(e)}, ensure_ascii=False))
        sys.exit(2)

    warnings = []
    doi = args.doi
    try:
        if args.pmcid:
            pmcid, core = args.pmcid, {}
        else:
            pmcid, core = find_pmcid(args.doi, args.proxy)
            if not pmcid:
                fail("Europe PMC 未检索到该 DOI 的 PMC 全文（可能不在 PMC 收录范围）")
        log = lambda m: print(m, file=sys.stderr)
        log(f"PMCID: {pmcid}")

        try:
            xml = fetch_fulltext_xml(pmcid, args.proxy)
        except (DownloadError, requests.RequestException) as e:
            fail(f"无法获取 Europe PMC 全文 XML：{e}")
        text, figures = xml_to_text(xml)
        log(f"全文 XML：{len(xml)} 字符，{len(figures)} 个图元素")

        # 落地页 → CDN 图片直链
        blob_map = {}
        try:
            html = fetch_landing_html(pmcid, args.proxy)
            for u in BLOB_RE.findall(html):
                base = os.path.basename(u)
                if base.lower().endswith(IMG_EXT) and base not in blob_map:
                    blob_map[base] = u
            log(f"落地页 CDN 图片：{len(blob_map)} 个")
        except (DownloadError, requests.RequestException) as e:
            warnings.append(f"PMC 落地页获取失败（图片不可用，仅文本）：{e}")

        saved, missing = [], []
        for fig in figures:
            base = os.path.basename(fig["href"])
            if not base.lower().endswith(IMG_EXT):
                continue
            url = blob_map.get(base)
            if not url:
                missing.append(base)
                continue
            try:
                validate_url(url)
                resp = safe_get(url, proxy=args.proxy)
                if resp.status_code == 200 and len(resp.content) > 1000:
                    rel = f"{FIG_DIR}/{base}"
                    write_bytes_in(root, rel, resp.content)
                    saved.append({"file": rel, "label": fig["label"]})
                else:
                    missing.append(base)
                resp.close()
            except (DownloadError, requests.RequestException) as e:
                warnings.append(f"图片 {base} 下载失败：{e}")
                missing.append(base)

        write_text_in(root, TEXT_NAME, text)
        meta = parse_meta(pmcid, core, args.doi)
        meta["figures"] = [s["file"] for s in saved]
        write_text_in(root, META_NAME,
                      json.dumps(meta, ensure_ascii=False, indent=2))
    except ValueError as e:
        print(json.dumps({"status": "error", "message": str(e)}, ensure_ascii=False))
        sys.exit(2)
    except requests.RequestException as e:
        fail(f"网络请求失败: {e}")

    if missing:
        warnings.append(f"{len(missing)} 个图元素未匹配到 CDN 图片：{missing[:5]}"
                        + ("…" if len(missing) > 5 else ""))
    if len(text) < 5000:
        warnings.append("全文文本偏短，请人工核对是否为完整全文")

    print(json.dumps({
        "status": "ok",
        "pmcid": pmcid,
        "outdir": root,
        "text_file": os.path.join(root, TEXT_NAME),
        "text_chars": len(text),
        "figure_count": len(saved),
        "figures": saved,
        "metadata_file": os.path.join(root, META_NAME),
        "title": meta.get("title"),
        "warnings": warnings,
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
