#!/usr/bin/env bash
# =============================================================================
# Controlvar — Local Demo Bootstrap
# Usage: ./demo/setup-local.sh
# =============================================================================
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEMO_DIR="$REPO_ROOT/demo"

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
BOLD='\033[1m'
NC='\033[0m'

log()  { echo -e "${BLUE}[demo]${NC} $*"; }
ok()   { echo -e "${GREEN}[demo]${NC} $*"; }
warn() { echo -e "${YELLOW}[demo]${NC} $*"; }
err()  { echo -e "${RED}[demo] ERROR:${NC} $*" >&2; }

# ---------------------------------------------------------------------------
# 1. Banner
# ---------------------------------------------------------------------------
echo ""
echo -e "${BOLD}╔══════════════════════════════════════════════╗${NC}"
echo -e "${BOLD}║  Controlvar Demo — Local Setup                ║${NC}"
echo -e "${BOLD}╚══════════════════════════════════════════════╝${NC}"
echo ""

# ---------------------------------------------------------------------------
# 2. Prerequisite checks
# ---------------------------------------------------------------------------
log "Checking prerequisites..."
MISSING=()
command -v docker   >/dev/null 2>&1 || MISSING+=("docker")
command -v python3  >/dev/null 2>&1 || MISSING+=("python3")
command -v node     >/dev/null 2>&1 || MISSING+=("node")
command -v npm      >/dev/null 2>&1 || MISSING+=("npm")

# Compose v2 is the `docker compose` plugin; the standalone `docker-compose`
# (v1) is used only when the plugin is missing.
if docker compose version >/dev/null 2>&1; then
    COMPOSE=(docker compose)
elif command -v docker-compose >/dev/null 2>&1; then
    COMPOSE=(docker-compose)
else
    MISSING+=("docker compose")
fi

if [[ ${#MISSING[@]} -gt 0 ]]; then
    err "Missing required tools: ${MISSING[*]}"
    err "Please install them and re-run this script."
    exit 1
fi
ok "All prerequisites present."

# ---------------------------------------------------------------------------
# 3. Setup directories
# ---------------------------------------------------------------------------
mkdir -p "$DEMO_DIR/.pids" "$DEMO_DIR/.logs"

# ---------------------------------------------------------------------------
# 4. Environment file
# ---------------------------------------------------------------------------
cd "$REPO_ROOT"
if [[ ! -f ".env" ]]; then
    if [[ -f ".env.example" ]]; then
        cp .env.example .env
        log "Created .env from .env.example"
    else
        # Create a minimal .env
        cat > .env <<'EOF'
DATABASE_URL=postgresql://postgres:postgres@localhost:5432/experimentation
POSTGRES_SERVER=localhost
POSTGRES_USER=postgres
POSTGRES_PASSWORD=postgres
POSTGRES_DB=experimentation
POSTGRES_SCHEMA=experimentation
REDIS_HOST=localhost
REDIS_PORT=6379
SECRET_KEY=demo_secret_key_change_in_production_32chars
ENVIRONMENT=development
LOG_LEVEL=INFO
CORS_ORIGINS=http://localhost:3100,http://localhost:3200,http://localhost:3300,http://localhost:8000
FIRST_SUPERUSER=admin@demo.com
FIRST_SUPERUSER_PASSWORD=Demo1234!
AUDIT_HMAC_KEY=demo_hmac_key_change_in_production
EOF
        log "Created minimal .env"
    fi
fi

# ---------------------------------------------------------------------------
# 5. Start Docker services (Postgres + Redis)
# ---------------------------------------------------------------------------
log "Starting Docker services (postgres, redis)..."
"${COMPOSE[@]}" up -d postgres redis

# ---------------------------------------------------------------------------
# 6. Wait for PostgreSQL to be ready
# ---------------------------------------------------------------------------
log "Waiting for PostgreSQL to be ready..."
MAX_WAIT=30
WAITED=0
until "${COMPOSE[@]}" exec -T postgres pg_isready -U postgres -q 2>/dev/null; do
    WAITED=$((WAITED + 1))
    if [[ $WAITED -ge $MAX_WAIT ]]; then
        err "PostgreSQL did not become ready within ${MAX_WAIT}s."
        exit 1
    fi
    sleep 1
done
ok "PostgreSQL is ready."

# ---------------------------------------------------------------------------
# 7. Python virtual environment + dependencies
# ---------------------------------------------------------------------------
if [[ ! -d "$REPO_ROOT/venv" ]]; then
    log "Creating Python virtual environment..."
    python3 -m venv "$REPO_ROOT/venv"
fi

log "Installing Python dependencies..."
source "$REPO_ROOT/venv/bin/activate"
pip install -r "$REPO_ROOT/backend/requirements.txt" -q

# ---------------------------------------------------------------------------
# 8. Database schema
# ---------------------------------------------------------------------------
log "Preparing the database..."
cd "$REPO_ROOT"
export ENVIRONMENT=development
export APP_ENV=development
# Background jobs run every minute in the demo so rollouts, safety checks and
# bandit refreshes are visible during a walkthrough (production defaults: 15/5/5).
export ROLLOUT_CHECK_INTERVAL_MINUTES="${ROLLOUT_CHECK_INTERVAL_MINUTES:-1}"
export SAFETY_CHECK_INTERVAL_MINUTES="${SAFETY_CHECK_INTERVAL_MINUTES:-1}"
export BANDIT_UPDATE_INTERVAL_MINUTES="${BANDIT_UPDATE_INTERVAL_MINUTES:-1}"
export POSTGRES_DB=experimentation
export POSTGRES_SCHEMA=experimentation
export POSTGRES_SERVER=localhost
export POSTGRES_USER=postgres
export POSTGRES_PASSWORD=postgres
# The bootstrap creates this administrator while `users` is empty: the one the
# demo guide signs in with, so the seed finds it rather than adding a second.
export FIRST_SUPERUSER=admin@demo.com
export FIRST_SUPERUSER_PASSWORD='Demo1234!'

# The bootstrap, not a bare `alembic upgrade heads`: the historical migration
# chain cannot be replayed from an empty database, so a new database is built
# from the models and stamped at the current heads, and an existing one is
# upgraded with `alembic upgrade heads`.
python -m backend.app.db.bootstrap
ok "Database is up to date."

# ---------------------------------------------------------------------------
# 9. Seed demo data
# ---------------------------------------------------------------------------
log "Seeding demo data (100K+ events — this takes ~60s)..."
python backend/scripts/seed_demo_data.py
ok "Demo data seeded."

# ShopLab storefront catalogue (experiments, flags, API key, 14-day history).
# Set SHOPLAB=0 to skip everything ShopLab-related.
SHOPLAB="${SHOPLAB:-1}"
if [[ "$SHOPLAB" != "0" ]]; then
    log "Seeding ShopLab demo catalogue..."
    python backend/scripts/seed_shoplab.py
    ok "ShopLab data seeded."
else
    warn "SHOPLAB=0 — skipping ShopLab seed."
fi

# StreamPulse mobile demo (flags, rollout schedule, safety config, MEG, holdout, API key).
# Set STREAMPULSE=0 to skip everything StreamPulse-related.
STREAMPULSE="${STREAMPULSE:-1}"
if [[ "$STREAMPULSE" != "0" ]]; then
    log "Seeding StreamPulse demo catalogue..."
    python backend/scripts/seed_streampulse.py
    ok "StreamPulse data seeded."
else
    warn "STREAMPULSE=0 — skipping StreamPulse seed."
fi

# ---------------------------------------------------------------------------
# 10. Start backend API
# ---------------------------------------------------------------------------
log "Starting backend API on port 8000..."
cd "$REPO_ROOT"
nohup python -m uvicorn backend.app.main:app \
    --host 127.0.0.1 --port 8000 \
    > "$DEMO_DIR/.logs/backend.log" 2>&1 &
echo $! > "$DEMO_DIR/.pids/backend.pid"
log "Backend PID: $(cat "$DEMO_DIR/.pids/backend.pid")"

# ---------------------------------------------------------------------------
# 11. Wait for backend health check
# ---------------------------------------------------------------------------
log "Waiting for backend API to be healthy..."
MAX_WAIT=30
WAITED=0
until curl -sf http://localhost:8000/health >/dev/null 2>&1; do
    WAITED=$((WAITED + 1))
    if [[ $WAITED -ge $MAX_WAIT ]]; then
        err "Backend did not become healthy within ${MAX_WAIT}s."
        err "Check logs: $DEMO_DIR/.logs/backend.log"
        exit 1
    fi
    sleep 1
done
ok "Backend API is healthy."

# ---------------------------------------------------------------------------
# 12. Create demo API key
# ---------------------------------------------------------------------------
log "Creating demo API key..."
API_KEY_RESPONSE=$(curl -sf -X POST http://localhost:8000/api/v1/tracking/demo-key \
    -H "Content-Type: application/json" \
    -d '{"name":"demo-simulator","email":"admin@demo.com","password":"Demo1234!"}' 2>/dev/null || echo "")

# Fallback: use the admin endpoint if demo-key doesn't exist
if [[ -z "$API_KEY_RESPONSE" ]]; then
    # Login to get a token
    LOGIN_RESPONSE=$(curl -sf -X POST http://localhost:8000/api/v1/auth/demo-login \
        -H "Content-Type: application/json" \
        -d '{"email":"admin@demo.com","password":"Demo1234!"}' 2>/dev/null || echo "{}")

    TOKEN=$(echo "$LOGIN_RESPONSE" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('access_token',''))" 2>/dev/null || echo "")

    if [[ -n "$TOKEN" ]]; then
        API_KEY_RESPONSE=$(curl -sf -X POST http://localhost:8000/api/v1/api-keys \
            -H "Content-Type: application/json" \
            -H "Authorization: Bearer $TOKEN" \
            -d '{"name":"demo-simulator","description":"Live event simulator key","scopes":"read,write"}' 2>/dev/null || echo "")
        DEMO_API_KEY=$(echo "$API_KEY_RESPONSE" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('key',d.get('api_key','demo-fallback-key')))" 2>/dev/null || echo "demo-fallback-key")
    else
        DEMO_API_KEY="demo-fallback-key"
    fi
else
    DEMO_API_KEY=$(echo "$API_KEY_RESPONSE" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('key',d.get('api_key','demo-fallback-key')))" 2>/dev/null || echo "demo-fallback-key")
fi

echo "$DEMO_API_KEY" > "$DEMO_DIR/.demo_api_key"
ok "Demo API key saved to demo/.demo_api_key"

# ---------------------------------------------------------------------------
# 13. Start frontend
# ---------------------------------------------------------------------------
log "Installing frontend dependencies..."
cd "$REPO_ROOT/frontend"
npm install --silent

log "Starting frontend on port 3100..."
nohup npm run dev -- -H 127.0.0.1 \
    > "$REPO_ROOT/demo/.logs/frontend.log" 2>&1 &
echo $! > "$REPO_ROOT/demo/.pids/frontend.pid"
log "Frontend PID: $(cat "$REPO_ROOT/demo/.pids/frontend.pid")"

# ---------------------------------------------------------------------------
# 13b. ShopLab storefront (port 3200) + its traffic simulator
# ---------------------------------------------------------------------------
SHOPLAB_STARTED=0
SHOPLAB_DIR="$REPO_ROOT/demo/shoplab"
if [[ "$SHOPLAB" == "0" ]]; then
    warn "SHOPLAB=0 — skipping ShopLab storefront."
elif [[ ! -f "$SHOPLAB_DIR/package.json" ]]; then
    warn "demo/shoplab/package.json not found — skipping ShopLab storefront."
else
    log "Installing ShopLab dependencies..."
    cd "$SHOPLAB_DIR"
    npm install --silent

    log "Starting ShopLab storefront on port 3200..."
    nohup npm run dev -- -H 127.0.0.1 \
        > "$DEMO_DIR/.logs/shoplab.log" 2>&1 &
    echo $! > "$DEMO_DIR/.pids/shoplab.pid"
    log "ShopLab PID: $(cat "$DEMO_DIR/.pids/shoplab.pid")"
    SHOPLAB_STARTED=1

    if [[ -f "$SHOPLAB_DIR/simulator/traffic.py" ]]; then
        log "Starting ShopLab traffic simulator (3 visitors/s)..."
        source "$REPO_ROOT/venv/bin/activate"
        cd "$REPO_ROOT"
        nohup python demo/shoplab/simulator/traffic.py --rate 3 \
            > "$DEMO_DIR/.logs/shoplab-simulator.log" 2>&1 &
        echo $! > "$DEMO_DIR/.pids/shoplab-simulator.pid"
        ok "ShopLab simulator PID: $(cat "$DEMO_DIR/.pids/shoplab-simulator.pid")"
    else
        warn "demo/shoplab/simulator/traffic.py not found — ShopLab simulator not started."
    fi
fi

# ---------------------------------------------------------------------------
# 13c. StreamPulse mobile demo (port 3300) + its device simulator
# ---------------------------------------------------------------------------
STREAMPULSE_STARTED=0
STREAMPULSE_DIR="$REPO_ROOT/demo/streampulse"
if [[ "$STREAMPULSE" == "0" ]]; then
    warn "STREAMPULSE=0 — skipping StreamPulse."
elif [[ ! -f "$STREAMPULSE_DIR/package.json" ]]; then
    warn "demo/streampulse/package.json not found — skipping StreamPulse."
else
    log "Installing StreamPulse dependencies..."
    cd "$STREAMPULSE_DIR"
    npm install --silent

    log "Starting StreamPulse on port 3300..."
    nohup npm run dev -- -H 127.0.0.1 \
        > "$DEMO_DIR/.logs/streampulse.log" 2>&1 &
    echo $! > "$DEMO_DIR/.pids/streampulse.pid"
    log "StreamPulse PID: $(cat "$DEMO_DIR/.pids/streampulse.pid")"
    STREAMPULSE_STARTED=1

    if [[ -f "$STREAMPULSE_DIR/simulator/traffic.py" ]]; then
        log "Starting StreamPulse device simulator (3 devices/s)..."
        source "$REPO_ROOT/venv/bin/activate"
        cd "$REPO_ROOT"
        nohup python demo/streampulse/simulator/traffic.py --rate 3 \
            > "$DEMO_DIR/.logs/streampulse-simulator.log" 2>&1 &
        echo $! > "$DEMO_DIR/.pids/streampulse-simulator.pid"
        ok "StreamPulse simulator PID: $(cat "$DEMO_DIR/.pids/streampulse-simulator.pid")"
    else
        warn "demo/streampulse/simulator/traffic.py not found — StreamPulse simulator not started."
    fi
fi

# ---------------------------------------------------------------------------
# 14. Start live event simulator
# ---------------------------------------------------------------------------
log "Starting live event simulator..."
source "$REPO_ROOT/venv/bin/activate"
cd "$REPO_ROOT"
nohup python backend/scripts/simulate_live_events.py \
    --api-url http://localhost:8000 \
    --api-key "$(cat "$DEMO_DIR/.demo_api_key")" \
    --rate 5 \
    > "$DEMO_DIR/.logs/simulator.log" 2>&1 &
echo $! > "$DEMO_DIR/.pids/simulator.pid"
ok "Simulator PID: $(cat "$DEMO_DIR/.pids/simulator.pid")"

# ---------------------------------------------------------------------------
# 15. Success banner
# ---------------------------------------------------------------------------
echo ""
echo -e "${GREEN}${BOLD}╔════════════════════════════════════════════════╗${NC}"
echo -e "${GREEN}${BOLD}║  Controlvar Demo — Ready!                      ║${NC}"
echo -e "${GREEN}${BOLD}║                                                ║${NC}"
echo -e "${GREEN}${BOLD}║  Frontend:    http://localhost:3100            ║${NC}"
if [[ "$SHOPLAB_STARTED" == "1" ]]; then
echo -e "${GREEN}${BOLD}║  ShopLab:     http://localhost:3200            ║${NC}"
fi
if [[ "$STREAMPULSE_STARTED" == "1" ]]; then
echo -e "${GREEN}${BOLD}║  StreamPulse: http://localhost:3300            ║${NC}"
fi
echo -e "${GREEN}${BOLD}║  API docs:    http://localhost:8000/api/v1/docs║${NC}"
echo -e "${GREEN}${BOLD}║  Admin:       admin@demo.com                   ║${NC}"
echo -e "${GREEN}${BOLD}║  Password:    Demo1234!                        ║${NC}"
echo -e "${GREEN}${BOLD}║                                                ║${NC}"
echo -e "${GREEN}${BOLD}║  Live events streaming in background           ║${NC}"
echo -e "${GREEN}${BOLD}║  Logs: demo/.logs/                             ║${NC}"
echo -e "${GREEN}${BOLD}║  Stop: ./demo/teardown-local.sh                ║${NC}"
echo -e "${GREEN}${BOLD}╚════════════════════════════════════════════════╝${NC}"
echo ""
