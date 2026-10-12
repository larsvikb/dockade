# SPDX-License-Identifier: Apache-2.0
# Control-plane UI (frontend). A stateless reverse proxy + static server, on
# control-ui-net (host-loopback publish) and control-net (to reach the backend).
# NOT the egress choke point; base pinned by tag (deliberate upgrades), like the
# backend. Built from the repo root, as the sandbox images are for sandbox-common/,
# because it also copies ui-common/: the browser guards and modules every UI shares,
# one implementation rather than a copy per UI. Everything lands in one directory,
# where app.py and the page import by plain name.
FROM python:3.12-slim

# Non-root runtime user. The frontend needs no privileges; it only serves HTTP
# on control-ui-net and relays to the backend on control-net.
RUN useradd --system --create-home --home-dir /home/cpui --uid 10002 cpui

WORKDIR /opt/control-plane-ui

COPY control-plane-ui/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY control-plane-ui/app.py ./app.py
COPY ui-common/guards.py ./guards.py
COPY control-plane-ui/index.html ./index.html
# The page's modules, served as files rather than inlined into index.html — that is
# what lets the CSP say `script-src 'self'` (see CSP in ui-common/guards.py). One
# line each: a test holds this list equal to UI_MODULES in app.py.
COPY control-plane-ui/app.js ./app.js
COPY control-plane-ui/audit.js ./audit.js
COPY control-plane-ui/dom.js ./dom.js
COPY control-plane-ui/egress-rules.js ./egress-rules.js
COPY control-plane-ui/holds.js ./holds.js
COPY control-plane-ui/leases.js ./leases.js
COPY control-plane-ui/mcp.js ./mcp.js
COPY control-plane-ui/payload.js ./payload.js
COPY control-plane-ui/provenance.js ./provenance.js
COPY control-plane-ui/saturation.js ./saturation.js
COPY control-plane-ui/status.js ./status.js
COPY control-plane-ui/tasks.js ./tasks.js
COPY ui-common/time.js ./time.js

USER cpui
EXPOSE 8090
# app.py serves itself, so it can refuse a bind before the socket opens (check_bind).
ENTRYPOINT ["python", "app.py"]
