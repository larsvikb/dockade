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
"""
from __future__ import annotations

import importlib.util
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

    The control plane serves two apps — the management API and a proxy-facing
    ``/authorize`` — and which app a handler lands on is a security property (see
    the module docstring in control-plane/app.py). Identity decorators would make
    that partition invisible to the suite, so the stub records ``(method, path)``
    per app instance and a test asserts the split directly, instead of asserting a
    constant that merely claims to describe the decorators."""
    def route(self, path, *_args, **_kwargs):
        def register(fn):
            self.__dict__.setdefault("routes", []).append((method, path))
            return fn
        return register
    return route


def _install_recording_routes(klass) -> None:
    """Force the recording decorators onto whichever FastAPI stub exists.

    Deliberately overwrites rather than filling gaps: test discovery order is
    arbitrary, and ``tests/test_control_plane_ui.py`` installs its own stub with
    plain identity decorators. If that one loaded first, the route partition would
    silently stop being asserted — a guard that quietly does nothing is the exact
    failure this repo keeps rejecting elsewhere."""
    klass.get = _route_recorder("GET")
    klass.post = _route_recorder("POST")
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
    _install_recording_routes(fa.FastAPI)

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
    return _load("dockade_control_plane", "control-plane/app.py")
