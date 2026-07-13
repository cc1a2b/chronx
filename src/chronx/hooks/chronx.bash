# chronx shell hook for bash — source from ~/.bashrc:
#   eval "$(chronx hook bash)"
#
# Fires PRE (command about to run) / POST (command finished) signals to the
# chronx daemon over a named pipe. Every write is best-effort, non-blocking,
# and runs in a detached subshell, so the prompt is never slowed or stalled —
# with or without a daemon running.

# Interactive shells only, and never twice.
case $- in *i*) ;; *) return 0 2>/dev/null || exit 0 ;; esac
[[ -n "${__CHRONX_HOOKED-}" ]] && return 0
__CHRONX_HOOKED=1

CHRONX_HOME="${CHRONX_HOME:-$HOME/.chronx}"
CHRONX_SESSION="${CHRONX_SESSION:-$(date +%s)-$$}"
export CHRONX_SESSION

__chronx_alive() {
    local pid pf="$CHRONX_HOME/daemon.pid"
    [[ -r "$pf" && -p "$CHRONX_HOME/daemon.fifo" ]] || return 1
    read -r pid <"$pf" 2>/dev/null || return 1
    [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null
}

# Join args with tabs and write one line to the fifo, detached, so a dead
# daemon can never hang the shell.
__chronx_send() {
    __chronx_alive || return 0
    local IFS=$'\t'
    (printf '%s\n' "$*" >"$CHRONX_HOME/daemon.fifo" 2>/dev/null &)
}

__chronx_b64() { printf '%s' "$1" | base64 2>/dev/null | tr -d '\n'; }

# Opt-in autostart (chronx daemon autostart on): bring the daemon up once
# per new shell, silently and in the background.
if [[ -f "$CHRONX_HOME/autostart" ]] && ! __chronx_alive; then
    (command chronx daemon start >/dev/null 2>&1 &)
fi

__chronx_at_prompt=1

__chronx_preexec() {
    local cmd
    cmd=$(HISTTIMEFORMAT= builtin history 1 2>/dev/null) || return 0
    cmd=$(printf '%s' "$cmd" | sed '1s/^ *[0-9][0-9]*[* ] //')
    [[ -n "$cmd" ]] || return 0
    __chronx_send "PRE" "$CHRONX_SESSION" "${EPOCHREALTIME:-$(date +%s)}" \
        "$(__chronx_b64 "$PWD")" "$(__chronx_b64 "${cmd:0:4096}")"
}

__chronx_debug_trap() {
    # Only the first simple command after a prompt marks a new command line.
    [[ -n "${COMP_LINE-}" ]] && return 0          # tab completion
    [[ -n "${READLINE_LINE-}" ]] && return 0      # bind -x handlers
    [[ "$BASH_COMMAND" == __chronx_* ]] && return 0
    [[ -z "$__chronx_at_prompt" ]] && return 0
    __chronx_at_prompt=
    __chronx_preexec
    return 0
}

__chronx_precmd() {
    # __chronx_rc was captured as the first step of PROMPT_COMMAND.
    if [[ -z "$__chronx_at_prompt" ]]; then
        __chronx_send "POST" "$CHRONX_SESSION" \
            "${EPOCHREALTIME:-$(date +%s)}" "${__chronx_rc:-0}"
    fi
    __chronx_at_prompt=1
    return "${__chronx_rc:-0}"
}

if [[ -n "${bash_preexec_imported:-${__bp_imported-}}" ]]; then
    # bash-preexec is installed: integrate with it instead of raw traps.
    __chronx_preexec_bp() {
        __chronx_send "PRE" "$CHRONX_SESSION" "${EPOCHREALTIME:-$(date +%s)}" \
            "$(__chronx_b64 "$PWD")" "$(__chronx_b64 "${1:0:4096}")"
    }
    __chronx_precmd_bp() {
        local rc=$?
        __chronx_send "POST" "$CHRONX_SESSION" "${EPOCHREALTIME:-$(date +%s)}" "$rc"
    }
    preexec_functions+=(__chronx_preexec_bp)
    precmd_functions+=(__chronx_precmd_bp)
else
    # Chain onto any existing DEBUG trap rather than clobbering it.
    eval "set -- $(trap -p DEBUG)"
    __chronx_prev_debug="${3-}"
    trap "__chronx_debug_trap${__chronx_prev_debug:+; $__chronx_prev_debug}" DEBUG
    unset __chronx_prev_debug
    # Capture $? first, run existing prompt hooks, send POST last.
    PROMPT_COMMAND='__chronx_rc=$?'"${PROMPT_COMMAND:+;$PROMPT_COMMAND};__chronx_precmd"
fi
