# Pinned: the egress proxy is the security choke point, so its base is not left on
# :latest (a rebuild could otherwise pull a mitmproxy whose addon API / TLS
# handling differs from what addon.py is written against — risking a fail-open).
# Upgrades are deliberate: bump this, rebuild, re-run boundary-check.sh. For full
# supply-chain immutability, append the digest:
#   FROM mitmproxy/mitmproxy:12.2.3@sha256:<digest>
# (get it with: docker image inspect mitmproxy/mitmproxy:12.2.3 \
#   --format '{{index .RepoDigests 0}}')
FROM mitmproxy/mitmproxy:12.2.3

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
