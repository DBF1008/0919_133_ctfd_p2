"""
Unit tests for CTFd.cache.clear_standings cache warming.

Clearing the standings cache must be followed by a best-effort warm-up of
the hottest standings caches so that a burst of concurrent requests after
a clear does not stampede the database.
"""

import sys
import types

import pytest

from tests.offline_compat import ensure_ctfd_importable

ensure_ctfd_importable()

import CTFd.cache as cache_module  # noqa: E402


def _recording_func(events, name, result=None, exc=None):
    def _func(*args, **kwargs):
        events.append(("call", name))
        if exc is not None:
            raise exc
        return result

    _func.__name__ = name
    return _func


@pytest.fixture
def standings_env(monkeypatch):
    """
    Replace the cache object and every module that clear_standings touches
    with recording fakes so the clear/warm sequence can be observed without
    a database.
    """
    events = []

    fake_cache = types.SimpleNamespace(
        delete_memoized=lambda f, *a, **k: events.append(
            ("delete_memoized", getattr(f, "__name__", str(f)))
        ),
        delete=lambda key: events.append(("delete", key)),
    )
    monkeypatch.setattr(cache_module, "cache", fake_cache)

    scores = types.ModuleType("CTFd.utils.scores")
    scores.get_standings = _recording_func(events, "get_standings", result=[])
    scores.get_team_standings = _recording_func(events, "get_team_standings")
    scores.get_user_standings = _recording_func(events, "get_user_standings")

    scoreboard = types.ModuleType("CTFd.utils.scoreboard")
    scoreboard.get_scoreboard_detail = _recording_func(
        events, "get_scoreboard_detail"
    )

    user = types.ModuleType("CTFd.utils.user")
    for name in (
        "get_team_place",
        "get_team_schema",
        "get_team_score",
        "get_user_place",
        "get_user_schema",
        "get_user_score",
    ):
        setattr(user, name, _recording_func(events, name))

    modes = types.ModuleType("CTFd.utils.modes")
    models = types.ModuleType("CTFd.models")

    class Teams:
        get_score = staticmethod(_recording_func(events, "Teams.get_score"))
        get_place = staticmethod(_recording_func(events, "Teams.get_place"))

    class Users:
        get_score = staticmethod(_recording_func(events, "Users.get_score"))
        get_place = staticmethod(_recording_func(events, "Users.get_place"))

    models.Teams = Teams
    models.Users = Users
    modes.get_model = lambda: models.Users

    api = types.ModuleType("CTFd.api")
    api.api = types.SimpleNamespace(name="api")
    api_v1 = types.ModuleType("CTFd.api.v1")
    api_scoreboard = types.ModuleType("CTFd.api.v1.scoreboard")
    api_scoreboard.ScoreboardList = types.SimpleNamespace(
        endpoint="scoreboard_list",
        get=_recording_func(events, "ScoreboardList.get"),
    )
    api_scoreboard.ScoreboardDetail = types.SimpleNamespace(
        endpoint="scoreboard_detail",
        get=_recording_func(events, "ScoreboardDetail.get"),
    )

    monkeypatch.setitem(sys.modules, "CTFd.api", api)
    monkeypatch.setitem(sys.modules, "CTFd.api.v1", api_v1)
    monkeypatch.setitem(sys.modules, "CTFd.api.v1.scoreboard", api_scoreboard)
    monkeypatch.setitem(sys.modules, "CTFd.models", models)
    monkeypatch.setitem(sys.modules, "CTFd.utils.modes", modes)
    monkeypatch.setitem(sys.modules, "CTFd.utils.scoreboard", scoreboard)
    monkeypatch.setitem(sys.modules, "CTFd.utils.scores", scores)
    monkeypatch.setitem(sys.modules, "CTFd.utils.user", user)

    return types.SimpleNamespace(
        events=events, scores=scores, modes=modes, models=models
    )


def _called(events, name):
    return ("call", name) in events


def _deleted(events, name):
    return ("delete_memoized", name) in events


def test_clear_standings_clears_and_warms_users_mode(standings_env):
    events = standings_env.events

    cache_module.clear_standings()

    # The bulk standings caches must be cleared
    assert _deleted(events, "get_standings")
    assert _deleted(events, "get_team_standings")
    assert _deleted(events, "get_user_standings")
    assert _deleted(events, "get_scoreboard_detail")

    # The hot standings cache must be re-warmed after the clear
    assert _called(events, "get_standings")
    last_clear = max(
        i for i, event in enumerate(events) if event[0] in ("delete_memoized", "delete")
    )
    warm = events.index(("call", "get_standings"))
    assert warm > last_clear

    # users mode: user standings are not needed by the scoreboard endpoints
    assert not _called(events, "get_user_standings")


def test_clear_standings_warms_user_standings_in_teams_mode(standings_env):
    standings_env.modes.get_model = lambda: standings_env.models.Teams
    events = standings_env.events

    cache_module.clear_standings()

    assert _called(events, "get_standings")
    assert _called(events, "get_user_standings")


def test_clear_standings_warming_is_best_effort(standings_env, monkeypatch):
    def boom(*args, **kwargs):
        standings_env.events.append(("call", "get_standings"))
        raise RuntimeError("database is unavailable")

    boom.__name__ = "get_standings"
    monkeypatch.setattr(standings_env.scores, "get_standings", boom)
    events = standings_env.events

    # A warming failure must not break the cache clear itself
    cache_module.clear_standings()

    assert _deleted(events, "get_standings")
    assert _called(events, "get_standings")
