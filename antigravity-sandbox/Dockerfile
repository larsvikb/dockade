# SPDX-License-Identifier: Apache-2.0
FROM debian:13-slim

ARG TZ=UTC
ENV TZ="$TZ"
ENV DEBIAN_FRONTEND=noninteractive

# Fail pipelines on the first error, so a piped `curl ... | bash` (Node install
# below) aborts if the download fails instead of piping empty input onward.
SHELL ["/bin/bash", "-o", "pipefail", "-c"]

# TIER 1 — Google's Antigravity CLI, `agy`, with governed egress through the proxy.
#
# Mirrors claude-sandbox/ on purpose: same base, same non-root user, same capability
# model, the SAME shared boundary scripts from sandbox-common/, and the same lint
# toolchain, so an agy session can run this repo's `make lint consistency test` as a
# Claude session can. What differs is the CLI, its config and its credential: a
# Google session login, kept in this tier's own config volume. No browser: agy's
# browser tools were not offered to the model when they were measured (NOTES.md,
# "agy behind dockade's proxy, measured before its tier"), and a test that drives a
# page in a real browser engine runs in Claude's image.

# Baseline Linux + dev stack: claude-sandbox/Dockerfile's set, without the browser.
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl wget ca-certificates gnupg \
    git openssh-client \
    less procps gosu unzip \
    fzf nano vim ripgrep jq \
    iptables ipset iproute2 dnsutils aggregate \
    python3 python3-pip python3-venv pipx \
    build-essential \
    shellcheck \
    && apt-get clean && rm -rf /var/lib/apt/lists/* \
    && update-alternatives --set editor /usr/bin/vim.basic

# Node.js — the same LTS line as claude-sandbox/Dockerfile, held equal by
# tests/test_topology.py.
RUN curl -fsSL https://deb.nodesource.com/setup_24.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

# hadolint, as claude-sandbox/Dockerfile fetches it. The three ARGs are copies of
# that file's, which CI reads, and tests/test_topology.py holds them equal.
ARG HADOLINT_VERSION=v2.15.1
ARG HADOLINT_SHA256_X86_64=c7187db94eeeeca956519a6af171adc31453941a1e777961f6e680f697c8c507
ARG HADOLINT_SHA256_ARM64=f6198ef8090f404dbb771abfee086eb8c48ac177f30da7fd3510aca35b344b5d
RUN arch="$(dpkg --print-architecture)" \
    && case "$arch" in \
         amd64) hadolint_arch=x86_64; sha="$HADOLINT_SHA256_X86_64" ;; \
         arm64) hadolint_arch=arm64; sha="$HADOLINT_SHA256_ARM64" ;; \
         *) echo "unsupported arch '$arch' for hadolint" >&2; exit 1 ;; \
       esac \
    && curl -fsSL \
       "https://github.com/hadolint/hadolint/releases/download/${HADOLINT_VERSION}/hadolint-linux-${hadolint_arch}" \
       -o /tmp/hadolint \
    && echo "${sha}  /tmp/hadolint" | sha256sum -c - \
    && install -m 0755 /tmp/hadolint /usr/local/bin/hadolint \
    && rm /tmp/hadolint \
    && hadolint --version

# agy, from the manifest its own installer reads (cli/install.sh on antigravity.google):
# the version, the tarball's URL and its sha512, per platform, with no signature. The
# sha512 comes from the same host as the URL, so the check catches a broken download,
# not a malicious release; the manifest is not pinned, so a rebuild takes the latest.
#
# Into a ROOT-OWNED directory OFF PATH, under the name the installer gives it.
# /usr/local/bin/agy is the wrapper (agy-wrapper.sh) that execs it, and the directory
# being read-only to the agent is one of the two things that stop agy updating
# itself; AGY_CLI_DISABLE_AUTO_UPDATE=true below is the other (NOTES.md). Update by
# rebuilding. The tarball's name for the binary is not the installer's (`antigravity`
# in 1.5.16), so it is named here and the extraction fails if a release renames it.
# The version is assigned before it is printed, so a binary that cannot run fails the
# build (a substitution inside echo would not), and that run carries the update
# switch itself: the ENV below is not set yet.
ARG AGY_MANIFEST_BASE=https://antigravity-cli-auto-updater-974169037036.us-central1.run.app/manifests
ARG AGY_TARBALL_BINARY=antigravity
RUN arch="$(dpkg --print-architecture)" \
    && case "$arch" in \
         amd64|arm64) ;; \
         *) echo "unsupported arch '$arch' for agy" >&2; exit 1 ;; \
       esac \
    && curl -fsSL "${AGY_MANIFEST_BASE}/linux_${arch}.json" -o /tmp/agy-manifest.json \
    && version="$(jq -er '.version' /tmp/agy-manifest.json)" \
    && url="$(jq -er '.url' /tmp/agy-manifest.json)" \
    && sha="$(jq -er '.sha512' /tmp/agy-manifest.json)" \
    && curl -fsSL "$url" -o /tmp/agy.tar.gz \
    && echo "${sha}  /tmp/agy.tar.gz" | sha512sum -c - \
    && mkdir /tmp/agy \
    && tar -xzf /tmp/agy.tar.gz -C /tmp/agy "$AGY_TARBALL_BINARY" \
    && install -d -m 0755 /opt/antigravity-cli \
    && install -m 0755 "/tmp/agy/$AGY_TARBALL_BINARY" /opt/antigravity-cli/agy \
    && rm -rf /tmp/agy /tmp/agy.tar.gz /tmp/agy-manifest.json \
    && v="$(AGY_CLI_DISABLE_AUTO_UPDATE=true /opt/antigravity-cli/agy --version)" \
    && echo "agy $version: $v"

# Non-root user, uid/gid matched to the host by the launcher so bind-mounted
# workspaces stay writable from both sides (see claude-sandbox/Dockerfile).
# hadolint ignore=DL3064
ARG USERNAME=sandbox
ARG USER_UID=1000
ARG USER_GID=1000
RUN groupadd --gid $USER_GID $USERNAME \
    && useradd --uid $USER_UID --gid $USER_GID -m -s /bin/bash $USERNAME \
    && mkdir -p /home/$USERNAME/.local/bin

ENV SANDBOX_CONFIG_DIR=/config
# `true`, not `1`: measured, `1` leaves the update check on (NOTES.md).
ENV AGY_CLI_DISABLE_AUTO_UPDATE=true

# agy keeps its login, settings, logs and conversations under these two directories
# (NOTES.md), so they are LINKS INTO THE CONFIG VOLUME: the login survives a restart
# and is this tier's alone. tier-setup.sh makes the targets each boot. Everything
# else under ~/.gemini stays in the container, above all ~/.gemini/config, which
# holds the gateway entry and its per-boot sandbox id: siblings share the volume and
# must never share an id.
RUN mkdir -p /workspace /config /home/$USERNAME/.gemini /home/$USERNAME/.config \
    && ln -s /config/antigravity-cli /home/$USERNAME/.gemini/antigravity-cli \
    && ln -s /config/antigravity /home/$USERNAME/.config/antigravity \
    && chown -R $USERNAME:$USERNAME /workspace /config /home/$USERNAME

WORKDIR /workspace
# Agent-writable dir goes LAST on PATH, as in claude-sandbox/Dockerfile: a prepended
# entry would let the agent shadow baked binaries, boundary-check.sh and the agy
# wrapper among them.
ENV PATH="$PATH:/home/$USERNAME/.local/bin"
ENV SHELL=/bin/bash
# UTF-8 by default — see the matching note in claude-sandbox/Dockerfile.
ENV LANG=C.UTF-8
ENV EDITOR=vim \
    VISUAL=vim

# ruff and yamllint, pipx-isolated as in claude-sandbox/Dockerfile.
USER $USERNAME
RUN pipx install ruff \
    && pipx install yamllint \
    && ruff --version \
    && yamllint --version

# Ends as root ON PURPOSE: the entrypoint needs root to arm the firewall
# (NET_ADMIN) before dropping to the non-root sandbox user via gosu, so the
# running agent is never root. See sandbox-common/entrypoint.sh.
# hadolint ignore=DL3002
USER root

# Dotfiles: the tier-agnostic ones shared with every tier, plus this tier's hook
# (the agy-yolo alias). The build context is the REPO ROOT, as for every tier.
COPY sandbox-common/dotfiles/.bashrc sandbox-common/dotfiles/.vimrc \
     sandbox-common/dotfiles/.inputrc sandbox-common/dotfiles/.gitconfig /home/$USERNAME/
COPY antigravity-sandbox/dotfiles/.bashrc.tier /home/$USERNAME/.bashrc.tier

# Root-owned baked config, installed for the sandbox user each boot by tier-setup.sh.
# The instructions are not named AGENTS.md here or in the source: agy reads that
# name as rules wherever it finds one up the tree from its working directory, so a
# copy under that name would load in its own right, on the host too.
# Rendered from antigravity-sandbox/instructions.md by `make instructions`.
COPY antigravity-sandbox/settings.json /etc/antigravity/settings.json
COPY antigravity-sandbox/user-AGENTS.md /etc/antigravity/AGENTS.md.template
# The status line settings.json points at, shared with claude-sandbox. Root-owned, so
# the agent can repoint the setting but not edit the code it runs.
COPY sandbox-common/statusline.sh /etc/antigravity/statusline.sh
COPY antigravity-sandbox/tier-setup.sh /usr/local/bin/tier-setup.sh
# Shadows nothing on PATH: the real binary is off it. Root-owned, so the agent
# cannot edit it; mistake-prevention, not containment, as the file says.
COPY antigravity-sandbox/agy-wrapper.sh /usr/local/bin/agy
COPY sandbox-common/init-firewall.sh /usr/local/bin/init-firewall.sh
COPY sandbox-common/entrypoint.sh /usr/local/bin/entrypoint.sh
COPY sandbox-common/boundary-check.sh /usr/local/bin/boundary-check.sh
# Sourced by tier-setup.sh, not run, so not on PATH: appends the operator's own
# instructions, when the launcher mounted them, after the baked AGENTS.md.
COPY sandbox-common/operator-instructions.sh /usr/local/lib/dockade/operator-instructions.sh
RUN chmod 0644 /etc/antigravity/settings.json /etc/antigravity/AGENTS.md.template \
               /usr/local/lib/dockade/operator-instructions.sh \
    && chmod 0755 /etc/antigravity/statusline.sh \
    && chmod 0755 /usr/local/bin/init-firewall.sh /usr/local/bin/entrypoint.sh \
                  /usr/local/bin/boundary-check.sh /usr/local/bin/tier-setup.sh \
                  /usr/local/bin/agy \
    && chown $USERNAME:$USERNAME /home/$USERNAME/.bashrc /home/$USERNAME/.bashrc.tier \
                                 /home/$USERNAME/.vimrc /home/$USERNAME/.inputrc /home/$USERNAME/.gitconfig

# Container starts as root so the entrypoint can set up the firewall,
# then drops to the non-root user via gosu.
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
CMD ["bash"]
