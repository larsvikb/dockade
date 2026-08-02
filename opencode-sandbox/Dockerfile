FROM debian:13-slim

ARG TZ=UTC
ENV TZ="$TZ"
ENV DEBIAN_FRONTEND=noninteractive

# Fail pipelines on the first error, so a piped `curl ... | bash` aborts if the
# download fails instead of piping empty input onward.
SHELL ["/bin/bash", "-o", "pipefail", "-c"]

# TIER 2 — the local-LLM agent sandbox (opencode against the `llm` service).
#
# Mirrors claude-sandbox/ on purpose: same base, same non-root user, same
# capability model, and the SAME shared boundary scripts from sandbox-common/.
# What differs is the capability grant, and it differs by subtraction:
#
#   - no Anthropic credentials, no credential volume semantics — tier 2 holds
#     NO credentials of any kind;
#   - no egress at runtime, governed or otherwise. Its only permitted
#     destination is the inference service (enforced by init-firewall.sh's
#     LOCAL mode, not by config);
#   - correspondingly no gh/GitHub CLI and no upstream DNS.
#
# Everything opencode needs at runtime is therefore baked at BUILD time, where
# egress is unrestricted (the firewall only arms at container start).

# Baseline. Leaner than tier 1 where the omission is principled: no gh and no
# openssh-client (with no egress there is no git remote to reach), no
# build-essential, no hadolint/shellcheck (this tier evaluates the local model,
# it does not lint this repo). iptables/ipset/dnsutils are required by the shared
# firewall script.
#
# python3 IS included, despite the lean intent, because of an asymmetry specific
# to this tier: with no egress, `pip install` and `npm install` cannot fetch
# anything at runtime. In tier 1 a missing tool is an inconvenience the agent
# installs its way out of; here it is a permanent wall, hit mid-task — possibly
# on an unattended scheduled run with nobody watching. Scripting glue around a
# local model call is the archetypal job for this tier, and python3 is also what
# this repo's own `make consistency` shells out to. python3-venv comes along so
# a vendored wheel can still be installed into an isolated env offline; pip
# itself is near-inert here and only useful for local wheels.
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl ca-certificates \
    git \
    less procps gosu \
    nano vim ripgrep jq \
    iptables ipset iproute2 dnsutils aggregate \
    python3 python3-venv python3-pip \
    && apt-get clean && rm -rf /var/lib/apt/lists/* \
    && update-alternatives --set editor /usr/bin/vim.basic

# Node.js — opencode is distributed as an npm package.
RUN curl -fsSL https://deb.nodesource.com/setup_24.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

# Non-root user, uid/gid matched to the host by the launcher so bind-mounted
# workspaces stay writable from both sides (see claude-sandbox/Dockerfile).
ARG USERNAME=sandbox
ARG USER_UID=1000
ARG USER_GID=1000
RUN groupadd --gid $USER_GID $USERNAME \
    && useradd --uid $USER_UID --gid $USER_GID -m -s /bin/bash $USERNAME \
    && mkdir -p /home/$USERNAME/.local/bin

ENV SANDBOX_CONFIG_DIR=/config
ENV DO_NOT_TRACK=1

RUN mkdir -p /workspace /config \
    && chown -R $USERNAME:$USERNAME /workspace /config /home/$USERNAME

WORKDIR /workspace
ENV PATH="/home/$USERNAME/.local/bin:$PATH"
ENV SHELL=/bin/bash
ENV EDITOR=vim \
    VISUAL=vim

# opencode, installed as the non-root user at BUILD time. Pin with
# OPENCODE_VERSION for a reproducible image; `latest` tracks upstream.
#
# Then WARM THE PROVIDER SDK. opencode resolves providers via npm at runtime
# ("npm": "@ai-sdk/openai-compatible" in the config below). At runtime this tier
# has no egress, so an unwarmed fetch would fail on first use — install it here,
# where the network is available. Verified empirically that a warmed opencode
# completes a request with no egress; see DESIGN.md "Local inference".
# npm's default global prefix is root-owned (/usr/lib/node_modules), so a `-g`
# install as the non-root user fails with EACCES. Point the prefix at the user's
# ~/.local instead — already on PATH above, and the same place tier 1 puts Claude
# Code and pipx tools. It also means the agent can npm-install at runtime without
# needing root, which the containment model requires it never has.
ENV NPM_CONFIG_PREFIX="/home/$USERNAME/.local"

USER $USERNAME
ARG OPENCODE_VERSION=latest
ARG AI_SDK_OPENAI_COMPAT_VERSION=latest
RUN npm install -g "opencode-ai@${OPENCODE_VERSION}" \
    && npm install -g "@ai-sdk/openai-compatible@${AI_SDK_OPENAI_COMPAT_VERSION}" \
    && opencode --version

# NOTE if offline provider resolution turns out to fail at runtime: opencode may
# populate its own cache on first use rather than reading the SDK from
# node_modules. The fix is a build-time warm — place the config, invoke opencode
# once here where egress exists, and tolerate the connection failure (the `llm`
# service does not exist at build time). Not done pre-emptively because the npm
# install above may already suffice; verify empirically before adding it.

# Shared boundary + entrypoint, byte-identical to tier 1. The build context is the
# REPO ROOT so these are reachable; a context cannot COPY from outside itself.
# tier-setup.sh is this image's own hook, invoked by the shared entrypoint.
#
# Ends as root ON PURPOSE: the entrypoint needs root to arm the firewall
# (NET_ADMIN) before dropping to the non-root sandbox user via gosu, so the
# running agent is never root. See sandbox-common/entrypoint.sh.
# hadolint ignore=DL3002
USER root
COPY opencode-sandbox/tier-setup.sh /usr/local/bin/tier-setup.sh
COPY opencode-sandbox/opencode.json /etc/opencode/opencode.json
COPY sandbox-common/init-firewall.sh /usr/local/bin/init-firewall.sh
COPY sandbox-common/entrypoint.sh /usr/local/bin/entrypoint.sh
COPY sandbox-common/boundary-check.sh /usr/local/bin/boundary-check.sh
# Dotfiles: the tier-agnostic ones shared with tier 1, plus this tier's hook
# (the `oc` alias and a prompt that distinguishes the two sandboxes).
COPY sandbox-common/dotfiles/.bashrc sandbox-common/dotfiles/.vimrc \
     sandbox-common/dotfiles/.inputrc sandbox-common/dotfiles/.gitconfig /home/$USERNAME/
COPY opencode-sandbox/dotfiles/.bashrc.tier /home/$USERNAME/.bashrc.tier
RUN chmod 0644 /etc/opencode/opencode.json \
    && chmod 0755 /usr/local/bin/init-firewall.sh /usr/local/bin/entrypoint.sh \
                  /usr/local/bin/boundary-check.sh /usr/local/bin/tier-setup.sh \
    && chown $USERNAME:$USERNAME /home/$USERNAME/.bashrc /home/$USERNAME/.bashrc.tier \
                                 /home/$USERNAME/.vimrc /home/$USERNAME/.inputrc /home/$USERNAME/.gitconfig

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
CMD ["bash"]
