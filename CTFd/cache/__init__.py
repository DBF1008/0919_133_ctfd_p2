from collections import OrderedDict, namedtuple
from functools import _make_key, wraps
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


def timed_lru_cache(timeout: int = 300, maxsize: int = 64, typed: bool = False):
    """
    lru_cache implementation that includes a time based expiry

    Parameters:
    timeout (int): Timeout in seconds before an individual entry expires, default = 5 minutes
    maxsize (int): Maximum Size of the Cache
    typed (bool): Same value of different type will be a different entry

    Entries expire and are evicted individually instead of clearing the
    WHOLE cache at once. This avoids the recomputation stampede that a
    full-cache clear causes when requests arrive right after an expiry.
    """

    def wrapper_cache(func):
        ttl_ns = timeout * 10**9
        store = OrderedDict()  # key -> (expiration_ns, value), LRU ordered
        lock = RLock()
        stats = [0, 0]  # hits, misses

        @wraps(func)
        def wrapped_func(*args, **kwargs):
            key = _make_key(args, kwargs, typed)
            now = monotonic_ns()
            with lock:
                entry = store.pop(key, None)
                if entry is not None:
                    expiration, value = entry
                    if now < expiration:
                        # Still fresh: refresh LRU position and serve it
                        store[key] = entry
                        stats[0] += 1
                        return value
                # Missing or expired: only this entry is evicted, the rest
                # of the cache is left untouched
                stats[1] += 1

            # Compute outside of the lock so that concurrent calls for
            # other keys (and reentrant calls) are not blocked
            value = func(*args, **kwargs)

            with lock:
                if maxsize is None or maxsize > 0:
                    # Evict expired entries first so that fresh entries are
                    # not pushed out by stale ones
                    expired = [k for k, (exp, _) in store.items() if exp <= now]
                    for k in expired:
                        del store[k]
                    if maxsize is not None:
                        while len(store) >= maxsize:
                            store.popitem(last=False)
                    store[key] = (now + ttl_ns, value)
            return value

        def cache_info():
            with lock:
                return _TimedCacheInfo(stats[0], stats[1], maxsize, len(store))

        def cache_clear():
            with lock:
                store.clear()
                stats[0] = stats[1] = 0

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


def clear_standings():
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

    # Re-warm the hottest standings caches so that the burst of requests
    # that typically follows a cache clear doesn't stampede the database
    # by recomputing standings concurrently (thundering herd)
    _warm_standings_cache()


def _warm_standings_cache():
    """
    Best-effort recomputation of the most frequently accessed standings
    caches right after they are cleared.

    Failures here must never break the request that triggered the cache
    clear, so any exception is swallowed.
    """
    from CTFd.models import Teams
    from CTFd.utils.modes import get_model
    from CTFd.utils.scores import get_standings, get_user_standings

    try:
        # Used by the scoreboard page, ScoreboardList and ScoreboardDetail
        get_standings()
        if get_model() is Teams:
            # ScoreboardList additionally uses user standings to display
            # member scores when CTFd is running in teams mode
            get_user_standings()
    except Exception:  # nosec B110
        pass


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
