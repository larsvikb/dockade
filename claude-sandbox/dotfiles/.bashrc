# Sandbox .bashrc

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

# Safety
alias rm='rm -i'
alias mv='mv -i'
alias cp='cp -i'

# Claude — yolo is an explicit, conscious opt-in (not forced by settings)
alias claude-yolo='claude --dangerously-skip-permissions'

# Misc
alias ..='cd ..'
alias ...='cd ../..'
alias cls='clear'
