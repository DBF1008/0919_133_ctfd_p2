from collections import OrderedDict, namedtuple
from functools import wraps
from hashlib import md5
from threading import RLock
from time import monotonic_ns

from flask import current_app, request
from flask_caching import Cache, make_template_fragment_key


class CTFdCache(Cache):
    """
    This subclass exists to give flask-caching some additional features
    Ideally likely we should have our own isolated redis connection but that might introduce more issues
    """

    def inc(self, *args, **kwargs):
        """
        Support redis INCR in flask-caching
        Note that redis INCR does not expire by default
        https://github.com/pallets-eco/flask-caching/issues/418
        """
        inc = getattr(self.cache, "inc", None)
        if inc is not None and callable(inc):
            return inc(*args, **kwargs)
        raise NotImplementedError

    def expire(self, key, timeout):
        """
        Support redis EXPIRE in flask-caching
        """
        if current_app.config["CACHE_TYPE"] == "redis":
            return self.cache._write_client.expire(
                f"{self.cache.key_prefix}{key}", timeout
            )
        else:
            # Generic alternative that leverages flask-caching built-ins to do expiration
            if timeout <= 0:
                self.cache.delete(key)
            value = self.get(key)
            if value:
                self.set(key=key, value=value, timeout=timeout)
                return True
            return False


cache = CTFdCache()


_TimedCacheInfo = namedtuple("CacheInfo", ["hits", "misses", "maxsize", "currsize"])

# Marker used to separate positional and keyword arguments in cache keys
_KWD_MARK = object()


def timed_lru_cache(timeout: int = 300, maxsize: int = 64, typed: bool = False):
    """
    lru_cache implementation that includes a time based expiry

    Entries expire individually once their age exceeds the timeout
    (gradual eviction). This avoids the cache-stampede/performance spike
    caused by clearing the entire cache at a single expiration instant.

    Parameters:
    timeout (int): Per-entry time to live in seconds, default = 5 minutes
    maxsize (int): Maximum Size of the Cache
    typed (bool): Same value of different type will be a different entry
    """

    def wrapper_cache(func):
        lock = RLock()
        # key -> (expiration_ns, value), ordered from least to most recently used
        entries = OrderedDict()
        stats = {"hits": 0, "misses": 0}
        delta = timeout * 10**9

        def make_key(args, kwargs):
            key = args
            if kwargs:
                key += (_KWD_MARK,) + tuple(sorted(kwargs.items()))
            if typed:
                key += tuple(type(v) for v in args)
                if kwargs:
                    key += tuple(type(v) for _, v in sorted(kwargs.items()))
            return key

        @wraps(func)
        def wrapped_func(*args, **kwargs):
            key = make_key(args, kwargs)
            now = monotonic_ns()
            # The lock is held while computing so that concurrent callers for
            # the same key cannot trigger a dogpile of duplicate computations
            with lock:
                entry = entries.pop(key, None)
                if entry is not None:
                    expiration, value = entry
                    if now < expiration:
                        entries[key] = entry
                        stats["hits"] += 1
                        return value
                    # Expired entries are evicted individually (gradual expiry)
                    stats["misses"] += 1
                else:
                    stats["misses"] += 1
                value = func(*args, **kwargs)
                entries[key] = (now + delta, value)
                while len(entries) > maxsize:
                    entries.popitem(last=False)
                return value

        def cache_clear():
            with lock:
                entries.clear()
                stats["hits"] = 0
                stats["misses"] = 0

        def cache_info():
            with lock:
                return _TimedCacheInfo(
                    stats["hits"], stats["misses"], maxsize, len(entries)
                )

        wrapped_func.cache_info = cache_info
        wrapped_func.cache_clear = cache_clear
        return wrapped_func

    return wrapper_cache


def make_cache_key(path=None, key_prefix="view/%s"):
    """
    This function mostly emulates Flask-Caching's `make_cache_key` function so we can delete cached api responses.
    Over time this function may be replaced with a cleaner custom cache implementation.
    :param path:
    :param key_prefix:
    :return:
    """
    if path is None:
        path = request.endpoint
    cache_key = key_prefix % path
    return cache_key


def make_cache_key_with_query_string(allowed_params=None, query_string_hash=None):
    if allowed_params is None:
        allowed_params = []

    def _make_cache_key_with_query_string(path=None, key_prefix="view/%s/%s"):
        if path is None:
            path = request.endpoint

        if query_string_hash:
            args_hash = query_string_hash
        else:
            args_hash = calculate_param_hash(
                params=tuple(request.args.items(multi=True)),
                allowed_params=allowed_params,
            )
        cache_key = key_prefix % (path, args_hash)
        return cache_key

    return _make_cache_key_with_query_string


def calculate_param_hash(params, allowed_params=None):
    # Copied from Flask-Caching but modified to allow only accepted parameters
    if allowed_params:
        args_as_sorted_tuple = tuple(
            sorted(pair for pair in params if pair[0] in allowed_params)
        )
    else:
        args_as_sorted_tuple = tuple(sorted(pair for pair in params))
    args_hash = md5(str(args_as_sorted_tuple).encode()).hexdigest()  # nosec B303 B324
    return args_hash


def clear_config():
    from CTFd.utils import _get_config, get_app_config

    cache.delete_memoized(_get_config)
    cache.delete_memoized(get_app_config)


def clear_standings(warm=True):
    from CTFd.api import api
    from CTFd.api.v1.scoreboard import ScoreboardDetail, ScoreboardList
    from CTFd.constants.static import CacheKeys
    from CTFd.models import Teams, Users  # noqa: I001
    from CTFd.utils.scoreboard import get_scoreboard_detail
    from CTFd.utils.scores import get_standings, get_team_standings, get_user_standings
    from CTFd.utils.user import (
        get_team_place,
        get_team_schema,
        get_team_score,
        get_user_place,
        get_user_schema,
        get_user_score,
    )

    # Clear out the bulk standings functions
    cache.delete_memoized(get_standings)
    cache.delete_memoized(get_team_standings)
    cache.delete_memoized(get_user_standings)
    cache.delete_memoized(get_scoreboard_detail)

    # Clear out the individual helpers for accessing score via the model
    cache.delete_memoized(Users.get_score)
    cache.delete_memoized(Users.get_place)
    cache.delete_memoized(Teams.get_score)
    cache.delete_memoized(Teams.get_place)

    # Clear the Jinja Attrs constants
    cache.delete_memoized(get_user_score)
    cache.delete_memoized(get_user_place)
    cache.delete_memoized(get_team_score)
    cache.delete_memoized(get_team_place)

    # Clear schema functions
    cache.delete_memoized(get_team_schema)
    cache.delete_memoized(get_user_schema)

    # Clear out HTTP request responses
    cache.delete(make_cache_key(path=api.name + "." + ScoreboardList.endpoint))
    cache.delete(make_cache_key(path=api.name + "." + ScoreboardDetail.endpoint))
    cache.delete_memoized(ScoreboardList.get)
    cache.delete_memoized(ScoreboardDetail.get)

    # Clear out scoreboard templates
    cache.delete(make_template_fragment_key(CacheKeys.PUBLIC_SCOREBOARD_TABLE))

    if warm:
        warm_standings()


def warm_standings():
    """
    Recompute the hot, globally-shared standings caches immediately after
    invalidation. Without warming, a burst of concurrent requests would all
    find an empty cache and stampede the database with identical expensive
    score queries (thundering herd). Warming collapses that burst into a
    single recomputation.
    """
    from CTFd.utils import get_config
    from CTFd.utils.modes import TEAMS_MODE
    from CTFd.utils.scores import (
        get_standings,
        get_team_standings,
        get_user_standings,
    )

    # Warm the default public standings used by the scoreboard page and API
    get_standings()
    if get_config("user_mode") == TEAMS_MODE:
        get_team_standings()
    else:
        get_user_standings()


def clear_challenges():
    from CTFd.utils.challenges import get_all_challenges  # noqa: I001
    from CTFd.utils.challenges import (
        get_rating_average_for_challenge_id,
        get_solve_counts_for_challenges,
        get_solve_ids_for_user_id,
        get_solves_for_challenge_id,
        get_submissions_for_user_id_for_challenge_id,
    )

    cache.delete_memoized(get_all_challenges)
    cache.delete_memoized(get_solves_for_challenge_id)
    cache.delete_memoized(get_submissions_for_user_id_for_challenge_id)
    cache.delete_memoized(get_solve_ids_for_user_id)
    cache.delete_memoized(get_solve_counts_for_challenges)
    cache.delete_memoized(get_rating_average_for_challenge_id)


def clear_ratings():
    from CTFd.utils.challenges import get_rating_average_for_challenge_id

    cache.delete_memoized(get_rating_average_for_challenge_id)


def clear_pages():
    from CTFd.utils.config.pages import get_page, get_pages

    cache.delete_memoized(get_pages)
    cache.delete_memoized(get_page)


def clear_user_recent_ips(user_id):
    from CTFd.utils.user import get_user_recent_ips

    cache.delete_memoized(get_user_recent_ips, user_id=user_id)


def clear_user_session(user_id):
    from CTFd.utils.user import (  # noqa: I001
        get_user_attrs,
        get_user_place,
        get_user_recent_ips,
        get_user_schema,
        get_user_score,
    )

    cache.delete_memoized(get_user_attrs, user_id=user_id)
    cache.delete_memoized(get_user_place, user_id=user_id)
    cache.delete_memoized(get_user_score, user_id=user_id)
    cache.delete_memoized(get_user_recent_ips, user_id=user_id)
    cache.delete_memoized(get_user_schema, user_id=user_id, user_type="user")
    cache.delete_memoized(get_user_schema, user_id=user_id, user_type="admin")


def clear_all_user_sessions():
    from CTFd.utils.user import (  # noqa: I001
        get_user_attrs,
        get_user_place,
        get_user_recent_ips,
        get_user_schema,
        get_user_score,
    )

    cache.delete_memoized(get_user_attrs)
    cache.delete_memoized(get_user_place)
    cache.delete_memoized(get_user_score)
    cache.delete_memoized(get_user_recent_ips)
    cache.delete_memoized(get_user_schema)


def clear_team_session(team_id):
    from CTFd.utils.user import (
        get_team_attrs,
        get_team_place,
        get_team_schema,
        get_team_score,
    )

    cache.delete_memoized(get_team_attrs, team_id=team_id)
    cache.delete_memoized(get_team_place, team_id=team_id)
    cache.delete_memoized(get_team_score, team_id=team_id)
    cache.delete_memoized(get_team_schema, team_id=team_id, user_type="user")
    cache.delete_memoized(get_team_schema, team_id=team_id, user_type="admin")


def clear_all_team_sessions():
    from CTFd.utils.user import (
        get_team_attrs,
        get_team_place,
        get_team_schema,
        get_team_score,
    )

    cache.delete_memoized(get_team_attrs)
    cache.delete_memoized(get_team_place)
    cache.delete_memoized(get_team_score)
    cache.delete_memoized(get_team_schema)
