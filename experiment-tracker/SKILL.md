---
name: experiment-tracker
description: 实验结果登记、汇总与可追溯审计。凡涉及"记录这次实验结果"、"生成实验对比表/SUMMARY"、"这批结果哪个是哪份数据跑的"、"结果可追溯性/泄漏检查"，一律使用本技能。每个结果 JSON 自带 git commit、数据指纹、切分指纹，杜绝"数字不知从哪来"。用户跑完实验要登记、或要对比多个实验时触发。
---

# experiment-tracker：实验结果登记与审计

把"这个 60.9% 是哪份代码、哪份数据、哪个切分跑出来的"变成自动记录的问题。每个结果是一个自包含 JSON：指标 + 配置 + git commit + 数据/切分指纹 + 备注。**指标只此一处登记，结果对比表自动生成**。

## 位置与运行

- 脚本：`/home/liyonghao/.agents/skills/experiment-tracker/scripts/`
- 解释器：任意 python3（纯标准库）

## 场景 A：登记一次实验（训练/评估完成后立刻做）

```bash
python3 /home/liyonghao/.agents/skills/experiment-tracker/scripts/new_result.py \
  --results-dir ./results --name cos25_crossdate \
  --metrics '{"rank1": 0.609, "rank1_std": 0.038, "eer": 0.1758}' \
  --config '{"backbone": "BEATs_iter3", "loss": "SupCon", "split": "crossdate_5fold"}' \
  --split-file ./splits/crossdate.json --data-dir /data/wavs16k \
  --notes "五折多切分均值±std"
```

- 数据指纹 = 目录结构 stat 哈希（秒级，不读内容）；切分指纹 = 切分文件内容哈希
- git commit 与 dirty 状态自动记录

## 场景 B：汇总对比 + 审计

```bash
python3 /home/liyonghao/.agents/skills/experiment-tracker/scripts/summarize.py \
  --results-dir ./results --out SUMMARY.md --check
```

- 生成 Markdown 对比表（核心指标列自动聚合）
- `--check` 逐条审计：缺切分/数据指纹、git dirty、同名实验指标不一致（区分多种子还是复现异常）

## 为什么不用 MLflow/W&B

需要起服务/联网/装包；本技能是"零依赖文件协议"——结果就是目录里的 JSON，git 可版本化，diff 可读，任何工具（包括本技能和其他脚本）都能直接消费。大型 sweep 需要实时曲线时再上专业工具，登记与审计仍可用本技能兜底。

## 纪律（结果可信的前提）

1. 训练/评估一结束**立即登记**，不靠回忆补录；
2. 每条结果必须带 `--split-file`（至少其一），否则 `--check` 会标黄；
3. 同一实验多次跑（多种子/多折）用相同 name，把种子/折号写进 config——summarize 会把它们分组提示；
4. 结论性数字以登记的 JSON 为准，聊天记录里的数字不算数。
