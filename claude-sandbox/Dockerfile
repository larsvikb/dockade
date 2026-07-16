FROM ubuntu:24.04

ARG TZ=UTC
ENV TZ="$TZ"
ENV DEBIAN_FRONTEND=noninteractive

# Baseline Linux + dev stack
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl wget ca-certificates gnupg2 \
    git openssh-client \
    less procps sudo gosu unzip \
    fzf nano vim ripgrep jq \
    iptables ipset iproute2 dnsutils aggregate \
    python3 python3-pip python3-venv pipx \
    build-essential \
    && apt-get clean && rm -rf /var/lib/apt/lists/* \
    && update-alternatives --set editor /usr/bin/vim.basic

# Node.js LTS (greenfield coding baseline)
RUN curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

# GitHub CLI (git push / PR workflows)
RUN curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
      -o /usr/share/keyrings/githubcli-archive-keyring.gpg \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
      > /etc/apt/sources.list.d/github-cli.list \
    && apt-get update && apt-get install -y --no-install-recommends gh \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

# Non-root user. ubuntu:24.04 ships a stock `ubuntu` user at uid/gid 1000 (the
# only thing at 1000 in the base image). Remove it so the sandbox user can own
# 1000 — matching the host user's uid keeps bind-mounted workspaces writable from
# both sides (WSL drvfs presents Windows-drive files as the host uid; a mismatch
# leaves the agent read-only on its own workspace). USER_UID/USER_GID default to
# 1000 and are set from the host by run-claude-sandbox.sh; override for a
# different host uid.
ARG USERNAME=sandbox
ARG USER_UID=1000
ARG USER_GID=1000
RUN (userdel -r ubuntu 2>/dev/null || true) \
    && (groupdel ubuntu 2>/dev/null || true) \
    && groupadd --gid $USER_GID $USERNAME \
    && useradd --uid $USER_UID --gid $USER_GID -m -s /bin/bash $USERNAME \
    && mkdir -p /home/$USERNAME/.local/bin

# Isolated, persistent Claude config dir (backed by its own volume at runtime).
# Never the host's ~/.claude.
ENV CLAUDE_CONFIG_DIR=/config

# Hardening env as REAL process env, not via a managed-settings.json `env` block.
# Under org auth the local managed file is not even loaded (the org's remote
# managed source shadows it entirely — verified, see DESIGN.md), so an `env` block
# there would never reach the process: DISABLE_AUTOUPDATER set that way never
# applied and the (always-failing, no-egress) updater kept running. A real env var
# is read directly and cannot be silenced by any settings layer. Update by rebuilding.
ENV DISABLE_AUTOUPDATER=1

# Telemetry opt-outs as REAL process env — the robust, unshadowable place for them
# (a settings `env` block can be overridden or, under org-managed settings, not
# loaded at all). These are defense-in-depth, NOT the containment boundary: if a
# telemetry exporter is enabled upstream, the firewall's default-deny is what
# actually stops metrics leaving (see DESIGN.md "Managed settings"). Do not claim
# telemetry is off until export behavior is checked.
ENV DISABLE_TELEMETRY=1 \
    DO_NOT_TRACK=1 \
    CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1

RUN mkdir -p /workspace /config \
    && chown -R $USERNAME:$USERNAME /workspace /config /home/$USERNAME

WORKDIR /workspace
ENV PATH="/home/$USERNAME/.local/bin:$PATH"
# Standardize on bash. The login shell is already /bin/bash (useradd -s above);
# pinning SHELL makes tools that auto-detect a shell (e.g. Claude Code's
# shell-snapshot) deterministically pick bash rather than any other installed shell.
ENV SHELL=/bin/bash

# Default editor. Real process env so it reaches every process (git, Claude Code,
# non-interactive shells), not just interactive bash. git honors these directly
# (GIT_EDITOR > core.editor > VISUAL > EDITOR); the update-alternatives above
# additionally points /usr/bin/editor (sensible-editor) at vim.
ENV EDITOR=vim \
    VISUAL=vim

# Install Claude Code as the non-root user.
# Auto-update is disabled via the DISABLE_AUTOUPDATER env above; update by rebuilding.
# CLAUDE_VERSION=latest (default) or pin a specific version e.g. 2.1.89.
USER $USERNAME
ARG CLAUDE_VERSION=latest
RUN curl -fsSL https://claude.ai/install.sh -o /tmp/install.sh \
    && if [ "$CLAUDE_VERSION" = "latest" ]; then bash /tmp/install.sh; \
       else bash /tmp/install.sh "$CLAUDE_VERSION"; fi \
    && rm /tmp/install.sh

# Dotfiles
COPY dotfiles/.bashrc dotfiles/.vimrc dotfiles/.inputrc dotfiles/.gitconfig /home/$USERNAME/

# Root-owned baked config (user-settings template, status line), firewall, and
# entrypoint. NB: no managed-settings.json — under org auth Claude Code loads the
# org's remote managed source as the sole managed tier and ignores a local managed
# file entirely, so it is not an enforcement lever here (verified; see DESIGN.md).
# Enforcement is the firewall + capability containment; hard policy is the org
# admin console.
USER root
COPY user-settings.json /etc/claude-code/user-settings.json
COPY statusline.sh /etc/claude-code/statusline.sh
COPY init-firewall.sh /usr/local/bin/init-firewall.sh
COPY entrypoint.sh /usr/local/bin/entrypoint.sh
COPY boundary-check.sh /usr/local/bin/boundary-check.sh
RUN chmod 0644 /etc/claude-code/user-settings.json \
    && chmod 0755 /etc/claude-code/statusline.sh \
    && chmod 0755 /usr/local/bin/init-firewall.sh /usr/local/bin/entrypoint.sh /usr/local/bin/boundary-check.sh \
    && chown $USERNAME:$USERNAME /home/$USERNAME/.bashrc /home/$USERNAME/.vimrc /home/$USERNAME/.inputrc /home/$USERNAME/.gitconfig

# Container starts as root so the entrypoint can set up the firewall,
# then drops to the non-root user via gosu.
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
CMD ["bash"]
