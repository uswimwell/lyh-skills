#!/usr/bin/env bash
# 把本仓库的技能复制到各 AI 工具的技能目录（软链接不被支持时的兜底）。
# 用法：
#   bash sync-skills.sh                     # 同步到默认目录（~/.agents/skills 与 ~/.codex/skills）
#   bash sync-skills.sh /path/to/skills_dir # 同步到指定目录
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TARGETS=("$@")
if [ ${#TARGETS[@]} -eq 0 ]; then
  TARGETS=("$HOME/.agents/skills" "$HOME/.codex/skills")
fi

for skill in "$REPO_DIR"/*/; do
  name="$(basename "$skill")"
  [ "$name" = ".git" ] && continue
  for target in "${TARGETS[@]}"; do
    mkdir -p "$target"
    dest="$target/$name"
    # 若已是软链接则直接替换；若是真实目录则先备份到 <目录>.bak.<时间戳>
    if [ -L "$dest" ]; then
      rm "$dest"
    elif [ -d "$dest" ]; then
      mv "$dest" "$dest.bak.$(date +%Y%m%d%H%M%S)"
    fi
    cp -r "$REPO_DIR/$name" "$dest"
    echo "已同步 $name -> $dest"
  done
done
echo "完成。注意：复制方式不随仓库更新自动同步，更新后需重跑本脚本。"
