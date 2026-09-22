#!/usr/bin/env python
# -*- coding: utf-8 -*-

import threading
import time
from functools import wraps

from redis.exceptions import ConnectionError

from CTFd.cache import (
    clear_all_user_sessions,
    clear_standings,
    clear_user_session,
    timed_lru_cache,
)
from CTFd.config import TestingConfig
from CTFd.models import Users
from CTFd.utils import _ConfigNotFound, _get_config, get_config, set_config
from CTFd.utils.security.auth import login_user
from CTFd.utils.user import get_current_user, is_admin
from tests.helpers import (
    create_ctfd,
    destroy_ctfd,
    gen_challenge,
    gen_solve,
    register_user,
)


def test_clear_user_session():
    app = create_ctfd()
    with app.app_context():
        register_user(app)

        # Users by default should have a non-admin type
        user = Users.query.filter_by(id=2).first()
        with app.test_request_context("/"):
            login_user(user)
            user = get_current_user()
            assert user.id == 2
            assert user.type == "user"
            assert is_admin() is False

            # Set the user's updated type
            user = Users.query.filter_by(id=2).first()
            user.type = "admin"
            app.db.session.commit()

            # Should still return False because this is still cached
            assert is_admin() is False

            clear_user_session(user_id=2)

            # Should now return True after clearing cache
            assert is_admin() is True
    destroy_ctfd(app)


def test_clear_all_user_sessions():
    app = create_ctfd()
    with app.app_context():
        register_user(app)

        # Users by default should have a non-admin type
        user = Users.query.filter_by(id=2).first()
        with app.test_request_context("/"):
            login_user(user)
            user = get_current_user()
            assert user.id == 2
            assert user.type == "user"
            assert is_admin() is False

            # Set the user's updated type
            user = Users.query.filter_by(id=2).first()
            user.type = "admin"
            app.db.session.commit()

            # Should still return False because this is still cached
            assert is_admin() is False

            clear_all_user_sessions()

            # Should now return True after clearing cache
            assert is_admin() is True
    destroy_ctfd(app)


def test_cache_subclass_commands():
    app = create_ctfd()
    with app.app_context():
        from CTFd.cache import cache

        cache.inc("testing_inc")
        resp = cache.inc("testing_inc")
        assert resp == 2
        assert cache.get("testing_inc") == 2
        cache.expire("testing_inc", 0)
        assert cache.get("testing_inc") is None
        resp = cache.inc("testing_inc")
        assert resp == 1
    destroy_ctfd(app)


def test_redis_cache_subclass_commands():
    class RedisConfig(TestingConfig):
        REDIS_URL = "redis://localhost:6379/1"
        CACHE_REDIS_URL = "redis://localhost:6379/1"
        CACHE_TYPE = "redis"

    try:
        app = create_ctfd(config=RedisConfig)
    except ConnectionError:
        print("Failed to connect to redis. Skipping test.")
    else:
        with app.app_context():
            from CTFd.cache import cache

            cache.inc("testing_inc")
            resp = cache.inc("testing_inc")
            assert resp == 2
            assert cache.get("testing_inc") == 2
            cache.expire("testing_inc", 0)
            assert cache.get("testing_inc") is None
            resp = cache.inc("testing_inc")
            assert resp == 1
        destroy_ctfd(app)


def test_timed_lru_cache_gradual_expiry():
    """timed_lru_cache should expire entries individually instead of clearing the whole cache"""
    calls = []

    @timed_lru_cache(timeout=1, maxsize=4)
    def fn(x):
        calls.append(x)
        return x * 2

    assert fn(1) == 2
    assert fn(1) == 2
    assert calls == [1]

    fn(2)
    time.sleep(1.2)

    # Adding a new entry after the timeout must not flush unrelated entries
    assert fn(3) == 6
    # The entry for 1 expired and is recomputed individually
    assert fn(1) == 2
    assert calls == [1, 2, 3, 1]
    # The entry for 3 is still fresh: no whole-cache clear happened
    assert fn(3) == 6
    assert calls == [1, 2, 3, 1]

    info = fn.cache_info()
    assert info.maxsize == 4
    assert info.currsize == 3

    fn.cache_clear()
    assert fn.cache_info().currsize == 0
    fn(1)
    assert calls == [1, 2, 3, 1, 1]


def test_timed_lru_cache_maxsize_eviction():
    """timed_lru_cache should evict the least recently used entry beyond maxsize"""
    calls = []

    @timed_lru_cache(timeout=300, maxsize=2)
    def fn(x):
        calls.append(x)
        return x

    fn(1)
    fn(2)
    fn(1)  # touch 1 so that 2 becomes the least recently used entry
    fn(3)  # evicts 2
    fn(2)  # 2 was evicted and must be recomputed
    assert calls == [1, 2, 3, 2]


def test_timed_lru_cache_no_dogpile():
    """Concurrent callers for the same key must not duplicate computation"""
    calls = []

    @timed_lru_cache(timeout=30, maxsize=8)
    def fn(x):
        calls.append(x)
        time.sleep(0.2)
        return x * 2

    threads = [threading.Thread(target=fn, args=(1,)) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert calls == [1]


def test_get_config_missing_sentinel():
    """Missing configs must use a dedicated sentinel that never leaks to callers"""
    app = create_ctfd()
    with app.app_context():
        sentinel = _get_config("missing_sentinel_test_key")
        assert sentinel is _ConfigNotFound
        assert sentinel is not KeyError

        # The sentinel must survive a cache roundtrip with identity intact
        assert _get_config("missing_sentinel_test_key") is _ConfigNotFound

        # Callers must receive defaults, never the sentinel itself
        assert get_config("missing_sentinel_test_key") is None
        assert get_config("missing_sentinel_test_key", default="fallback") == "fallback"

        set_config("missing_sentinel_test_key", "real_value")
        assert get_config("missing_sentinel_test_key") == "real_value"
    destroy_ctfd(app)


def test_clear_standings_warms_cache():
    """clear_standings should recompute hot standings caches to avoid a thundering herd"""
    import CTFd.utils.scores as scores_module

    app = create_ctfd()
    with app.app_context():
        register_user(app)
        user = Users.query.filter_by(id=2).first()
        chal = gen_challenge(app.db)
        gen_solve(app.db, user_id=user.id, challenge_id=chal.id)

        recomputes = []
        orig_get_standings = scores_module.get_standings

        @wraps(orig_get_standings)
        def spy_get_standings(*args, **kwargs):
            recomputes.append((args, kwargs))
            return orig_get_standings(*args, **kwargs)

        scores_module.get_standings = spy_get_standings
        try:
            clear_standings()
        finally:
            scores_module.get_standings = orig_get_standings

        # The default public standings must be recomputed (warmed) during clear
        assert recomputes, "clear_standings should warm the standings cache"
    destroy_ctfd(app)
