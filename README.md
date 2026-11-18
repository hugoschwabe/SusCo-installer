# SusCo — the cost of thinking in Qwen3-1.7B on GSM8K

SusCo is a local measurement project that answers two questions:

1. **What does thinking mode cost?** How much extra **energy**, how many extra
   **tokens**, and how much extra **latency** does Qwen3 thinking mode use
   compared with non-thinking mode, by difficulty level?
2. **Where does reasoning pay off?** Where does thinking improve **energy per
   correct answer**, and what is the **extra energy per additional correct
   answer**?

The experiment uses GSM8K grade-school math problems and a locally served
Qwen3-1.7B. Every question is asked twice, once with `think=false` and once
with `think=true`, and each request gets its own hardware energy measurement.

A detailed design and decision record is in [`SPEC.md`](SPEC.md).

---

## Current status

| Part | State |
| --- | --- |
| Linux provisioning (`scripts/setup_linux.sh`) | Implemented; needs end-to-end validation on the target Linux host |
| Linux verification (`scripts/verify_linux.sh`) | Implemented; independent PASS/FAIL checks |
| GSM8K preparation | Implemented (`make data`) |
| Ollama paired runner with energy windows | Implemented (`make test/pilot/run`) |
| Grading (strict + SymPy equivalence) | Implemented |
| Analysis and plots | Implemented (`make analyze`) |
| Unit tests | 31 passing on macOS (non-hardware logic) |
| End-to-end Linux measurement | Pending target-host validation |

The experiment is **Linux x86_64 only** because accurate energy measurement
requires RAPL hardware counters exposed through Kepler. macOS `powermetrics`
numbers are not comparable and are not implemented here.

---

## What you need

- **Bare-metal Linux**, Ubuntu 22.04/24.04 or Debian 12+.
- **x86_64 CPU.** Kepler 0.13.0 publishes only amd64 binaries and images.
- A readable, increasing RAPL counter under `/sys/class/powercap/*-rapl:*/energy_uj`.
- At least 8 GiB RAM and 20 GiB free disk.
- Root/sudo for installation and service management.
- Network access for packages, the model, and GSM8K.

Virtual machines and containers are rejected unless you explicitly pass
`--allow-virtualized`. They normally hide RAPL, which makes the energy results
invalid. If no RAPL counter increases, installation stops unless you pass
`--allow-no-rapl`.

On ARM64, installation stops unless you pass `--allow-unsupported-arch`; that
installs the rest of the stack but skips Kepler and Prometheus, so energy
measurement is unavailable.

---

## Quick start

```bash
git clone <this-repo> SusCo-project
cd SusCo-project

# 1. Provision the Linux measurement host.
make install

# 2. Download and prepare GSM8K (once).
make data

# 3. Independent installation check.
make verify

# 4. Five-question end-to-end smoke test.
make test

# 5. Calibration run: 100 questions, one repetition.
make pilot

# 6. Full experiment: all 1,319 questions, three repetitions.
make run

# Optional: reproducible random subset (100 questions, seed 7, three reps).
make run QUESTIONS=100 SEED=7

# 7. Tables and plots.
make analyze
```

`make install` runs the verifier automatically at the end. It needs sudo and
will re-execute itself as root. Re-running is safe; installed components are
reused.

The default `make install` provisions the **core measurement stack** only:

```bash
sudo ./scripts/setup_linux.sh --skip-k8s --skip-clue
```

Kubernetes/kind and CLUE are optional and are not needed for the experiment.
To install the full legacy stack, run `sudo ./scripts/setup_linux.sh` without
those skips.

---

## Hetzner checklist

Hetzner **Cloud** instances are KVM virtual machines and normally hide RAPL.
They are not suitable for the energy study. Use a Hetzner **dedicated** server
(Robot), or another bare-metal x86_64 host. On the server, check before
installing:

```bash
uname -m                 # must print x86_64
systemd-detect-virt      # must print "none"; "kvm" means Cloud/VM
python3 --version        # must be >= 3.11 (Ubuntu 24.04/Debian 12+)
cat /sys/class/powercap/*-rapl:*/name
cat /sys/class/powercap/*-rapl:*/energy_uj
```

The last two commands must list RAPL domains and readable energy counters that
increase over time. If they do not, stop: the installer will refuse to
continue without `--allow-no-rapl`, and energy results would be invalid.

Transfer the project **without** the macOS virtualenv or old caches:

```bash
rsync -av --exclude .venv --exclude .pytest_cache --exclude __pycache__ \
  --exclude results --exclude data/processed \
  ./ user@server:/opt/SusCo-project/
```

Then on the server:

```bash
cd /opt/SusCo-project
make install       # provisions Docker, Kepler, Prometheus, native Ollama, venv
make data          # downloads GSM8K
make verify        # must exit 0
make test          # 5 questions
make pilot         # 100 questions; use this to estimate full-run duration
```

If the installer added your user to the `docker` group, log out and back in (or
run `newgrp docker`) before `make test`; otherwise the harness cannot inspect
the Prometheus container.

Expect the full run to take a long time on a CPU-only server. Thinking mode can
generate far more tokens than non-thinking mode, and Hetzner dedicated servers
have no GPU. Run `make pilot` first, measure the elapsed time, and extrapolate
before starting 7,914 full-run requests.

Kepler binds to `127.0.0.1:28282`, Prometheus to `127.0.0.1:9090`, and Ollama to
`127.0.0.1:11434`; none needs to be exposed publicly.

---

## What each action does

### `make install`

Provisions the host:

- Base packages, build tools, Python 3, venv, `msr-tools`, `cpupower`.
- RAPL kernel modules and a check that `energy_uj` increases.
- Stable CPU settings: `performance` governor, turbo/boost disabled. The
  original state is saved to `/var/lib/susco/cpu-state.tsv`.
- Docker, used for Prometheus.
- Native Ollama with serialized, keep-loaded, CPU-only configuration.
- The `qwen3:1.7b` model.
- Kepler as a native `kepler.service`, reading RAPL in host mode.
- Prometheus as the `susco-prometheus` Docker container on the host network.
- The project `.venv` with analysis dependencies.
- A final verification run.

The installer does **not** download GSM8K; run `make data` once.

### `make data`

Downloads `openai/gsm8k` (test split, 1,319 rows), extracts the gold answer,
computes the difficulty proxy, and writes:

```text
data/processed/gsm8k_test.jsonl
data/processed/gsm8k_test.meta.json
```

### `make verify`

Runs independent checks and exits non-zero on required failures:

- OS, architecture, bare-metal, cgroup v2.
- RAPL domains and increasing `energy_uj`.
- CPU governor and turbo/boost state.
- Docker, Prometheus readiness, Kepler scrape target.
- Kepler service, endpoint, and increasing joule counter.
- Ollama service, API, model, `think=false` vs `think=true`, CPU-only execution,
  serialization, keep-alive.
- Python environment, imports, Locust.
- Disk, memory, background load.

Warnings can be made fatal with `--strict`. CLUE checks are optional with
`--skip-clue`; energy checks can be skipped with `--skip-kepler` and
`--skip-prometheus` for partial installs.

### `make test`, `make pilot`, `make run`

Start the required services, run the Python preflight, and execute paired
requests:

| Target | Questions | Repetitions | Requests |
| --- | ---: | ---: | ---: |
| `make test` | 5 | 1 | 10 |
| `make pilot` | 100 | 1 | 200 |
| `make run` | 1,319 | 3 | 7,914 |

To run a reproducible random subset:

```bash
make run QUESTIONS=100 SEED=7
```

This selects 100 questions uniformly at random with seed 7, records the selected
set in `results/<run>/requests.jsonl`, and stores the sampling plan in
`config.json`. Use `SAMPLE=stratified` for the old difficulty-stratified
selection instead of `SAMPLE=random` (the default when `QUESTIONS` is set).

Each request is measured individually:

1. Scrape Kepler for an idle baseline.
2. Scrape Kepler immediately before the request.
3. Send one serialized request to Ollama.
4. Scrape Kepler immediately after the request.
5. Subtract estimated idle energy from the counter delta.
6. Write the record to `results/<run>/requests.jsonl`.

Requests are one at a time. Concurrent requests would make energy attribution
ambiguous and change CPU frequency behavior.

Every run prints a performance table for `no_think`, `think`, and both modes
combined (total energy, tokens, latency, correctness, and energy per correct).
It also prints an explicit correct-answer count line, for example
`Correct answers: no_think 3/5, think 4/5, total 7/10`, and the
thinking-minus-no-thinking deltas. The same counts are in `summary.json`
under `thinking_minus_no_thinking` (`no_think_correct`, `think_correct`,
`total_correct`). `make test` and
`make pilot` print every per-question row; `make run` prints the first 200 rows
by default. The full per-question table is always written to
`results/<run>/report.txt`; use `--max-print-rows` to change the terminal limit.

### `make analyze`

Reads `results/latest` and writes to `results/latest/analysis/`:

- Mode, difficulty, and repetition summaries (CSV/JSON).
- Paired per-question tests: Wilcoxon signed-rank and bootstrap intervals.
- McNemar tests for paired correctness.
- Marginal energy per additional correct answer.
- Plots: mode bars, difficulty heatmaps, paired deltas, marginal energy.

### `make preflight`

Checks dataset, model digest, Ollama, Kepler, Prometheus, disk, memory, and
background load without starting a run. Useful before a long experiment.

### `make restore-cpu`

Restores the governor and turbo/boost state saved during installation.

### `make unit`

Runs the unit test suite with `pytest`.

### `make clean`

Removes Python caches and `.pytest_cache`. Results are kept.

---

## How the measurement works

```text
GSM8K (HuggingFace datasets)
        │
        ▼
Python harness on host (.venv)
  ├── paired prompts: think=false / think=true
  ├── native Ollama 127.0.0.1:11434
  ├── direct Kepler scrape 127.0.0.1:28282
  └── results/<run>/requests.jsonl
        ▲
        │ RAPL /sys/class/powercap
  Kepler (native systemd, host mode)
        │
        ▼
Prometheus (Docker, host network, 1 s scrape)
  operational checks only; not used for per-request energy
```

### Why native Ollama

Kepler can see the real model process, there is no container/VM boundary, and
there is no Docker overhead. The harness forces CPU inference with
`options.num_gpu: 0` on each request. `OLLAMA_NUM_GPU` is not a real Ollama
environment variable and is not used.

### Why direct Kepler scrapes

Per-request windows are often only a few hundred milliseconds to a few seconds.
Prometheus scrape and query alignment is too coarse for that, so the harness
scrapes Kepler directly before and after each request and uses the
`kepler_node_cpu_joules_total` counter delta. Prometheus is kept for readiness,
target health, and coarse observability.

### Energy calculation

```text
energy_raw_j = counter_end_j - counter_start_j
idle_energy_j = idle_power_w × request_window_s
energy_j = max(0, energy_raw_j - idle_energy_j)
```

A window is marked `low_confidence` when:

- the counter decreased (reset or scrape race),
- no idle baseline was available,
- the window is shorter than `5 ×` the Kepler monitor interval, or
- the idle estimate exceeds the measured energy.

Kepler is configured for RAPL only, a 100 ms monitor interval, and node-level
metrics to minimize overhead. Overlapping zones are never summed; the meter
selects `psys` when present, otherwise all `package-*` zones.

### Reproducibility

- Model digest is resolved through Ollama and recorded in every request.
- If a digest is configured, a mismatch aborts the run.
- `temperature=0`, `seed=42`, `top_p=1.0`, `top_k=1`.
- `num_predict=2048`, `num_ctx=4096`.
- Two warm-up prompts per mode; the model stays loaded (`KEEP_ALIVE=-1`).
- Question sampling is stratified across five difficulty bins and seeded.
- Mode order is randomized per question and repetition.
- Three full-run repetitions estimate energy/latency/system noise; at
  temperature 0 they do not estimate answer-sampling variance.

---

## Difficulty levels

GSM8K has no official difficulty labels. SusCo uses a transparent proxy:

```text
difficulty_raw = solution_lines + 0.25 × arithmetic_operators
```

Rows are sorted by this value and split into five equal-frequency bins
(`1` easiest to `5` hardest). This is a proxy, not a validated human difficulty
measure. All results are reported by these bins and the definition is restated
with the results.

---

## Correctness

Gold answers come from the text after the final `####` marker. Model output is
searched in this order:

1. `#### <answer>`
2. `\boxed{...}`
3. an "answer is" / "final answer" sentence
4. the last number or fraction in the response

Answers are cleaned and parsed with SymPy. Two values are equivalent when they
match numerically within `1e-6` or simplify to the same symbolic expression.

Each request records:

- `correct`: symbolic/numeric equivalence.
- `strict_correct`: exact canonical text match.
- `status`: `correct`, `incorrect`, `unparseable`, `truncated`, or `error`.

Final-answer equivalence does not audit the reasoning chain. A correct final
answer can come from flawed reasoning, and a wrong final answer can come from a
mostly correct solution. No LLM judge or manual reasoning audit is used in the
current scope.

---

## Results

Each run creates:

```text
results/run-<timestamp>-<profile>/
  config.json          # experiment, host, and run/sampling plan
  environment.json     # host, platform, CPU count, load, UTC time
  requests.jsonl       # one row per request, append-only, resumable
  summary.json         # per-mode, per-repetition, total, marginal summary
  report.txt           # aggregate tables + full per-question answers/metrics
results/latest -> run-<...>/
```

Important request fields:

- **Identity:** `run_id`, `profile`, `rep`, `mode`, `think`, `question_id`.
- **Question:** `question`, `gold`, `difficulty`, `difficulty_raw`,
  `reasoning_steps`.
- **Answer:** `prediction`, `normalized_prediction`, `normalized_gold`,
  `correct`, `strict_correct`, `status`, `content`, `thinking`.
- **Tokens/timing:** `eval_count`, `prompt_eval_count`, Ollama duration fields,
  `wall_duration_s`, `time_to_first_token_s`, `tokens_per_second`,
  `done_reason`.
- **Energy:** `energy_j`, `energy_raw_j`, `energy_valid`, `idle_power_w`,
  `idle_energy_j`, `request_window_s`, `counter_start_j`, `counter_end_j`,
  `energy_zones`, `low_confidence`, `energy_note`.
- **Provenance:** `model`, `model_digest`, `kepler_zone`,
  `monitor_interval_ms`, timestamps.

`eval_count` includes thinking tokens, so token cost is directly comparable
across modes.

### Main quantities

For each mode:

- accuracy and strict accuracy,
- total/mean energy, tokens, latency, and time to first token,
- energy, tokens, and latency per correct answer,
- low-confidence rate.

For thinking vs non-thinking:

```text
extra_energy_j       = energy(think) - energy(no_think)
extra_tokens         = tokens(think) - tokens(no_think)
extra_latency_s      = latency(think) - latency(no_think)
extra_correct        = correct(think) - correct(no_think)
extra_energy_per_additional_correct_j = extra_energy_j / extra_correct
energy_per_correct_ratio = energy_per_correct(think) / energy_per_correct(no_think)
```

The marginal ratio is `null` when thinking adds no correct answers. A
question-level bootstrap provides a confidence interval.

---

## Configuration

Committed defaults are in:

- `configs/experiment.yaml` — model, endpoints, decoding, profiles, timing,
  Kepler settings, difficulty bins, seeds.
- `configs/host.yaml` — host defaults for CPU layout, RAPL zone, Kepler zone,
  monitor interval, model digest.

`configs/host.local.yaml` is an optional, git-ignored per-host override. If it
exists, non-empty values override the defaults. It is useful for recording the
detected CPU layout, RAPL zone, and model digest on the measurement machine.

Common edits:

```yaml
# configs/host.local.yaml
ollama_cpus: "0-5"        # systemd CPUAffinity for Ollama
harness_cpus: "6-7"       # reserve cores for the harness if you pin it
kepler_zone: package      # or psys, or a specific package-0
kepler_monitor_interval_ms: 100
rapl_zone: package-0
model_digest: "sha256:..."
```

Precedence:

```text
built-in defaults < configs/experiment.yaml < configs/host.yaml < configs/host.local.yaml
```

CLI overrides (`--model`, `--kepler-zone`, `--seed`, `--sample-seed`,
`--sample-mode`, `--limit`, `--reps`) take highest precedence.

### Useful runner commands

```bash
# Resume the newest unfinished run.
.venv/bin/python -m susco.runner --profile full --resume

# Limit a run for debugging (difficulty-stratified by default).
.venv/bin/python -m susco.runner --profile test --limit 20

# Reproducible uniform random subset.
.venv/bin/python -m susco.runner --profile full --limit 100 --sample-mode random --seed 7

# Use a different model tag.
.venv/bin/python -m susco.runner --profile test --model qwen3:1.7b

# Skip preflight only if you know the services are healthy.
.venv/bin/python -m susco.runner --profile test --skip-preflight
```

A run holds `results/.lock`; a second concurrent run exits with an error.
`Ctrl-C` keeps partial results. Error rows are retried on `--resume`; successful
`(rep, question_id, mode)` rows are skipped.

---

## Accuracy choices and trade-offs

- **RAPL, not estimates.** Energy comes from hardware counters through Kepler.
- **CPU only.** GPU energy is out of scope; `num_gpu=0` forces CPU inference.
- **Serialized requests.** One request at a time gives unambiguous attribution.
- **Keep-alive on.** Model loading cost is excluded from per-request energy.
- **Idle subtraction.** Background energy during each request window is
  estimated and subtracted.
- **Low-confidence flags.** Short or invalid windows are never silently
  dropped; they are reported and can be filtered in analysis.
- **Repetitions.** Three full-run repetitions quantify system noise, not
  sampling variance.
- **Final-answer grading.** Symbolic equivalence is robust to formatting but
  does not validate the reasoning process.
- **No absolute wall-power claims.** RAPL covers CPU package/psys domains, not
  PSU losses, fans, or peripherals. Use results for relative mode comparison on
  the same host.

---

## Troubleshooting

### No RAPL domains or `energy_uj` does not move

You are probably in a VM/container, RAPL is disabled in BIOS, or the kernel
modules are missing. Run on bare metal and check:

```bash
cat /sys/class/powercap/*-rapl:*/name
cat /sys/class/powercap/*-rapl:*/energy_uj
```

Re-run installation with `--allow-no-rapl` only if you explicitly accept
invalid energy data.

### Kepler runs but exposes no `kepler_node_cpu*` metrics

```bash
journalctl -u kepler -n 100 --no-pager
curl -s http://127.0.0.1:28282/metrics | grep kepler_node_cpu
```

If you installed Kepler in kind mode, switch to host mode; kind nodes usually
cannot see host RAPL:

```bash
sudo ./scripts/setup_linux.sh --kepler-mode host
```

### Ollama is not CPU-only

```bash
ollama ps
```

The model should report `100% CPU`. The runner sends `options.num_gpu: 0` on
every request. `OLLAMA_NUM_GPU` is not a valid Ollama environment variable.

### Prometheus is not ready

```bash
docker ps --filter name=susco-prometheus
docker logs --tail 100 susco-prometheus
curl -s http://127.0.0.1:9090/-/ready
```

`make run` starts an existing container automatically, but it does not create
one. Run `make install` if the container is missing.

### The model digest does not match

If `model_digest` is set in `configs/host.local.yaml` or
`configs/experiment.yaml`, the served model must match it. Pull the intended
model or clear the digest after confirming the model is correct.

### CLUE is missing or its private package fails

CLUE is optional and is skipped by `make install`. The private `psc` package
may not be publicly reachable; this is reported as a warning. The measurement
pipeline does not depend on CLUE.

### Need the original CPU settings back

```bash
make restore-cpu
# or
sudo ./scripts/setup_linux.sh --restore-cpu
```

### ARM64 host

Kepler 0.13.0 has no arm64 host binary. Accurate energy measurement requires
an x86_64 host. On arm64, `--allow-unsupported-arch` installs the non-energy
parts only.

---

## macOS note

The project does not implement a macOS measurement path. On macOS,
`powermetrics` can give relative on-machine power estimates, but it cannot run
inside Linux containers, Kepler/RAPL are unavailable, and CLUE has no usable
energy data. A macOS path (native Ollama + host `powermetrics` sampler +
serialized runner) could be added later, but its numbers would not be
comparable to RAPL.

---

## Repository layout

```text
SusCo-project/
├── Makefile
├── README.md
├── SPEC.md
├── pyproject.toml
├── configs/
│   ├── experiment.yaml
│   ├── host.yaml
│   └── host.local.yaml       # optional, git-ignored
├── data/
│   ├── raw/
│   └── processed/            # gsm8k_test.jsonl + meta
├── results/
│   ├── run-*/
│   └── latest -> run-*/
├── scripts/
│   ├── setup_linux.sh
│   ├── verify_linux.sh
│   └── run_experiment.sh
├── src/susco/
│   ├── config.py
│   ├── data.py
│   ├── grade.py
│   ├── ollama_client.py
│   ├── preflight.py
│   ├── runner.py
│   ├── energy/
│   │   ├── kepler.py
│   │   └── prometheus.py
│   └── analysis/
│       ├── metrics.py
│       └── report.py
└── tests/
```

`docker/`, `infra/`, `loadtest/`, `notebooks/`, and `docs/` are placeholders
and are not part of the core measurement path.

---

## Acceptance checklist

Before trusting a full run:

- [ ] `make install` completes and its verification exits zero.
- [ ] `make verify` shows no required failures.
- [ ] RAPL `energy_uj` and Kepler `kepler_node_cpu_joules_total` both increase.
- [ ] `make test` produces 10 paired records with valid energy and token data.
- [ ] `make pilot` completes 200 requests and writes `summary.json`.
- [ ] `make run` completes 7,914 requests and can resume after interruption.
- [ ] `make analyze` produces summaries, tests, marginal ratios, and plots.
- [ ] Low-confidence windows are reported, not silently discarded.
- [ ] Results state that difficulty is a proxy and energy is CPU-domain only.
