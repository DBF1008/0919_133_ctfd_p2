"""
Unit tests for CTFd.cache.timed_lru_cache.

These tests focus on the per-entry expiry behavior: expiring one entry must
never evict unrelated entries, so that a cache expiry cannot trigger a
recomputation stampede.
"""

from tests.offline_compat import ensure_ctfd_importable

ensure_ctfd_importable()

import CTFd.cache as cache_module  # noqa: E402
from CTFd.cache import timed_lru_cache  # noqa: E402

SECOND_NS = 10**9


class FakeClock:
    def __init__(self, start=0):
        self.now = start

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += int(seconds * SECOND_NS)


def _install_clock(monkeypatch):
    clock = FakeClock()
    monkeypatch.setattr(cache_module, "monotonic_ns", clock)
    return clock


def test_caches_results():
    calls = []

    @timed_lru_cache(timeout=30)
    def func(x):
        calls.append(x)
        return x * 2

    assert func(1) == 2
    assert func(1) == 2
    assert calls == [1]
    assert func(2) == 4
    assert calls == [1, 2]

    info = func.cache_info()
    assert info.hits == 1
    assert info.misses == 2
    assert info.maxsize == 64
    assert info.currsize == 2


def test_unexpired_entries_survive_other_entries_expiry(monkeypatch):
    """
    Regression test: the old implementation cleared the WHOLE cache once the
    timeout elapsed. Entries that are still fresh must keep being served
    from cache after other entries have expired.
    """
    clock = _install_clock(monkeypatch)
    calls = []

    @timed_lru_cache(timeout=30)
    def func(x):
        calls.append(x)
        return x

    func("old")
    clock.advance(20)  # "old" expires at t=30
    func("fresh")  # "fresh" expires at t=50
    clock.advance(15)  # t=35: "old" is expired, "fresh" is still valid

    assert func("fresh") == "fresh"
    assert calls == ["old", "fresh"]  # served from cache, no recomputation

    func("old")
    assert calls == ["old", "fresh", "old"]  # only the expired entry recomputed


def test_expiry_is_progressive_not_wholesale(monkeypatch):
    """
    After an entry expires, the rest of the cache content must be left in
    place instead of being cleared.
    """
    clock = _install_clock(monkeypatch)

    @timed_lru_cache(timeout=30)
    def func(x):
        return x

    func("a")
    clock.advance(20)  # "a" expires at t=30
    func("b")
    clock.advance(15)  # t=35: "a" is expired, "b" is fresh until t=50

    func("a")  # recomputes only "a"
    info = func.cache_info()
    # The still-fresh "b" entry must survive; the old implementation would
    # have cleared the whole cache and report currsize == 1
    assert info.currsize == 2
    assert func("b") == "b"
    assert func.cache_info().hits == 1


def test_expired_entries_are_evicted_before_fresh_ones(monkeypatch):
    clock = _install_clock(monkeypatch)

    @timed_lru_cache(timeout=30, maxsize=2)
    def func(x):
        return x

    func("stale")
    clock.advance(31)  # "stale" expired
    func("x")
    func("y")  # would overflow maxsize=2 unless "stale" is evicted first

    info = func.cache_info()
    assert info.currsize == 2
    # "x" must still be cached: the expired entry was evicted, not the fresh one
    assert func("x") == "x"
    assert func.cache_info().hits == 1


def test_maxsize_lru_eviction():
    @timed_lru_cache(timeout=300, maxsize=2)
    def func(x):
        return x

    func("a")
    func("b")
    func("a")  # touch "a" so "b" becomes the least recently used
    func("c")  # evicts "b"

    info = func.cache_info()
    assert info.currsize == 2
    assert info.hits == 1
    assert info.misses == 3


def test_cache_clear_resets_entries_and_stats():
    @timed_lru_cache(timeout=30)
    def func(x):
        return x

    func(1)
    func(1)
    func.cache_clear()

    info = func.cache_info()
    assert info.hits == 0
    assert info.misses == 0
    assert info.currsize == 0


def test_typed_cache_distinguishes_types():
    calls = []

    @timed_lru_cache(timeout=30, typed=True)
    def func(x):
        calls.append(x)
        return x

    func(1)
    func(1.0)
    assert calls == [1, 1.0]


def test_zero_maxsize_disables_caching():
    calls = []

    @timed_lru_cache(timeout=30, maxsize=0)
    def func(x):
        calls.append(x)
        return x

    func(1)
    func(1)
    assert calls == [1, 1]
    assert func.cache_info().currsize == 0
