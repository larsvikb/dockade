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

# App + the policy seed (loaded into SQLite on first boot only).
COPY control-plane/app.py ./app.py
COPY policies/egress-allowlist.txt /etc/control-plane/egress-allowlist.txt

# The SQLite store lives here; a named volume mounts over it for persistence
# (the crown-jewel state). Owned by the runtime user so the volume inherits
# writable ownership.
RUN mkdir -p /var/lib/control-plane && chown -R cp:cp /var/lib/control-plane

USER cp
EXPOSE 8090
ENTRYPOINT ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8090"]
