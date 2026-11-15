#!/bin/bash
set -e

PHASE_FILE="/output/.tako_phase"
if [ -d /tako-meta ] && touch /tako-meta/.tako_phase 2>/dev/null; then
    PHASE_FILE="/tako-meta/.tako_phase"
fi

get_time_ms() {
    echo $(($(date +%s%N) / 1000000))
}

write_total_time() {
    END_TOTAL=$(get_time_ms)
    echo "total_ms=$((END_TOTAL - START_TOTAL))" >> "$PHASE_FILE"
}

START_TOTAL=$(get_time_ms)
echo "container_start_ms=$START_TOTAL" > "$PHASE_FILE"

echo "phase=startup" >> "$PHASE_FILE"
START_STARTUP=$(get_time_ms)

REQS_FILE="/input/_requirements.txt"

if [ -s "$REQS_FILE" ]; then
    echo "dep_install_started=true" >> "$PHASE_FILE"

    TARGET_DIR="/tmp/site-packages"

    SANDBOX_HOME="/home/sandbox"
    UV_CACHE_DIR="${UV_CACHE_DIR:-/tmp/uv-cache}"
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

    set +e
    if [ -n "$TAKO_STARTUP_TIMEOUT" ]; then
        timeout --signal=TERM --kill-after=10s "${TAKO_STARTUP_TIMEOUT}s" "${UV_INSTALL_CMD[@]}" 1>&2
    else
        "${UV_INSTALL_CMD[@]}" 1>&2
    fi
    DEP_EXIT_CODE=$?
    set -e

    unset TAKO_DEPENDENCY_PROXY_URL

    export PYTHONPATH="$TARGET_DIR:$PYTHONPATH"

    END_DEP=$(get_time_ms)

    if [ -n "$TAKO_STARTUP_TIMEOUT" ] && [ "$DEP_EXIT_CODE" -eq 137 ] \
        && [ $((END_DEP - START_STARTUP)) -ge $((TAKO_STARTUP_TIMEOUT * 1000)) ]; then
        DEP_EXIT_CODE=124
    fi

    echo "dep_install_ms=$((END_DEP - START_STARTUP))" >> "$PHASE_FILE"
    echo "dep_install_exit_code=$DEP_EXIT_CODE" >> "$PHASE_FILE"

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

echo "phase=execution" >> "$PHASE_FILE"
START_EXEC=$(get_time_ms)
echo "execution_start_ms=$START_EXEC" >> "$PHASE_FILE"

export XDG_CACHE_HOME=/tmp/.cache
export MPLCONFIGDIR=/tmp/.cache/matplotlib
gosu sandbox mkdir -p "$XDG_CACHE_HOME" "$MPLCONFIGDIR"

set +e
if [ -n "$TAKO_EXECUTION_TIMEOUT" ]; then
    timeout --signal=TERM --kill-after=10s "${TAKO_EXECUTION_TIMEOUT}s" gosu sandbox python -u /code/main.py
else
    gosu sandbox python -u /code/main.py
fi
EXEC_EXIT_CODE=$?
set -e

END_EXEC=$(get_time_ms)

if [ -n "$TAKO_EXECUTION_TIMEOUT" ] && [ "$EXEC_EXIT_CODE" -eq 137 ] \
    && [ $((END_EXEC - START_EXEC)) -ge $((TAKO_EXECUTION_TIMEOUT * 1000)) ]; then
    EXEC_EXIT_CODE=124
fi

echo "execution_ms=$((END_EXEC - START_EXEC))" >> "$PHASE_FILE"
echo "execution_exit_code=$EXEC_EXIT_CODE" >> "$PHASE_FILE"

if [ $EXEC_EXIT_CODE -eq 0 ]; then
    echo "phase=completed" >> "$PHASE_FILE"
else
    echo "phase=failed" >> "$PHASE_FILE"
    echo "failed_phase=execution" >> "$PHASE_FILE"
fi

write_total_time

exit $EXEC_EXIT_CODE
