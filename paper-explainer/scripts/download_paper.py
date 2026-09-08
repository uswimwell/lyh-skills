#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""按 DOI（或直链 URL）下载论文 PDF，优先开放获取渠道。

尝试顺序：
  1. arXiv DOI 直取（10.48550/arXiv.XXXX）
  2. Unpaywall 开放获取链接
  3. OpenAlex 开放获取链接
  4. Europe PMC（PMC 全文）
  5. 出版社直链模式（Springer/Nature/PNAS/Wiley/ACM/ScienceDirect/IEEE）
  6. arXiv 标题搜索（预印本兜底，结果会标记 is_preprint=true）

输出（--outdir 目录内，文件名固定，杜绝路径注入）：
  paper.pdf        下载的 PDF
  metadata.json    论文元数据（标题/作者/期刊/被引/开放获取状态等）

安全约束：
  - 防 SSRF：仅允许 http/https；每次请求（包括重定向的每一跳）之前校验
    目标主机，拒绝回环/私有/保留/链路本地/组播等非公网地址。
  - 防路径穿越：outdir 禁止包含 ".."；写入前用 realpath 规范化并逐次确认
    目标仍在 outdir 内部。

用法：
  python3 download_paper.py --doi 10.1038/s41586-021-03819-2 --outdir paper_notes/foo/
  python3 download_paper.py --url https://xxx/paper.pdf --outdir paper_notes/foo/
  python3 download_paper.py --doi ... --proxy http://127.0.0.1:7890   # 直连失败时走代理

stdout 最后输出一行 JSON 摘要；退出码：0 成功 / 1 下载失败 / 2 环境或参数错误。
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import os
import pathlib
import re
import socket
import sys
import urllib.parse

try:
    import requests
except ImportError:
    print(json.dumps({"status": "error",
                      "message": "缺少 requests 库：请用 /home/liyonghao/anaconda3/bin/python3 运行，"
                                 "或先 pip install requests"}, ensure_ascii=False))
    sys.exit(2)

UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
TIMEOUT = (15, 90)
MAX_REDIRECTS = 10
MAX_BYTES = 300 * 1024 * 1024

ALTERNATIVES = [
    "核对 DOI 拼写是否正确（可在 https://doi.org 或 https://search.crossref.org 验证）",
    "把本地 PDF 路径发给助手：通过学校/机构订阅（校园网内）或实验室共享渠道下载后提供文件",
    "查找预印本版本：在 arXiv / bioRxiv / medRxiv / ChemRxiv / SSRN 用论文标题搜索",
    "通过图书馆文献传递服务获取，或给论文通讯作者发邮件索取",
    "如果你有可用的 PDF 直链 URL，把链接发给助手用 --url 方式重试",
]

PDF_NAME = "paper.pdf"
META_NAME = "metadata.json"


class DownloadError(Exception):
    pass


def log(msg: str) -> None:
    print(msg, file=sys.stderr)


def prepare_outdir(outdir: str) -> str:
    """校验并规范化 outdir，返回其绝对路径（目录已创建）。

    outdir 不允许为空或包含 '..'；规范化后确认其中固定文件名
    paper.pdf / metadata.json 的落点仍在本目录内。
    """
    if not outdir or ".." in outdir:
        raise ValueError(f"outdir 不允许为空或包含 '..'：{outdir!r}")
    root = os.path.realpath(os.path.abspath(outdir))
    for name in (PDF_NAME, META_NAME):
        target = os.path.realpath(os.path.join(root, name))
        if os.path.dirname(target) != root or not target.startswith(root + os.sep):
            raise ValueError(f"输出路径越界，已拒绝：{target!r}")
    os.makedirs(root, exist_ok=True)
    return root


def safe_write_bytes(root: str, name: str, data: bytes) -> pathlib.Path:
    """在 root 目录内写入固定文件名；写入前再次做包含性校验。"""
    if name not in (PDF_NAME, META_NAME):
        raise ValueError(f"不允许的文件名：{name!r}")
    target = pathlib.Path(root) / name
    if os.path.dirname(os.path.realpath(str(target))) != root:
        raise ValueError(f"输出路径越界，已拒绝：{name!r}")
    target.write_bytes(data)
    return target


def safe_write_text(root: str, name: str, text: str) -> pathlib.Path:
    """在 root 目录内写入固定文件名；写入前再次做包含性校验。"""
    if name not in (PDF_NAME, META_NAME):
        raise ValueError(f"不允许的文件名：{name!r}")
    target = pathlib.Path(root) / name
    if os.path.dirname(os.path.realpath(str(target))) != root:
        raise ValueError(f"输出路径越界，已拒绝：{name!r}")
    target.write_text(text, encoding="utf-8")
    return target


# ---------------- SSRF 防护 ----------------

def _ip_is_forbidden(ip) -> bool:
    return (ip.is_loopback or ip.is_private or ip.is_link_local or
            ip.is_reserved or ip.is_multicast or ip.is_unspecified or
            not ip.is_global)


def validate_url(url: str) -> str:
    """仅允许 http/https，且主机解析出的所有 IP 都必须是公网地址。"""
    try:
        parsed = urllib.parse.urlsplit(url)
    except ValueError:
        raise DownloadError(f"URL 无法解析: {url!r}")
    if parsed.scheme not in ("http", "https"):
        raise DownloadError(f"仅允许 http/https 协议，实际为 {parsed.scheme!r}")
    host = parsed.hostname
    if not host:
        raise DownloadError(f"URL 缺少主机名: {url!r}")
    try:
        ipaddress.ip_address(host)
        ips = {host}
    except ValueError:
        infos = []
        for family in (socket.AF_INET, socket.AF_INET6):
            try:
                infos += socket.getaddrinfo(host, None, family)
            except socket.gaierror:
                pass
        if not infos:
            raise DownloadError(f"域名解析失败: {host}")
        ips = {info[4][0] for info in infos}
    for raw in sorted(ips):
        try:
            ip = ipaddress.ip_address(raw)
        except ValueError:
            continue
        if _ip_is_forbidden(ip):
            raise DownloadError(f"拒绝请求非公网地址 {raw}（主机 {host}）")
    return url


def safe_get(url: str, proxy=None, stream: bool = True):
    """带 SSRF 校验与手动重定向跟踪的 GET。"""
    current = url
    proxies = {"http": proxy, "https": proxy} if proxy else None
    for _ in range(MAX_REDIRECTS + 1):
        validate_url(current)
        resp = requests.get(current, headers={"User-Agent": UA, "Accept": "*/*"},
                            timeout=TIMEOUT, stream=stream,
                            allow_redirects=False, proxies=proxies)
        if resp.status_code in (301, 302, 303, 307, 308):
            loc = resp.headers.get("Location", "")
            resp.close()
            if not loc:
                raise DownloadError(f"重定向缺少 Location: {current}")
            current = urllib.parse.urljoin(current, loc)
            continue
        return resp
    raise DownloadError(f"重定向次数超过 {MAX_REDIRECTS}: {url}")


def fetch_pdf(url: str, proxy):
    """下载并校验是真正的 PDF。返回 (bytes, None) 或 (None, 原因)。"""
    resp = safe_get(url, proxy=proxy)
    try:
        if resp.status_code != 200:
            return None, f"HTTP {resp.status_code}"
        chunks, total = [], 0
        for chunk in resp.iter_content(chunk_size=1 << 16):
            chunks.append(chunk)
            total += len(chunk)
            if total > MAX_BYTES:
                raise DownloadError("文件过大（>300MB）")
        data = b"".join(chunks)
    finally:
        resp.close()
    ctype = resp.headers.get("Content-Type", "").split(";")[0]
    if len(data) < 20000:
        return None, f"内容过小（{len(data)}B）"
    if b"%PDF" not in data[:2048]:
        return None, f"返回的不是 PDF（Content-Type={ctype}，可能是网页/验证页）"
    return data, None


def fetch_json(url: str, proxy):
    resp = safe_get(url, proxy=proxy, stream=False)
    try:
        if resp.status_code != 200:
            return None
        return resp.json()
    except ValueError:
        return None
    finally:
        resp.close()


# ---------------- 输入处理 ----------------

def normalize_doi(raw: str) -> str:
    s = raw.strip()
    s = re.sub(r"^\s*(https?://)?(dx\.)?doi\.org/", "", s, flags=re.I)
    s = re.sub(r"^\s*doi\s*:\s*", "", s, flags=re.I)
    s = s.strip()
    if not re.match(r"^10\.\d{4,9}/\S+$", s):
        raise ValueError(f"不是合法的 DOI：{raw!r}（示例：10.1038/s41586-021-03819-2）")
    return s


def strip_tags(html):
    if not html:
        return None
    txt = re.sub(r"<[^>]+>", " ", html)
    return re.sub(r"\s+", " ", txt).strip() or None


# ---------------- 元数据源 ----------------

def crossref_meta(doi: str, proxy):
    data = fetch_json(f"https://api.crossref.org/works/{urllib.parse.quote(doi)}", proxy)
    if not data:
        return {}
    m = data.get("message", {}) or {}
    authors = []
    for a in m.get("author", []) or []:
        name = " ".join(x for x in [a.get("given"), a.get("family")] if x)
        if name:
            authors.append(name)
    year = None
    for key in ("published-print", "published-online", "issued"):
        parts = ((m.get(key) or {}).get("date-parts") or [[None]])[0]
        if parts and parts[0]:
            year = parts[0]
            break
    alts = []
    for a in m.get("alternative-id") or []:
        if isinstance(a, dict) and a.get("id"):
            alts.append(str(a["id"]))
        elif isinstance(a, str):
            alts.append(a)
    return {
        "doi": doi,
        "title": (m.get("title") or [None])[0],
        "authors": authors,
        "journal": (m.get("container-title") or [None])[0],
        "year": year,
        "publisher": m.get("publisher"),
        "citations": m.get("is-referenced-by-count"),
        "abstract": strip_tags(m.get("abstract")),
        "alternative_id": alts,
        "type": m.get("type"),
    }


def datacite_meta(doi: str, proxy):
    """DataCite 元数据（arXiv/Zenodo 等 DOI 注册在 DataCite，CrossRef 查不到）。"""
    data = fetch_json(f"https://api.datacite.org/dois/{urllib.parse.quote(doi)}", proxy)
    if not data:
        return {}
    attrs = data.get("data", {}).get("attributes", {}) or {}
    authors = [c.get("name") for c in attrs.get("creators", []) or [] if c.get("name")]
    titles = attrs.get("titles") or []
    return {
        "doi": doi,
        "title": (titles[0].get("title") if titles and titles[0].get("title") else None),
        "authors": authors,
        "journal": (attrs.get("publisher") or {}).get("name")
        if isinstance(attrs.get("publisher"), dict) else attrs.get("publisher"),
        "year": attrs.get("publicationYear"),
        "publisher": attrs.get("publisher") if isinstance(attrs.get("publisher"), str) else None,
        "abstract": strip_tags((attrs.get("descriptions") or [{}])[0].get("description")
                               if attrs.get("descriptions") else None),
    }


def unpaywall_candidates(doi: str, email: str, proxy):
    url = (f"https://api.unpaywall.org/v2/{urllib.parse.quote(doi)}"
           f"?email={urllib.parse.quote(email)}")
    data = fetch_json(url, proxy)
    if not data:
        return [], {}
    meta = {"is_oa": data.get("is_oa"),
            "oa_status": data.get("oa_status"),
            "unpaywall_title": data.get("title"),
            "unpaywall_journal": data.get("journal_name"),
            "license": (data.get("best_oa_location") or {}).get("license")}
    urls = []
    locs = []
    if data.get("best_oa_location"):
        locs.append(data["best_oa_location"])
    locs += data.get("oa_locations") or []
    for loc in locs:
        for key in ("url_for_pdf", "url"):
            u = loc.get(key)
            if u and u not in urls:
                urls.append(u)
    return urls, meta


def openalex_candidates(doi: str, proxy):
    url = ("https://api.openalex.org/works/doi:" + urllib.parse.quote(doi)
           + "?mailto=paper-explainer@example.org")
    data = fetch_json(url, proxy)
    if not data:
        return [], {}
    meta = {"openalex_citations": data.get("cited_by_count"),
            "openalex_oa_url": (data.get("open_access") or {}).get("oa_url"),
            "openalex_type": data.get("type")}
    urls = []
    best = data.get("best_oa_location") or {}
    locs = []
    if best.get("pdf_url"):
        locs.append(best["pdf_url"])
    oa = (data.get("open_access") or {}).get("oa_url")
    if oa:
        locs.append(oa)
    for loc in data.get("locations") or []:
        if loc.get("pdf_url"):
            locs.append(loc["pdf_url"])
    for u in locs:
        if u and u not in urls:
            urls.append(u)
    return urls, meta


def europepmc_candidates(doi: str, proxy):
    q = urllib.parse.quote(f'DOI:"{doi}"')
    url = ("https://www.ebi.ac.uk/europepmc/webservices/rest/search"
           f"?query={q}&format=json&resultType=core")
    data = fetch_json(url, proxy)
    hits = ((data or {}).get("resultList") or {}).get("result") or []
    if not hits:
        return [], {}
    h = hits[0]
    meta = {"pmcid": h.get("pmcid"), "pmid": h.get("pmid"),
            "europepmc_oa": h.get("isOpenAccess")}
    urls = []
    if h.get("pmcid"):
        urls.append("https://www.ebi.ac.uk/europepmc/webservices/rest/"
                    f"PMC/{h['pmcid']}/fullTextPDF")
    return urls, meta


def publisher_candidates(doi: str, crossref: dict):
    doi_l = doi.lower().strip()
    urls = []

    def add(u):
        if u and u not in urls:
            urls.append(u)

    if re.match(r"^10\.(1007|1186|3758|10696|10083)/", doi_l):
        add(f"https://link.springer.com/content/pdf/{doi_l}.pdf")
    if doi_l.startswith("10.1038/"):
        add(f"https://www.nature.com/articles/{doi_l.split('/', 1)[1]}.pdf")
    if doi_l.startswith("10.1073/"):
        add(f"https://www.pnas.org/doi/pdf/{doi_l}")
    if doi_l.startswith("10.1002/"):
        add(f"https://onlinelibrary.wiley.com/doi/pdfdirect/{doi_l}?download=true")
    if doi_l.startswith("10.1145/"):
        add(f"https://dl.acm.org/doi/pdf/{doi_l}")
    m = re.match(r"^10\.7717/(peerj(?:-[a-z]+)?)[.\-](\d+)$", doi_l)
    if m:
        slug = m.group(2) if m.group(1) == "peerj" else \
            f"{m.group(1)[len('peerj-'):]}-{m.group(2)}"
        add(f"https://peerj.com/articles/{slug}.pdf")
    alts = crossref.get("alternative_id") or []
    if doi_l.startswith("10.1016/"):
        for v in alts:
            if re.fullmatch(r"\S{10,}", v) and not v.startswith("10."):
                add(f"https://www.sciencedirect.com/science/article/pii/"
                    f"{v}/pdfft?isDTMRedir=true&download=true")
                break
    if doi_l.startswith("10.1109/"):
        for v in alts:
            if v.isdigit():
                add(f"https://ieeexplore.ieee.org/stamp/stamp.jsp?tp=&arnumber={v}")
                break
    return urls


def arxiv_doi_candidates(doi: str):
    m = re.match(r"(?i)^10\.48550/arXiv\.(.+)$", doi.strip())
    return [f"https://arxiv.org/pdf/{m.group(1)}"] if m else []


def arxiv_title_candidates(title, proxy):
    if not title:
        return []
    t = re.sub(r"\s+", " ", title).strip()
    variants = [t, " ".join(t.split()[:10])]
    ids = []
    for v in variants:
        if len(v) < 8:
            continue
        q = urllib.parse.quote(f'ti:"{v}"')
        resp = safe_get(f"https://export.arxiv.org/api/query?search_query={q}"
                        f"&max_results=3", proxy, stream=False)
        try:
            text = resp.text
        finally:
            resp.close()
        ids += re.findall(r"<id>https?://arxiv\.org/abs/([^<]+)</id>", text)
        if ids:
            break
    urls = []
    for i in ids:
        i = re.sub(r"v\d+$", "", i.strip())
        u = f"https://arxiv.org/pdf/{i}"
        if u not in urls:
            urls.append(u)
    return urls


# ---------------- 主流程 ----------------

def url_variants(url: str):
    """由落地页 URL 推导可能的 PDF 直链（HAL 存档、PMC 全文）。"""
    out = [url]
    host = urllib.parse.urlsplit(url).hostname or ""
    if host in ("hal.science",) or host.endswith(".hal.science") or \
       host.startswith("hal."):
        u = url.rstrip("/")
        if not u.endswith("/document"):
            out.append(u + "/document")
    if host == "pmc.ncbi.nlm.nih.gov":
        u = url.rstrip("/")
        if not u.endswith("/pdf"):
            out.append(u + "/pdf/")
    return out


def try_urls(urls, source, attempts, proxy):
    for u0 in urls:
        for u in url_variants(u0):
            try:
                data, err = fetch_pdf(u, proxy)
                note = err or f"{len(data)} bytes"
                attempts.append({"source": source, "url": u, "ok": not err, "note": note})
                log(f"  [{source}] {'OK' if not err else '失败'} {u} -> {note}")
                if data:
                    return data, u
            except DownloadError as e:
                attempts.append({"source": source, "url": u, "ok": False, "note": str(e)})
                log(f"  [{source}] 拒绝 {u} -> {e}")
            except requests.RequestException as e:
                note = f"网络错误: {type(e).__name__}"
                attempts.append({"source": source, "url": u, "ok": False, "note": note})
                log(f"  [{source}] 错误 {u} -> {note}")
    return None, None


STOPWORDS = {"a", "an", "the", "of", "in", "for", "and", "with", "on", "to",
             "via", "by", "from", "using", "based", "study", "towards"}


def suggest_dir(meta: dict) -> str:
    """从元数据生成建议目录短名：第一作者姓氏+年份+标题前几个实词（kebab）。"""
    authors = meta.get("authors") or []
    fam = re.sub(r"[^A-Za-z]", "", authors[0].rsplit(" ", 1)[-1])[:20] if authors else ""
    year = str(meta.get("year") or "")
    words = []
    for w in re.split(r"[^A-Za-z0-9]+", (meta.get("title") or "").lower()):
        if w and w not in STOPWORDS and len(w) > 1:
            words.append(w)
        if len(words) >= 3:
            break
    base = "_".join([f"{fam}{year}".strip("_")] + ["-".join(words)]) or "paper"
    base = re.sub(r"[^A-Za-z0-9._-]+", "_", base).strip("._-")
    return base[:60] or "paper"


def main():
    ap = argparse.ArgumentParser(description="按 DOI / URL 下载论文 PDF（开放获取优先）")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--doi", help="论文 DOI，也接受 doi.org 链接或 doi: 前缀")
    g.add_argument("--url", help="直接的 PDF 下载链接")
    ap.add_argument("--outdir", required=True, help="输出目录；内含固定文件名 paper.pdf")
    ap.add_argument("--proxy", default=os.environ.get("PAPER_EXPLAINER_PROXY"),
                    help="直连失败时使用的代理，如 http://127.0.0.1:7890")
    ap.add_argument("--email", default=os.environ.get("UNPAYWALL_EMAIL",
                    "paper-explainer@example.org"),
                    help="Unpaywall 礼貌池用的联系邮箱")
    args = ap.parse_args()

    attempts, meta_errors = [], []
    meta = {"doi": None, "title": None, "authors": [], "journal": None,
            "year": None, "publisher": None, "citations": None,
            "abstract": None, "is_oa": None, "oa_status": None,
            "license": None, "pmcid": None}
    is_preprint = False
    data = chosen_url = chosen_source = None

    try:
        if args.url:
            data, chosen_url = fetch_pdf(args.url, args.proxy)
            if data:
                chosen_source = "direct-url"
                attempts.append({"source": chosen_source, "url": args.url,
                                 "ok": True, "note": f"{len(data)} bytes"})
            else:
                attempts.append({"source": "direct-url", "url": args.url,
                                 "ok": False, "note": "该链接未返回有效 PDF"})
        else:
            doi = normalize_doi(args.doi)
            meta["doi"] = doi
            log(f"DOI: {doi}")

            xr = {}
            try:
                xr = crossref_meta(doi, args.proxy)
                for k in ("title", "authors", "journal", "year", "publisher",
                          "citations", "abstract"):
                    if xr.get(k):
                        meta[k] = xr[k]
            except (DownloadError, requests.RequestException) as e:
                meta_errors.append(f"CrossRef 元数据获取失败: {e}")
            if not meta.get("title"):
                try:
                    dc = datacite_meta(doi, args.proxy)
                    for k in ("title", "authors", "journal", "year",
                              "publisher", "abstract"):
                        if dc.get(k) and not meta.get(k):
                            meta[k] = dc[k]
                    xr = xr or {k: dc.get(k) for k in
                                ("title", "authors", "journal", "year",
                                 "publisher", "citations", "abstract",
                                 "alternative_id") if dc.get(k)}
                except (DownloadError, requests.RequestException) as e:
                    meta_errors.append(f"DataCite 元数据获取失败: {e}")
            if not meta.get("title"):
                meta_errors.append("CrossRef/DataCite 均未查到该 DOI 的元数据，请核对 DOI 是否正确")
            log(f"标题: {meta.get('title')}")

            queue = [("arXiv-DOI", arxiv_doi_candidates(doi), False)]
            try:
                up_urls, up_meta = unpaywall_candidates(doi, args.email, args.proxy)
                meta.update({k: v for k, v in up_meta.items() if v is not None})
                queue.append(("Unpaywall", up_urls, False))
            except (DownloadError, requests.RequestException) as e:
                meta_errors.append(f"Unpaywall 查询失败: {e}")
            try:
                oa_urls, oa_meta = openalex_candidates(doi, args.proxy)
                for k, v in oa_meta.items():
                    if v is not None and not meta.get(k):
                        meta[k] = v
                queue.append(("OpenAlex", oa_urls, False))
            except (DownloadError, requests.RequestException) as e:
                meta_errors.append(f"OpenAlex 查询失败: {e}")
            try:
                ep_urls, ep_meta = europepmc_candidates(doi, args.proxy)
                meta.update({k: v for k, v in ep_meta.items() if v is not None})
                queue.append(("EuropePMC", ep_urls, False))
            except (DownloadError, requests.RequestException) as e:
                meta_errors.append(f"EuropePMC 查询失败: {e}")
            queue.append(("Publisher", publisher_candidates(doi, xr), False))
            try:
                ax_urls = arxiv_title_candidates(xr.get("title"), args.proxy)
                queue.append(("arXiv-预印本", ax_urls, True))
            except (DownloadError, requests.RequestException) as e:
                meta_errors.append(f"arXiv 检索失败: {e}")

            for source, urls, pre in queue:
                if not urls:
                    continue
                log(f"尝试 {source}（{len(urls)} 个候选链接）…")
                data, chosen_url = try_urls(urls, source, attempts, args.proxy)
                if data:
                    chosen_source, is_preprint = source, pre
                    break

    except ValueError as e:
        print(json.dumps({"status": "error", "message": str(e)}, ensure_ascii=False))
        sys.exit(2)
    except requests.RequestException as e:
        print(json.dumps({"status": "failure", "message": f"网络请求失败: {e}",
                          "attempts": attempts, "alternatives": ALTERNATIVES},
                         ensure_ascii=False))
        sys.exit(1)

    if not data:
        print(json.dumps({
            "status": "failure",
            "message": "未能获取到可下载的开放获取 PDF（所有渠道均失败）",
            "paper": {k: meta.get(k) for k in ("doi", "title", "journal", "year",
                                               "publisher", "is_oa", "oa_status")},
            "attempts": attempts,
            "meta_errors": meta_errors,
            "alternatives": ALTERNATIVES,
        }, ensure_ascii=False))
        sys.exit(1)

    try:
        root = prepare_outdir(args.outdir)
        pdf_path = safe_write_bytes(root, PDF_NAME, data)
        meta["download_source"] = chosen_source
        meta["download_url"] = chosen_url
        meta["is_preprint"] = is_preprint
        meta_path = safe_write_text(root, META_NAME,
                                    json.dumps(meta, ensure_ascii=False, indent=2))
    except ValueError as e:
        print(json.dumps({"status": "error", "message": str(e)}, ensure_ascii=False))
        sys.exit(2)

    print(json.dumps({
        "status": "ok",
        "path": str(pdf_path),
        "metadata_path": str(meta_path),
        "source": chosen_source,
        "download_url": chosen_url,
        "is_preprint": is_preprint,
        "suggested_dir": suggest_dir(meta),
        "title": meta.get("title"),
        "authors": meta.get("authors"),
        "journal": meta.get("journal"),
        "year": meta.get("year"),
        "citations": meta.get("citations"),
        "is_oa": meta.get("is_oa"),
        "attempts": attempts,
        "meta_errors": meta_errors,
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
