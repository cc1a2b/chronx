# chronx shell hook for zsh — source from ~/.zshrc:
#   eval "$(chronx hook zsh)"
#
# Fires PRE (command about to run) / POST (command finished) signals to the
# chronx daemon over a named pipe. Writes are best-effort, non-blocking, and
# detached, so the prompt is never slowed or stalled.

[[ -o interactive ]] || return 0
[[ -n "${__CHRONX_HOOKED-}" ]] && return 0
__CHRONX_HOOKED=1

zmodload zsh/datetime 2>/dev/null

CHRONX_HOME="${CHRONX_HOME:-$HOME/.chronx}"
CHRONX_SESSION="${CHRONX_SESSION:-$(date +%s)-$$}"
export CHRONX_SESSION

__chronx_alive() {
    local pid pf="$CHRONX_HOME/daemon.pid"
    [[ -r "$pf" && -p "$CHRONX_HOME/daemon.fifo" ]] || return 1
    read -r pid <"$pf" 2>/dev/null || return 1
    [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null
}

__chronx_send() {
    __chronx_alive || return 0
    local IFS=$'\t'
    (printf '%s\n' "$*" >"$CHRONX_HOME/daemon.fifo" 2>/dev/null &) 2>/dev/null
}

__chronx_b64() { printf '%s' "$1" | base64 2>/dev/null | tr -d '\n'; }

__chronx_ran=

__chronx_preexec() {
    local ts="${EPOCHREALTIME:-$EPOCHSECONDS}"
    __chronx_ran=1
    __chronx_send "PRE" "$CHRONX_SESSION" "${ts:-$(date +%s)}" \
        "$(__chronx_b64 "$PWD")" "$(__chronx_b64 "${1[1,4096]}")"
}

__chronx_precmd() {
    local rc=$?
    [[ -n "$__chronx_ran" ]] || return 0
    __chronx_ran=
    __chronx_send "POST" "$CHRONX_SESSION" \
        "${EPOCHREALTIME:-${EPOCHSECONDS:-$(date +%s)}}" "$rc"
    return $rc
}

autoload -Uz add-zsh-hook
add-zsh-hook preexec __chronx_preexec
add-zsh-hook precmd __chronx_precmd
