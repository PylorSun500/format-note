#!/usr/bin/env bash
# renumber.sh — renumber.py 的薄包装
#
# 让调用不必关心 python3 与脚本自身的路径，直接：
#     ./tools/renumber.sh "/绝对/路径/笔记.md"
#     ./tools/renumber.sh --write "/绝对/路径/笔记.md"
#     ./tools/renumber.sh --diff  "/绝对/路径/笔记.md"
#
# 所有参数原样透传给 renumber.py（默认 dry-run，加 --write 才落盘）。

set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec python3 "${here}/renumber.py" "$@"
