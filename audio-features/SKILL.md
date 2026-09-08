---
name: audio-features
description: 传统声学参数批量提取。凡涉及"提取 MFCC/F0 基频/峰频/带宽/谱质心/时长等传统声学特征"、"给叫声/语音算声学参数"、"深度特征之外要经典声学指标做对照"（如动物声纹论文里的 25 参数表、语音基线特征），一律使用本技能。用户要求分析音频的声学特性、或为统计建模（GLMM/判别分析）准备特征表时触发。
---

# audio-features：传统声学参数批量提取

深度嵌入之外的"经典"声学特征管线——生物声学论文的标准参数表（时长、F0、峰频、带宽…）与语音基线特征（MFCC、谱质心、过零率）一套搞定。**只依赖 numpy/scipy**（mel 滤波与 DCT 内置实现），无需 librosa/torchaudio。

## 位置与运行

```bash
/home/liyonghao/anaconda3/bin/python3 \
  /home/liyonghao/.agents/skills/audio-features/scripts/extract_features.py \
  --input /data/wavs --outdir ./feat \
  --features duration,peak_freq,bandwidth,spectral,f0,mfcc
```

- 产出：`acoustic_features.csv`（每文件一行）+ `acoustic_features_summary.json`
- 输入目录建议先用 audio-io-toolkit 体检并统一为 16kHz 单声道 WAV；原始 .pcm 直接可用（加 `--pcm-sr 16000`）

## 可选特征集

| 特征名 | 内容 | 典型用途 |
|---|---|---|
| duration | 时长 + RMS 能量(dB) | 叫声描述表第一行永远是它 |
| f0 | 基频均值/中位数/范围（自相关法，50–2000Hz） | 音高相关研究、性别/年龄差异 |
| peak_freq | 能量主频中位数 | 啮齿类超声、鸟叫峰频比较 |
| bandwidth | 90% 能量带宽 | 叫声"宽窄"描述 |
| spectral | 谱质心、85% 谱滚降 | 音色明亮度、栖息地声学适应假说 |
| mfcc | 前 13 维 MFCC 均值 | 经典基线分类特征 |
| zcr | 过零率 | 粗糙度/噪声性度量 |

## 与深度特征的关系

本技能产出的是**可解释的传统参数**，用途：(1) 论文里的声学参数描述表（直接可进 supplementary）；(2) 喂给统计模型（DFA/GLMM）做传统路线对照；(3) 与 BEATs/WavLM 深度嵌入对比，论证深度特征的增益。参数口径参考 Fan et al. 2022（白头叶猴 25 参数体系）等文献时，可在 `--features` 组合基础上按论文定义补充定制。

## 注意

- F0 用自相关法，适合鸣叫/语音类周期信号；对噪声性信号（蝙蝠回声定位、击打声）会返回空，属预期行为
- 参数默认帧长 40ms；对超短音（<100ms）结果会偏粗，必要时改脚本帧参数
- 大文件保护：>300MB 的 WAV 会拒绝读取（先用 audio-io-toolkit 裁剪）
