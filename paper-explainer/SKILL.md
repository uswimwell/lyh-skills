---
name: paper-explainer
description: 论文文献讲解与精读：根据 DOI / arXiv 编号 / 论文链接 / 本地 PDF 自动下载论文 PDF，并生成面向零基础初学者的中文讲解 Markdown（总体总结 + 逐张图片解析 + 所有英文术语通俗解释 + 批判性思考与延伸）。只要用户提到 DOI、"下载/讲解/解读/精读/带读这篇论文（文献/paper/文章）"、"这篇论文讲了什么"、想读懂某篇文献、读论文做笔记等场景，一律使用本技能——即使用户只发来一个 DOI 而没有明说"讲解"二字也适用。
---

# 论文文献讲解（Paper Explainer）

目标：为初学者产出一篇"自己能看懂"的论文讲解文档。流程：识别输入 → 下载 PDF → 提取文本与图 → 精读 → 按 `references/output-template.md` 写讲解 → 交付。

## 环境

- 脚本固定用 `/home/liyonghao/anaconda3/bin/python3` 运行（系统 python3 缺 PyMuPDF/requests；若报缺库则先 `pip install pymupdf requests pillow`）。
- 直连国外站点超时/失败时，给命令追加 `--proxy http://127.0.0.1:7890`（本机代理）再试一次。

## 第 0 步：识别输入

| 用户给的东西 | 处理 |
|---|---|
| DOI（`10.xxxx/xxx`、doi.org 链接、`doi:` 前缀） | 直接传给 `download_paper.py --doi` |
| arXiv 编号或链接 | 换算成 DOI `10.48550/arXiv.<编号>`，或直接用 `https://arxiv.org/pdf/<编号>` 作为 `--url` |
| 本地 PDF 路径 | 跳过下载，直接进入第 2 步 |
| 只有标题/模糊描述 | 先 WebSearch 确认论文身份（标题 + 第一作者 + 年份 → DOI），找不到就问用户 |

工作目录约定：每篇论文一个目录 `paper_notes/<第一作者姓氏><年份>_<标题关键词-kebab>/`，下载、提取、讲解文档都放这里，讲解文档固定叫 `讲解.md`。

## 第 1 步：获取全文（降级链路）

第一优先：下载 PDF。

```bash
/home/liyonghao/anaconda3/bin/python3 \
  /home/liyonghao/.agents/skills/paper-explainer/scripts/download_paper.py \
  --doi <DOI> --outdir paper_notes/<短名>
```

- 渠道顺序：arXiv → Unpaywall → OpenAlex → Europe PMC → 出版社直链 → arXiv 预印本兜底；只保存校验过 `%PDF` 魔数的真 PDF；stdout 输出 JSON 结果（含 `suggested_dir` 建议目录名），进度在 stderr。
- `status=ok`：目录名用 `suggested_dir`（第一作者+年份+标题关键词）；`is_preprint=true` 时讲解文档必须注明"以下基于预印本版本，内容可能与正式发表版有差异"。
- `status=failure`：按下面的降级链路走，不要对同一渠道无限重试（加 `--proxy http://127.0.0.1:7890` 算第 2 轮）。

**降级链路（受阻时逐级尝试；每一级都要在讲解文档的"讲解状态说明"里如实披露完整度）**：

1. **代理与直链**：加 `--proxy http://127.0.0.1:7890` 重试；用户给过直链 URL 时用 `--url`。
2. **PMC 全文救援**（论文在 PubMed Central 有开放全文时，即使 PDF 被反爬也能拿到全文+图）：
   ```bash
   /home/liyonghao/anaconda3/bin/python3 \
     /home/liyonghao/.agents/skills/paper-explainer/scripts/pmc_fetch.py \
     --doi <DOI> --outdir paper_notes/<短名>
   ```
   产出 `paper_text.txt` + `figures/`（原始文件名）+ `metadata.json`，等价于第 2 步产物，直接跳到第 3 步。
3. **预印本**：WebSearch 查 bioRxiv/arXiv/medRxiv 版本（记 `is_preprint` 并注明版本差异）。注意 WebFetch 的出口 IP 与本机 curl 不同——本机被限流（如 bioRxiv 429）时改用 WebFetch 常能成功。
4. **作者自存档**：WebSearch `"<论文标题>" PDF`，作者实验室主页/机构库的自存档是合法来源（用 `--url` 下载）；站点证书链损坏时可用 requests `verify=False` 兜底（仅限已知公共大学站点，并在状态说明中注明）。
5. **概览级**：以上全失败时，基于出版元数据与公开摘要写"概览版"讲解：明确标注信息边界、给出"拿到 PDF 后待补清单"与获取替代方案；**不转述任何没读过的正文与图表**。

**已知反爬特性**（省时间）：NCBI PMC 的 `/pdf/` 路径有 JS 下载拦截，但文章落地页不拦（可从中提取 `cdn.ncbi.nlm.nih.gov/pmc/blobs/` 图片直链——`pmc_fetch.py` 已内置）；PeerJ 与 ScienceDirect 是 Cloudflare JS 盾，脚本别硬试；r.jina.ai、scholar.archive.org 在本网络不可达；本机无浏览器后端，browser-use 不可用时直接走上述脚本链路。

## 第 2 步：提取文本与图片

```bash
/home/liyonghao/anaconda3/bin/python3 \
  /home/liyonghao/.agents/skills/paper-explainer/scripts/extract_figures.py \
  --pdf paper_notes/<短名>/paper.pdf --outdir paper_notes/<短名>
```

产出：`paper_text.txt`（带页码分隔与题注清单）、`figures/fig_NNN_pP.png`、`figures_manifest.json`（每图的页码/图号/题注/来源类型）、`paper_outline.txt`（按字号/加粗识别的章节大纲）。

读 stdout JSON 的 `warnings` 并处理：
- 提示"扫描件"→ 告知用户文本不可用，改为对关键页整页渲染（PyMuPDF 或 `pdftoppm`）后看图讲解，并在文档开头注明。
- 提示"图题注数量 > 提取图数"→ 对照 `paper_text.txt` 文末题注清单逐个核对，缺的图对该页整页渲染补齐（命名 `figures/fig_manual_pP.png`）。

注：期刊摘要框等排版文本框已自动过滤（区域内文字占比 > 50% 且几乎无位图则跳过）；PyMuPDF 1.23+ 才有 `cluster_drawings`，旧版（如 1.22.5）自动走兜底聚类，效果略差但可用。

## 第 3 步：精读

1. 若有 `paper_outline.txt`，先通读大纲把"章节 ↔ 页码"装进脑子，再按页码跳读——长文（综述等）必先读大纲，避免在全文里线性漫游。
2. 通读 `paper_text.txt`（重点：Abstract → 引言末尾的贡献列表 → Methods → Results → Discussion/Conclusion），抓出：核心工作、研究对象与任务、研究目标、主要方法、主要结论。
3. 用 Read 工具**逐张查看** `figures/` 里每张 PNG，结合 manifest 的题注与页码，回到正文该图的上下文段落理解。不许只看题注猜内容。图片资源确实拿不到时（如仅获得预印本文本），改按题注+正文描述解析并如实注明"未目视核验图面"。
4. 随手记录所有英文专业名词（术语/人名/模型名/方法名/数据集名/指标名/机构名/缩写），每个都准备一句零基础能懂的解释。

## 第 4 步：写讲解

严格按 `references/output-template.md` 的章节结构、文风与检查清单，写 `paper_notes/<短名>/讲解.md`。六条硬性要求：

1. **总体总结在前**：核心工作、研究对象与任务、研究目标、主要方法、主要结论各小节齐全。
2. **逐图解析在后**：每张图一节，用相对路径嵌入（`![图 1](figures/fig_001_p2.png)`），依次写「图片内容 / 传递的信息 / 在论文中的作用」三要素。
3. **术语解释**：每个英文专业名词**首次出现**时紧跟一句大白话解释，写法统一为「**术语（English）**：解释」；文末术语速查表汇总。判断标准是读者完全没接触过该领域——宁愿多解释，不许漏（Transformer、CNN、p 值、DNA 这类"常见"词也必须解释）。
4. **批判性思考**：3-5 个关键问题并解答（标明依据论文哪部分，推测注明"讲解者推测"）；研究可完善方向；值得学习与复刻之处。
5. **初学者语气**：先直觉后细节，多用类比，行文完整清晰，不写行话堆砌的电报体。
6. **如实**：预印本、扫描件、缺图、不确定的内容都要注明，不编造图表内容。

## 第 5 步：自查与交付

写完讲解后先跑自查脚本，**所有"硬性"问题修完才允许交付**：

```bash
/home/liyonghao/anaconda3/bin/python3 \
  /home/liyonghao/.agents/skills/paper-explainer/scripts/audit_explainer.py \
  paper_notes/<短名>/讲解.md
```

- 硬性检查：必选章节齐全（概览级自动放宽逐图解析）、图片相对路径有效、有"讲解状态说明"披露。
- 启发式提示（需人工逐条判断补齐）：正文英文词未进术语表；术语首次出现处缺 `**术语（English）**：解释` 式内联解释。

然后回复用户：讲解文档的 Markdown 链接（绝对路径）+ 3-5 句话最简总结 + 下载来源（是否预印本/降级到哪一级）+ 图片数量。下载失败时说明原因并给出替代方案。不要把讲解全文复制进对话。批量讲解时最后生成 README.md 总索引（表格：篇目/出处/完整度/入口链接 + 合读主线）。
