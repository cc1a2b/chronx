# chronx shell hook for fish — add to ~/.config/fish/config.fish:
#   chronx hook fish | source
#
# Fires PRE (command about to run) / POST (command finished) signals to the
# chronx daemon over a named pipe. Writes are best-effort, non-blocking, and
# detached, so the prompt is never slowed or stalled.

if status is-interactive; and not set -q __CHRONX_HOOKED
    set -g __CHRONX_HOOKED 1

    set -q CHRONX_HOME; or set -gx CHRONX_HOME $HOME/.chronx
    set -q CHRONX_SESSION; or set -gx CHRONX_SESSION (date +%s)-$fish_pid

    function __chronx_alive
        set -l pf $CHRONX_HOME/daemon.pid
        test -r $pf; and test -p $CHRONX_HOME/daemon.fifo; or return 1
        set -l pid (head -n1 $pf 2>/dev/null)
        test -n "$pid"; and kill -0 $pid 2>/dev/null
    end

    function __chronx_b64
        printf '%s' $argv[1] | base64 2>/dev/null | tr -d \n
    end

    # Opt-in autostart (chronx daemon autostart on).
    if test -f $CHRONX_HOME/autostart; and not __chronx_alive
        command chronx daemon start >/dev/null 2>&1 &
        disown 2>/dev/null
    end

    function __chronx_send
        __chronx_alive; or return 0
        set -l line (string join \t -- $argv)
        begin
            printf '%s\n' $line >$CHRONX_HOME/daemon.fifo
        end 2>/dev/null &
        disown 2>/dev/null
    end

    function __chronx_pre --on-event fish_preexec
        set -g __chronx_ran 1
        __chronx_send PRE $CHRONX_SESSION (date +%s.%N) \
            (__chronx_b64 $PWD) (__chronx_b64 (string sub -l 4096 -- "$argv[1]"))
    end

    function __chronx_post --on-event fish_postexec
        set -l rc $status
        set -q __chronx_ran; or return 0
        set -e __chronx_ran
        __chronx_send POST $CHRONX_SESSION (date +%s.%N) $rc
    end
end
