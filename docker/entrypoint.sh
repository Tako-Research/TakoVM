#!/bin/bash
set -e

# Phase tracking file - Tako VM reads this to know which phase timed out.
#
# Prefer the root-only /tako-meta control mount: /output is world-writable
# (0777) and the sandbox user (uid 1000) could unlink and re-create
# /output/.tako_phase to forge phase/timing data that feeds the server's
# status determination. /tako-meta is mounted 0755 owned by the server uid,
# so only this entrypoint (container root) can write it. Fall back to the
# legacy /output location when /tako-meta is absent (old server that does
# not mount it) or not writable by container root (server running as a
# non-root host user; --cap-drop=ALL strips CAP_DAC_OVERRIDE, so root is
# subject to normal permission checks on the bind mount). The writability
# probe runs as root, before any privilege drop.
PHASE_FILE="/output/.tako_phase"
if [ -d /tako-meta ] && touch /tako-meta/.tako_phase 2>/dev/null; then
    PHASE_FILE="/tako-meta/.tako_phase"
fi

# Helper to get current time in milliseconds
get_time_ms() {
    # Use date with nanoseconds, convert to ms
    echo $(($(date +%s%N) / 1000000))
}

write_total_time() {
    END_TOTAL=$(get_time_ms)
    echo "total_ms=$((END_TOTAL - START_TOTAL))" >> "$PHASE_FILE"
}

# Initialize phase tracking
START_TOTAL=$(get_time_ms)
echo "container_start_ms=$START_TOTAL" > "$PHASE_FILE"

# ============================================================
# Phase 1: Dependency Installation (startup phase)
# ============================================================
echo "phase=startup" >> "$PHASE_FILE"
START_STARTUP=$(get_time_ms)

REQS_FILE="/input/_requirements.txt"

if [ -s "$REQS_FILE" ]; then
    echo "dep_install_started=true" >> "$PHASE_FILE"

    # Install with uv to a writable target directory (avoids read-only filesystem issues)
    # Using --target instead of --system to install to /tmp/site-packages
    TARGET_DIR="/tmp/site-packages"

    # Run the install as the unprivileged sandbox user (uid 1000), NOT as
    # container root. Installing a package can execute arbitrary code at build
    # time (setup.py / PEP 517 hooks), and the requirements list is reachable
    # from the API request, so installing as root would hand an attacker-chosen
    # sdist in-container root. The install only needs to write its target and
    # cache dirs, which the sandbox user can own. (issue #102)
    #
    # As root (we have not dropped privileges yet), make those writable targets
    # owned by the sandbox user before handing the install to gosu. HOME is set
    # explicitly so uv never tries to write under /root (uid 1000 cannot).
    SANDBOX_HOME="/home/sandbox"
    UV_CACHE_DIR="${UV_CACHE_DIR:-/tmp/uv-cache}"
    # Create the install target and cache dirs AS the sandbox user. Creating
    # them as root and chown'ing needs CAP_CHOWN, which --cap-drop=ALL strips,
    # and the failure aborted the run under `set -e` (see the /tmp/.cache note
    # below). The chown fallback covers the one case gosu-mkdir cannot handle:
    # a shared-cache volume whose mountpoint Docker created root-owned.
    gosu sandbox mkdir -p "$TARGET_DIR"
    if ! gosu sandbox mkdir -p "$UV_CACHE_DIR" 2>/dev/null; then
        mkdir -p "$UV_CACHE_DIR"
        chown -R sandbox:sandbox "$UV_CACHE_DIR" 2>/dev/null || true
    fi

    INSTALL_ENV=("HOME=$SANDBOX_HOME" "UV_CACHE_DIR=$UV_CACHE_DIR")
    if [ -n "$TAKO_DEPENDENCY_PROXY_URL" ]; then
        INSTALL_ENV+=(
            "HTTP_PROXY=$TAKO_DEPENDENCY_PROXY_URL"
            "HTTPS_PROXY=$TAKO_DEPENDENCY_PROXY_URL"
            "ALL_PROXY=$TAKO_DEPENDENCY_PROXY_URL"
        )
    fi
    UV_INSTALL_CMD=(
        gosu sandbox
        env "${INSTALL_ENV[@]}"
        uv pip install --target "$TARGET_DIR" --link-mode=copy -r "$REQS_FILE"
    )

    # Capture install result (don't exit on error yet so we can record timing).
    # uv output goes to stderr (1>&2): user stdout stays clean, and the server
    # classifies dependency_error patterns against stderr only.
    # --kill-after sends SIGKILL if the process survives SIGTERM, so a hung
    # installer cannot outlive the timeout.
    set +e
    if [ -n "$TAKO_STARTUP_TIMEOUT" ]; then
        timeout --signal=TERM --kill-after=10s "${TAKO_STARTUP_TIMEOUT}s" "${UV_INSTALL_CMD[@]}" 1>&2
    else
        "${UV_INSTALL_CMD[@]}" 1>&2
    fi
    DEP_EXIT_CODE=$?
    set -e

    unset TAKO_DEPENDENCY_PROXY_URL

    # Set PYTHONPATH so Python can find the installed packages
    export PYTHONPATH="$TARGET_DIR:$PYTHONPATH"

    # Record dep install completion
    END_DEP=$(get_time_ms)

    # GNU timeout exits 124 when the command stops on SIGTERM, but 137 when the
    # --kill-after SIGKILL was needed. Tako VM treats 124 as an internal timeout
    # and 137 as an OOM kill, so map kill-after deaths back to 124 when this
    # phase ran for at least the full time limit.
    if [ -n "$TAKO_STARTUP_TIMEOUT" ] && [ "$DEP_EXIT_CODE" -eq 137 ] \
        && [ $((END_DEP - START_STARTUP)) -ge $((TAKO_STARTUP_TIMEOUT * 1000)) ]; then
        DEP_EXIT_CODE=124
    fi

    echo "dep_install_ms=$((END_DEP - START_STARTUP))" >> "$PHASE_FILE"
    echo "dep_install_exit_code=$DEP_EXIT_CODE" >> "$PHASE_FILE"

    # Exit if dep install failed
    if [ $DEP_EXIT_CODE -ne 0 ]; then
        echo "startup_ms=$((END_DEP - START_STARTUP))" >> "$PHASE_FILE"
        echo "phase=failed" >> "$PHASE_FILE"
        echo "failed_phase=startup" >> "$PHASE_FILE"
        write_total_time
        exit $DEP_EXIT_CODE
    fi
else
    echo "dep_install_started=false" >> "$PHASE_FILE"
    echo "dep_install_ms=0" >> "$PHASE_FILE"
fi

unset TAKO_DEPENDENCY_PROXY_URL

END_STARTUP=$(get_time_ms)
echo "startup_ms=$((END_STARTUP - START_STARTUP))" >> "$PHASE_FILE"

# ============================================================
# Phase 2: Code Execution
# ============================================================
echo "phase=execution" >> "$PHASE_FILE"
START_EXEC=$(get_time_ms)
echo "execution_start_ms=$START_EXEC" >> "$PHASE_FILE"

# Redirect library cache/config dirs onto the writable /tmp tmpfs. The root
# filesystem is mounted --read-only and the sandbox user's HOME (/home/sandbox)
# lives there, so any library that wants $HOME/.cache (ezdxf, matplotlib,
# fontconfig, ...) would otherwise fail to create it and warn on every run.
# gosu preserves the environment, so both vars reach the Python process below.
# Created as container root, so hand ownership to the sandbox user (uid 1000)
# the same way the uv cache dir is above, since code runs unprivileged.
export XDG_CACHE_HOME=/tmp/.cache
export MPLCONFIGDIR=/tmp/.cache/matplotlib
# Create these AS the sandbox user rather than creating them as root and
# chown'ing afterwards. The default posture is --cap-drop=ALL (only SETUID and
# SETGID are re-added, for gosu), which strips CAP_CHOWN, so a chown here fails
# with EPERM; under `set -e` that aborted the entrypoint BEFORE user code ran,
# breaking every job in the shipped default configuration. /tmp is a mode-1777
# tmpfs, so uid 1000 can create its own dirs and no capability is required.
gosu sandbox mkdir -p "$XDG_CACHE_HOME" "$MPLCONFIGDIR"

# Drop privileges and run user code as sandbox user
# Using exec replaces this process, so we need a wrapper to capture timing.
# --kill-after sends SIGKILL after a grace period so untrusted code that
# ignores/traps SIGTERM (signal.SIG_IGN) cannot outlive the timeout.
set +e
if [ -n "$TAKO_EXECUTION_TIMEOUT" ]; then
    timeout --signal=TERM --kill-after=10s "${TAKO_EXECUTION_TIMEOUT}s" gosu sandbox python -u /code/main.py
else
    gosu sandbox python -u /code/main.py
fi
EXEC_EXIT_CODE=$?
set -e

# Record execution completion
END_EXEC=$(get_time_ms)

# GNU timeout exits 124 when the command stops on SIGTERM, but 137 when the
# --kill-after SIGKILL was needed. Tako VM treats 124 as an internal timeout
# and 137 as an OOM kill, so map kill-after deaths back to 124 when this
# phase ran for at least the full time limit.
if [ -n "$TAKO_EXECUTION_TIMEOUT" ] && [ "$EXEC_EXIT_CODE" -eq 137 ] \
    && [ $((END_EXEC - START_EXEC)) -ge $((TAKO_EXECUTION_TIMEOUT * 1000)) ]; then
    EXEC_EXIT_CODE=124
fi

echo "execution_ms=$((END_EXEC - START_EXEC))" >> "$PHASE_FILE"
echo "execution_exit_code=$EXEC_EXIT_CODE" >> "$PHASE_FILE"

# Final phase marker
if [ $EXEC_EXIT_CODE -eq 0 ]; then
    echo "phase=completed" >> "$PHASE_FILE"
else
    echo "phase=failed" >> "$PHASE_FILE"
    echo "failed_phase=execution" >> "$PHASE_FILE"
fi

# Total time
write_total_time

exit $EXEC_EXIT_CODE
