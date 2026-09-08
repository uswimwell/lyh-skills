---
name: audio-io-toolkit
description: 音频数据批量体检与转换工具。凡涉及"检查/审计这批音频"（采样率、声道、时长、静音、削波、异常文件）、"批量重采样/转单声道/裁剪/归一化"、"数据集入库前的质检"，一律使用本技能。用户给出一个音频目录让你了解数据情况、或要求数据清洗预处理时触发。
---

# audio-io-toolkit：音频批量体检与转换

解决"拿到一批录音，先搞清楚里面有什么、有什么坑"的问题。输出结构化报告，可疑文件（削波/高静音/过短过长/全零）自动标记。

## 位置与运行

- 脚本：`/home/liyonghao/.agents/skills/audio-io-toolkit/scripts/`
- 解释器：`/home/liyonghao/anaconda3/bin/python3`（依赖 numpy+scipy）
- 格式支持：WAV 全支持；**无头 PCM 原生支持**（加 `--pcm-sr 16000 [--pcm-dtype int16]`，白头叶猴原始录音即此格式）；mp3/flac/ogg 需 `pip install soundfile`（当前环境未装，遇到会明确报错而不是静默跳过）

## 场景 A：批量体检（拿到新数据第一件事）

```bash
/home/liyonghao/anaconda3/bin/python3 \
  /home/liyonghao/.agents/skills/audio-io-toolkit/scripts/audio_audit.py \
  --input /data/wavs --outdir ./audit_report [--light] [--min-sec 0.2]
```

- 产出 `audio_audit.csv`（逐文件：采样率/声道/时长/峰值/削波比/静音比）+ `audio_audit.json`（采样率分布、时长分位数、可疑清单）
- 可疑判定默认阈值：削波>1%、静音>80%、时长<0.2s 或 >120s，均可用参数调
- 大库快扫：加 `--light` 只读文件头（不读数据，秒级出报告）；默认对 >300MB 的文件跳过质量指标
- 之后把可疑清单里的文件抽听/剔除，再进入特征提取或训练

## 场景 B：批量转换（重采样/单声道/裁剪/归一化）

```bash
/home/liyonghao/anaconda3/bin/python3 \
  /home/liyonghao/.agents/skills/audio-io-toolkit/scripts/audio_convert.py \
  --input /data/raw --outdir /data/wav16k --sr 16000 --mono \
  [--trim 0-30] [--normalize] [--pattern "A_*.wav"]
```

- 输出 16-bit PCM WAV，文件名带参数后缀（`name_16000hz_mono.wav`）不覆盖原始数据
- **outdir 必须与 input 不同目录**（脚本强制，防止覆盖原始录音）
- 重采样用 polyphase 滤波（scipy.resample_poly），抗混叠优于 FFT 截断

## 典型工作流

新数据 → `audio_audit.py --light` 快扫 → 对可疑子集全量体检 → `audio_convert.py` 统一到 16kHz 单声道 → 再进入训练或特征提取。
