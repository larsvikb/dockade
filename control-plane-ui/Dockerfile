# Control-plane UI (frontend). A stateless reverse proxy + static server, on
# control-ui-net (host-loopback publish) and control-net (to reach the backend).
# NOT the egress choke point; base pinned by tag (deliberate upgrades), like the
# backend. Self-contained build context (control-plane-ui/), so — unlike the
# backend — it needs no repo-root context.
FROM python:3.12-slim

# Non-root runtime user. The frontend needs no privileges; it only serves HTTP
# on control-ui-net and relays to the backend on control-net.
RUN useradd --system --create-home --home-dir /home/cpui --uid 10002 cpui

WORKDIR /opt/control-plane-ui

COPY requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py ./app.py
COPY index.html ./index.html
# The page script, served as a file rather than inlined into index.html — that is
# what lets the CSP say `script-src 'self'` (see _CSP in app.py).
COPY app.js ./app.js

USER cpui
EXPOSE 8090
ENTRYPOINT ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8090"]
