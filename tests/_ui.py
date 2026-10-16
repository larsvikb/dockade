# SPDX-License-Identifier: Apache-2.0
"""The stand-ins every UI's tests load its app with (``control-plane-ui/app.py``,
``tasks-ui/app.py``), and the fake requests they call it with.

Dependency-free, as ``tests/_loader.py`` is: the third-party imports (``httpx``, the
``fastapi``/``starlette`` bits) are stubbed before an app is loaded by path. The httpx
``build_request`` stub resolves the passed path against the base URL with stdlib
``urllib.parse.urljoin`` — which follows the SAME RFC-3986 rules httpx does for
network-path references — and records the resulting authority plus headers in
``sent``, so the tests assert real end-to-end outcomes rather than string shapes. One
``sent`` for every app, since the first stub installed is the one every module gets."""
from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path
from typing import ClassVar
from urllib.parse import urljoin

ROOT = Path(__file__).resolve().parents[1]

sent: dict = {}

# (class, its parent), parents first, as httpx declares them.
_HTTPX_ERRORS = (
    ("TransportError", "RequestError"),
    ("TimeoutException", "TransportError"),
    ("ConnectTimeout", "TimeoutException"), ("PoolTimeout", "TimeoutException"),
    ("ReadTimeout", "TimeoutException"), ("WriteTimeout", "TimeoutException"),
    ("NetworkError", "TransportError"),
    ("ConnectError", "NetworkError"), ("ReadError", "NetworkError"),
    ("WriteError", "NetworkError"),
    ("ProtocolError", "TransportError"), ("RemoteProtocolError", "ProtocolError"),
)


def install_stubs() -> None:
    # httpx: an AsyncClient whose build_request resolves the path against base_url
    # (RFC-3986, via urljoin) so we can assert the effective upstream authority, and
    # records the outgoing headers so we can assert provenance handling.
    if "httpx" not in sys.modules:
        httpx = types.ModuleType("httpx")

        class _Resp:
            status_code = 200
            headers: ClassVar[dict] = {}

            def aiter_raw(self):
                async def _gen():
                    if False:
                        yield b""
                return _gen()

            async def aclose(self):
                pass

        class AsyncClient:
            def __init__(self, *args, base_url="", **kwargs):
                self._base = str(base_url)

            def build_request(self, method, url, **kwargs):
                sent["url"] = urljoin(self._base, url)
                sent["method"] = method
                sent["headers"] = list(kwargs.get("headers") or [])
                return object()

            async def send(self, req, stream=False):
                return _Resp()

        class RequestError(Exception):
            """Stand-in for the base of httpx's transport errors — what the relay
            catches to answer 502 when the backend is unreachable."""

        httpx.AsyncClient = AsyncClient
        httpx.RequestError = RequestError
        sys.modules["httpx"] = httpx

    # Augment rather than assume ownership: discovery order is arbitrary, so another
    # module may have installed an httpx stub of its own first.
    if not hasattr(sys.modules["httpx"], "RequestError"):
        sys.modules["httpx"].RequestError = type("RequestError", (Exception,), {})
    # httpx's transport errors, as much of its hierarchy as the tasks UI's relay splits
    # on (`_NOT_SENT` there): what fails before a request is sent, and what after.
    for name, parent in _HTTPX_ERRORS:
        if not hasattr(sys.modules["httpx"], name):
            base = getattr(sys.modules["httpx"], parent)
            setattr(sys.modules["httpx"], name, type(name, (base,), {}))
    if not hasattr(sys.modules["httpx"], "Timeout"):
        sys.modules["httpx"].Timeout = lambda *a, **k: (a, k)

    # Another test module may have installed the shared fastapi stub first (test
    # discovery order is arbitrary), so augment whatever exists rather than assume
    # a clean slate: ensure FastAPI.api_route/.middleware and the responses the UI
    # imports. Response stubs expose .status_code so guard refusals are assertable.
    def _decorator(*_a, **_k):
        return lambda fn: fn

    class _R:
        def __init__(self, *a, **k):
            self.args, self.kwargs = a, k
            self.status_code = k.get("status_code", 200)
            self.body = a[0] if a else None
            # A dict stands in for Starlette's MutableHeaders — `security_headers`
            # writes onto every response it wraps, refusals included.
            self.headers = dict(k.get("headers") or {})

    if "fastapi" not in sys.modules:
        fa = types.ModuleType("fastapi")

        class FastAPI:
            def __init__(self, *a, **k):
                pass

            get = staticmethod(_decorator)
            post = staticmethod(_decorator)
            api_route = staticmethod(_decorator)
            middleware = staticmethod(_decorator)

        class Request:
            pass

        fa.FastAPI = FastAPI
        fa.Request = Request
        responses = types.ModuleType("fastapi.responses")
        responses.StreamingResponse = _R
        fa.responses = responses
        sys.modules["fastapi"] = fa
        sys.modules["fastapi.responses"] = responses

    fa = sys.modules["fastapi"]
    for meth in ("get", "post", "api_route", "middleware"):
        if not hasattr(fa.FastAPI, meth):
            setattr(fa.FastAPI, meth, staticmethod(_decorator))
    responses = sys.modules["fastapi.responses"]
    # `Response` is the BASE class the relay handler is annotated with (a union
    # annotation makes real FastAPI raise at import — see the note on `proxy`), so the
    # stub must expose it too or the module under test cannot be imported at all.
    for name in ("FileResponse", "StreamingResponse", "PlainTextResponse",
                 "JSONResponse", "Response"):
        if not hasattr(responses, name):
            setattr(responses, name, _R)

    if "starlette.background" not in sys.modules:
        starlette = sys.modules.setdefault("starlette", types.ModuleType("starlette"))
        bg = types.ModuleType("starlette.background")
        bg.BackgroundTask = lambda *a, **k: None
        starlette.background = bg
        sys.modules["starlette.background"] = bg


def load_ui(folder: str, name: str) -> types.ModuleType:
    """``<folder>/app.py``, loaded by path under ``name``, with the stubs in place."""
    install_stubs()
    # The image copies ui-common/'s modules beside app.py, which imports them by plain
    # name; here they are found on the path instead.
    if str(ROOT / "ui-common") not in sys.path:
        sys.path.insert(0, str(ROOT / "ui-common"))
    spec = importlib.util.spec_from_file_location(name, ROOT / folder / "app.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


class FakeRequest:
    """Minimal stand-in for a Starlette Request. Headers are lowercased on the way
    in, matching Starlette's case-insensitive mapping, since the app reads them in
    lowercase."""

    def __init__(self, method="GET", headers=None, peer="127.0.0.1"):
        self.method = method
        self.query_params = {}
        self._headers = {k.lower(): v for k, v in (headers or {}).items()}
        self.headers = types.SimpleNamespace(
            get=self._headers.get, items=lambda: list(self._headers.items()))
        self.client = types.SimpleNamespace(host=peer) if peer else None

    async def body(self) -> bytes:
        return b""


def ok_host(**kw):
    """A request that already satisfies the Host guard, for tests about other guards."""
    headers = {"host": "127.0.0.1:28090", **kw.pop("headers", {})}
    return FakeRequest(headers=headers, **kw)


async def passthrough(_request):
    return "REACHED-THE-APP"
