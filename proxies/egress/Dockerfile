FROM mitmproxy/mitmproxy:latest

# Policy + audit addon and the baked starter allowlist. The allowlist is re-read
# per request, so it can be edited live (docker exec / bind mount) or, later,
# rewritten by the control plane.
COPY addon.py /opt/egress/addon.py
COPY allowlist.txt /etc/egress/allowlist.txt

# Durable audit sink directory, owned by the runtime user so a mounted named
# volume inherits writable ownership. stdout remains the primary audit stream
# (captured by `docker compose logs egress-proxy`); this file is for persistence.
USER root
RUN mkdir -p /var/log/egress \
    && chown -R mitmproxy:mitmproxy /var/log/egress /etc/egress /opt/egress
USER mitmproxy

# Regular (forward) proxy on 8080.
#   connection_strategy=lazy — don't dial upstream until after we've decided, so
#     a denied CONNECT never opens an outbound connection.
#   flow_detail=1 — keep mitmdump's own output terse; our addon emits the
#     structured JSON audit lines.
ENTRYPOINT ["mitmdump", \
    "--mode", "regular", \
    "--listen-host", "0.0.0.0", \
    "--listen-port", "8080", \
    "-s", "/opt/egress/addon.py", \
    "--set", "connection_strategy=lazy", \
    "--set", "flow_detail=1"]
