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


def _install_fastapi_stub() -> None:
    if "fastapi" in sys.modules:
        return
    fa = types.ModuleType("fastapi")

    def _decorator(*_args, **_kwargs):
        return lambda fn: fn

    class FastAPI:
        def __init__(self, *args, **kwargs):
            pass

        on_event = staticmethod(_decorator)
        get = staticmethod(_decorator)
        post = staticmethod(_decorator)

    class Request:  # only referenced in a handler signature
        pass

    fa.FastAPI = FastAPI
    fa.Request = Request

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

    responses.JSONResponse = _Resp
    responses.PlainTextResponse = _Resp
    responses.StreamingResponse = _Resp
    fa.responses = responses

    pydantic = types.ModuleType("pydantic")

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

    pydantic.BaseModel = BaseModel

    sys.modules["fastapi"] = fa
    sys.modules["fastapi.responses"] = responses
    sys.modules["pydantic"] = pydantic


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
