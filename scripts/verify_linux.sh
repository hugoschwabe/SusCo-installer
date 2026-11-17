#!/usr/bin/env bash
#
# SusCo Linux installation verifier
# ---------------------------------
# Independently checks that the Linux measurement stack is installed AND that
# hardware energy counters actually produce increasing values. Exits non-zero
# if any required check fails.
#
# Usage:
#   sudo ./scripts/verify_linux.sh [options]
#
set -uo pipefail

SCRIPT_PATH="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/$(basename -- "${BASH_SOURCE[0]}")"
PROJECT_DIR="$(cd -- "$(dirname -- "$SCRIPT_PATH")/.." && pwd)"
ORIG_ARGS=("$@")

INSTALL_DIR="$PROJECT_DIR"
MODEL="qwen3:1.7b"
CLUSTER_NAME="susco"
KEPLER_MODE="host"
SKIP_KEPLER=0
SKIP_PROMETHEUS=0
SKIP_K8S=0
SKIP_CLUE=0
STRICT=0
STRICT_CLUE=0

usage() {
  cat <<'EOF'
SusCo Linux verifier

Options:
  --install-dir DIR     Project root (default: repo containing this script)
  --model TAG           Ollama model tag (default qwen3:1.7b)
  --cluster-name NAME   kind cluster name (default susco)
  --kepler-mode MODE    host (default) or kind
  --skip-kepler         Do not check Kepler
  --skip-prometheus     Do not check Prometheus
  --skip-k8s            Do not check kubectl/Helm/kind
  --skip-clue           Do not check CLUE
  --strict              Treat warnings as failures
  --strict-clue         Treat CLUE 'psc' import failure as a failure
  -h, --help            Show this help
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --install-dir) [[ $# -ge 2 ]] || { usage; exit 2; }; INSTALL_DIR="$(cd -- "$2" && pwd)"; shift ;;
    --model) [[ $# -ge 2 ]] || { usage; exit 2; }; MODEL="$2"; shift ;;
    --cluster-name) [[ $# -ge 2 ]] || { usage; exit 2; }; CLUSTER_NAME="$2"; shift ;;
    --kepler-mode) [[ $# -ge 2 ]] || { usage; exit 2; }; KEPLER_MODE="$2"; shift ;;
    --skip-kepler) SKIP_KEPLER=1 ;;
    --skip-prometheus) SKIP_PROMETHEUS=1 ;;
    --skip-k8s) SKIP_K8S=1 ;;
    --skip-clue) SKIP_CLUE=1 ;;
    --strict) STRICT=1 ;;
    --strict-clue) STRICT_CLUE=1 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown option: $1" >&2; usage; exit 2 ;;
  esac
  shift
done

if [[ -t 1 ]]; then
  C_RESET=$'\033[0m'; C_GREEN=$'\033[1;32m'; C_RED=$'\033[1;31m'
  C_YELLOW=$'\033[1;33m'; C_BLUE=$'\033[1;34m'
else
  C_RESET=""; C_GREEN=""; C_RED=""; C_YELLOW=""; C_BLUE=""
fi

PASS=0; FAIL=0; WARN=0; SKIP=0
PASSED_NAMES=(); FAILED_NAMES=(); WARNED_NAMES=()

pass() { PASS=$((PASS+1)); PASSED_NAMES+=("$1"); printf '%s[PASS]%s %-38s %s\n' "$C_GREEN" "$C_RESET" "$1" "${2:-}"; }
fail() { FAIL=$((FAIL+1)); FAILED_NAMES+=("$1"); printf '%s[FAIL]%s %-38s %s\n' "$C_RED" "$C_RESET" "$1" "${2:-}"; }
warn() {
  if [[ "$STRICT" -eq 1 ]]; then fail "$1" "$2"; else
    WARN=$((WARN+1)); WARNED_NAMES+=("$1")
    printf '%s[WARN]%s %-38s %s\n' "$C_YELLOW" "$C_RESET" "$1" "${2:-}"
  fi
}
skip() { SKIP=$((SKIP+1)); printf '%s[SKIP]%s %-38s %s\n' "$C_BLUE" "$C_RESET" "$1" "${2:-}"; }
section() { printf '\n%s== %s ==%s\n' "$C_BLUE" "$1" "$C_RESET"; }

have() { command -v "$1" >/dev/null 2>&1; }

# Re-exec with sudo for privileged counter reads when possible.
if [[ "${EUID}" -ne 0 ]] && have sudo && sudo -n true 2>/dev/null; then
  exec sudo -E bash "$SCRIPT_PATH" "${ORIG_ARGS[@]}"
fi

mkdir -p /var/log/susco 2>/dev/null || true
LOG_FILE="/var/log/susco/verify-$(date +%Y%m%d-%H%M%S).log"
if [[ -w /var/log/susco ]]; then
  exec > >(tee -a "$LOG_FILE") 2>&1
  echo "Verification log: $LOG_FILE"
fi

printf '%sSusCo Linux verification%s\n' "$C_BLUE" "$C_RESET"
printf 'Date: %s\nHost: %s\nKernel: %s\nInstall dir: %s\n' "$(date -Is)" "$(hostname)" "$(uname -r)" "$INSTALL_DIR"

# ---------------------------------------------------------------------------
section "Host / kernel"
# ---------------------------------------------------------------------------
if [[ -r /etc/os-release ]]; then
  # shellcheck disable=SC1091
  . /etc/os-release
  case "${ID:-}" in
    ubuntu|debian) pass "OS" "${PRETTY_NAME:-$ID}" ;;
    *) warn "OS" "expected Ubuntu/Debian, found ${ID:-unknown}" ;;
  esac
else
  fail "OS" "/etc/os-release missing"
fi

case "$(uname -m)" in
  x86_64) pass "Architecture" "$(uname -m) (Kepler-supported)" ;;
  aarch64|arm64) warn "Architecture" "$(uname -m) (Kepler 0.13.0 artifacts are amd64-only)" ;;
  *) warn "Architecture" "$(uname -m)" ;;
esac

if have jq; then pass "jq" "$(jq --version 2>/dev/null)"
else fail "jq" "not installed"; fi

VIRT="$(systemd-detect-virt 2>/dev/null || echo unknown)"
if [[ "$VIRT" == "none" ]]; then pass "Bare metal" "no virtualization detected"
else warn "Bare metal" "virtualized: $VIRT (RAPL may be hidden)"; fi

if [[ "$(stat -fc %T /sys/fs/cgroup 2>/dev/null || true)" == "cgroup2fs" ]]; then
  pass "cgroup v2" "unified hierarchy"
else
  warn "cgroup v2" "not detected"
fi

# ---------------------------------------------------------------------------
section "Energy counters (RAPL)"
# ---------------------------------------------------------------------------
if [[ -d /sys/module/msr ]]; then pass "msr kernel module" "loaded"
else warn "msr kernel module" "not loaded (modprobe msr)"; fi

domains=()
for f in /sys/class/powercap/*-rapl:*/name; do
  [[ -r "$f" ]] || continue
  domains+=("$(basename "$(dirname "$f")")=$(cat "$f" 2>/dev/null || true)")
done
if [[ ${#domains[@]} -gt 0 ]]; then
  pass "RAPL domains" "${domains[*]}"
else
  fail "RAPL domains" "none under /sys/class/powercap"
fi

rapl_ok=0
rapl_detail="no readable energy_uj"
if [[ "${EUID}" -eq 0 ]]; then
  for f in /sys/class/powercap/*-rapl:*/energy_uj; do
    [[ -r "$f" ]] || continue
    a="$(cat "$f" 2>/dev/null || echo x)"
    sleep 0.5
    b="$(cat "$f" 2>/dev/null || echo y)"
    if [[ "$a" =~ ^[0-9]+$ && "$b" =~ ^[0-9]+$ ]] && (( b > a )); then
      rapl_ok=1
      rapl_detail="$(basename "$(dirname "$f")") increased ${a} -> ${b} uJ"
      break
    fi
  done
else
  skip "RAPL counter increment" "run as root to verify energy_uj"
fi
if [[ "${EUID}" -eq 0 ]]; then
  if [[ "$rapl_ok" -eq 1 ]]; then pass "RAPL counter increment" "$rapl_detail"
  else fail "RAPL counter increment" "$rapl_detail"; fi
fi

hwmon_found=0
for f in /sys/class/hwmon/hwmon*/power*_input /sys/class/hwmon/hwmon*/energy*_input; do
  [[ -r "$f" ]] && { hwmon_found=1; break; }
done
if [[ "$hwmon_found" -eq 1 ]]; then warn "hwmon sensors" "present; usable as fallback, less precise than RAPL"
else skip "hwmon sensors" "none"; fi

# ---------------------------------------------------------------------------
section "CPU stability"
# ---------------------------------------------------------------------------
govs="$(cat /sys/devices/system/cpu/cpu[0-9]*/cpufreq/scaling_governor 2>/dev/null | sort -u | tr '\n' ' ' || true)"
if [[ -z "$govs" ]]; then skip "CPU governor" "cpufreq not exposed"
elif [[ "$govs" == "performance " ]]; then pass "CPU governor" "performance"
else warn "CPU governor" "found: $govs"; fi

if [[ -r /sys/devices/system/cpu/intel_pstate/no_turbo ]]; then
  if [[ "$(cat /sys/devices/system/cpu/intel_pstate/no_turbo)" == "1" ]]; then pass "Intel turbo" "disabled"
  else warn "Intel turbo" "enabled (writes to no_turbo=1 for stability)"; fi
elif [[ -r /sys/devices/system/cpu/cpufreq/boost ]]; then
  if [[ "$(cat /sys/devices/system/cpu/cpufreq/boost)" == "0" ]]; then pass "CPU boost" "disabled"
  else warn "CPU boost" "enabled"; fi
else
  skip "Turbo/boost control" "not exposed"
fi

# ---------------------------------------------------------------------------
section "Containers / Kubernetes"
# ---------------------------------------------------------------------------
if have docker && docker info >/dev/null 2>&1; then pass "Docker" "$(docker --version)"
else fail "Docker" "daemon not reachable"; fi

if [[ "$SKIP_K8S" -eq 1 ]]; then
  skip "Kubernetes" "skipped by flag"
else
if have kubectl; then pass "kubectl" "$(kubectl version --client=true --output=yaml 2>/dev/null | awk '/gitVersion/{print $2; exit}')"
else fail "kubectl" "not installed"; fi

if have helm; then pass "Helm" "$(helm version --short 2>/dev/null)"
else fail "Helm" "not installed"; fi

if have kind; then pass "kind" "$(kind --version 2>/dev/null)"
else fail "kind" "not installed"; fi

if have kind && kind get clusters 2>/dev/null | grep -qx "$CLUSTER_NAME"; then
  pass "kind cluster" "$CLUSTER_NAME exists"
  if kubectl --context "kind-${CLUSTER_NAME}" cluster-info >/dev/null 2>&1; then
    pass "kubectl context" "kind-${CLUSTER_NAME} reachable"
  else
    fail "kubectl context" "kind-${CLUSTER_NAME} unreachable"
  fi
  ready="$(kubectl --context "kind-${CLUSTER_NAME}" get nodes \
    -o jsonpath='{range .items[*]}{.metadata.name}={.status.conditions[?(@.type=="Ready")].status}{"\n"}{end}' 2>/dev/null || true)"
  if [[ -n "$ready" ]] && ! grep -qv '=True' <<<"$ready"; then pass "Kubernetes nodes" "$(tr '\n' ' ' <<<"$ready")"
  else fail "Kubernetes nodes" "${ready:-no nodes}"; fi
else
  fail "kind cluster" "'$CLUSTER_NAME' not found"
fi
fi

# ---------------------------------------------------------------------------
section "Kepler"
# ---------------------------------------------------------------------------
if [[ "$SKIP_KEPLER" -eq 1 ]]; then
  skip "Kepler" "skipped by flag"
  skip "Kepler CPU energy" "skipped by flag"
else
if [[ "$KEPLER_MODE" == "host" ]]; then
  if systemctl is-active --quiet kepler 2>/dev/null; then pass "Kepler service" "active (host mode)"
  else fail "Kepler service" "kepler.service not active"; fi
else
  ds_name="$(kubectl --context "kind-${CLUSTER_NAME}" -n kepler get ds \
    -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || true)"
  if [[ -n "$ds_name" ]]; then
    dsn="$(kubectl --context "kind-${CLUSTER_NAME}" -n kepler get ds "$ds_name" \
      -o jsonpath='{.status.numberReady}/{.status.desiredNumberScheduled}' 2>/dev/null || true)"
    if [[ "$dsn" == */* && "${dsn%%/*}" == "${dsn##*/}" && "${dsn%%/*}" != "0" ]]; then
      pass "Kepler DaemonSet" "$ds_name $dsn ready"
    else
      fail "Kepler DaemonSet" "$ds_name $dsn ready"
    fi
  else
    fail "Kepler DaemonSet" "not installed in namespace kepler"
  fi
fi

kepler_metrics=/tmp/susco-kepler-metrics-1.$$
if curl -fsS --max-time 10 http://127.0.0.1:28282/metrics -o "$kepler_metrics" 2>/dev/null; then
  pass "Kepler endpoint" "http://127.0.0.1:28282/metrics"
  if grep -q '^kepler_node_cpu_joules_total' "$kepler_metrics"; then
    j1="$(awk '$1 ~ /^kepler_node_cpu_joules_total/ {s+=$2} END{printf "%.6f", s+0}' "$kepler_metrics")"
    sleep 3
    curl -fsS --max-time 10 http://127.0.0.1:28282/metrics -o "${kepler_metrics}.2" 2>/dev/null || true
    j2="$(awk '$1 ~ /^kepler_node_cpu_joules_total/ {s+=$2} END{printf "%.6f", s+0}' "${kepler_metrics}.2" 2>/dev/null || echo 0)"
    if awk -v a="$j1" -v b="$j2" 'BEGIN{exit !(b>a)}'; then
      pass "Kepler CPU energy" "joules increased ${j1} -> ${j2}"
    else
      fail "Kepler CPU energy" "joules did not increase (${j1} -> ${j2}); RAPL not visible?"
    fi
  else
    fail "Kepler CPU energy" "kepler_node_cpu_joules_total missing"
  fi
else
  fail "Kepler endpoint" "cannot scrape http://127.0.0.1:28282/metrics"
fi
rm -f "$kepler_metrics" "${kepler_metrics}.2"
fi

# ---------------------------------------------------------------------------
section "Prometheus"
# ---------------------------------------------------------------------------
if [[ "$SKIP_PROMETHEUS" -eq 1 ]]; then
  skip "Prometheus" "skipped by flag"
else
if curl -fsS --max-time 5 http://127.0.0.1:9090/-/ready >/dev/null 2>&1; then
  pass "Prometheus endpoint" "http://127.0.0.1:9090"
  up="$(curl -fsS --get --max-time 5 \
    --data-urlencode 'query=up{job="kepler"}' \
    http://127.0.0.1:9090/api/v1/query 2>/dev/null \
    | jq -r '.data.result[0].value[1] // empty' 2>/dev/null || true)"
  if [[ "$up" == "1" ]]; then pass "Prometheus -> Kepler" "target up"
  else fail "Prometheus -> Kepler" "target not up (got '${up:-none}')"; fi

  q='sum(kepler_node_cpu_joules_total)'
  v1="$(curl -fsS --get --max-time 5 --data-urlencode "query=$q" http://127.0.0.1:9090/api/v1/query 2>/dev/null \
    | jq -r '.data.result[0].value[1] // empty' 2>/dev/null || true)"
  sleep 3
  v2="$(curl -fsS --get --max-time 5 --data-urlencode "query=$q" http://127.0.0.1:9090/api/v1/query 2>/dev/null \
    | jq -r '.data.result[0].value[1] // empty' 2>/dev/null || true)"
  if [[ -n "$v1" && -n "$v2" ]] && awk -v a="$v1" -v b="$v2" 'BEGIN{exit !(b>a)}'; then
    pass "Prometheus energy query" "increased ${v1} -> ${v2}"
  else
    fail "Prometheus energy query" "no increasing series (${v1:-none} -> ${v2:-none})"
  fi
else
  fail "Prometheus endpoint" "http://127.0.0.1:9090 not ready"
fi
fi

# ---------------------------------------------------------------------------
section "Ollama / model"
# ---------------------------------------------------------------------------
if have ollama; then pass "Ollama binary" "$(ollama --version 2>/dev/null || echo installed)"
else fail "Ollama binary" "not installed"; fi

if systemctl is-active --quiet ollama 2>/dev/null; then pass "Ollama service" "active"
else fail "Ollama service" "ollama.service not active"; fi

if curl -fsS --max-time 5 http://127.0.0.1:11434/api/tags >/dev/null 2>&1; then
  pass "Ollama API" "http://127.0.0.1:11434"
else
  fail "Ollama API" "not reachable"
fi

if have ollama && ollama list 2>/dev/null | awk 'NR>1{print $1}' | grep -qx "$MODEL"; then
  pass "Model present" "$MODEL"
else
  fail "Model present" "$MODEL not found"
fi

envdump="$(systemctl show ollama -p Environment 2>/dev/null || true)"
if grep -q 'OLLAMA_NUM_PARALLEL=1' <<<"$envdump"; then pass "Ollama serialization" "NUM_PARALLEL=1"
else warn "Ollama serialization" "NUM_PARALLEL=1 not set; energy attribution may be ambiguous"; fi
if grep -q 'OLLAMA_KEEP_ALIVE=-1' <<<"$envdump"; then pass "Ollama keep-alive" "KEEP_ALIVE=-1"
else warn "Ollama keep-alive" "KEEP_ALIVE=-1 not set; model may unload between runs"; fi

if curl -fsS --max-time 5 http://127.0.0.1:11434/api/tags >/dev/null 2>&1; then
  prompt='What is 17*23? End with #### <number>.'
  payload_false="$(jq -n --arg m "$MODEL" --arg p "$prompt" \
    '{model:$m,messages:[{role:"user",content:$p}],think:false,stream:false,options:{temperature:0,num_predict:256,num_gpu:0}}')"
  resp_false="$(curl -fsS --max-time 300 http://127.0.0.1:11434/api/chat \
    -H 'Content-Type: application/json' -d "$payload_false" 2>/dev/null || true)"
  if [[ "$(jq -r '.done // false' <<<"$resp_false" 2>/dev/null)" == "true" ]]; then
    eval_f="$(jq -r '.eval_count // 0' <<<"$resp_false")"
    think_f="$(jq -r '(.message.thinking // "") | length' <<<"$resp_false")"
    content_f="$(jq -r '(.message.content // "") | length' <<<"$resp_false")"
    if [[ "$eval_f" -gt 0 && "$content_f" -gt 0 ]]; then
      pass "think=false inference" "eval_count=$eval_f answer_chars=$content_f"
    else
      fail "think=false inference" "empty response"
    fi
    if [[ "$think_f" -eq 0 ]]; then pass "think=false suppression" "no thinking emitted"
    else warn "think=false suppression" "$think_f thinking chars emitted (check Ollama think support)"; fi

    ps_out="$(ollama ps 2>/dev/null || true)"
    if grep -q '100% CPU' <<<"$ps_out"; then
      pass "Ollama CPU-only" "ollama ps reports 100% CPU"
    else
      warn "Ollama CPU-only" "could not confirm CPU execution${ps_out:+: $(tr '\n' ' ' <<<"$ps_out")}"
    fi
  else
    fail "think=false inference" "request failed"
  fi

  payload_true="$(jq -n --arg m "$MODEL" --arg p "$prompt" \
    '{model:$m,messages:[{role:"user",content:$p}],think:true,stream:false,options:{temperature:0,num_predict:512,num_gpu:0}}')"
  resp_true="$(curl -fsS --max-time 600 http://127.0.0.1:11434/api/chat \
    -H 'Content-Type: application/json' -d "$payload_true" 2>/dev/null || true)"
  if [[ "$(jq -r '.done // false' <<<"$resp_true" 2>/dev/null)" == "true" ]]; then
    eval_t="$(jq -r '.eval_count // 0' <<<"$resp_true")"
    think_t="$(jq -r '(.message.thinking // "") | length' <<<"$resp_true")"
    if [[ "$eval_t" -gt 0 && "$think_t" -gt 0 ]]; then
      pass "think=true inference" "eval_count=$eval_t thinking_chars=$think_t"
    else
      warn "think=true inference" "eval_count=$eval_t thinking_chars=$think_t (thinking may be disabled)"
    fi
  else
    fail "think=true inference" "request failed"
  fi
fi

# ---------------------------------------------------------------------------
section "Python / analysis"
# ---------------------------------------------------------------------------
if [[ -x "$INSTALL_DIR/.venv/bin/python" ]]; then
  if "$INSTALL_DIR/.venv/bin/python" -c 'import pandas, numpy, scipy, matplotlib, seaborn, yaml, requests' >/dev/null 2>&1; then
    pass "Analysis deps" "pandas/numpy/scipy/matplotlib/seaborn ok"
  else
    fail "Analysis deps" "one or more imports failed"
  fi
  if "$INSTALL_DIR/.venv/bin/python" -c 'import susco' >/dev/null 2>&1; then
    pass "susco package" "importable"
  else
    warn "susco package" "not importable (expected before the package is implemented)"
  fi
  if "$INSTALL_DIR/.venv/bin/python" -c 'import locust' >/dev/null 2>&1; then
    pass "Locust" "$("$INSTALL_DIR/.venv/bin/python" -c 'import locust; print(locust.__version__)' 2>/dev/null)"
  else
    fail "Locust" "not importable from project venv"
  fi
else
  fail "Project venv" "$INSTALL_DIR/.venv missing"
fi

CLUE_DIR="$INSTALL_DIR/vendor/Clue"
if [[ "$SKIP_CLUE" -eq 1 ]]; then
  skip "CLUE" "skipped by flag"
elif [[ -d "$CLUE_DIR/.git" ]]; then
  pass "CLUE checkout" "$CLUE_DIR"
  if [[ -x /opt/susco-tools/bin/pipenv ]]; then
    clue_py="$(cd "$CLUE_DIR" && PIPENV_IGNORE_VIRTUALENVS=1 /opt/susco-tools/bin/pipenv --venv 2>/dev/null || true)"
    if [[ -n "$clue_py" && -x "$clue_py/bin/python" ]]; then
      if "$clue_py/bin/python" -c 'import kubernetes, locust, scipy, seaborn' >/dev/null 2>&1; then
        pass "CLUE deps" "kubernetes/locust/scipy/seaborn ok"
      else
        fail "CLUE deps" "core imports failed"
      fi
      if "$clue_py/bin/python" -c 'import psc' >/dev/null 2>&1; then
        pass "CLUE psc" "energy tracker importable"
      elif [[ "$STRICT_CLUE" -eq 1 ]]; then
        fail "CLUE psc" "import failed (private TU-Berlin package)"
      else
        warn "CLUE psc" "import failed (private TU-Berlin package)"
      fi
    else
      fail "CLUE venv" "pipenv environment missing"
    fi
  else
    fail "pipenv" "not installed"
  fi
else
  fail "CLUE checkout" "$CLUE_DIR missing"
fi

# ---------------------------------------------------------------------------
section "Capacity / noise"
# ---------------------------------------------------------------------------
avail_gb="$(df -Pk /var/lib 2>/dev/null | awk 'NR==2{printf "%d", $4/1024/1024}')"
if [[ -n "$avail_gb" ]]; then
  if (( avail_gb < 5 )); then fail "Disk space" "${avail_gb} GiB free (<5 GiB)"
  elif (( avail_gb < 20 )); then warn "Disk space" "${avail_gb} GiB free (<20 GiB recommended)"
  else pass "Disk space" "${avail_gb} GiB free"; fi
fi

mem_gb="$(awk '/MemTotal/{printf "%d", $2/1024/1024}' /proc/meminfo 2>/dev/null || echo 0)"
if (( mem_gb > 0 )); then
  if (( mem_gb < 8 )); then warn "Memory" "${mem_gb} GiB (<8 GiB recommended for kind+Prometheus+model)"
  else pass "Memory" "${mem_gb} GiB"; fi
fi

load1="$(awk '{print $1}' /proc/loadavg 2>/dev/null || echo 0)"
ncpu="$(nproc 2>/dev/null || echo 1)"
if awk -v l="$load1" -v n="$ncpu" 'BEGIN{exit !(l > n*0.5)}'; then
  warn "Background load" "load1=$load1 on $ncpu CPUs; close other workloads for clean measurements"
else
  pass "Background load" "load1=$load1 on $ncpu CPUs"
fi

# ---------------------------------------------------------------------------
section "Summary"
# ---------------------------------------------------------------------------
printf 'PASS=%d  FAIL=%d  WARN=%d  SKIP=%d\n' "$PASS" "$FAIL" "$WARN" "$SKIP"
if [[ "$FAIL" -gt 0 ]]; then
  printf '\nFailed checks:\n'
  printf '  - %s\n' "${FAILED_NAMES[@]}"
  printf '\nThe stack is NOT fully verified.\n'
  exit 1
fi
if [[ "$WARN" -gt 0 ]]; then
  printf '\nWarnings:\n'
  printf '  - %s\n' "${WARNED_NAMES[@]}"
fi
printf '\nAll required checks passed.\n'
