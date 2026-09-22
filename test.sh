#!/usr/bin/env bash
#
# 缓存策略修复的单元测试脚本
#
# 覆盖以下三个修复点:
#   1. clear_standings 缓存清空后的预热 (tests/cache/test_clear_standings.py)
#   2. _get_config 缓存未命中 sentinel 值 (tests/cache/test_config_cache.py)
#   3. timed_lru_cache 逐条过期而非整体清空 (tests/cache/test_timed_lru_cache.py)
#
# 用法:
#   ./test.sh                      # 使用默认 python3
#   PYTHON_BIN=/path/to/python ./test.sh
#
set -euo pipefail

cd "$(dirname "$0")"

PYTHON_BIN="${PYTHON_BIN:-python3}"

exec "$PYTHON_BIN" -m pytest -v \
    tests/cache/test_timed_lru_cache.py \
    tests/cache/test_clear_standings.py \
    tests/cache/test_config_cache.py \
    "$@"
