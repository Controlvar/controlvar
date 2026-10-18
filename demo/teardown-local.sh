#!/usr/bin/env bash
# =============================================================================
# Controlvar — Local Demo Teardown
# Usage: ./demo/teardown-local.sh
# =============================================================================
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEMO_DIR="$REPO_ROOT/demo"

GREEN='\033[0;32m'
BLUE='\033[0;34m'
YELLOW='\033[1;33m'
NC='\033[0m'

log()  { echo -e "${BLUE}[demo]${NC} $*"; }
ok()   { echo -e "${GREEN}[demo]${NC} $*"; }
warn() { echo -e "${YELLOW}[demo]${NC} $*"; }

echo ""
log "Stopping Controlvar demo environment..."

# ---------------------------------------------------------------------------
# 1. Kill tracked processes
# ---------------------------------------------------------------------------
# Every process started under PID, children before their parents. A pid file
# holds the process setup-local.sh started, and for the three web apps that is
# `npm run dev`: stopping npm alone leaves the Next.js server it started
# running, still holding its port.
descendants() {
    local child
    for child in $(pgrep -P "$1" 2>/dev/null); do
        descendants "$child"
        printf '%s\n' "$child"
    done
}

# What setup-local.sh starts under each pid file's name: part of the command
# line it runs. A pid file can outlive its process (a reboot, a stop by hand),
# and its pid can then belong to another program; a process whose command line
# does not contain this text is not one setup-local.sh started, and is left
# alone, with the processes under it.
started_command() {
    case "$1" in
        backend) printf '%s' 'uvicorn backend.app.main:app' ;;
        frontend | shoplab | streampulse) printf '%s' 'run dev' ;;
        shoplab-simulator) printf '%s' 'demo/shoplab/simulator/traffic.py' ;;
        streampulse-simulator) printf '%s' 'demo/streampulse/simulator/traffic.py' ;;
        simulator) printf '%s' 'backend/scripts/simulate_live_events.py' ;;
        *) printf '' ;;
    esac
}

PIDS_DIR="$DEMO_DIR/.pids"
if [[ -d "$PIDS_DIR" ]]; then
    for pid_file in "$PIDS_DIR"/*.pid; do
        [[ -f "$pid_file" ]] || continue
        PID=$(cat "$pid_file" 2>/dev/null || echo "")
        PROC_NAME=$(basename "$pid_file" .pid)
        EXPECTED=$(started_command "$PROC_NAME")
        COMMAND=""
        if [[ "$PID" =~ ^[0-9]+$ ]]; then
            COMMAND=$(ps -o args= -p "$PID" 2>/dev/null || true)
        fi
        if [[ -n "$COMMAND" ]] && { [[ -z "$EXPECTED" ]] || [[ "$COMMAND" != *"$EXPECTED"* ]]; }; then
            warn "$PROC_NAME (PID $PID) is not the process setup-local.sh started; leaving it running."
        elif [[ -n "$COMMAND" ]] && kill -0 "$PID" 2>/dev/null; then
            log "Stopping $PROC_NAME (PID $PID)..."
            # The whole tree, read before anything is stopped: a child whose
            # parent has gone is no longer found under it.
            TREE=("$PID")
            for child in $(descendants "$PID"); do
                TREE+=("$child")
            done
            kill "${TREE[@]}" 2>/dev/null || true
            # Wait up to 5 seconds for graceful shutdown
            for _ in {1..5}; do
                ALIVE=0
                for proc in "${TREE[@]}"; do
                    kill -0 "$proc" 2>/dev/null && ALIVE=1
                done
                [[ "$ALIVE" == "1" ]] || break
                sleep 1
            done
            # Force kill if still running
            kill -9 "${TREE[@]}" 2>/dev/null || true
            ok "$PROC_NAME stopped."
        else
            warn "$PROC_NAME (PID ${PID:-unknown}) not running, skipping."
        fi
    done
else
    warn "No .pids directory found — nothing to kill."
fi

# ---------------------------------------------------------------------------
# 2. Stop Docker services
# ---------------------------------------------------------------------------
# Same choice as setup-local.sh: the `docker compose` plugin, else the
# standalone `docker-compose`. If they cannot be stopped the cleanup below
# still runs, and the script then exits 1.
DOCKER_STOPPED=1
log "Stopping Docker services..."
cd "$REPO_ROOT"
if docker compose version >/dev/null 2>&1; then
    COMPOSE=(docker compose)
elif command -v docker-compose >/dev/null 2>&1; then
    COMPOSE=(docker-compose)
else
    COMPOSE=()
fi
if [[ ${#COMPOSE[@]} -eq 0 ]]; then
    warn "Neither 'docker compose' nor 'docker-compose' found — Docker services not stopped."
    DOCKER_STOPPED=0
elif "${COMPOSE[@]}" down; then
    ok "Docker services stopped."
else
    warn "'${COMPOSE[*]} down' failed — Docker services may still be running."
    DOCKER_STOPPED=0
fi

# ---------------------------------------------------------------------------
# 3. Cleanup demo artifacts
# ---------------------------------------------------------------------------
log "Cleaning up demo artifacts..."
rm -rf "$DEMO_DIR/.pids"
rm -rf "$DEMO_DIR/.logs"
rm -f  "$DEMO_DIR/.demo_api_key"
rm -f  "$DEMO_DIR/.demo_api_key_aws"
ok "Cleanup complete."

echo ""
if [[ "$DOCKER_STOPPED" != "1" ]]; then
    warn "Demo processes stopped, but the Docker services were not: stop them with 'docker compose down'."
    exit 1
fi
ok "Demo environment stopped."
echo ""
