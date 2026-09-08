#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""audio-metrics 统一指标库（可导入，也可被 compute_metrics.py 调用）。

设计原则：
  - 只依赖 numpy，训练环境/分析环境都能跑；
  - 一处实现、处处同口径：EER/OSCR/宏F1 等指标全项目只此一份定义；
  - 全部输入为普通 list/numpy 数组，输出为原生 dict/float，便于 JSON 化。

指标族：
  1. 闭集分类：accuracy / 宏F1 / 每类P-R-F1 / 混淆矩阵
  2. 校验型（1:1 比对，如声纹 verification）：EER / 任意阈值的 FAR-FRR / DET 曲线
  3. 检索型（排名，如 Rank-1@k）：rank-k 准确率
  4. 开放集：AUROC / OSCR 曲线与摘要（Huang et al. 2025 口径）
  5. 语音识别：WER / CER（编辑距离）
  6. 声音事件检测：逐类 AP 与 mAP（多热标签 + 分数矩阵）
"""

from __future__ import annotations

import numpy as np


def _to_np(x, dtype=float):
    return np.asarray(x, dtype=dtype)


# ================= 1. 闭集分类 =================

def accuracy(y_true, y_pred) -> float:
    y_true, y_pred = _to_np(y_true, str), _to_np(y_pred, str)
    return float(np.mean(y_true == y_pred))


def confusion_matrix(y_true, y_pred, labels=None):
    """返回 (混淆矩阵 rows=真实 cols=预测, labels)。"""
    y_true, y_pred = _to_np(y_true, str), _to_np(y_pred, str)
    if labels is None:
        labels = sorted(set(y_true) | set(y_pred))
    lab = {l: i for i, l in enumerate(labels)}
    cm = np.zeros((len(labels), len(labels)), dtype=int)
    for t, p in zip(y_true, y_pred):
        cm[lab[t], lab[p]] += 1
    return cm, list(labels)


def prf_per_class(y_true, y_pred, labels=None):
    """每类 precision / recall / f1 / 支持数。"""
    cm, labels = confusion_matrix(y_true, y_pred, labels)
    out = {}
    for i, l in enumerate(labels):
        tp = cm[i, i]
        fp = cm[:, i].sum() - tp
        fn = cm[i, :].sum() - tp
        prec = tp / (tp + fp) if (tp + fp) else 0.0
        rec = tp / (tp + fn) if (tp + fn) else 0.0
        f1 = 2 * prec * rec / (prec + rec) if (prec + rec) else 0.0
        out[l] = {"precision": round(prec, 6), "recall": round(rec, 6),
                  "f1": round(f1, 6), "support": int(cm[i, :].sum())}
    return out


def macro_f1(y_true, y_pred, labels=None) -> float:
    per = prf_per_class(y_true, y_pred, labels)
    return round(float(np.mean([v["f1"] for v in per.values()])), 6)


def classification_report(y_true, y_pred, labels=None) -> dict:
    return {
        "accuracy": round(accuracy(y_true, y_pred), 6),
        "macro_f1": macro_f1(y_true, y_pred, labels),
        "per_class": prf_per_class(y_true, y_pred, labels),
        "confusion_matrix": _cm_json(confusion_matrix(y_true, y_pred, labels)),
    }


def _cm_json(cm_labels):
    cm, labels = cm_labels
    return {"labels": labels, "matrix": cm.tolist()}


# ================= 2. 校验型（verification） =================

def _fpr_fnr_at(pos, neg, thresholds):
    """各阈值下的 FPR（负样本误纳）与 FNR（正样本误拒）。阈值语义：score >= t 判为正。"""
    pos_s = np.sort(_to_np(pos))
    neg_s = np.sort(_to_np(neg))
    idx_p = np.searchsorted(pos_s, thresholds, side="left")
    idx_n = np.searchsorted(neg_s, thresholds, side="left")
    fnr = idx_p / max(len(pos_s), 1)          # pos < t → 误拒
    fpr = 1.0 - idx_n / max(len(neg_s), 1)    # neg >= t → 误纳
    return fpr, fnr


def eer(pos_scores, neg_scores):
    """等错误率。返回 (eer, eer_threshold)。

    pos_scores：同类/同个体对的相似度分数；neg_scores：异类对分数。
    """
    pos = _to_np(pos_scores)
    neg = _to_np(neg_scores)
    if len(pos) == 0 or len(neg) == 0:
        raise ValueError("pos_scores / neg_scores 不能为空")
    lo = min(pos.min(), neg.min()) - 1.0
    hi = max(pos.max(), neg.max()) + 1.0
    # 粗扫 + 相邻两点线性插值
    thresholds = np.unique(np.concatenate([pos, neg, np.linspace(lo, hi, 2001)]))
    fpr, fnr = _fpr_fnr_at(pos, neg, thresholds)
    diff = fpr - fnr
    i = int(np.argmin(np.abs(diff)))
    if 0 < i < len(thresholds) - 1 and diff[i - 1] * diff[i] <= 0:
        # 在 fpr-fnr 变号的两点间线性插值求交点
        t0, t1 = thresholds[i - 1], thresholds[i]
        d0, d1 = diff[i - 1], diff[i]
        t = t0 - d0 * (t1 - t0) / (d1 - d0) if d1 != d0 else t0
        fpr, fnr = _fpr_fnr_at(pos, neg, np.array([t]))
        return round(float((fpr[0] + fnr[0]) / 2), 6), round(float(t), 6)
    return round(float((fpr[i] + fnr[i]) / 2), 6), round(float(thresholds[i]), 6)


def far_frr_at(pos_scores, neg_scores, threshold):
    fpr, fnr = _fpr_fnr_at(pos_scores, neg_scores, np.array([float(threshold)]))
    return {"far": round(float(fpr[0]), 6), "frr": round(float(fnr[0]), 6),
            "threshold": float(threshold)}


def det_points(pos_scores, neg_scores, n=40):
    """DET 曲线采样点 [(FPR, FRR)]，供画 DET 图。"""
    pos, neg = _to_np(pos_scores), _to_np(neg_scores)
    qs = np.linspace(0.001, 0.999, n)
    thresholds = np.unique(np.quantile(np.concatenate([pos, neg]), qs))
    fpr, fnr = _fpr_fnr_at(pos, neg, thresholds)
    pairs, seen = [], set()
    for a, b in zip(fpr, fnr):
        key = (round(float(a), 6), round(float(b), 6))
        if key not in seen:
            seen.add(key)
            pairs.append(list(key))
    return pairs


# ================= 3. 检索型（ranking） =================

def rank_accuracy(ranks, k=(1, 5)):
    """ranks：每个查询的"正确项排名"（1-based）。返回 {Rank-k: acc}。"""
    r = _to_np(ranks, int)
    return {f"rank{k0}": round(float(np.mean(r <= k0)), 6) for k0 in k}


# ================= 4. 开放集 =================

def auroc(pos_scores, neg_scores) -> float:
    """AUROC（正例得分高于负例的概率），等价于 Mann-Whitney U 统计量。"""
    pos, neg = _to_np(pos_scores), _to_np(neg_scores)
    if len(pos) == 0 or len(neg) == 0:
        raise ValueError("pos_scores / neg_scores 不能为空")
    order = np.argsort(np.concatenate([pos, neg]), kind="mergesort")
    ranks = np.empty(len(order), dtype=float)
    ranks[order] = np.arange(1, len(order) + 1)
    # 并列分数取平均秩
    allv = np.concatenate([pos, neg])
    for v in np.unique(allv):
        m = allv == v
        if m.sum() > 1:
            ranks[m] = ranks[m].mean()
    r_pos = ranks[:len(pos)].sum()
    u = r_pos - len(pos) * (len(pos) + 1) / 2
    return round(float(u / (len(pos) * len(neg))), 6)


def oscr_curve(y_true, y_pred, scores, unknown_label="unknown", n_points=100):
    """开放集分类率曲线（OSCR，Huang et al. 2025 口径）。

    输入：y_true（unknown 样本标记为 unknown_label）、y_pred、scores（模型的
    最大置信度）。阈值 t 语义：score >= t 才接受预测。
      TPI(t) = 已知样本中 pred==true 且 score>=t
      FPI(t) = 已知样本中 pred!=true 且 score>=t + unknown 样本 score>=t
      OSCR(t) = TPI / (TPI + FPI)
    返回 dict：thresholds / tpi_rate / fpi_rate / oscr，及摘要。
    """
    yt = _to_np(y_true, str)
    yp = _to_np(y_pred, str)
    sc = _to_np(scores)
    known = yt != unknown_label
    n_known, n_unknown = int(known.sum()), int((~known).sum())
    if n_known == 0:
        raise ValueError("没有已知类样本")
    thresholds = np.linspace(sc.min(), sc.max() + 1e-9, n_points)
    tpi_r, fpi_r, oscr = [], [], []
    for t in thresholds:
        acc = sc >= t
        tpi = int(np.sum(known & acc & (yp == yt)))
        fpi = int(np.sum(known & acc & (yp != yt))) + int(np.sum((~known) & acc))
        tpi_r.append(tpi / n_known)
        fpi_r.append((fpi / (n_known + n_unknown)) if (n_known + n_unknown) else 0.0)
        oscr.append(tpi / (tpi + fpi) if (tpi + fpi) else 0.0)
    summary = {
        "best_oscr": round(float(np.max(oscr)), 6),
        "best_threshold": round(float(thresholds[int(np.argmax(oscr))]), 6),
        "n_known": n_known, "n_unknown": n_unknown,
    }
    return {"thresholds": [round(float(v), 6) for v in thresholds],
            "tpi_rate": [round(float(v), 6) for v in tpi_r],
            "fpi_rate": [round(float(v), 6) for v in fpi_r],
            "oscr": [round(float(v), 6) for v in oscr],
            "summary": summary}


# ================= 5. 语音识别 =================

def _edit_distance(a: str, b: str) -> int:
    dp = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        prev = dp[0]
        dp[0] = i
        for j, cb in enumerate(b, 1):
            cur = dp[j]
            dp[j] = min(dp[j] + 1, dp[j - 1] + 1, prev + (ca != cb))
            prev = cur
    return dp[len(b)]


def _lev_tokens(s: str, level: str):
    return list(s) if level == "cer" else s.split()


def wer_cer(refs, hyps, level="wer") -> dict:
    """refs/hyps：等长的文本列表。WER 按词、CER 按字。"""
    if len(refs) != len(hyps):
        raise ValueError("refs 与 hyps 数量不一致")
    total_e = total_n = 0
    for r, h in zip(refs, hyps):
        rt, ht = _lev_tokens(str(r).strip(), level), _lev_tokens(str(h).strip(), level)
        total_e += _edit_distance(rt, ht)
        total_n += len(rt)
    rate = total_e / total_n if total_n else 0.0
    return {(level.upper() if level == "wer" else "CER"): round(rate, 6),
            "errors": total_e, "total_tokens": total_n}


def wer(refs, hyps) -> dict:
    return wer_cer(refs, hyps, "wer")


def cer(refs, hyps) -> dict:
    return wer_cer(refs, hyps, "cer")


# ================= 6. 声音事件检测 =================

def average_precision(y_true_bin, y_score) -> float:
    """单类 AP（排序积分法）。y_true_bin：0/1，y_score：分数。"""
    y = _to_np(y_true_bin, int)
    s = _to_np(y_score)
    if y.sum() == 0:
        return float("nan")
    order = np.argsort(-s, kind="mergesort")
    y = y[order]
    tp = np.cumsum(y)
    prec = tp / (np.arange(len(y)) + 1)
    rec = tp / y.sum()
    ap, prev_rec = 0.0, 0.0
    for p, r in zip(prec, rec):
        if r > prev_rec:
            ap += p * (r - prev_rec)
            prev_rec = r
    return round(float(ap), 6)


def sed_map(y_true_multihot, y_score, classes=None, threshold=0.5) -> dict:
    """多标签 SED：逐类 AP → mAP；另报阈值化后的逐类 F1。"""
    Y = _to_np(y_true_multihot, int)
    S = _to_np(y_score)
    if Y.shape != S.shape:
        raise ValueError("y_true_multihot 与 y_score 形状不一致")
    classes = classes or [f"class_{i}" for i in range(Y.shape[1])]
    aps, per = {}, {}
    for j, c in enumerate(classes):
        ap = average_precision(Y[:, j], S[:, j])
        aps[c] = ap
        pred = S[:, j] >= threshold
        tp = int(np.sum((Y[:, j] == 1) & pred))
        fp = int(np.sum((Y[:, j] == 0) & pred))
        fn = int(np.sum((Y[:, j] == 1) & ~pred))
        f1 = 2 * tp / (2 * tp + fp + fn) if (2 * tp + fp + fn) else 0.0
        per[c] = {"ap": ap, "f1@%.2f" % threshold: round(f1, 6)}
    valid = [v for v in aps.values() if not np.isnan(v)]
    return {"mAP": round(float(np.mean(valid)), 6) if valid else float("nan"),
            "per_class": per}
