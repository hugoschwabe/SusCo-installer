#!/usr/bin/env bash
#
# Run the SusCo measurement pipeline.
#
# This wrapper checks that the host services are alive, tries to start them
# when possible, and then hands over to `python -m susco.runner`. The Python
# preflight performs the detailed model/dataset/Kepler checks.
#
set -Eeuo pipefail

SCRIPT_PATH="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/$(basename -- "${BASH_SOURCE[0]}")"
PROJECT_DIR="$(cd -- "$(dirname -- "$SCRIPT_PATH")/.." && pwd)"
PYTHON="$PROJECT_DIR/.venv/bin/python"

log()  { printf '[run] %s\n' "$*"; }
warn() { printf '[run] warning: %s\n' "$*" >&2; }
die()  { printf '[run] error: %s\n' "$*" >&2; exit 1; }

[[ -x "$PYTHON" ]] || die "project venv missing. Run 'make install' first."

ensure_systemd_service() {
  local unit="$1"
  if ! command -v systemctl >/dev/null 2>&1; then
    warn "systemctl not available; cannot check $unit"
    return 0
  fi
  if systemctl is-active --quiet "$unit"; then
    log "$unit is active"
    return 0
  fi
  log "$unit is not active; attempting to start it"
  if ! sudo -n systemctl start "$unit" 2>/dev/null; then
    sudo systemctl start "$unit" || die "could not start $unit (run 'make install' or start it manually)"
  fi
  systemctl is-active --quiet "$unit" || die "$unit failed to start"
  log "$unit started"
}

ensure_prometheus() {
  if curl -fsS --max-time 5 http://127.0.0.1:9090/-/ready >/dev/null 2>&1; then
    log "Prometheus is ready"
    return 0
  fi
  if command -v docker >/dev/null 2>&1 && docker inspect susco-prometheus >/dev/null 2>&1; then
    log "Starting existing Prometheus container"
    docker start susco-prometheus >/dev/null
  elif command -v docker >/dev/null 2>&1 && ! docker info >/dev/null 2>&1; then
    warn "current user cannot access Docker; run 'newgrp docker' or log out/in, then retry"
  fi
  for _ in $(seq 1 30); do
    if curl -fsS --max-time 5 http://127.0.0.1:9090/-/ready >/dev/null 2>&1; then
      log "Prometheus is ready"
      return 0
    fi
    sleep 1
  done
  die "Prometheus is not ready. Run 'make install' or inspect the susco-prometheus container."
}

ensure_kepler_endpoint() {
  for _ in $(seq 1 30); do
    if curl -fsS --max-time 5 http://127.0.0.1:28282/metrics 2>/dev/null \
      | grep -q '^kepler_node_cpu_joules_total'; then
      log "Kepler energy metrics are available"
      return 0
    fi
    sleep 1
  done
  die "Kepler is not exposing kepler_node_cpu_joules_total on 127.0.0.1:28282"
}

ensure_systemd_service kepler
ensure_systemd_service ollama
ensure_prometheus
ensure_kepler_endpoint

cd "$PROJECT_DIR"
exec "$PYTHON" -m susco.runner "$@"
