#!/usr/bin/env bash
#
# SusCo Linux provisioner
# ------------------------
# Installs and configures the full Linux-only measurement stack for the
# Qwen3-1.7B thinking-vs-non-thinking study:
#
#   RAPL -> Kepler -> Prometheus, kind/kubectl/Helm, Docker, Ollama + Qwen3,
#   Python analysis environment, Locust and CLUE.
#
# Accuracy first: this script refuses to silently continue when RAPL is not
# readable, because without hardware energy counters the study cannot answer
# the energy research questions. Run it on a bare-metal Linux host.
#
# Usage:
#   sudo ./scripts/setup_linux.sh [options]
#   sudo ./scripts/setup_linux.sh --verify-only
#   sudo ./scripts/setup_linux.sh --restore-cpu
#
set -Eeuo pipefail
shopt -s nullglob

SCRIPT_PATH="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/$(basename -- "${BASH_SOURCE[0]}")"
PROJECT_DIR="$(cd -- "$(dirname -- "$SCRIPT_PATH")/.." && pwd)"
ORIG_ARGS=("$@")

# ---------------------------------------------------------------------------
# Defaults / CLI
# ---------------------------------------------------------------------------
ASSUME_YES=0
VERIFY_ONLY=0
RESTORE_CPU=0
ALLOW_VIRTUALIZED=0
ALLOW_NO_RAPL=0
ALLOW_UNSUPPORTED_ARCH=0
CPU_TUNING=1
KEPLER_MODE="host"          # host | kind
MODEL="qwen3:1.7b"
OLLAMA_CPUS=""
CLUSTER_NAME="susco"
INSTALL_DIR="$PROJECT_DIR"
SKIP_SYSTEM=0
SKIP_DOCKER=0
SKIP_K8S=0
SKIP_KEPLER=0
SKIP_PROMETHEUS=0
SKIP_OLLAMA=0
SKIP_CLUE=0
SKIP_PYTHON=0
STRICT_CLUE=0

usage() {
  cat <<'EOF'
SusCo Linux provisioner

Options:
  -y, --yes                 Non-interactive (no prompts)
      --verify-only         Do not install; run scripts/verify_linux.sh
      --restore-cpu         Restore CPU governor / turbo state saved earlier
      --allow-virtualized   Allow VMs/containers (RAPL is usually hidden there)
      --allow-no-rapl       Continue even if no readable RAPL counter is found
      --allow-unsupported-arch
                            Continue on non-amd64 (Kepler/Prometheus skipped)
      --no-cpu-tuning       Do not set performance governor or disable turbo
      --kepler-mode MODE    host (default, accurate RAPL) or kind (Helm)
      --model TAG           Ollama model tag (default qwen3:1.7b)
      --ollama-cpus CPUS    systemd CPUAffinity for Ollama, e.g. "0-3"
      --cluster-name NAME   kind cluster name (default susco)
      --install-dir DIR     Project root (default: repo containing this script)
      --skip-system         Skip apt packages
      --skip-docker         Skip Docker
      --skip-k8s            Skip kubectl/Helm/kind/cluster
      --skip-kepler         Skip Kepler
      --skip-prometheus     Skip Prometheus
      --skip-ollama         Skip Ollama and the model download
      --skip-clue           Skip CLUE
      --skip-python         Skip the project Python venv
      --strict-clue         Fail the run if CLUE's dependencies cannot install
  -h, --help                Show this help

Exit codes: 0 success, 1 failure, 2 usage error.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    -y|--yes) ASSUME_YES=1 ;;
    --verify-only) VERIFY_ONLY=1 ;;
    --restore-cpu) RESTORE_CPU=1 ;;
    --allow-virtualized) ALLOW_VIRTUALIZED=1 ;;
    --allow-no-rapl) ALLOW_NO_RAPL=1 ;;
    --allow-unsupported-arch) ALLOW_UNSUPPORTED_ARCH=1 ;;
    --no-cpu-tuning) CPU_TUNING=0 ;;
    --kepler-mode)
      [[ $# -ge 2 ]] || { usage; exit 2; }
      KEPLER_MODE="$2"; shift ;;
    --model)
      [[ $# -ge 2 ]] || { usage; exit 2; }
      MODEL="$2"; shift ;;
    --ollama-cpus)
      [[ $# -ge 2 ]] || { usage; exit 2; }
      OLLAMA_CPUS="$2"; shift ;;
    --cluster-name)
      [[ $# -ge 2 ]] || { usage; exit 2; }
      CLUSTER_NAME="$2"; shift ;;
    --install-dir)
      [[ $# -ge 2 ]] || { usage; exit 2; }
      INSTALL_DIR="$(cd -- "$2" && pwd)"; shift ;;
    --skip-system) SKIP_SYSTEM=1 ;;
    --skip-docker) SKIP_DOCKER=1 ;;
    --skip-k8s) SKIP_K8S=1 ;;
    --skip-kepler) SKIP_KEPLER=1 ;;
    --skip-prometheus) SKIP_PROMETHEUS=1 ;;
    --skip-ollama) SKIP_OLLAMA=1 ;;
    --skip-clue) SKIP_CLUE=1 ;;
    --skip-python) SKIP_PYTHON=1 ;;
    --strict-clue) STRICT_CLUE=1 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown option: $1" >&2; usage; exit 2 ;;
  esac
  shift
done

if [[ "$KEPLER_MODE" != "host" && "$KEPLER_MODE" != "kind" ]]; then
  echo "--kepler-mode must be 'host' or 'kind'" >&2
  exit 2
fi

# ---------------------------------------------------------------------------
# Logging / helpers
# ---------------------------------------------------------------------------
if [[ -t 1 ]]; then
  C_RESET=$'\033[0m'; C_BLUE=$'\033[1;34m'; C_GREEN=$'\033[1;32m'
  C_YELLOW=$'\033[1;33m'; C_RED=$'\033[1;31m'
else
  C_RESET=""; C_BLUE=""; C_GREEN=""; C_YELLOW=""; C_RED=""
fi
log()  { printf '%s[setup]%s %s\n' "$C_BLUE" "$C_RESET" "$*"; }
ok()   { printf '%s[ ok  ]%s %s\n' "$C_GREEN" "$C_RESET" "$*"; }
warn() { printf '%s[warn ]%s %s\n' "$C_YELLOW" "$C_RESET" "$*" >&2; }
die()  { printf '%s[error]%s %s\n' "$C_RED" "$C_RESET" "$*" >&2; exit 1; }

trap 'die "failed at line ${LINENO}: ${BASH_COMMAND}"' ERR

confirm() {
  [[ "$ASSUME_YES" -eq 1 ]] && return 0
  local reply
  read -r -p "$1 [y/N] " reply
  [[ "$reply" =~ ^[Yy]$ ]]
}

STATE_DIR="/var/lib/susco"
LOG_DIR="/var/log/susco"
CONF_DIR="/etc/susco"

# Re-exec as root after argument parsing.
if [[ "${EUID}" -ne 0 ]]; then
  log "root privileges are required; re-running with sudo"
  exec sudo -E bash "$SCRIPT_PATH" "${ORIG_ARGS[@]}"
fi

mkdir -p "$STATE_DIR" "$LOG_DIR" "$CONF_DIR"

TARGET_USER="${SUDO_USER:-}"
if [[ -z "$TARGET_USER" || "$TARGET_USER" == "root" ]]; then
  TARGET_USER=""
fi

APT_UPDATED=0
apt_update() {
  [[ "$APT_UPDATED" -eq 1 ]] && return 0
  DEBIAN_FRONTEND=noninteractive apt-get update -y
  APT_UPDATED=1
}
apt_install() {
  apt_update
  DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends "$@"
}

download() {
  local url="$1" dest="$2"
  curl -fL --retry 3 --retry-delay 2 -o "$dest" "$url"
}

# ---------------------------------------------------------------------------
# Environment detection
# ---------------------------------------------------------------------------
detect_platform() {
  [[ -r /etc/os-release ]] || die "/etc/os-release not found; unsupported OS"
  # shellcheck disable=SC1091
  . /etc/os-release
  case "${ID:-}" in
    ubuntu|debian) ;;
    *) die "This provisioner supports Ubuntu/Debian only (found '${ID:-unknown}')." ;;
  esac
  log "Detected ${PRETTY_NAME:-$ID}"

  case "$(uname -m)" in
    x86_64) ARCH="amd64" ;;
    aarch64|arm64) ARCH="arm64" ;;
    *) die "Unsupported architecture: $(uname -m)" ;;
  esac
  log "Architecture: $(uname -m) (${ARCH})"

  if [[ "$ARCH" != "amd64" ]]; then
    if [[ "$ALLOW_UNSUPPORTED_ARCH" -eq 1 ]]; then
      warn "Kepler 0.13.0 publishes amd64-only host binaries and container images."
      warn "Skipping Kepler and Prometheus on ${ARCH}; energy measurement is not available."
      SKIP_KEPLER=1
      SKIP_PROMETHEUS=1
    else
      cat >&2 <<EOF
Accurate energy measurement requires Kepler, and Kepler 0.13.0 publishes only
amd64 host binaries and container images. This host is ${ARCH}.

Use an x86_64 bare-metal Linux host for the energy study. If you only want the
rest of the stack (Ollama, kind, CLUE, Python), re-run with:
  --allow-unsupported-arch
Note that ARM64 Linux hosts usually do not expose RAPL counters anyway.
EOF
      exit 1
    fi
  fi

  local virt
  # systemd-detect-virt exits 1 on bare metal (no virtualization detected) but
  # still prints "none"; use `|| true` so the fallback does not append to it.
  virt="$(systemd-detect-virt 2>/dev/null || true)"
  [[ -n "$virt" ]] || virt="unknown"
  if [[ "$virt" != "none" ]]; then
    if [[ "$ALLOW_VIRTUALIZED" -eq 1 ]]; then
      warn "Running inside '${virt}'. RAPL is usually hidden in VMs/containers; measurements may be invalid."
    else
      die "Virtualized environment detected (${virt}). RAPL is usually unavailable. Use --allow-virtualized to override."
    fi
  else
    ok "Bare-metal Linux detected"
  fi

  if [[ "$(stat -fc %T /sys/fs/cgroup 2>/dev/null || true)" != "cgroup2fs" ]]; then
    warn "cgroup v2 not detected; Kepler/container attribution may be degraded."
  else
    ok "cgroup v2 detected"
  fi
}

# ---------------------------------------------------------------------------
# RAPL / hardware counters
# ---------------------------------------------------------------------------
rapl_domains() {
  local f
  for f in /sys/class/powercap/*-rapl:*/name; do
    [[ -r "$f" ]] && printf '%s=%s\n' "$(basename "$(dirname "$f")")" "$(cat "$f" 2>/dev/null || true)"
  done
}

rapl_has_increment() {
  local f a b
  for f in /sys/class/powercap/*-rapl:*/energy_uj; do
    [[ -r "$f" ]] || continue
    a="$(cat "$f" 2>/dev/null || echo 0)"
    sleep 0.4
    b="$(cat "$f" 2>/dev/null || echo 0)"
    if [[ "$b" =~ ^[0-9]+$ && "$a" =~ ^[0-9]+$ ]] && (( b > a )); then
      return 0
    fi
  done
  return 1
}

hwmon_present() {
  local f
  for f in /sys/class/hwmon/hwmon*/power*_input /sys/class/hwmon/hwmon*/energy*_input; do
    [[ -r "$f" ]] && return 0
  done
  return 1
}

setup_rapl() {
  log "Configuring hardware energy counters (RAPL)"
  modprobe msr 2>/dev/null || warn "could not load 'msr' module"
  modprobe intel_rapl_common 2>/dev/null || true
  modprobe intel_rapl_msr 2>/dev/null || true
  modprobe amd_energy 2>/dev/null || true

  local domains
  domains="$(rapl_domains || true)"
  if [[ -n "$domains" ]]; then
    ok "RAPL domains:"
    while IFS= read -r domain; do
      printf '        %s\n' "$domain"
    done <<<"$domains"
  else
    warn "No powercap RAPL domains found under /sys/class/powercap"
  fi

  if rapl_has_increment; then
    ok "RAPL energy counters are readable and increasing"
    RAPL_OK=1
  else
    RAPL_OK=0
    if hwmon_present; then
      warn "RAPL counters did not increase, but hwmon power/energy sensors exist; Kepler will use hwmon."
    fi
    if [[ "$ALLOW_NO_RAPL" -eq 1 ]]; then
      warn "Continuing without a verified RAPL counter (--allow-no-rapl). Energy results may be invalid."
    else
      cat >&2 <<EOF
No readable/increasing RAPL counter was found.

Accurate energy measurement requires hardware counters. Common causes:
  * running in a VM/container (use bare metal)
  * RAPL disabled in BIOS or by the hypervisor
  * kernel modules not available for this CPU
  * 'energy_uj' is not readable (this script runs as root, so it is not a chmod issue)

Inspect manually:
  cat /sys/class/powercap/*-rapl:*/name
  cat /sys/class/powercap/*-rapl:*/energy_uj

If this host truly has no RAPL but you accept invalid energy data, re-run with
  --allow-no-rapl
EOF
      exit 1
    fi
  fi

  if [[ -w /sys/devices/system/cpu/intel_pstate/no_turbo ]]; then
    ok "Intel RAPL + intel_pstate detected"
  fi
}

# ---------------------------------------------------------------------------
# CPU tuning for measurement stability
# ---------------------------------------------------------------------------
save_cpu_state() {
  [[ -f "$STATE_DIR/cpu-state.tsv" ]] && return 0
  : > "$STATE_DIR/cpu-state.tsv"
  local f
  for f in /sys/devices/system/cpu/cpu[0-9]*/cpufreq/scaling_governor \
           /sys/devices/system/cpu/intel_pstate/no_turbo \
           /sys/devices/system/cpu/cpufreq/boost; do
    [[ -r "$f" ]] || continue
    printf '%s\t%s\n' "$f" "$(cat "$f" 2>/dev/null || true)" >> "$STATE_DIR/cpu-state.tsv"
  done
  ok "Saved original CPU state to $STATE_DIR/cpu-state.tsv"
}

apply_cpu_tuning() {
  log "Applying performance governor and disabling turbo for stable measurements"
  save_cpu_state

  if command -v cpupower >/dev/null 2>&1; then
    cpupower frequency-set -g performance >/dev/null 2>&1 || warn "cpupower failed; falling back to sysfs"
  fi

  local f
  for f in /sys/devices/system/cpu/cpu[0-9]*/cpufreq/scaling_governor; do
    [[ -w "$f" ]] && echo performance > "$f" 2>/dev/null || true
  done
  for f in /sys/devices/system/cpu/intel_pstate/no_turbo /sys/devices/system/cpu/cpufreq/boost; do
    [[ -w "$f" ]] || continue
    if [[ "$f" == *no_turbo ]]; then echo 1 > "$f" 2>/dev/null || true
    else echo 0 > "$f" 2>/dev/null || true; fi
  done

  local governors
  governors="$(cat /sys/devices/system/cpu/cpu[0-9]*/cpufreq/scaling_governor 2>/dev/null | sort -u | tr '\n' ' ' || true)"
  ok "CPU governors now: ${governors:-n/a}"
}

restore_cpu_state() {
  local f v
  if [[ ! -r "$STATE_DIR/cpu-state.tsv" ]]; then
    warn "No saved CPU state found at $STATE_DIR/cpu-state.tsv"
    return 0
  fi
  while IFS=$'\t' read -r f v; do
    [[ -w "$f" ]] && printf '%s\n' "$v" > "$f" 2>/dev/null || true
  done < "$STATE_DIR/cpu-state.tsv"
  rm -f "$STATE_DIR/cpu-state.tsv"
  ok "Restored original CPU governor/turbo state"
}

# ---------------------------------------------------------------------------
# System packages
# ---------------------------------------------------------------------------
install_system_packages() {
  log "Installing base packages"
  apt_install \
    ca-certificates curl gnupg lsb-release git jq tar gzip unzip xz-utils \
    build-essential bc make pciutils procps util-linux iproute2 \
    python3 python3-venv python3-dev python3-pip \
    msr-tools kmod
  apt_install linux-tools-common linux-tools-generic || warn "linux-tools packages unavailable; using sysfs fallback"
  ok "Base packages installed"

  local need_py311=0
  if ! python3 -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)' 2>/dev/null; then
    need_py311=1
  fi
  if [[ "$need_py311" -eq 1 || "$SKIP_CLUE" -eq 0 ]]; then
    if ! command -v python3.11 >/dev/null 2>&1; then
      log "Installing Python 3.11 (required by the project and CLUE)"
      if [[ "${ID:-}" == "ubuntu" ]]; then
        apt_install software-properties-common
        add-apt-repository -y ppa:deadsnakes/ppa >/dev/null 2>&1 || warn "could not add deadsnakes PPA"
        apt_update
        apt_install python3.11 python3.11-venv python3.11-dev || warn "python3.11 install failed"
      else
        warn "python3.11 unavailable; the project needs Python >= 3.11"
      fi
    fi
  fi
  if [[ "$need_py311" -eq 1 ]] && ! command -v python3.11 >/dev/null 2>&1; then
    die "Python >= 3.11 is required. Install python3.11/python3.11-venv (Ubuntu 22.04 needs the deadsnakes PPA) or use Ubuntu 24.04/Debian 12+."
  fi
}

# ---------------------------------------------------------------------------
# Docker / kubectl / Helm / kind
# ---------------------------------------------------------------------------
install_docker() {
  if command -v docker >/dev/null 2>&1; then
    ok "Docker already installed: $(docker --version)"
  else
    log "Installing Docker"
    download https://get.docker.com /tmp/get-docker.sh
    sh /tmp/get-docker.sh >/dev/null
    rm -f /tmp/get-docker.sh
  fi
  systemctl enable --now docker
  docker info >/dev/null 2>&1 || die "Docker daemon is not responding"
  if [[ -n "$TARGET_USER" ]]; then
    usermod -aG docker "$TARGET_USER" 2>/dev/null || true
  fi
  ok "Docker is running"
}

install_kubectl() {
  local version
  mkdir -p /usr/local/bin
  version="$(curl -fsSL https://dl.k8s.io/release/stable.txt)"
  log "Installing kubectl ${version}"
  download "https://dl.k8s.io/release/${version}/bin/linux/${ARCH}/kubectl" /usr/local/bin/kubectl
  chmod +x /usr/local/bin/kubectl
  ok "kubectl: $(kubectl version --client=true --output=yaml 2>/dev/null | awk '/gitVersion/{print $2; exit}')"
}

install_helm() {
  if command -v helm >/dev/null 2>&1; then
    ok "Helm already installed: $(helm version --short)"
    return
  fi
  log "Installing Helm"
  download https://raw.githubusercontent.com/helm/helm/main/scripts/get-helm-3 /tmp/get-helm-3
  bash /tmp/get-helm-3 >/dev/null
  rm -f /tmp/get-helm-3
  ok "Helm: $(helm version --short)"
}

install_kind() {
  if command -v kind >/dev/null 2>&1; then
    ok "kind already installed: $(kind --version)"
    return
  fi
  local tag
  mkdir -p /usr/local/bin
  tag="$(curl -fsSL https://api.github.com/repos/kubernetes-sigs/kind/releases/latest | jq -r .tag_name 2>/dev/null || true)"
  [[ -n "$tag" && "$tag" != "null" ]] || tag="v0.33.0"
  log "Installing kind ${tag}"
  download "https://kind.sigs.k8s.io/dl/${tag}/kind-linux-${ARCH}" /usr/local/bin/kind
  chmod +x /usr/local/bin/kind
  local expected actual
  expected="$(curl -fsSL "https://github.com/kubernetes-sigs/kind/releases/download/${tag}/kind-linux-${ARCH}.sha256sum" | awk '{print $1}')"
  actual="$(sha256sum /usr/local/bin/kind | awk '{print $1}')"
  [[ "$expected" == "$actual" ]] || die "kind checksum mismatch"
  ok "kind ${tag} installed and checksum verified"
}

# ---------------------------------------------------------------------------
# kind cluster (with host energy counters exposed, best effort)
# ---------------------------------------------------------------------------
create_kind_config() {
  local cfg="$CONF_DIR/kind-${CLUSTER_NAME}.yaml"
  {
    cat <<EOF
kind: Cluster
apiVersion: kind.x-k8s.io/v1alpha4
nodes:
  - role: control-plane
EOF
    local have_mounts=0
    local mounts=()
    [[ -d /sys/class/powercap ]] && mounts+=("/sys/class/powercap")
    [[ -d /sys/class/hwmon ]] && mounts+=("/sys/class/hwmon")
    [[ -d /dev/cpu ]] && mounts+=("/dev/cpu")
    if [[ ${#mounts[@]} -gt 0 ]]; then
      printf '    extraMounts:\n'
      local m
      for m in "${mounts[@]}"; do
        printf '      - hostPath: %s\n        containerPath: %s\n        readOnly: true\n' "$m" "$m"
        have_mounts=1
      done
    fi
    if [[ "$have_mounts" -eq 0 ]]; then
      warn "No powercap/hwmon/dev/cpu paths found; kind nodes will not see host energy counters."
    fi
  } > "$cfg"
  printf '%s' "$cfg"
}

ensure_kind_cluster() {
  log "Ensuring kind cluster '${CLUSTER_NAME}'"
  if ! kind get clusters 2>/dev/null | grep -qx "$CLUSTER_NAME"; then
    local cfg
    cfg="$(create_kind_config)"
    kind create cluster --name "$CLUSTER_NAME" --config "$cfg" --wait 180s
  else
    ok "kind cluster '${CLUSTER_NAME}' already exists"
  fi
  kubectl --context "kind-${CLUSTER_NAME}" cluster-info >/dev/null
  kubectl --context "kind-${CLUSTER_NAME}" wait --for=condition=Ready node --all --timeout=180s >/dev/null
  ok "kind cluster is ready"
}

# ---------------------------------------------------------------------------
# Ollama + model
# ---------------------------------------------------------------------------
wait_for_ollama() {
  for _ in $(seq 1 90); do
    if curl -fsS http://127.0.0.1:11434/api/tags >/dev/null 2>&1; then
      return 0
    fi
    sleep 1
  done
  return 1
}

install_ollama() {
  if ! command -v ollama >/dev/null 2>&1; then
    log "Installing Ollama"
    download https://ollama.com/install.sh /tmp/ollama-install.sh
    sh /tmp/ollama-install.sh
    rm -f /tmp/ollama-install.sh
  else
    ok "Ollama already installed: $(ollama --version 2>/dev/null || true)"
  fi

  log "Configuring Ollama for serialized, keep-loaded measurements"
  # Ollama has no OLLAMA_NUM_GPU environment variable. CPU-only execution is
  # requested per call by the runner with options.num_gpu=0 (or a CPU-only
  # host simply has no GPU). Here we only pin serialization and keep-alive.
  mkdir -p /etc/systemd/system/ollama.service.d
  {
    printf '[Service]\n'
    printf 'Environment="OLLAMA_KEEP_ALIVE=-1"\n'
    printf 'Environment="OLLAMA_NUM_PARALLEL=1"\n'
    printf 'Environment="OLLAMA_MAX_LOADED_MODELS=1"\n'
    if [[ -n "$OLLAMA_CPUS" ]]; then
      printf 'CPUAffinity=%s\n' "$OLLAMA_CPUS"
    fi
  } > /etc/systemd/system/ollama.service.d/override.conf
  systemctl daemon-reload
  systemctl enable ollama >/dev/null 2>&1
  systemctl restart ollama
  wait_for_ollama || die "Ollama API did not become ready on 127.0.0.1:11434"
  ok "Ollama API is ready"

  log "Downloading model ${MODEL} (this can take a while)"
  ollama pull "$MODEL"
  ollama list | awk 'NR==1 || $1=="'"$MODEL"'"'
  ok "Model ${MODEL} is available"
}

# ---------------------------------------------------------------------------
# Python project + Locust
# ---------------------------------------------------------------------------
install_python_env() {
  [[ -f "$INSTALL_DIR/pyproject.toml" ]] || { warn "No pyproject.toml in $INSTALL_DIR; skipping project venv"; return; }
  local py="python3"
  if ! "$py" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)' 2>/dev/null; then
    py="python3.11"
  fi
  log "Creating project virtualenv with $py"
  "$py" -m venv --clear "$INSTALL_DIR/.venv"
  "$INSTALL_DIR/.venv/bin/python" -m pip install --upgrade pip setuptools wheel >/dev/null
  "$INSTALL_DIR/.venv/bin/pip" install -e "$INSTALL_DIR" >/dev/null
  "$INSTALL_DIR/.venv/bin/pip" install locust >/dev/null
  if [[ -n "$TARGET_USER" ]]; then
    chown -R "$TARGET_USER":"$TARGET_USER" "$INSTALL_DIR/.venv" 2>/dev/null || true
  fi
  ok "Project venv ready at $INSTALL_DIR/.venv"
}

# ---------------------------------------------------------------------------
# CLUE
# ---------------------------------------------------------------------------
install_clue() {
  local clue_dir="$INSTALL_DIR/vendor/Clue"
  local log_file="$LOG_DIR/clue-install.log"
  log "Installing CLUE into $clue_dir (log: $log_file)"
  mkdir -p "$INSTALL_DIR/vendor"
  if [[ ! -d "$clue_dir/.git" ]]; then
    git clone --depth 1 https://github.com/ISE-TU-Berlin/Clue.git "$clue_dir"
  else
    git -C "$clue_dir" pull --ff-only >/dev/null 2>&1 || warn "could not update CLUE checkout"
  fi

  local py="/usr/bin/python3"
  [[ -x /usr/bin/python3.11 ]] && py="/usr/bin/python3.11"
  log "Using $py for CLUE tooling"
  if [[ ! -x /opt/susco-tools/bin/pipenv ]]; then
    "$py" -m venv /opt/susco-tools
    /opt/susco-tools/bin/pip install --upgrade pip pipenv >/dev/null
  fi
  ln -sf /opt/susco-tools/bin/pipenv /usr/local/bin/pipenv

  local rc=0
  (
    cd "$clue_dir"
    PIPENV_IGNORE_VIRTUALENVS=1 /opt/susco-tools/bin/pipenv install --python "$py" --skip-lock
  ) >>"$log_file" 2>&1 || rc=$?

  if [[ "$rc" -ne 0 ]]; then
    if [[ "$STRICT_CLUE" -eq 1 ]]; then
      die "CLUE dependency installation failed; see $log_file"
    fi
    warn "CLUE dependency installation failed (often the private 'psc' package); see $log_file"
  else
    ok "CLUE dependencies installed"
  fi
  if [[ -n "$TARGET_USER" ]]; then
    chown -R "$TARGET_USER":"$TARGET_USER" "$clue_dir" /opt/susco-tools 2>/dev/null || true
  fi
}

# ---------------------------------------------------------------------------
# Kepler
# ---------------------------------------------------------------------------
write_host_kepler_config() {
  local meters="[rapl]"
  if [[ "$RAPL_OK" -ne 1 ]] && hwmon_present; then
    meters="[hwmon]"
  fi
  cat > /etc/susco/kepler-host-config.yaml <<EOF
log:
  level: info
  format: text
host:
  sysfs: /sys
  procfs: /proc
monitor:
  interval: 100ms
  staleness: 100ms
cpu:
  preferredMeters: ${meters}
rapl:
  zones: []
exporter:
  prometheus:
    enabled: true
    metricsLevel:
      - node
web:
  listenAddresses:
    - "127.0.0.1:28282"
kube:
  enabled: false
EOF
}

install_kepler_host() {
  [[ "$ARCH" == "amd64" ]] || die "Kepler 0.13.0 host binaries are amd64-only (this host: ${ARCH})"
  if ! command -v kepler >/dev/null 2>&1; then
    local tag version
    tag="${KEPLER_VERSION:-v0.13.0}"
    [[ "$tag" == v* ]] || tag="v${tag}"
    version="${tag#v}"
    log "Installing Kepler ${tag} (host mode, accurate RAPL)"
    download "https://github.com/sustainable-computing-io/kepler/releases/download/${tag}/kepler-${version}.linux-amd64.tar.gz" /tmp/kepler.tar.gz
    rm -rf /opt/kepler
    mkdir -p /opt/kepler
    tar -xzf /tmp/kepler.tar.gz -C /opt/kepler --strip-components=1
    rm -f /tmp/kepler.tar.gz
    local bin
    bin="$(find /opt/kepler -type f -name kepler | head -n1)"
    [[ -n "$bin" ]] || die "Kepler binary not found in release archive"
    install -m 0755 "$bin" /usr/local/bin/kepler
  fi

  write_host_kepler_config
  cat > /etc/systemd/system/kepler.service <<'EOF'
[Unit]
Description=Kepler (host power exporter for SusCo)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStart=/usr/local/bin/kepler --config.file=/etc/susco/kepler-host-config.yaml
Restart=always
RestartSec=5
LimitNOFILE=65536

[Install]
WantedBy=multi-user.target
EOF
  systemctl disable --now susco-kepler-portforward.service >/dev/null 2>&1 || true
  rm -f /etc/systemd/system/susco-kepler-portforward.service
  systemctl daemon-reload
  systemctl enable --now kepler
  sleep 3
  systemctl is-active --quiet kepler || { journalctl -u kepler -n 80 --no-pager >&2 || true; die "Kepler did not start"; }

  for _ in $(seq 1 30); do
    if curl -fsS http://127.0.0.1:28282/metrics 2>/dev/null | grep -q '^kepler_node_cpu'; then
      ok "Kepler is exposing node CPU energy metrics on :28282"
      return 0
    fi
    sleep 2
  done
  journalctl -u kepler -n 80 --no-pager >&2 || true
  die "Kepler is running but no 'kepler_node_cpu*' metrics appeared. Check RAPL/hwmon visibility."
}

install_kepler_kind() {
  [[ "$ARCH" == "amd64" ]] || die "Kepler 0.13.0 container images are amd64-only (this host: ${ARCH})"
  log "Installing Kepler via Helm into kind (best effort; verify RAPL visibility)"
  cat > /etc/susco/kepler-values.yaml <<'EOF'
daemonset:
  securityContext:
    privileged: true
config:
  log:
    level: info
    format: text
  monitor:
    interval: 100ms
    staleness: 100ms
  cpu:
    preferredMeters: [rapl]
  rapl:
    zones: []
  exporter:
    prometheus:
      enabled: true
      metricsLevel:
        - node
  web:
    listenAddresses:
      - ":28282"
EOF
  helm upgrade --install kepler oci://quay.io/sustainable_computing_io/charts/kepler \
    --version "${KEPLER_VERSION:-0.13.0}" \
    --kube-context "kind-${CLUSTER_NAME}" \
    --namespace kepler --create-namespace \
    -f /etc/susco/kepler-values.yaml \
    --wait --timeout 5m

  local ds_name svc_name
  ds_name="$(kubectl --context "kind-${CLUSTER_NAME}" -n kepler get ds -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || true)"
  [[ -n "$ds_name" ]] || die "Kepler DaemonSet not found in namespace kepler"
  kubectl --context "kind-${CLUSTER_NAME}" -n kepler rollout status "ds/${ds_name}" --timeout=180s
  svc_name="$(kubectl --context "kind-${CLUSTER_NAME}" -n kepler get svc -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || true)"
  [[ -n "$svc_name" ]] || die "Kepler service not found in namespace kepler"

  systemctl disable --now kepler.service >/dev/null 2>&1 || true
  cat > /etc/systemd/system/susco-kepler-portforward.service <<EOF
[Unit]
Description=Port-forward Kepler service for Prometheus
After=network-online.target
Wants=network-online.target

[Service]
ExecStart=/usr/local/bin/kubectl --context kind-${CLUSTER_NAME} -n kepler port-forward svc/${svc_name} 28282:28282 --address 127.0.0.1
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF
  systemctl daemon-reload
  systemctl enable --now susco-kepler-portforward.service
  sleep 3
  for _ in $(seq 1 30); do
    if curl -fsS http://127.0.0.1:28282/metrics 2>/dev/null | grep -q '^kepler_node_cpu'; then
      ok "Kepler (kind) is exposing node CPU energy metrics via port-forward"
      return 0
    fi
    sleep 2
  done
  warn "Kepler (kind) exposes no 'kepler_node_cpu*' metrics. This is expected when kind nodes cannot see host RAPL."
  warn "Use --kepler-mode host for accurate measurements."
  [[ "$ALLOW_NO_RAPL" -eq 1 ]] || die "Kepler (kind) did not report energy metrics"
}

install_kepler() {
  if [[ "$KEPLER_MODE" == "host" ]]; then
    install_kepler_host
  else
    [[ "$SKIP_K8S" -eq 0 ]] || die "--kepler-mode kind requires the kind cluster (do not use --skip-k8s)"
    install_kepler_kind
  fi
}

# ---------------------------------------------------------------------------
# Prometheus
# ---------------------------------------------------------------------------
install_prometheus() {
  log "Installing Prometheus (host network, 1s scrape)"
  mkdir -p /etc/susco/prometheus /var/lib/susco/prometheus
  # The official image runs as nobody (65534); give it ownership of its TSDB.
  chown -R 65534:65534 /var/lib/susco/prometheus
  cat > /etc/susco/prometheus/prometheus.yml <<'EOF'
global:
  scrape_interval: 1s
  evaluation_interval: 1s
scrape_configs:
  - job_name: kepler
    scrape_interval: 1s
    static_configs:
      - targets: ["127.0.0.1:28282"]
  - job_name: prometheus
    static_configs:
      - targets: ["127.0.0.1:9090"]
EOF
  docker rm -f susco-prometheus >/dev/null 2>&1 || true
  docker run -d \
    --name susco-prometheus \
    --restart unless-stopped \
    --network host \
    -v /etc/susco/prometheus:/etc/prometheus:ro \
    -v /var/lib/susco/prometheus:/prometheus \
    prom/prometheus:latest \
    --config.file=/etc/prometheus/prometheus.yml \
    --storage.tsdb.retention.time=30d \
    --web.listen-address=127.0.0.1:9090 >/dev/null
  for _ in $(seq 1 60); do
    if curl -fsS http://127.0.0.1:9090/-/ready >/dev/null 2>&1; then
      ok "Prometheus is ready on http://127.0.0.1:9090"
      return 0
    fi
    sleep 2
  done
  docker logs --tail 80 susco-prometheus >&2 || true
  die "Prometheus did not become ready"
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
run_verify() {
  local args=(
    --install-dir "$INSTALL_DIR"
    --model "$MODEL"
    --cluster-name "$CLUSTER_NAME"
    --kepler-mode "$KEPLER_MODE"
  )
  [[ "$SKIP_KEPLER" -eq 1 ]] && args+=(--skip-kepler)
  [[ "$SKIP_PROMETHEUS" -eq 1 ]] && args+=(--skip-prometheus)
  [[ "$SKIP_K8S" -eq 1 ]] && args+=(--skip-k8s)
  [[ "$SKIP_CLUE" -eq 1 ]] && args+=(--skip-clue)
  exec bash "$INSTALL_DIR/scripts/verify_linux.sh" "${args[@]}"
}

main() {
  log "Install dir: $INSTALL_DIR"
  detect_platform

  if [[ "$SKIP_KEPLER" -eq 1 && "$SKIP_PROMETHEUS" -eq 0 ]]; then
    warn "Kepler is skipped; skipping Prometheus because it has no energy target."
    SKIP_PROMETHEUS=1
  fi

  if [[ "$RESTORE_CPU" -eq 1 ]]; then
    restore_cpu_state
    exit 0
  fi

  if [[ "$VERIFY_ONLY" -eq 1 ]]; then
    run_verify
  fi

  if [[ "$SKIP_SYSTEM" -eq 0 ]]; then
    install_system_packages
  else
    warn "Skipping system packages"
  fi

  setup_rapl

  if [[ "$CPU_TUNING" -eq 1 ]]; then
    apply_cpu_tuning
  else
    warn "CPU tuning disabled; measurement variance will be higher"
  fi

  if [[ "$SKIP_DOCKER" -eq 0 ]]; then install_docker; else warn "Skipping Docker"; fi
  if [[ "$SKIP_K8S" -eq 0 ]]; then
    install_kubectl
    install_helm
    install_kind
    ensure_kind_cluster
  else
    warn "Skipping kubectl/Helm/kind/cluster"
  fi
  if [[ "$SKIP_OLLAMA" -eq 0 ]]; then install_ollama; else warn "Skipping Ollama"; fi
  if [[ "$SKIP_PYTHON" -eq 0 ]]; then install_python_env; else warn "Skipping project venv"; fi
  if [[ "$SKIP_CLUE" -eq 0 ]]; then install_clue; else warn "Skipping CLUE"; fi
  if [[ "$SKIP_KEPLER" -eq 0 ]]; then
    install_kepler
  else
    warn "Skipping Kepler"
  fi
  if [[ "$SKIP_PROMETHEUS" -eq 0 ]]; then
    install_prometheus
  else
    warn "Skipping Prometheus"
  fi

  log "Running verification"
  run_verify
}

main "$@"
