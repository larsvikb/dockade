# SPDX-License-Identifier: Apache-2.0
"""The agent listener: MCP over HTTP, for the gateway, the one caller it takes.

Stateless, as the gateway is toward its own agents: one JSON-RPC message per POST and no
session, so ``tools/list`` and ``tools/call`` each stand alone, which is how the gateway
dials a server (``tool-gateway/discovery.py``). What is served is decided in
``handle`` and ``agent_tools``; the app only reads the request. ``build`` makes the
app, and nothing here binds it.

Every request must carry the gateway's secret, and one without it is refused before
its body is read (``task-registry/DESIGN.md``, "The agent listener")."""
from __future__ import annotations

import hmac
import json
import os
import re
from collections.abc import AsyncIterator

import agent_tools
from fastapi import FastAPI, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse, Response

#: The protocol revisions spoken, newest first.
PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")

#: The server name the gateway dials and keys on: `mcp__gateway__tasks__…`.
SERVER_NAME = "tasks"
SERVER_VERSION = "0.1.0"

MCP_PATH = "/mcp"

PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602

#: Where the secret is mounted: the file the gateway reads it from,
#: `$(MCP_SECRETS)/tasks.json`, on its own.
SECRET_FILE = os.environ.get("TASKS_SECRET_FILE", "/run/dockade/secrets/tasks.json")
SECRET_MIN = 32
# Printable ASCII without spaces: anything else cannot arrive intact. The gateway's
# http.client cannot send a character past latin-1, and h11 strips the whitespace
# around a header value, so such a secret would start the listener and refuse every
# call with a 401 that points away from the file.
_SECRET_RE = re.compile(r"[\x21-\x7e]{%d,}" % SECRET_MIN)

#: The most of one request this listener reads, in bytes: well past the largest body
#: and note the store takes, even with every character escaped.
BODY_MAX = 1024 * 1024

UNAUTHORIZED = ("this listener takes requests from the dockade gateway only, with the "
                "secret it holds for server `tasks`")


# --- the wire ----------------------------------------------------------------------

def _result(message_id: object, result: dict) -> dict:
    return {"jsonrpc": "2.0", "id": message_id, "result": result}


def _error(message_id: object, code: int, text: str) -> dict:
    return {"jsonrpc": "2.0", "id": message_id, "error": {"code": code, "message": text}}


def handle(message: object) -> dict | None:
    """One JSON-RPC message in, its answer out; None for a notification, which gets no
    answer. A tool that ran and refused is a result flagged ``isError``; an error is
    kept for a request that was malformed."""
    if isinstance(message, list):
        return _error(None, INVALID_REQUEST, "JSON-RPC batches are not accepted")
    if not isinstance(message, dict):
        return _error(None, INVALID_REQUEST, "a JSON-RPC message is an object")
    method = message.get("method")
    if message.get("jsonrpc") != "2.0" or not isinstance(method, str):
        return _error(message.get("id"), INVALID_REQUEST,
                      "a JSON-RPC message needs jsonrpc=\"2.0\" and a method")
    if "id" not in message:
        return None
    message_id = message["id"]
    params = message.get("params")
    params = params if isinstance(params, dict) else {}
    if method == "initialize":
        requested = params.get("protocolVersion")
        return _result(message_id, {
            "protocolVersion": (requested if requested in PROTOCOL_VERSIONS
                                else PROTOCOL_VERSIONS[0]),
            "capabilities": {"tools": {}},
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION}})
    if method == "ping":
        return _result(message_id, {})
    if method == "tools/list":
        return _result(message_id, {"tools": agent_tools.listing()})
    if method == "tools/call":
        if not isinstance(params.get("name"), str):
            return _error(message_id, INVALID_PARAMS, "tools/call needs a tool name")
        return _result(message_id,
                       agent_tools.call(params["name"], params.get("arguments")))
    return _error(message_id, METHOD_NOT_FOUND, f"{method!r} is not served here")


def answer(body: bytes) -> tuple[int, dict | None]:
    """The HTTP status and JSON for one request body; None for a notification."""
    try:
        message = json.loads(body)
    except ValueError:
        return 400, _error(None, PARSE_ERROR, "request body is not valid JSON")
    reply = handle(message)
    return (202, None) if reply is None else (200, reply)


# --- the secret ----------------------------------------------------------------------

def load_secret(path: str = SECRET_FILE) -> str:
    """The token the gateway sends, from the file it reads it from:
    ``{"token": "…"}``. Raises if the file is missing, unreadable or holds no token of
    at least ``SECRET_MIN`` characters, since the listener must not start without one."""
    try:
        with open(path, encoding="utf-8") as secret_file:
            data = json.load(secret_file)
    except (OSError, ValueError) as exc:
        raise RuntimeError(f"no secret for the agent listener: {path}: {exc}") from exc
    token = data.get("token") if isinstance(data, dict) else None
    if not isinstance(token, str) or not _SECRET_RE.fullmatch(token):
        raise RuntimeError(f"{path} holds no 'token' of {SECRET_MIN} or more printable "
                           f'ASCII characters without spaces: {{"token": "..."}}, the '
                           f"file the gateway reads too")
    return token


def authorized(header: str | None, expected: bytes) -> bool:
    return header is not None and hmac.compare_digest(header.encode(), expected)


class BodyTooLarge(Exception):
    pass


async def respond(header: str | None, chunks: AsyncIterator[bytes],
                  expected: bytes) -> tuple[int, dict | None]:
    """The status and JSON for one request: refused before its body is read without
    the secret, and once the body passes ``BODY_MAX`` without holding the rest."""
    if not authorized(header, expected):
        return 401, {"error": UNAUTHORIZED}
    body, size = [], 0
    async for chunk in chunks:
        size += len(chunk)
        if size > BODY_MAX:
            return 413, {"error": f"request body exceeds {BODY_MAX} bytes"}
        body.append(chunk)
    # Off the event loop: a tool call is a SQLite transaction, and may wait on
    # another writer's lock.
    return await run_in_threadpool(answer, b"".join(body))


# --- the app -----------------------------------------------------------------------

def build(secret: str) -> FastAPI:
    """The agent listener's app, taking only requests that carry ``secret`` as the
    gateway sends it: ``Authorization: Bearer <secret>``."""
    if not isinstance(secret, str) or not _SECRET_RE.fullmatch(secret):
        raise RuntimeError(f"the agent listener's secret is {SECRET_MIN} or more "
                           f"printable ASCII characters without spaces")
    expected = f"Bearer {secret}".encode()
    # No schema or docs pages: nothing here is for a browser.
    app = FastAPI(title="dockade task registry, agent listener", openapi_url=None,
                  docs_url=None, redoc_url=None)

    # No secret: the healthcheck asks only whether the process answers, and a 401
    # would tell anyone that much anyway.
    @app.get("/healthz")
    async def healthz() -> dict:
        return {"ok": True}

    @app.post(MCP_PATH)
    async def mcp(request: Request) -> Response:
        status, payload = await respond(request.headers.get("authorization"),
                                        request.stream(), expected)
        if payload is None:
            return Response(status_code=status)
        return JSONResponse(payload, status_code=status)

    return app
