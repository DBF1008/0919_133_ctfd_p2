#!/usr/bin/env bash
#
# Manual unit test runner for the cache-strategy fixes:
#   1. clear_standings() now warms hot standings caches after invalidation
#      (prevents a thundering herd of concurrent score queries hitting the DB)
#   2. _get_config() uses a dedicated _ConfigNotFound sentinel instead of the
#      KeyError class (prevents sentinel misidentification and bad value caching)
#   3. timed_lru_cache() expires entries individually (gradual eviction)
#      instead of flushing the whole cache at once (removes expiry spikes)
#
# Usage:
#   ./test.sh            # run all stages
#   PYTEST="python -m pytest" ./test.sh
#
set -euo pipefail
cd "$(dirname "$0")"

PYTEST="${PYTEST:-pytest}"

echo "==> [1/5] Cache fix unit tests (tests/cache)"
"$PYTEST" tests/cache -v

echo "==> [2/5] Config sentinel regression tests"
"$PYTEST" tests/test_config.py tests/api/v1/test_config.py -v

echo "==> [3/5] Scoreboard / standings regression tests"
"$PYTEST" tests/api/v1/test_scoreboard.py tests/api/v1/statistics -v

echo "==> [4/5] Utils regression tests (incl. health/timed_lru_cache consumers)"
"$PYTEST" tests/utils -v

echo "==> [5/5] Full test suite"
"$PYTEST" tests -v

echo "All test stages passed."
