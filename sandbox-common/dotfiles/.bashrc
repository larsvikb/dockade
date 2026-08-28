# Sandbox .bashrc — SHARED BY EVERY TIER (sandbox-common/dotfiles/).
# Anything tier-specific belongs in that tier's .bashrc.tier, sourced at the end
# of this file, not here. Same rule as the boundary scripts: one shared
# implementation, per-tier hooks.

# History
HISTSIZE=10000
HISTFILESIZE=20000
HISTCONTROL=ignoreboth:erasedups
shopt -s histappend
PROMPT_COMMAND='history -a'

# Prompt — makes it obvious you're in the sandbox
PS1='\[\e[1;33m\][sandbox]\[\e[0m\] \[\e[1;34m\]\w\[\e[0m\]\$ '

# Color support
alias ls='ls --color=auto'
alias ll='ls -alF'
alias la='ls -A'
alias l='ls -CF'
alias grep='grep --color=auto'

# Git
alias g='git'
alias gs='git status'
alias gss='git status -s'
alias gd='git diff'
alias gds='git diff --staged'
alias ga='git add'
alias gaa='git add -A'
alias gc='git commit'
alias gcm='git commit -m'
alias gca='git commit --amend'
alias gco='git checkout'
alias gsw='git switch'
alias gb='git branch'
alias gl='git log --oneline -20'
alias glg='git log --graph --oneline --decorate --all'
alias gp='git pull'      # NB: pull here (not push); gps is push
alias gps='git push'
alias gf='git fetch --prune'
alias gst='git stash'
alias grs='git restore'

# Safety. `-I` and not `-i`, because the primary user of this shell is an AGENT, and
# Claude Code's shell snapshot picks these aliases up — so a per-file prompt reaches a
# caller that cannot answer it, the command does nothing, and it still exits 0
# (`rm f && echo done` printed `done` with f untouched). A footgun documented in the
# baked CLAUDE.md rather than removed is the weaker half of this repo's own instinct.
# `-I` keeps the intent — one prompt for a recursive delete or three-plus files, where
# a mistake is expensive — and asks nothing for the ordinary single-file case.
# `mv`/`cp` have no `-I`, so they lose the prompt entirely: their overwrite is a
# narrower hazard than an unanswerable prompt on every remove.
alias rm='rm -I'

# Misc
alias ..='cd ..'
alias ...='cd ../..'
alias cls='clear'

# Tier-specific additions (agent aliases, prompt tweaks). Last so a tier can
# override anything above it.
[ -f ~/.bashrc.tier ] && . ~/.bashrc.tier
