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
#
# app.py imports its siblings by plain name, which resolves because WORKDIR is
# their directory and `python app.py` puts it first on sys.path. Copied one by
# one rather than as a directory so an added module is a deliberate line here
# (and so the build context can never smuggle in a stray file next to them).
COPY control-plane/app.py ./app.py
COPY control-plane/store.py ./store.py
COPY control-plane/audit.py ./audit.py
COPY control-plane/policy.py ./policy.py
COPY control-plane/holds.py ./holds.py
COPY control-plane/ingest.py ./ingest.py
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
