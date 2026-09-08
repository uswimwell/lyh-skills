# lyh-skills：AI 助手技能合集（SKILL.md 开放格式）

本仓库是**唯一事实源**：所有 AI 助手技能都在这里维护，各工具目录通过软链接（或同步脚本）指向本仓库。改进后 `git push`，其他机器/其他 AI `git pull` 即同步。

## 技能清单

| 技能 | 功能 | 触发示例 |
|---|---|---|
| **paper-explainer** | 根据 DOI/arXiv/链接/本地 PDF 下载论文并生成面向零基础初学者的中文讲解（总体总结+逐图解析+术语解释+批判性思考），含 PMC 救援等反爬降级链路 | "帮我讲解 10.1038/s41586-021-03819-2" |
| **audio-metrics** | 声音科研统一指标库（EER/FAR-FRR/AUROC/OSCR/宏F1/Rank-1/WER/CER/mAP） | "算一下这两个模型的 EER" |
| **audio-io-toolkit** | 音频批量体检（采样率/时长/静音/削波）与转换（重采样/单声道/裁剪），支持 WAV 与无头 PCM | "检查这批录音有没有坏文件" |
| **audio-features** | 传统声学参数批量提取（F0/峰频/带宽/谱质心/MFCC→CSV） | "给这批叫声提取 F0 和 MFCC" |
| **experiment-tracker** | 实验结果指纹化登记 + 汇总对比表 + 可追溯性/泄漏审计 | "登记这次实验结果并出对比表" |

## 安装（三种方式，按你的 AI 工具能力选择）

### 方式 1：原生支持 Agent Skills 的工具（ZCode / Claude Code 等）

```bash
git clone https://github.com/uswimwell/lyh-skills.git ~/skills-repo
ln -s ~/skills-repo/<技能名> ~/.agents/skills/<技能名>     # ZCode
# Claude Code 视版本用 ~/.claude/skills/，同理软链或复制
```

装好后**无需任何说明**：直接说需求（如"帮我讲解 <DOI>"），AI 会自动触发对应技能。

### 方式 2：不认软链接的工具

```bash
bash sync-skills.sh ~/.codex/skills   # 把仓库技能复制到目标技能目录
```

之后重新打开会话即可。注意：复制方式不会随仓库更新自动同步，更新后需重跑。

### 方式 3：任何对话式 AI（无技能机制）

直接在对话里给一句引导语 + 指向 SKILL.md 的内容/路径：

```
请阅读并严格遵循以下技能文件中的工作流来完成我接下来的任务：
<paste SKILL.md 全文，或给出可读的文件路径>
任务：<你的需求，如"讲解这篇论文 10.7717/peerj.20655">
```

SKILL.md 是自包含的（含触发条件、步骤、脚本用法），任何能读文件/接受长提示的 AI 都能照做。

## 改进同步流程（让所有 AI 保持一致）

1. 在任何一处改进技能 → `cd ~/skills-repo && git add -A && git commit -m "..." && git push`
2. 其他机器/其他 AI 会话使用前：`git pull`
3. 让 AI 了解外部改进：`git pull && git diff HEAD@{1} HEAD`（把 diff 给 AI 看即可）

## 约定

- 每个技能一个目录，目录名 = SKILL.md frontmatter 里的 name，必须一致
- SKILL.md 的 description 写"什么时候触发"，正文写"怎么做"；脚本放 `scripts/`
- 本仓库为私有：技能内含个人环境路径（解释器、NFS 等），勿公开
