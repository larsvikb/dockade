# SPDX-License-Identifier: Apache-2.0
"""Dependency-free import helpers for the governance modules under test.

The two governance modules import third-party packages at module scope that we
deliberately do NOT want to install just to unit-test pure decision logic:

  - ``proxies/egress/addon.py``  imports ``mitmproxy`` (only for hook signatures
    and ``http.Response.make`` inside the async hooks — never in the functions we
    test).
  - ``control-plane/app.py``     imports ``fastapi`` / ``pydantic`` and builds a
    ``FastAPI`` app + ``BaseModel`` request models at import time.

So we install minimal stand-ins in ``sys.modules`` before import. The stubs only
need to satisfy the import + module-level construction; every function we assert
on uses the stdlib (``sqlite3``, ``socket``, ``threading``) or plain Python. This
keeps ``python -m unittest`` runnable with no pip installs (see DESIGN.md).

Both modules are loaded by absolute path (``control-plane`` has a hyphen, so it
is not importable as a package name) under a private module name.

``control-plane/app.py`` is itself the top of a small set of sibling modules
(``store``, ``policy``, ``holds``, ``ingest``, the ``api_*`` surfaces) that it
imports by plain name — the way any script does from its own directory. So the
loader puts that directory on
``sys.path`` before executing it, which is exactly what ``python app.py`` does for
the container. Tests reach a sibling through the app module (``cp.holds``), and
that indirection is load-bearing: rebinding a tunable has to happen on the module
whose functions READ it, so ``cp.holds.MAX_PENDING = 2`` works where a re-exported
``cp.MAX_PENDING = 2`` would silently not. The same holds for a handler's own
tunable: ``cp.api_views.AUDIT_GROUP_SCAN``, never ``cp.AUDIT_GROUP_SCAN``.
"""
from __future__ import annotations

import importlib.util
import os
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _install_mitmproxy_stub() -> None:
    if "mitmproxy" in sys.modules:
        return
    mitm = types.ModuleType("mitmproxy")
    http = types.ModuleType("mitmproxy.http")
    tls = types.ModuleType("mitmproxy.tls")

    class _Response:
        @staticmethod
        def make(*args, **kwargs):  # addon calls http.Response.make(...)
            return object()

    http.Response = _Response
    http.HTTPFlow = object
    tls.ClientHelloData = object
    mitm.http = http
    mitm.tls = tls
    sys.modules["mitmproxy"] = mitm
    sys.modules["mitmproxy.http"] = http
    sys.modules["mitmproxy.tls"] = tls


def _route_recorder(method: str):
    """A stub route decorator that REMEMBERS what it registered.

    The control plane serves three apps, and which app a handler lands on is a
    security property (see the module docstring in control-plane/app.py). Identity
    decorators would make that partition invisible to the suite, so the stub records
    ``(method, path)`` per app or router instance, ``include_router`` carries a
    router's into the app, and a test asserts the split directly, instead of
    asserting a constant that merely claims to describe the decorators."""
    def route(self, path, *_args, **_kwargs):
        def register(fn):
            self.__dict__.setdefault("routes", []).append((method, path))
            return fn
        return register
    return route


def _include_router(self, router, *_args, **_kwargs) -> None:
    """Copy a router's recorded routes into the app, so the app's ``routes`` is what
    that listener serves. FastAPI 0.141 resolves an included router lazily instead;
    copying gives the same answer because every route is registered at import,
    before app.py includes the router."""
    self.__dict__.setdefault("routes", []).extend(getattr(router, "routes", []))


def _install_recording_routes(klass) -> None:
    """Force the recording decorators onto whichever FastAPI stub exists.

    Deliberately overwrites rather than filling gaps: test discovery order is
    arbitrary, and ``tests/test_control_plane_ui.py`` installs its own stub with
    plain identity decorators. If that one loaded first, the route partition would
    silently stop being asserted — a guard that quietly does nothing is the exact
    failure this repo keeps rejecting elsewhere."""
    klass.get = _route_recorder("GET")
    klass.post = _route_recorder("POST")
    klass.include_router = _include_router
    # `middleware` and `on_event` take a kind ("http" / "startup"), not a path, so
    # they stay identity — recording them as routes would be a lie.
    for name in ("middleware", "on_event", "api_route"):
        if not hasattr(klass, name):
            setattr(klass, name, staticmethod(lambda *_a, **_k: (lambda fn: fn)))


def _install_fastapi_stub() -> None:
    """Install — or COMPLETE — the shared fastapi/pydantic stubs.

    Additive, not all-or-nothing. ``tests/test_control_plane_ui.py`` installs a
    fastapi stub of its own (it needs ``api_route`` and ``middleware``, which the
    control plane does not), and whichever test module imports first owns
    ``sys.modules``. This used to return early on finding one, which left the
    control plane unimportable in that order because ``pydantic`` was never
    installed — invisible only because ``test_control_plane_api`` sorts before
    ``test_control_plane_ui``. Filling gaps instead means neither module cares who
    got there first."""
    def _decorator(*_args, **_kwargs):
        return lambda fn: fn

    class FastAPI:
        def __init__(self, *args, **kwargs):
            self.routes: list[tuple[str, str]] = []

        on_event = staticmethod(_decorator)
        get = _route_recorder("GET")
        post = _route_recorder("POST")

    class APIRouter:
        def __init__(self, *args, **kwargs):
            self.routes: list[tuple[str, str]] = []

    class Request:  # only referenced in a handler signature
        pass

    fa = sys.modules.get("fastapi")
    if fa is None:
        fa = types.ModuleType("fastapi")
        sys.modules["fastapi"] = fa
    if not hasattr(fa, "FastAPI"):
        fa.FastAPI = FastAPI
    if not hasattr(fa, "Request"):
        fa.Request = Request
    if not hasattr(fa, "APIRouter"):
        fa.APIRouter = APIRouter
    _install_recording_routes(fa.FastAPI)
    _install_recording_routes(fa.APIRouter)

    responses = sys.modules.get("fastapi.responses")
    if responses is None:
        responses = types.ModuleType("fastapi.responses")

    class _Resp:
        """Enough of a Starlette response to assert on: the handlers return these,
        and tests check the status. ``status_code`` mirrors the real attribute so a
        test never has to reach into ``kwargs`` (and so this stub stays usable by the
        control-plane-ui tests, which share whichever fastapi stub loads first).

        ``headers`` is a plain dict standing in for Starlette's ``MutableHeaders``:
        the UI's ``_security_headers`` middleware writes the CSP onto whatever
        response it wraps, refusals included, so a response object without it is not
        enough of a response to test that path."""

        def __init__(self, *args, **kwargs):
            self.args, self.kwargs = args, kwargs
            self.status_code = kwargs.get("status_code", 200)
            self.body = args[0] if args else None
            self.headers = dict(kwargs.get("headers") or {})

    for name in ("JSONResponse", "PlainTextResponse", "StreamingResponse",
                 "FileResponse", "Response"):
        if not hasattr(responses, name):
            setattr(responses, name, _Resp)
    fa.responses = responses
    sys.modules["fastapi.responses"] = responses

    # The gateway hands its blocking handler to starlette's threadpool so one slow tool
    # call cannot stall the event loop. Only the import needs satisfying here: the
    # handler that uses it is the thin adapter, and everything it calls is tested
    # directly.
    concurrency = sys.modules.get("fastapi.concurrency")
    if concurrency is None:
        concurrency = types.ModuleType("fastapi.concurrency")
    if not hasattr(concurrency, "run_in_threadpool"):
        async def run_in_threadpool(fn, *args, **kwargs):
            return fn(*args, **kwargs)
        concurrency.run_in_threadpool = run_in_threadpool
    fa.concurrency = concurrency
    sys.modules["fastapi.concurrency"] = concurrency

    class BaseModel:
        """Enough of pydantic for the control plane's request/response models to
        be constructed directly in tests: apply class-declared field defaults
        (annotations that have a value, e.g. ``port: int | None = None``) first, then
        override with kwargs. Fields without a default that aren't passed stay
        unset (accessing one raises, mirroring a required field)."""

        def __init__(self, **kwargs):
            for klass in reversed(type(self).__mro__):
                for name in getattr(klass, "__annotations__", {}):
                    if name not in kwargs and hasattr(klass, name):
                        setattr(self, name, getattr(klass, name))
            for k, v in kwargs.items():
                setattr(self, k, v)

    pydantic = sys.modules.get("pydantic")
    if pydantic is None:
        pydantic = types.ModuleType("pydantic")
        sys.modules["pydantic"] = pydantic
    if not hasattr(pydantic, "BaseModel"):
        pydantic.BaseModel = BaseModel


def _load(name: str, relpath: str) -> types.ModuleType:
    path = ROOT / relpath
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def load_egress_addon() -> types.ModuleType:
    _install_mitmproxy_stub()
    return _load("dockade_egress_addon", "proxies/egress/addon.py")


def load_control_plane() -> types.ModuleType:
    _install_fastapi_stub()
    # app.py imports its siblings by plain name, so its own directory has to be
    # importable — the position `python app.py` gives it in the container.
    pkg_dir = str(ROOT / "control-plane")
    if pkg_dir not in sys.path:
        sys.path.insert(0, pkg_dir)
    module = _load("dockade_control_plane", "control-plane/app.py")
    # Every sibling reachable as ``cp.<name>`` whether or not app.py imports it, since
    # app.py imports only what the process needs. These attributes would mask app.py
    # using a name it never imported; `make lint` (pyflakes) is what catches that.
    for path in sorted((ROOT / "control-plane").glob("*.py")):
        if not hasattr(module, path.stem):
            setattr(module, path.stem, importlib.import_module(path.stem))
    return module


def load_inventory() -> types.ModuleType:
    """The control plane's in-memory tool inventory, fresh per call.

    A fresh module each time IS the isolation: the inventory is module-level state by
    design (it must be shared across the three listeners in the one process), so tests
    that shared one instance would leak a previous case's servers into the next."""
    pkg_dir = str(ROOT / "control-plane")
    if pkg_dir not in sys.path:
        sys.path.insert(0, pkg_dir)
    return _load(f"dockade_inventory_{len(sys.modules)}", "control-plane/inventory.py")


def load_discovery(env: dict[str, str] | None = None) -> types.ModuleType:
    """The gateway's discovery module alone, without importing the app.

    Separate from ``load_tool_gateway`` because the two read different environments
    and a test that varies one must not have to satisfy the other's bind guard. Same
    fresh-name-per-call rule, and for the same reason: these constants resolve at
    module scope."""
    previous = {k: os.environ.get(k) for k in (env or {})}
    os.environ.update(env or {})
    try:
        name = f"dockade_discovery_{len(sys.modules)}"
        return _load(name, "tool-gateway/discovery.py")
    finally:
        for key, was in previous.items():
            if was is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = was


def load_surface() -> types.ModuleType:
    """The gateway's curated tool surface, fresh per call.

    Fresh for the reason ``load_inventory`` is: the published listing is module-level
    state because one thread writes it and another serves from it, so a shared instance
    would let one test's roster answer another's ``listing()``."""
    return _load(f"dockade_surface_{len(sys.modules)}", "tool-gateway/surface.py")


def load_execute(env: dict[str, str] | None = None) -> types.ModuleType:
    """The gateway's executing half, fresh per call.

    Fresh because it imports ``surface``, whose published listing is module state — a
    cached instance would share one roster across tests. ``env`` is applied at import
    for the reason ``load_discovery`` takes one: the timeouts resolve at module scope,
    exactly as they do in the container."""
    pkg_dir = str(ROOT / "tool-gateway")
    if pkg_dir not in sys.path:
        sys.path.insert(0, pkg_dir)
    previous = {k: os.environ.get(k) for k in (env or {})}
    os.environ.update(env or {})
    try:
        return _load(f"dockade_execute_{len(sys.modules)}", "tool-gateway/execute.py")
    finally:
        for key, was in previous.items():
            if was is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = was


def load_outcomes(env: dict[str, str] | None = None) -> types.ModuleType:
    """The gateway's outcome stream, fresh per call.

    Fresh because the sink is module state: ``setup`` attaches a handler to a
    module-level logger, so a cached instance would keep writing to the previous
    test's file. ``env`` is applied at import because ``AUDIT_PATH`` and the caps
    resolve at module scope, exactly as they do in the container."""
    pkg_dir = str(ROOT / "tool-gateway")
    if pkg_dir not in sys.path:
        sys.path.insert(0, pkg_dir)
    previous = {k: os.environ.get(k) for k in (env or {})}
    os.environ.update(env or {})
    try:
        return _load(f"dockade_outcomes_{len(sys.modules)}",
                     "tool-gateway/outcomes.py")
    finally:
        for key, was in previous.items():
            if was is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = was


def load_protocol() -> types.ModuleType:
    """The gateway's MCP wire. Stateless, so one instance is reusable — loaded through
    here anyway so no test has to know where the file lives."""
    return _load("dockade_protocol", "tool-gateway/protocol.py")


def load_tool_gateway(env: dict[str, str] | None = None) -> types.ModuleType:
    """The MCP gateway, with its bind-guard environment applied at import.

    Takes an ``env`` because the thing worth testing here resolves at MODULE
    SCOPE: ``GATEWAY_AGENT_BIND`` and ``GATEWAY_BIND_FORBIDDEN`` are read into
    constants when the module executes, exactly as they are in the container,
    where the process is started once with a fixed environment. Rebinding them
    afterwards would test a configuration the gateway can never actually be in.

    Reloaded under a fresh module name per call for the same reason — a cached
    module would carry the previous call's constants and quietly answer for the
    wrong configuration."""
    _install_fastapi_stub()
    # app.py imports `discovery` by plain name, the way any script does from its own
    # directory — the same arrangement load_control_plane makes for its siblings.
    pkg_dir = str(ROOT / "tool-gateway")
    if pkg_dir not in sys.path:
        sys.path.insert(0, pkg_dir)
    previous = {k: os.environ.get(k) for k in (env or {})}
    os.environ.update(env or {})
    try:
        name = f"dockade_tool_gateway_{len(sys.modules)}"
        return _load(name, "tool-gateway/app.py")
    finally:
        for key, was in previous.items():
            if was is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = was
