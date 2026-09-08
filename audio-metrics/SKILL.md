---
name: audio-metrics
description: 声音/语音科研统一指标库。凡涉及计算或解读 EER、FAR/FRR、Rank-1、AUROC、OSCR、宏F1、混淆矩阵、WER/CER、mAP 等指标——无论是训练后评估、对比实验结果、核对论文数字还是设计评估方案——一律使用本技能，保证全项目指标口径一致，避免各脚本重复实现导致的偏差。用户提到"算一下 EER/指标""这两个模型谁好""开放集评估"等场景即触发。
---

# audio-metrics：声音科研统一指标库

原则：**指标全项目只此一份定义**。任何脚本需要算指标时导入本库，而不是复制公式。

## 位置与运行

- 库：`/home/liyonghao/.agents/skills/audio-metrics/scripts/metrics.py`（纯 numpy，零重依赖）
- CLI：`/home/liyonghao/.agents/skills/audio-metrics/scripts/compute_metrics.py`
- 运行解释器：`/home/liyonghao/anaconda3/bin/python3`（numpy 即可，无需 torch）

## 两种用法

### A. CLI（快速算一批结果）

准备任务 JSON，五种任务任选：

```json
{"task": "verification", "pos_scores": [0.8, 0.9], "neg_scores": [0.2, 0.3], "threshold": 0.5}
{"task": "classification", "y_true": ["A","B"], "y_pred": ["A","A"]}
{"task": "openset", "y_true": ["A","U"], "y_pred": ["A","A"], "scores": [0.9, 0.7], "unknown_label": "U"}
{"task": "asr", "refs": ["你好世界"], "hyps": ["你好世"]}
{"task": "sed", "y_true_multihot": [[1,0]], "y_score": [[0.9,0.2]]}
```

```bash
/home/liyonghao/anaconda3/bin/python3 \
  /home/liyonghao/.agents/skills/audio-metrics/scripts/compute_metrics.py \
  --input spec.json [--pretty]
```

输出：`{"status":"ok","task":...,"metrics":{...}}`。verification 附带 DET 曲线点；openset 附带 OSCR 曲线与 best 阈值。

### B. 导入库（训练脚本内使用）

```python
import sys
sys.path.append("/home/liyonghao/.agents/skills/audio-metrics/scripts")
import metrics
eer, thr = metrics.eer(pos_scores, neg_scores)
```

## 指标口径速查（避免解读歧义）

- **EER**：FPR=FNR 处的错误率；阈值语义 `score >= t` 判正；对完美可分数据返回 0
- **FAR/FRR**：FAR=负样本分数≥阈值的比例（误纳，"把别人认成我"）；FRR=正样本<阈值（误拒）
- **OSCR**：TPI/(TPI+FPI)——TPI=已知且预测正确且过阈值；FPI=已知但认错 + unknown 误收（Huang et al. 2025 口径）
- **AUROC**：正对得分 > 负对得分的概率（Mann-Whitney）
- **宏F1**：逐类 F1 的算术平均（类别不平衡时的默认指标）
- **mAP**：多标签 SED 逐类 AP 平均

## 选择指南

| 场景 | 用什么 |
|---|---|
| 闭集多类（13 只猴分类） | accuracy + macro_f1 + per_class + confusion_matrix |
| 声纹 verification（1:1 比对） | EER + FAR/FRR@工作阈值 + DET |
| 跨日期检索式评估 | rank_accuracy（Rank-1@k） |
| 开放集/新个体拒识 | auroc + oscr_curve（报 best 阈值与运行点） |
| ASR 转写质量 | wer / cer |
| 多标签事件检测 | sed_map（mAP + 逐类 F1@阈值） |

## 注意

- 指标只认"样本对/样本级"输入；算之前先确认没有时间泄漏（train/test 同源切分问题）——那是数据问题，指标救不了
- 小样本下指标波动大，报告请附多种子/多切分的均值 ± 标准差，不要报单次
- 输出文件固定名 `metrics_result.json`（CLI `--out`），防止覆盖其他文件
