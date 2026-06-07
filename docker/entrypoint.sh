#!/bin/bash
set -e

# Phase tracking file - Tako VM reads this to know which phase timed out
PHASE_FILE="/output/.tako_phase"

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
    mkdir -p "$TARGET_DIR"
    UV_INSTALL_CMD=(uv pip install --target "$TARGET_DIR" --link-mode=copy -r "$REQS_FILE")

    if [ -n "$TAKO_DEPENDENCY_PROXY_URL" ]; then
        UV_INSTALL_CMD=(
            env
            "HTTP_PROXY=$TAKO_DEPENDENCY_PROXY_URL"
            "HTTPS_PROXY=$TAKO_DEPENDENCY_PROXY_URL"
            "ALL_PROXY=$TAKO_DEPENDENCY_PROXY_URL"
            "${UV_INSTALL_CMD[@]}"
        )
    fi

    # Capture install result (don't exit on error yet so we can record timing)
    set +e
    if [ -n "$TAKO_STARTUP_TIMEOUT" ]; then
        timeout --signal=TERM "${TAKO_STARTUP_TIMEOUT}s" "${UV_INSTALL_CMD[@]}" 2>&1
    else
        "${UV_INSTALL_CMD[@]}" 2>&1
    fi
    DEP_EXIT_CODE=$?
    set -e

    unset TAKO_DEPENDENCY_PROXY_URL

    # Set PYTHONPATH so Python can find the installed packages
    export PYTHONPATH="$TARGET_DIR:$PYTHONPATH"

    # Record dep install completion
    END_DEP=$(get_time_ms)
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

# Drop privileges and run user code as sandbox user
# Using exec replaces this process, so we need a wrapper to capture timing
set +e
if [ -n "$TAKO_EXECUTION_TIMEOUT" ]; then
    timeout --signal=TERM "${TAKO_EXECUTION_TIMEOUT}s" gosu sandbox python -u /code/main.py
else
    gosu sandbox python -u /code/main.py
fi
EXEC_EXIT_CODE=$?
set -e

# Record execution completion
END_EXEC=$(get_time_ms)
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
