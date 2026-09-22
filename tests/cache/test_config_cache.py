"""
Unit tests for the config cache miss sentinel used by
CTFd.utils._get_config / CTFd.utils.get_config.

The sentinel must be a dedicated singleton (not the KeyError class) so that
identity checks are reliable and so it can never leak to callers as a
regular config value.
"""

import copy
import pickle
import types

from tests.offline_compat import ensure_ctfd_importable

ensure_ctfd_importable()

from flask import Flask  # noqa: E402

import CTFd.utils as utils  # noqa: E402
from CTFd.cache import cache  # noqa: E402
from CTFd.utils import _CONFIG_CACHE_MISS, get_config  # noqa: E402


def _uncached(func):
    return getattr(func, "uncached", None) or func.__wrapped__


def _fake_db(rows):
    """Build a fake db object returning the given {key: value} rows."""

    class Result:
        def __init__(self, row):
            self._row = row

        def fetchone(self):
            return self._row

    class Session:
        def __init__(self):
            self.calls = []

        def execute(self, query):
            self.calls.append(query)
            # The fake Configs table ignores the query; tests key off rows
            row = rows.pop("row", None)
            return Result(row)

    return types.SimpleNamespace(session=Session())


def _config_row(value):
    return types.SimpleNamespace(value=value)


def test_sentinel_is_singleton_and_survives_pickle():
    assert pickle.loads(pickle.dumps(_CONFIG_CACHE_MISS)) is _CONFIG_CACHE_MISS
    assert copy.deepcopy(_CONFIG_CACHE_MISS) is _CONFIG_CACHE_MISS


def test_sentinel_is_not_an_exception_type():
    # The old implementation returned the KeyError class itself
    assert not isinstance(_CONFIG_CACHE_MISS, type)
    assert _CONFIG_CACHE_MISS is not KeyError


def test_get_config_returns_sentinel_for_missing_key(monkeypatch):
    monkeypatch.setattr(utils, "db", _fake_db({"row": None}))
    assert _uncached(utils._get_config)("missing_key") is _CONFIG_CACHE_MISS


def test_get_config_parses_values(monkeypatch):
    uncached = _uncached(utils._get_config)

    monkeypatch.setattr(utils, "db", _fake_db({"row": _config_row("123")}))
    assert uncached("number") == 123

    monkeypatch.setattr(utils, "db", _fake_db({"row": _config_row("true")}))
    assert uncached("flag") is True

    monkeypatch.setattr(utils, "db", _fake_db({"row": _config_row("false")}))
    assert uncached("flag") is False

    monkeypatch.setattr(utils, "db", _fake_db({"row": _config_row("ctf")}))
    assert uncached("name") == "ctf"


def test_get_config_maps_sentinel_to_default(monkeypatch):
    app = Flask("test")
    with app.app_context():
        monkeypatch.setattr(
            utils, "_get_config", lambda key: _CONFIG_CACHE_MISS
        )
        assert get_config("missing_key", default="fallback") == "fallback"
        # Keys with a DEFAULTS entry fall back to the default value
        assert get_config("ctf_name") == utils.DEFAULTS["ctf_name"]
        # Unknown keys without a default return None
        assert get_config("definitely_missing_key") is None


def test_get_config_never_leaks_sentinel(monkeypatch):
    app = Flask("test")
    with app.app_context():
        monkeypatch.setattr(
            utils, "_get_config", lambda key: _CONFIG_CACHE_MISS
        )
        for key in ("missing_key", "ctf_name", "user_mode"):
            assert get_config(key) is not _CONFIG_CACHE_MISS
            assert get_config(key, default="x") is not _CONFIG_CACHE_MISS


def test_get_config_returns_real_values(monkeypatch):
    app = Flask("test")
    with app.app_context():
        monkeypatch.setattr(utils, "_get_config", lambda key: "real_value")
        assert get_config("some_key") == "real_value"

        monkeypatch.setattr(utils, "_get_config", lambda key: False)
        assert get_config("some_key") is False


def test_get_config_preset_configs_bypass_cache(monkeypatch):
    app = Flask("test")
    app.config["PRESET_CONFIGS"] = {"preset_key": "preset_value"}
    with app.app_context():
        def fail(key):
            raise AssertionError("cache should not be consulted")

        monkeypatch.setattr(utils, "_get_config", fail)
        assert get_config("preset_key") == "preset_value"


def test_cached_sentinel_roundtrip_through_memoize(monkeypatch):
    """
    The memoized _get_config must return the identical sentinel object even
    after the value has been serialized into the cache backend.
    """
    app = Flask("test")
    # Make sure the global cache object is bound to a real backend
    cache.init_app(app, config={"CACHE_TYPE": "SimpleCache"})
    with app.app_context():
        monkeypatch.setattr(utils, "db", _fake_db({"row": None}))
        cache.delete_memoized(utils._get_config)
        first = utils._get_config("missing_key_roundtrip")
        # Second call is served from the cache backend (pickle roundtrip)
        second = utils._get_config("missing_key_roundtrip")
        assert first is _CONFIG_CACHE_MISS
        assert second is _CONFIG_CACHE_MISS
