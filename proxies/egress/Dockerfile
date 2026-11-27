# SPDX-License-Identifier: Apache-2.0
# Digest-pinned: the egress proxy is the security choke point, so its base is not
# left on :latest or even a bare tag (a rebuild could otherwise pull a mitmproxy
# whose addon API / TLS handling differs from what addon.py is written against —
# risking a fail-open, and a tag can be repointed at a new image under the same
# name). The tag is kept for human readability; the @sha256 is what's enforced.
# Upgrades are deliberate: bump the tag AND the digest together, rebuild, re-run
# boundary-check.sh, and check `docker logs egress-proxy` shows mitmdump's startup
# line at once (the PYTHONUNBUFFERED below works only while mitmdump is a plain
# Python console script). Get the digest for a new tag with:
#   docker image inspect mitmproxy/mitmproxy:<tag> --format '{{index .RepoDigests 0}}'
FROM mitmproxy/mitmproxy:12.2.3@sha256:00b77b5d8804c8ad18cb6caefbf9d5849e895e8986c5ce011f4ae30f4385962f

# Policy + audit addon. Policy is no longer a baked file: the addon asks the
# control plane per connection (EGRESS_CONTROL_PLANE_URL). The canonical seed
# lives at policies/egress-allowlist.txt and is loaded by the control plane.
COPY addon.py /opt/egress/addon.py

# Durable audit sink directory, owned by the runtime user so a mounted named
# volume inherits writable ownership. The control plane ingests this file, which
# is how the decisions the proxy makes itself reach the central store; stdout
# (`docker compose logs egress-proxy`) carries the same lines live.
USER root
RUN mkdir -p /var/log/egress \
    && chown -R mitmproxy:mitmproxy /var/log/egress /opt/egress
USER mitmproxy

# mitmdump's termlog prints each record, the addon's audit lines among them,
# without flushing, and a container's stdout is a pipe, so Python block-buffers it:
# without this, `docker logs egress-proxy` (and `make logs-ep`) holds them until
# about 8 KB has built up or a flow mitmdump prints (a refused CONNECT, a plain-HTTP
# request) flushes the stream; an allowed HTTPS tunnel is not printed and flushes
# nothing. Held lines are lost if the process is killed outright (SIGKILL, the OOM
# killer); on SIGTERM mitmdump flushes them as it exits.
ENV PYTHONUNBUFFERED=1

# Regular (forward) proxy on 8080.
#   connection_strategy=lazy — don't dial upstream until after we've decided, so a
#     CONNECT refused at the connect stage never opens an outbound connection. The
#     SNI stage is the exception, and it is one: a fronting mismatch is refused by
#     KEEPING interception on rather than by killing the flow (addon.py,
#     `tls_clienthello`), and interception makes mitmproxy dial upstream for the
#     certificate it mints from — carrying the client's SNI. Nothing is relayed
#     either way, because `request` re-gates the flow by name, but the connection
#     is made.
#   flow_detail=1 — keep mitmdump's own output terse; our addon emits the
#     structured JSON audit lines.
ENTRYPOINT ["mitmdump", \
    "--mode", "regular", \
    "--listen-host", "0.0.0.0", \
    "--listen-port", "8080", \
    "-s", "/opt/egress/addon.py", \
    "--set", "connection_strategy=lazy", \
    "--set", "flow_detail=1"]
