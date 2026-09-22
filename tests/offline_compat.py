"""
Helpers that let the cache unit tests run in minimal environments.

When the full CTFd dependency stack is installed (e.g. CI) this module does
nothing and the tests exercise the real packages. When third party packages
are missing (e.g. a bare offline checkout) lightweight stand-ins are
installed so that the modules under test can be imported in isolation.
"""

import functools
import os
import pickle
import sys
import types

CTFd_PACKAGE_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "CTFd"
)


def _full_stack_available():
    try:
        import CTFd  # noqa: F401
    except Exception:
        return False
    return True


def _install_flask_caching_stub():
    module = types.ModuleType("flask_caching")

    class Cache:
        """Minimal in-memory stand-in for flask_caching.Cache.

        Values are pickled/unpickled on the way in and out so that the
        serialization roundtrip behavior of real backends (redis,
        SimpleCache) is preserved.
        """

        def __init__(self, *args, **kwargs):
            self._store = {}

        def init_app(self, app, **kwargs):
            return None

        def get(self, key):
            value = self._store.get(key)
            return pickle.loads(value) if value is not None else None

        def set(self, key, value, timeout=None):
            self._store[key] = pickle.dumps(value)
            return True

        def delete(self, key):
            return self._store.pop(key, None) is not None

        def inc(self, key, delta=1):
            value = (self.get(key) or 0) + delta
            self.set(key, value)
            return value

        def memoize(self, timeout=None, **kwargs):
            def decorator(func):
                store = {}

                @functools.wraps(func)
                def wrapper(*args, **kw):
                    key = (args, tuple(sorted(kw.items())))
                    try:
                        hash(key)
                    except TypeError:
                        key = repr(key)
                    if key not in store:
                        store[key] = pickle.dumps(func(*args, **kw))
                    return pickle.loads(store[key])

                def delete_memoized(*args, **kw):
                    if args or kw:
                        key = (args, tuple(sorted(kw.items())))
                        store.pop(key, None)
                    else:
                        store.clear()

                wrapper.uncached = func
                wrapper.delete_memoized = delete_memoized
                return wrapper

            return decorator

        def delete_memoized(self, func, *args, **kwargs):
            func.delete_memoized(*args, **kwargs)

        def cached(self, timeout=None, key_prefix=None, **kwargs):
            def decorator(func):
                @functools.wraps(func)
                def wrapper(*args, **kw):
                    key = key_prefix() if callable(key_prefix) else key_prefix
                    if key is None:
                        key = func.__qualname__
                    if key not in self._store:
                        self.set(key, func(*args, **kw))
                    return self.get(key)

                return wrapper

            return decorator

    def make_template_fragment_key(fragment_name, *args):
        return "_fragment_" + fragment_name + "_".join(str(a) for a in args)

    module.Cache = Cache
    module.make_template_fragment_key = make_template_fragment_key
    sys.modules["flask_caching"] = module


def _install_cmarkgfm_stub():
    module = types.ModuleType("cmarkgfm")
    cmark = types.ModuleType("cmarkgfm.cmark")

    class Options:
        CMARK_OPT_UNSAFE = 0

    cmark.Options = Options
    module.cmark = cmark
    module.markdown_to_html_with_extensions = (
        lambda text, extensions=None, options=0: text
    )
    sys.modules["cmarkgfm"] = module
    sys.modules["cmarkgfm.cmark"] = cmark


def _install_models_stub():
    module = types.ModuleType("CTFd.models")

    class _FakeTable:
        def select(self):
            return self

        def where(self, *args, **kwargs):
            return self

    class _FakeQuery:
        def filter_by(self, **kwargs):
            return self

        def first(self):
            return None

    class Configs:
        key = None
        __table__ = _FakeTable()
        query = _FakeQuery()

        def __init__(self, key=None, value=None):
            self.key = key
            self.value = value

    class Teams:
        pass

    class Users:
        pass

    module.Configs = Configs
    module.Teams = Teams
    module.Users = Users
    module.db = types.SimpleNamespace(
        session=types.SimpleNamespace(add=lambda obj: None, commit=lambda: None)
    )
    sys.modules["CTFd.models"] = module


def ensure_ctfd_importable():
    """
    Make `import CTFd.cache` / `import CTFd.utils` work without the full
    dependency stack. No-op when the real stack is importable.
    """
    if _full_stack_available():
        return

    # Package shell that points at the real source tree so that lightweight
    # submodules (CTFd.cache, CTFd.constants, CTFd.utils, ...) load from disk
    # without executing the heavyweight CTFd/__init__.py
    package = types.ModuleType("CTFd")
    package.__path__ = [CTFd_PACKAGE_DIR]
    sys.modules["CTFd"] = package

    _install_flask_caching_stub()
    _install_cmarkgfm_stub()
    _install_models_stub()
