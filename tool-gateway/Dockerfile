# SPDX-License-Identifier: Apache-2.0
# MCP gateway — the governed tool data plane. Brokers credentials and decides tool
# calls, but is NOT the internet-facing egress choke point (that is proxies/egress),
# so this base is pinned by tag rather than digest — the same bar as the control
# plane, and upgrades stay deliberate (bump, rebuild).
#
# Build context is this directory, not the repo root: unlike the control plane there
# is no top-level seed to COPY, so the narrower context is both sufficient and the
# smaller thing to get wrong.
FROM python:3.12-slim

# Non-root runtime user. The gateway needs no privileges — it serves HTTP on one
# sandbox-net address and dials siblings. It holds credentials in memory, which is
# an argument for fewer privileges rather than more.
# 10003, not 10002: control-plane-ui already holds that one. Two infra images sharing
# a uid is harmless until they share a mount, and then it is invisible.
#
# Mostly a FALLBACK — compose runs this service as the invoking user so it can read the
# host-owned secrets mount. This uid is what a hand-run `docker run` gets.
RUN useradd --system --create-home --home-dir /home/gw --uid 10003 gw

WORKDIR /opt/tool-gateway

# Deps first for layer caching; pinned in requirements.txt.
COPY requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# Copied one by one rather than as a directory, following control-plane/Dockerfile:
# an added module becomes a deliberate line here, and the build context cannot
# smuggle in a stray file beside them.
COPY app.py ./app.py
COPY discovery.py ./discovery.py
COPY protocol.py ./protocol.py
COPY surface.py ./surface.py

USER gw
# One listener: the agent-facing MCP endpoint. EXPOSE is documentation — the
# binding that matters is GATEWAY_AGENT_BIND, which names a single address so this
# port is NOT served on mcp-net. See app.py's module docstring.
EXPOSE 8100
# `python app.py`, not `uvicorn app:app`: the bind guard in main() has to run before
# any listener opens, and an ASGI factory invocation would open one first.
ENTRYPOINT ["python", "app.py"]
