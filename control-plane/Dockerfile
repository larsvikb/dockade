# SPDX-License-Identifier: Apache-2.0
# Control plane — the governance authority. NOT an internet-facing service and
# NOT the egress choke point (that is proxies/egress), so this base is pinned by
# tag rather than digest; upgrades are still deliberate (bump, rebuild).
#
# Build context is the REPO ROOT (see docker-compose.yml) so this can COPY the
# top-level policies/ seed. See .dockerignore for what that context excludes.
FROM python:3.12-slim

# Non-root runtime user. The control plane needs no privileges — it only serves
# HTTP on control-net and writes its SQLite store on a mounted volume.
RUN useradd --system --create-home --home-dir /home/cp --uid 10001 cp

WORKDIR /opt/control-plane

# Deps first for layer caching; pinned in requirements.txt.
COPY control-plane/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# App + the policy seed (loaded into SQLite on first boot). The approval UI is
# served by the separate control-plane-ui frontend, not from here.
COPY control-plane/app.py ./app.py
COPY policies/egress-allowlist.txt /etc/control-plane/egress-allowlist.txt

# The SQLite store lives here; a named volume mounts over it for persistence
# (the crown-jewel state). Owned by the runtime user so the volume inherits
# writable ownership.
RUN mkdir -p /var/lib/control-plane && chown -R cp:cp /var/lib/control-plane

USER cp
# Two listeners, deliberately: 8090 is the management API (approvals, resolve,
# the read-only views) and 8091 is the proxy-facing /authorize. They are split so
# the egress proxy has a route to the second and none to the first — see the
# module docstring in app.py and DESIGN.md.
EXPOSE 8090 8091
# `python app.py`, not `uvicorn app:app`: the process owns two uvicorn servers on
# one event loop (app.main), because a held /authorize and the resolve that
# releases it share in-process state and cannot be split across containers.
ENTRYPOINT ["python", "app.py"]
