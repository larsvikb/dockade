"""Regression test for the control-plane-ui relay's host pinning
(``control-plane-ui/app.py``).

The frontend reverse-proxies every non-``/`` path to the backend. A request like
``//evil.com/x`` is captured by ``{path:path}`` as ``/evil.com/x``; the old
``"/" + path`` turned that back into the network-path reference ``//evil.com/x``,
which httpx resolves against the base URL as a DIFFERENT authority
(``http://evil.com/x``) — a caller-controlled host override / SSRF. The fix
collapses leading slashes so the upstream host always stays the backend.

Dependency-free: like ``tests/_loader.py`` we stub the third-party imports
(``httpx``, the ``fastapi``/``starlette`` bits) before loading the module by path.
The httpx ``build_request`` stub resolves the passed path against the base URL with
stdlib ``urllib.parse.urljoin`` — which follows the SAME RFC-3986 rules httpx does
for network-path references — and records the resulting authority, so the test
asserts the real end-to-end outcome, not just the string shape."""
from __future__ import annotations

import asyncio
import importlib.util
import sys
import types
import unittest
from pathlib import Path
from urllib.parse import urljoin, urlsplit

ROOT = Path(__file__).resolve().parents[1]

_BASE = "http://control-plane:8090"
_sent: dict = {}


def _install_stubs() -> None:
    # httpx: an AsyncClient whose build_request resolves the path against base_url
    # (RFC-3986, via urljoin) so we can assert the effective upstream authority.
    if "httpx" not in sys.modules:
        httpx = types.ModuleType("httpx")

        class _Resp:
            status_code = 200
            headers: dict = {}

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
                _sent["url"] = urljoin(self._base, url)
                return object()

            async def send(self, req, stream=False):
                return _Resp()

        httpx.AsyncClient = AsyncClient
        sys.modules["httpx"] = httpx

    # Another test module may have installed the shared fastapi stub first (test
    # discovery order is arbitrary), so augment whatever exists rather than assume
    # a clean slate: ensure FastAPI.api_route and a FileResponse the UI imports.
    def _decorator(*_a, **_k):
        return lambda fn: fn

    class _R:
        def __init__(self, *a, **k):
            self.args, self.kwargs = a, k

    if "fastapi" not in sys.modules:
        fa = types.ModuleType("fastapi")

        class FastAPI:
            def __init__(self, *a, **k):
                pass

            get = staticmethod(_decorator)
            post = staticmethod(_decorator)
            api_route = staticmethod(_decorator)

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
    for meth in ("get", "post", "api_route"):
        if not hasattr(fa.FastAPI, meth):
            setattr(fa.FastAPI, meth, staticmethod(_decorator))
    responses = sys.modules["fastapi.responses"]
    if not hasattr(responses, "FileResponse"):
        responses.FileResponse = _R
    if not hasattr(responses, "StreamingResponse"):
        responses.StreamingResponse = _R

    if "starlette.background" not in sys.modules:
        starlette = sys.modules.setdefault("starlette", types.ModuleType("starlette"))
        bg = types.ModuleType("starlette.background")
        bg.BackgroundTask = lambda *a, **k: None
        starlette.background = bg
        sys.modules["starlette.background"] = bg


def _load_ui() -> types.ModuleType:
    _install_stubs()
    path = ROOT / "control-plane-ui" / "app.py"
    spec = importlib.util.spec_from_file_location("dockade_control_plane_ui", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules["dockade_control_plane_ui"] = module
    spec.loader.exec_module(module)
    return module


ui = _load_ui()


class _FakeRequest:
    def __init__(self, path_captured: str):
        self.method = "GET"
        self.query_params = {}
        self.headers = types.SimpleNamespace(items=lambda: [])
        self._captured = path_captured

    async def body(self) -> bytes:
        return b""


class RelayHostPinningTests(unittest.TestCase):
    def _upstream_host(self, captured_path: str) -> str:
        _sent.clear()
        asyncio.run(ui.proxy(captured_path, _FakeRequest(captured_path)))
        return urlsplit(_sent["url"]).netloc

    def test_normal_paths_reach_the_backend(self):
        # {path:path} captures these WITHOUT a leading slash.
        for p in ("approvals", "approvals/stream", "api/audit"):
            self.assertEqual(self._upstream_host(p), "control-plane:8090",
                             f"normal path {p!r} must relay to the backend")

    def test_network_path_reference_cannot_override_the_host(self):
        # {path:path} RETAINS one leading slash for a '//host' request, which is the
        # SSRF vector. The upstream authority must stay the backend regardless.
        for p in ("/evil.com/x", "/evil.com", "/attacker:1234/steal"):
            self.assertEqual(
                self._upstream_host(p), "control-plane:8090",
                f"captured path {p!r} must NOT override the upstream host")


if __name__ == "__main__":
    unittest.main()
