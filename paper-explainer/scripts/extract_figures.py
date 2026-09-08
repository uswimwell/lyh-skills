#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""从论文 PDF 中提取全文文本与所有插图，供后续逐图讲解使用。

输入：--pdf 指向一个 PDF 文件（只读，不会修改）；--outdir 为输出目录。

输出（全部落在 outdir 内，文件名由脚本生成，杜绝路径注入）：
  paper_text.txt            全文，带 ===== Page N ===== 分页标记，文末附题注清单
  figures/fig_NNN_pP.png    每张插图（光栅图/矢量图统一按区域高清渲染）
  figures_manifest.json     图清单：文件、页码、图号、题注、来源类型、像素尺寸

处理策略（题注中心的两阶段法）：
  1. 每页收集候选图形区域 = 嵌入位图位置（get_image_rects）∪ 矢量绘图
     聚类（get_drawings 按 30pt 间距贪心聚类，被大段文本隔开的绘图不合并），
     过滤过小/过扁/近整页边框的区域；
  2. 把每个候选区域指派给"最近"的题注（Figure/Fig./Scheme/Chart/Plate/
     Graph/Table N：常规在图下方，也兼容题注贴着图底边的情况），同一题注
     的多个候选（如左右两个子图面板）合并为一张图；
  3. 指派给 Table 题注的组视为表格并跳过；没有匹配到题注的候选独立输出；
  4. 统一从页面按 --dpi 渲染成 PNG（保留坐标轴、图例等矢量元素与文字），
     按 SHA-256 去重（页眉 logo 等），降采样后近乎纯色的剔除。

stdout 输出一行 JSON 摘要；退出码：0 成功 / 2 环境或参数错误。
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import pathlib
import re
import sys

try:
    import fitz  # PyMuPDF
except ImportError:
    print(json.dumps({"status": "error",
                      "message": "缺少 PyMuPDF：请改用 /home/liyonghao/anaconda3/bin/python3 运行本脚本，"
                                 "或先 pip install pymupdf"}, ensure_ascii=False))
    sys.exit(2)

try:
    from PIL import Image
except ImportError:
    Image = None

FIG_DIR = "figures"
TEXT_NAME = "paper_text.txt"
MANIFEST_NAME = "figures_manifest.json"
OUTLINE_NAME = "paper_outline.txt"

FIG_CAPTION_RE = re.compile(
    r"^\s*(?:\d{1,3}\s+)?(?:(Extended\s+Data|Supplementary|Supp\.?|SI)\s*\.?\s*"
    r"(Figure|Fig\.?|Scheme|Chart|Plate|Graph)\s*\.?\s*([A-Za-z]?\d+)"
    r"|(Figure|Fig\.?|Scheme|Chart|Plate|Graph)\s*\.?\s*(\d+))", re.I)
TABLE_CAPTION_RE = re.compile(r"^\s*Table\s*\.?\s*(\d+)", re.I)

MIN_DIM_PT = 40        # 候选区域任一边小于此值（pt）则忽略
MIN_AREA_PT2 = 3000    # 候选区域面积下限
MAX_ASPECT = 15        # 长宽比上限（排除横线/竖条）
MERGE_GAP_PT = 30      # 矢量绘图聚类间距
CAPTION_BELOW_PT = 260
CAPTION_ABOVE_PT = 130


# ---------------- 路径安全 ----------------

def prepare_outdir(outdir: str) -> str:
    """校验并规范化 outdir（禁止 '..'），创建目录与 figures 子目录。"""
    if not outdir or ".." in outdir:
        raise ValueError(f"outdir 不允许为空或包含 '..'：{outdir!r}")
    root = os.path.realpath(os.path.abspath(outdir))
    os.makedirs(os.path.join(root, FIG_DIR), exist_ok=True)
    return root


def contained(root: str, target: pathlib.Path) -> pathlib.Path:
    """写入前确认目标路径仍位于 root 内部，防止路径穿越。"""
    real = os.path.realpath(str(target))
    if not real.startswith(root + os.sep):
        raise ValueError(f"输出路径越界，已拒绝：{target!r}")
    return pathlib.Path(real)


def write_bytes_in(root: str, parts, data: bytes) -> str:
    target = contained(root, pathlib.Path(root).joinpath(*parts))
    target.write_bytes(data)
    return str(target)


def write_text_in(root: str, parts, text: str) -> str:
    target = contained(root, pathlib.Path(root).joinpath(*parts))
    target.write_text(text, encoding="utf-8")
    return str(target)


# ---------------- 文本/题注 ----------------

def block_text(block) -> str:
    lines = []
    for line in block.get("lines", []):
        lines.append("".join(span.get("text", "") for span in line.get("spans", [])))
    return re.sub(r"\s+", " ", " ".join(lines)).strip()


def collect_captions(doc):
    """收集图/表题注。is_table=True 的题注用于把相邻区域归为表格并跳过。"""
    captions = []
    for pno, page in enumerate(doc):
        try:
            blocks = page.get_text("dict")["blocks"]
        except Exception:
            continue
        for block in blocks:
            if block.get("type") != 0:
                continue
            text = block_text(block)
            m = TABLE_CAPTION_RE.match(text)
            if m:
                captions.append({"page": pno, "rect": fitz.Rect(block["bbox"]),
                                 "label": f"Table {m.group(1)}", "text": text[:500],
                                 "is_table": True,
                                 "key": f"{pno}:{block['bbox'][0]:.0f}:{block['bbox'][1]:.0f}"})
                continue
            m = FIG_CAPTION_RE.match(text)
            if m:
                if m.group(2):          # 带前缀：Extended Data Fig. 1 等
                    prefix = re.sub(r"\s+", " ", m.group(1).rstrip("."))
                    label = f"{prefix} {m.group(2).rstrip('.')} {m.group(3)}"
                else:
                    kind = m.group(4).rstrip(".").capitalize()
                    label = f"{kind} {m.group(5)}"
                captions.append({"page": pno, "rect": fitz.Rect(block["bbox"]),
                                 "label": label, "text": text[:500],
                                 "is_table": False,
                                 "key": f"{pno}:{block['bbox'][0]:.0f}:{block['bbox'][1]:.0f}"})
    return captions


def big_text_rects(page):
    """较大的文本块（题注/段落级别），用于阻止跨文本合并绘图。"""
    out = []
    try:
        for b in page.get_text("dict")["blocks"]:
            if b.get("type") != 0:
                continue
            r = fitz.Rect(b["bbox"])
            if r.height >= 20 and r.width >= 30:
                out.append(r)
    except Exception:
        pass
    return out


# ---------------- 图形区域 ----------------

def raster_rects(page, page_rect):
    rects = []
    try:
        seen = set()
        for img in page.get_images(full=True):
            xref = img[0]
            if xref in seen:
                continue
            seen.add(xref)
            try:
                for r in page.get_image_rects(xref):
                    r = fitz.Rect(r) & page_rect
                    if not r.is_empty and r.width > 2 and r.height > 2:
                        rects.append(fitz.Rect(r))
            except Exception:
                continue
    except Exception:
        pass
    return rects


def drawing_rects(page):
    rects = []
    try:
        rects = [fitz.Rect(d["rect"]) for d in page.get_drawings()]
    except Exception:
        pass
    return rects


def _text_between(t, a, b) -> bool:
    """文本块 t 是否严格隔在区域 a 与 b 之间（一上一下或一左一右）。"""
    vert = (a.y1 <= t.y0 + 1 and b.y0 >= t.y1 - 1) or \
           (b.y1 <= t.y0 + 1 and a.y0 >= t.y1 - 1)
    if vert:
        ov_a = min(a.x1, t.x1) - max(a.x0, t.x0)
        ov_b = min(b.x1, t.x1) - max(b.x0, t.x0)
        if ov_a >= 20 and ov_b >= 20:
            return True
    horiz = (a.x1 <= t.x0 + 1 and b.x0 >= t.x1 - 1) or \
            (b.x1 <= t.x0 + 1 and a.x0 >= t.x1 - 1)
    if horiz:
        ov_a = min(a.y1, t.y1) - max(a.y0, t.y0)
        ov_b = min(b.y1, t.y1) - max(b.y0, t.y0)
        if ov_a >= 20 and ov_b >= 20:
            return True
    return False


def merge_drawings(rects, text_rects, gap=MERGE_GAP_PT):
    """按间距贪心聚类矢量绘图；被大段文本隔开的不合并。"""
    clusters = []
    for r in sorted((fitz.Rect(x) for x in rects), key=lambda x: (x.y0, x.x0)):
        target = None
        for c in clusters:
            if c["rect"].intersects(fitz.Rect(r.x0 - gap, r.y0 - gap,
                                              r.x1 + gap, r.y1 + gap)):
                if any(_text_between(t, c["rect"], r) for t in text_rects):
                    continue  # 文本隔断：不并入该簇
                target = c
                break
        if target is None:
            clusters.append({"rect": fitz.Rect(r)})
        else:
            target["rect"] |= r
    return [c["rect"] for c in clusters]


def page_text_rects(page):
    """页面上全部文本块矩形（用于文字占比过滤）。"""
    rects = []
    try:
        for b in page.get_text("dict")["blocks"]:
            if b.get("type") != 0:
                continue
            r = fitz.Rect(b["bbox"])
            if r.width >= 5 and r.height >= 5:
                rects.append(r)
    except Exception:
        pass
    return rects


def build_page_candidates(page):
    """返回本页候选图形区域 [(rect, raster覆盖占比), ...]。"""
    page_rect = page.rect
    all_text = page_text_rects(page)
    ras = raster_rects(page, page_rect)
    vecs = merge_drawings(drawing_rects(page),
                          [r for r in all_text if r.height >= 20 and r.width >= 30])

    def keep(r):
        if r.width < MIN_DIM_PT or r.height < MIN_DIM_PT:
            return False
        if r.width * r.height < MIN_AREA_PT2:
            return False
        short = min(r.width, r.height)
        if max(r.width, r.height) / max(short, 1) > MAX_ASPECT:
            return False
        # 几乎整页的边框矩形（页面装饰框）跳过
        if r.width > 0.97 * page_rect.width and r.height > 0.97 * page_rect.height:
            return False
        return True

    cands = [fitz.Rect(r) for r in ras + vecs if keep(fitz.Rect(r))]
    # 嵌套去重：小候选几乎完全落在大候选内部时视为碎片剔除
    cands.sort(key=lambda r: -(r.width * r.height))
    kept = []
    for r in cands:
        if any((r & k).get_area() >= 0.85 * max(r.get_area(), 1e-6) for k in kept):
            continue
        kept.append(r)
    out = []
    for r in kept:
        covered = 0.0
        for rr in ras:
            inter = r & rr
            if not inter.is_empty:
                covered += inter.width * inter.height
        cover = min(covered / max(r.width * r.height, 1e-6), 1.0)
        # 文本框假阳性过滤：几乎不含位图、且区域内一半以上面积被文字
        # 占据的"矢量框"，多为排版元素（如期刊摘要框、签名栏），跳过
        if cover < 0.05:
            tcov = sum((t & r).get_area() for t in all_text) / max(r.get_area(), 1e-6)
            if tcov > 0.5:
                continue
        out.append((r, cover))
    out.sort(key=lambda t: (round(t[0].y0), t[0].x0))
    return out


def caption_dist(R, C):
    """候选区域 R 与题注块 C 的匹配距离；不匹配返回 None。

    题注常规在图下方；也兼容：题注在上方（加大惩罚，同栏上下两图时
    下方的题注更可能属于当前图）、题注贴着图的底边/顶边（热图单元格
    与题注文字在 bbox 上略有重叠的排版）。
    """
    overlap = min(R.x1, C.x1) - max(R.x0, C.x0)
    if overlap < 10:
        return None
    if C.y0 >= R.y1 - 8:                      # 下方
        d = C.y0 - R.y1
        return d + 0.5 if d <= CAPTION_BELOW_PT else None
    if C.y1 <= R.y0 + 8:                      # 上方（软惩罚）
        d = R.y0 - C.y1
        return d + 100.5 if d <= CAPTION_ABOVE_PT else None
    # 有重叠：题注贴着区域底边（题注底部不低于区域底部 15pt）
    if C.y1 >= R.y1 - 15 and (R.y1 - C.y0) <= max(30, 0.3 * R.height):
        return 0.0
    # 题注贴着区域顶边
    if C.y0 <= R.y0 + 15 and (C.y1 - R.y0) <= max(30, 0.3 * R.height):
        return 0.0
    return None


def assemble_figures(doc, captions):
    """题注中心两阶段：候选区域 → 最近题注分组 → 同题注合并。"""
    figures, tables_skipped = [], 0
    for pno, page in enumerate(doc):
        cands = build_page_candidates(page)
        caps = [c for c in captions if c["page"] == pno]
        if not cands:
            continue
        assign = {}   # cand idx -> (cap, dist)
        content_x0 = min(r.x0 for r, _ in cands)
        content_x1 = max(r.x1 for r, _ in cands)
        for i, (r, _cover) in enumerate(cands):
            best = None
            for cap in caps:
                d = caption_dist(r, cap["rect"])
                if d is not None and (best is None or d < best[1]):
                    best = (cap, d)
            if best is None:
                # 兜底：Nature 等排版题注只占部分栏宽，但图面板横跨全页。
                # 把题注横向扩展到页面实际内容宽度后再匹配（加大惩罚，
                # 保证严格匹配优先）。
                for cap in caps:
                    ext = fitz.Rect(content_x0, cap["rect"].y0,
                                    content_x1, cap["rect"].y1)
                    d = caption_dist(r, ext)
                    if d is not None and (best is None or d + 250 < best[1]):
                        best = (cap, d + 250)
            if best:
                assign[i] = best
        groups = {}
        for i, (cap, _d) in assign.items():
            groups.setdefault(cap["key"], {"cap": cap, "idx": []})["idx"].append(i)
        # 题注锚定带状兜底：排版型图（文字+小箭头组成）的绘图元素过小
        # 无法成簇，或题注名下候选碎片过窄时，提取题注上方整条带状区域。
        page_top = page.rect.y0 + 36
        big_blocks = big_text_rects(page)
        for cap in caps:
            if cap["is_table"]:
                continue
            g = groups.get(cap["key"])
            union = None
            if g:
                rects = [cands[i][0] for i in g["idx"]]
                union = rects[0]
                for r in rects[1:]:
                    union |= r
            thin = union is None or (union.width < 0.5 * cap["rect"].width
                                     and union.width < 200)
            if not thin:
                continue
            y_top = page_top
            for t in big_blocks:
                if (t.height >= 30 and t.width >= 250 and
                        t.y1 <= cap["rect"].y0 - 2 and
                        t.y1 > cap["rect"].y0 - 450 and t.y1 + 2 > y_top):
                    y_top = t.y1 + 2
            for other in caps:
                if (other["key"] != cap["key"] and
                        other["rect"].y1 <= cap["rect"].y0 - 2 and
                        other["rect"].y1 + 2 > y_top):
                    y_top = other["rect"].y1 + 2
            band = fitz.Rect(cap["rect"].x0, y_top, cap["rect"].x1,
                             cap["rect"].y0 - 2) & page.rect
            if band.is_empty or band.height < 40:
                continue
            if union is not None:
                band |= union
            band = band & page.rect
            if g:
                g["band_rect"] = band
            else:
                groups[cap["key"]] = {"cap": cap, "idx": [], "band_rect": band}
        for key, g in groups.items():
            cap = g["cap"]
            if g.get("band_rect") is not None:
                rect, cover = g["band_rect"], 0.0
            else:
                rects = [cands[i][0] for i in g["idx"]]
                rect = rects[0]
                for r in rects[1:]:
                    rect |= r
                cover = max(cands[i][1] for i in g["idx"])
            if cap["is_table"]:
                tables_skipped += 1
                continue
            figures.append({"page": pno, "rect": rect, "raster_cover": cover,
                            "label": cap["label"], "caption": cap["text"]})
        claimed = set(assign.keys())
        for i, (r, cover) in enumerate(cands):
            if i not in claimed:
                figures.append({"page": pno, "rect": r, "raster_cover": cover,
                                "label": None, "caption": ""})
    figures.sort(key=lambda f: (f["page"], round(f["rect"].y0), f["rect"].x0))
    return figures, tables_skipped


def is_mostly_blank(png_bytes) -> bool:
    """降采样后只剩 1-2 种灰度即视为空白/装饰框。"""
    if Image is None:
        return False
    try:
        im = Image.open(io.BytesIO(png_bytes)).convert("L")
        im = im.resize((64, 64))
        colors = im.getcolors(maxcolors=64 * 64)
        return colors is not None and len(colors) <= 2
    except Exception:
        return False


def render_region(page, rect, dpi) -> bytes:
    zoom = dpi / 72.0
    pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), clip=rect, alpha=False)
    return pix.tobytes("png")


def extract_outline(doc, max_items=400):
    """识别标题行，生成论文大纲。

    两条规则：(1) 字号 ≥ 正文 1.25 倍的文本块；(2) 整块加粗、长度适中、
    不以句号结尾且非图表题注的短行（同字号加粗标题的期刊模板，如 PeerJ）。
    返回 (大纲行列表, 正文字号)；无文本时返回 (None, 0)。
    """
    sizes = []
    blocks_seq = []  # (page, max_span_size, all_bold, block_text)
    for pno, page in enumerate(doc):
        try:
            blocks = page.get_text("dict")["blocks"]
        except Exception:
            continue
        for b in blocks:
            if b.get("type") != 0:
                continue
            bmax = 0.0
            parts = []
            n_bold = n_span = 0
            for line in b.get("lines", []):
                for span in line.get("spans", []):
                    s = float(span.get("size", 0.0) or 0.0)
                    txt = span.get("text", "").strip()
                    if txt:
                        bmax = max(bmax, s)
                        parts.append(txt)
                        n_span += 1
                        n_bold += 1 if (span.get("flags", 0) or 0) & 16 else 0
            t = re.sub(r"\s+", " ", " ".join(parts)).strip()
            if t:
                blocks_seq.append((pno, bmax, n_span > 0 and n_bold == n_span, t))
                sizes.append(bmax)
    if not sizes:
        return None, 0
    sizes.sort()
    body_size = sizes[len(sizes) // 2]
    threshold = body_size * 1.25
    items = []
    for p, sz, bold, t in blocks_seq:
        if len(t) < 3:
            continue
        if sz >= threshold:
            items.append(f"[Page {p + 1} | {sz:.1f}pt] {t[:150]}")
        elif (bold and 5 <= len(t) <= 90 and not t.endswith((".", "。", ";", "，", ","))
              and not FIG_CAPTION_RE.match(t) and not TABLE_CAPTION_RE.match(t)):
            items.append(f"[Page {p + 1} | {sz:.1f}pt bold] {t[:150]}")
    return items[:max_items], round(body_size, 1)


# ---------------- 主流程 ----------------

def main():
    ap = argparse.ArgumentParser(description="提取论文 PDF 的全文文本与所有插图")
    ap.add_argument("--pdf", required=True, help="输入 PDF 路径（只读）")
    ap.add_argument("--outdir", required=True, help="输出目录（内部生成固定文件名）")
    ap.add_argument("--dpi", type=int, default=200, help="渲染分辨率，默认 200")
    args = ap.parse_args()

    warnings = []
    pdf_path = os.path.realpath(args.pdf)
    if not os.path.isfile(pdf_path):
        print(json.dumps({"status": "error",
                          "message": f"PDF 不存在：{args.pdf}"}, ensure_ascii=False))
        sys.exit(2)

    try:
        root = prepare_outdir(args.outdir)
    except ValueError as e:
        print(json.dumps({"status": "error", "message": str(e)}, ensure_ascii=False))
        sys.exit(2)

    doc = fitz.open(pdf_path)
    if doc.needs_pass:
        if not doc.authenticate(""):
            print(json.dumps({"status": "error",
                              "message": "PDF 已加密，无法解析"}, ensure_ascii=False))
            sys.exit(2)
    pages = doc.page_count

    captions = collect_captions(doc)
    figures, tables_skipped = assemble_figures(doc, captions)

    # 渲染 + 去重
    manifest, hashes = [], set()
    duplicates = blanks = 0
    seq = 0
    for fig in figures:
        page = doc[fig["page"]]
        try:
            png = render_region(page, fig["rect"], args.dpi)
        except Exception as e:
            warnings.append(f"第 {fig['page'] + 1} 页某区域渲染失败: {e}")
            continue
        h = hashlib.sha256(png).hexdigest()
        if h in hashes:
            duplicates += 1
            continue
        if is_mostly_blank(png):
            blanks += 1
            continue
        hashes.add(h)
        seq += 1
        name = f"fig_{seq:03d}_p{fig['page'] + 1}.png"
        rel = write_bytes_in(root, (FIG_DIR, name), png)
        try:
            im = Image.open(io.BytesIO(png))
            w_px, h_px = im.size
        except Exception:
            w_px = h_px = None
        manifest.append({
            "file": f"{FIG_DIR}/{name}",
            "path": rel,
            "page": fig["page"] + 1,
            "label": fig.get("label") or "未识别编号",
            "caption": fig.get("caption") or "",
            "source": "raster" if fig["raster_cover"] > 0.5 else "vector",
            "rect_pt": [round(v, 1) for v in fig["rect"]],
            "width_px": w_px,
            "height_px": h_px,
        })

    # 全文文本
    text_parts = []
    total_chars = 0
    for pno, page in enumerate(doc):
        t = page.get_text("text")
        total_chars += len(t.strip())
        text_parts.append(f"\n\n===== Page {pno + 1} =====\n\n{t}")
    text_parts.append("\n\n===== 检测到的图表题注 =====\n")
    for cap in captions:
        text_parts.append(f"[Page {cap['page'] + 1}] {cap['label']}: {cap['text']}\n")
    write_text_in(root, (TEXT_NAME,), "".join(text_parts))
    write_text_in(root, (MANIFEST_NAME,),
                  json.dumps(manifest, ensure_ascii=False, indent=2))

    # 论文大纲（按字号识别标题行，供长文快速定位结构）
    outline_items, body_size = extract_outline(doc)
    outline_file = None
    if outline_items:
        outline_file = write_text_in(root, (OUTLINE_NAME,),
                                     "\n".join(outline_items) + "\n")
    doc.close()

    if total_chars < 120 * pages:
        warnings.append("PDF 文本层很薄，可能是扫描件：正文无法用文本方式精读，"
                        "请改用逐页渲染看图的方式讲解")
    if not manifest:
        warnings.append("未检测到插图：可能论文确实无图，或图片以不受支持的方式嵌入，"
                        "必要时可对相关页面整页截图补充")
    caption_fig_count = len({c["label"] for c in captions if not c["is_table"]})
    if caption_fig_count > len(manifest):
        warnings.append(f"检测到 {caption_fig_count} 个图题注但只提取出 {len(manifest)} 张图，"
                        "请人工核对是否有漏图")

    print(json.dumps({
        "status": "ok",
        "pdf": pdf_path,
        "outdir": root,
        "pages": pages,
        "text_chars": total_chars,
        "captions_found": len(captions),
        "figure_count": len(manifest),
        "tables_skipped": tables_skipped,
        "duplicates_removed": duplicates,
        "blanks_removed": blanks,
        "text_file": os.path.join(root, TEXT_NAME),
        "manifest_file": os.path.join(root, MANIFEST_NAME),
        "outline_file": outline_file,
        "outline_items": len(outline_items) if outline_items else 0,
        "body_font_size": body_size,
        "figures": [{"file": m["file"], "page": m["page"], "label": m["label"]}
                    for m in manifest],
        "warnings": warnings,
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
