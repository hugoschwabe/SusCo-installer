# SusCo — Specification

**Project:** Measuring the energy, token, and latency cost of thinking mode in Qwen3-1.7B on GSM8K
**Status:** Measurement pipeline implemented; Linux provisioning and verification scripts implemented; end-to-end validation on Linux pending
**Last updated:** 2026-11-17

---

## 1. Purpose

SusCo measures the marginal cost of chain-of-thought "thinking" mode relative to non-thinking mode for a local Qwen3-1.7B model, and relates that cost to answer correctness.

The project answers two research questions:

1. **Cost of thinking.** How much additional energy, how many additional tokens, and how much extra latency does thinking mode cost compared with non-thinking mode, across task types and difficulty levels?
2. **Where reasoning pays off.** Where does reasoning improve *energy per correct answer*, and what is the *extra energy per additional correct answer*?

### 1.1 Scope

- **Task family:** GSM8K grade-school math word problems only.
- **Task types:** not varied in the current scope. GSM8K is the single task type.
- **Difficulty levels:** five equal-frequency bins derived from a reasoning-length proxy (see §7.2).
- **Model:** `qwen3:1.7b` served by native Ollama, pinned by digest for a run.
- **Modes:** paired `think=false` (`no_think`) and `think=true` (`think`) requests for every question.
- **Platform:** bare-metal Linux, `x86_64`, with readable RAPL hardware energy counters.

Out of scope for the current implementation: MATH-500, TriviaQA, multi-task comparisons, LLM-as-judge grading, manual reasoning audits, GPU energy, and macOS-comparable measurements.

---

## 2. Research design

### 2.1 Paired within-question design

Every question is asked in both modes. Mode order is randomized per question and repetition using a deterministic seed. This controls for question difficulty and ordering effects, and enables paired statistics.

For each question `q`, repetition `r`, and mode `m ∈ {no_think, think}`, the pipeline records one request row containing correctness, token counts, latency, energy, and metadata.

### 2.2 Run profiles

| Profile | Questions | Repetitions | Purpose |
| --- | ---: | ---: | --- |
| `test` | 5 | 1 | End-to-end smoke test (`make test`) |
| `pilot` | 100 | 1 | Calibration and variance check |
| `full` | all 1,319 GSM8K test rows | 3 | Primary experiment |

Full-run requests: `1319 × 3 × 2 = 7,914`.

### 2.3 Repetitions

At `temperature=0`, correctness is effectively deterministic on a fixed host and model digest. Three full-run repetitions are used to estimate energy, latency, and system-noise variability, not to estimate answer-sampling variance.

Between repetitions, the runner waits `inter_repetition_s` (default 60 s) to let thermal and frequency state settle. Between individual requests it waits `inter_request_s` (default 1 s).

### 2.4 Sampling

Questions are sampled with stratification over the five difficulty bins, preserving the overall proportion and returning exactly the requested count. The same sampled question set is used across repetitions. The sample seed is fixed (`sample_seed: 42`) so runs are reproducible.

### 2.5 Warm-up

Before measurement, two warm-up prompts per mode run without energy attribution. The runner then sleeps 2 s. This removes model-load and first-inference effects. `OLLAMA_KEEP_ALIVE=-1` keeps the model resident for the whole run.

### 2.6 Model determinism

- Model tag: `qwen3:1.7b`.
- Model digest is resolved through the Ollama API and recorded in every request row.
- If a digest is configured, the runner refuses to start when the served digest does not match.
- `temperature=0`, `seed=42`, `top_p=1.0`, `top_k=1`.
- `num_predict=2048`, `num_ctx=4096`.
- `num_gpu=0` is sent per request to force CPU-only inference.
- One request at a time (`OLLAMA_NUM_PARALLEL=1`), one loaded model (`OLLAMA_MAX_LOADED_MODELS=1`).

---

## 3. Measurement architecture

### 3.1 Selected stack

```text
GSM8K (HuggingFace datasets)
        │
        ▼
Python harness on host (.venv)
  ├── builds paired prompts
  ├── calls native Ollama 127.0.0.1:11434
  ├── scrapes Kepler 127.0.0.1:28282 before/after each request
  └── writes results/<run>/requests.jsonl
        ▲
        │ RAPL /sys/class/powercap
  Kepler (native systemd, host mode)
        │
        ▼
Prometheus (Docker, host network, 1 s scrape)  ← operational observability only
```

### 3.2 Core decisions

| Decision | Choice | Rationale |
| --- | --- | --- |
| Energy source | Kepler reading RAPL | Hardware counters, highest available accuracy; no model-based estimation |
| Ollama deployment | **Native** on the host, not Docker | Kepler can see the real process; no Docker Desktop VM; lower overhead; CPU-only host |
| Kepler deployment | **Native systemd service**, host mode | Only mode that reliably sees host RAPL; kind nodes usually cannot |
| Prometheus | Docker container, `--network host` | Convenient retention and operational queries; **not** used for per-request energy |
| Harness | Python in project `.venv` on host | Direct access to Kepler endpoint and Ollama; minimal interference |
| Per-request energy | Direct Kepler scrape delta | Avoids Prometheus scrape/query alignment error at request timescales |
| Concurrency | Serialized, one request at a time | Required for unambiguous energy attribution |
| Correctness | Final-answer extraction + SymPy equivalence | Robust to formatting; no LLM judge |
| Task scope | GSM8K only | Keeps the study focused and statistically tractable |

### 3.3 Rejected alternatives

- **macOS + `powermetrics`:** possible as a separate relative-comparison path, but `powermetrics` cannot run inside Linux containers, Kepler/RAPL are unavailable, and CLUE has no usable energy data. Numbers would not be comparable to RAPL. Deferred.
- **Ollama in Docker on Linux:** unnecessary isolation, hides the model process from host-level attribution, and adds container overhead.
- **Kepler in kind/Helm for measurement:** kind nodes are containers and normally cannot see host RAPL. Kept only as an optional installation mode for Stack validation, not for the study.
- **Prometheus range queries for per-request energy:** 1 s scrape cadence and query alignment are too coarse for request windows. Prometheus is used only for readiness and coarse operational checks.
- **Locust concurrency:** concurrent requests make per-request energy attribution impossible and change CPU frequency/thermal behavior. Locust is installed but not part of the core experiment.
- **CLUE as the experiment runner:** CLUE targets TeaStore with a 10 s `ResourceTracker` sampling interval and requires an existing Scaphandre/Kepler + Prometheus setup. It is retained as optional vendor tooling, not the primary harness.
- **LLM judge / manual reasoning audit:** adds cost and subjectivity; final-answer correctness is the primary endpoint. Exact-match and symbolic equivalence are both reported.
- **Direct RAPL reads in the harness:** bypasses Kepler's normalization and zone handling; Kepler is the single energy source of truth.

---

## 4. Accuracy requirements

### 4.1 Hardware

- Bare-metal `x86_64` Linux. VMs and containers are rejected unless `--allow-virtualized` is explicitly passed.
- Readable, increasing RAPL counter under `/sys/class/powercap/*-rapl:*/energy_uj`.
- If no RAPL counter increases, the installer stops unless `--allow-no-rapl` is passed.
- Kepler 0.13.0 is `amd64`-only. On `arm64`, the installer stops unless `--allow-unsupported-arch` is passed, in which case Kepler and Prometheus are skipped and energy measurement is unavailable.

### 4.2 Kepler configuration

- `cpu.preferredMeters: [rapl]` (`[hwmon]` only as an explicit fallback when RAPL is unavailable).
- `rapl.zones: []` with a configured zone selection of `package` (or `psys` if chosen by the host overlay).
- `monitor.interval: 100ms`.
- `monitor.staleness: 100ms` (never greater than the interval).
- `exporter.prometheus.metricsLevel: [node]` to minimize exporter overhead.
- `web.listenAddresses: [":28282"]`.
- `kube.enabled: false`.
- Host mode runs as root as a systemd service.

### 4.3 Per-request energy

For each request:

1. Scrape Kepler once to measure an idle window of `idle_window_s` (default 1 s).
2. Scrape Kepler immediately before the request.
3. Call Ollama.
4. Scrape Kepler immediately after the request.
5. Compute the raw counter delta and the request wall window.
6. Estimate idle power from the idle window and subtract `idle_power_w × request_window_s`.
7. Record both raw and idle-corrected energy.

Rules:

- The counter is `kepler_node_cpu_joules_total`.
- Zone selection must avoid summing overlapping zones. `psys` is preferred when present, otherwise all `package-*` zones.
- A counter decrease marks the window invalid (reset or scrape race).
- A window shorter than `5 × monitor interval` (default 500 ms) is flagged `low_confidence`.
- An invalid idle baseline is flagged `low_confidence`.
- If the idle estimate exceeds the measured energy, the request is flagged and corrected energy is clamped to zero.
- `energy_j` is the idle-corrected value; `energy_raw_j` is the raw counter delta. Analysis can filter invalid or low-confidence rows.
- Prometheus is not used to derive per-request energy.

### 4.4 Installer self-check

A one-time check compares the Kepler counter delta against a raw RAPL delta over a 30 s window. A mismatch produces a **warning only** and never fails installation. This preserves the "Kepler only" measurement path while surfacing gross misconfiguration.

### 4.5 CPU stability

- Save the original governor and turbo/boost state to `/var/lib/susco/cpu-state.tsv`.
- Set the `performance` governor.
- Disable Intel `no_turbo` or cpufreq `boost`.
- `--restore-cpu` restores the saved state.
- CPU topology is inspected during installation. The installer chooses or prompts for Ollama and harness CPU pinning; the selection is written to `configs/host.local.yaml`.
- Ollama pinning is applied through a systemd `CPUAffinity` drop-in. Harness pinning is applied with `taskset` when available.

---

## 5. Correctness methodology

### 5.1 Gold answers

GSM8K gold answers end with `#### <number>`. `extract_gold` takes the text after the final marker and normalizes it.

### 5.2 Prediction extraction

For model output, `extract_prediction` uses this preference order:

1. Text after the final `####`.
2. The last balanced `\boxed{...}`.
3. The value in an "answer is" / "final answer" sentence.
4. The last number or fraction in the response.

The system prompt instructs the model to end with a line of the form `#### <number>`.

### 5.3 Normalization and equivalence

- Strip whitespace, backticks, quotes, trailing punctuation, currency symbols, and thousands separators.
- If the value contains `=`, keep the right-hand side.
- Remove a trailing `%`.
- Parse with SymPy using implicit multiplication and `^` as exponent.
- Compare numerically with tolerance `1e-6`, or symbolically when a numeric conversion is not possible.

### 5.4 Reported correctness

For every request:

- `correct`: symbolic/numeric equivalence to gold.
- `strict_correct`: exact canonical text equality after cleanup.
- `status`: `correct`, `incorrect`, `unparseable`, `truncated`, or `error`.
- `truncated` is assigned when Ollama finishes with `done_reason == "length"`.
- A prediction that normalizes to non-numeric text is `unparseable`.

Primary endpoint: `correct`. Secondary endpoint: `strict_correct`.

No LLM judge and no manual reasoning audit are used in the current scope.

---

## 6. Data and records

### 6.1 Dataset preparation

Source: HuggingFace `openai/gsm8k`, configuration `main`, split `test` (1,319 rows). The repository stores parquet only; the pipeline uses the `datasets` library.

Prepared JSONL row fields:

```json
{
  "id": "test-0",
  "question": "...",
  "answer": "...",
  "gold": "...",
  "reasoning_steps": 5,
  "difficulty_raw": 6.25,
  "difficulty": 3
}
```

Output: `data/processed/gsm8k_test.jsonl`, with a sidecar `.meta.json`.

### 6.2 Difficulty proxy

GSM8K has no official difficulty labels. The project uses a transparent proxy:

```text
difficulty_raw = number_of_solution_lines + 0.25 × arithmetic_operator_count
```

Rows are sorted by `difficulty_raw` and split into five equal-frequency bins (`1` easiest to `5` hardest). The proxy is a design choice, not a validated difficulty measure; results are always reported by the proxy bins.

### 6.3 Request record

Each line of `results/<run>/requests.jsonl` is one request:

- **Identity:** `run_id`, `profile`, `rep`, `mode`, `think`, `question_id`.
- **Question:** `question`, `gold`, `difficulty`, `difficulty_raw`, `reasoning_steps`.
- **Answer:** `prediction`, `normalized_prediction`, `normalized_gold`, `correct`, `strict_correct`, `status`, `content`, `thinking`.
- **Tokens and timing:** `content_chars`, `thinking_chars`, `eval_count`, `prompt_eval_count`, Ollama duration fields (`total_duration_ns`, `load_duration_ns`, `prompt_eval_duration_ns`, `eval_duration_ns`), `wall_duration_s`, `time_to_first_token_s`, `tokens_per_second`, `done_reason`.
- **Energy:** `energy_j`, `energy_raw_j`, `energy_valid`, `idle_power_w`, `idle_energy_j`, `request_window_s`, `counter_start_j`, `counter_end_j`, `energy_zones`, `low_confidence`, `energy_note`.
- **Provenance:** `model`, `model_digest`, `kepler_zone`, `monitor_interval_ms`, `started_at`, `finished_at`.

`eval_count` includes thinking tokens. Token cost is therefore directly comparable across modes.

### 6.4 Run artifacts

```text
results/run-<timestamp>-<profile>/
  config.json        # full experiment and host configuration
  environment.json   # hostname, platform, CPU count, loadavg, UTC time
  requests.jsonl     # one row per request, append-only, resumable
  summary.json       # per-mode, per-repetition, marginal summary
results/latest -> run-<...>/
```

The run is append-only. A lock file `results/.lock` prevents concurrent runs. `--resume` reuses the newest run or a specified `--out` directory and skips already completed `(rep, question_id, mode)` keys. Error rows are not considered complete and are retried.

---

## 7. Analysis

### 7.1 Primary aggregates

By mode:

- Request count.
- Accuracy and strict accuracy.
- Total and mean energy (J).
- Total and mean tokens (`eval_count`, `prompt_eval_count`).
- Mean total latency and time to first token (TTFT).
- Energy per correct answer, tokens per correct answer, latency per correct answer.
- Low-confidence rate.

By mode × difficulty bin, and by mode × repetition.

### 7.2 Marginal cost of thinking

For each repetition:

```text
extra_energy_j       = energy(think) - energy(no_think)
extra_tokens         = eval_count(think) - eval_count(no_think)
extra_latency_s      = wall_duration(think) - wall_duration(no_think)
extra_correct        = correct(think) - correct(no_think)
extra_energy_per_additional_correct_j = extra_energy_j / extra_correct
energy_per_correct_ratio = energy_per_correct(think) / energy_per_correct(no_think)
```

The marginal ratio is `null` when there are no additional correct answers. Question-level bootstrap resampling provides a confidence interval for the marginal ratio.

### 7.3 Statistical tests

- **McNemar** on paired correctness, per repetition: exact binomial test for discordant pairs, plus chi-square when appropriate.
- **Wilcoxon signed-rank** on paired per-question energy and latency.
- **Bootstrap** confidence intervals for paired mean differences.
- **Paired deltas** for every metric of interest.

### 7.4 Outputs

`python -m susco.analysis.report` writes to `results/latest/analysis/`:

- CSV summaries: mode, difficulty, repetition, paired tests, marginal ratios.
- `summary.json`.
- Plots:
  - Mode comparison bars (energy, tokens, latency, accuracy).
  - Difficulty heatmaps (energy per correct, accuracy).
  - Paired delta bars by difficulty.
  - Marginal energy per additional correct answer.

Matplotlib uses the non-interactive `Agg` backend.

### 7.5 Interpretation rules

- Primary claims use idle-corrected `energy_j` on valid, non-low-confidence rows.
- Low-confidence windows are reported, not silently dropped; sensitivity analysis compares included vs excluded rows.
- Accuracy is reported both as symbolic-equivalent and strict match.
- Difficulty effects are reported by proxy bin, with the proxy definition restated.
- No claim is made about absolute wall-socket energy; RAPL covers CPU package/psys domains only.

---

## 8. Software structure

```text
SusCo-project/
├── Makefile
├── README.md
├── SPEC.md
├── pyproject.toml
├── configs/
│   ├── experiment.yaml       # committed experiment defaults
│   ├── host.yaml             # committed host defaults
│   └── host.local.yaml       # generated by installer, git-ignored
├── data/
│   ├── raw/
│   └── processed/            # gsm8k_test.jsonl + meta
├── results/
│   ├── run-*/
│   └── latest -> run-*/
├── scripts/
│   ├── setup_linux.sh        # provision Linux host
│   ├── verify_linux.sh       # independent verification
│   └── run_experiment.sh     # service checks + runner wrapper
├── src/susco/
│   ├── __main__.py           # python -m susco
│   ├── cli.py                # run/data/analyze/preflight dispatch
│   ├── config.py             # Config, HostConfig, YAML overlays
│   ├── data.py               # GSM8K download, difficulty, stratification
│   ├── grade.py              # extraction, normalization, equivalence
│   ├── ollama_client.py      # streaming /api/chat, think toggle, timings
│   ├── preflight.py          # dataset/model/Kepler/Prometheus/host checks
│   ├── runner.py             # paired experiment loop, energy windows, JSONL
│   ├── energy/
│   │   ├── kepler.py         # parse metrics, zone selection, counter delta
│   │   └── prometheus.py     # readiness and target checks
│   └── analysis/
│       ├── metrics.py        # summaries, tests, bootstrap, marginal ratios
│       └── report.py         # tables, CSV, plots
└── tests/                    # unit tests for all non-hardware logic
```

`docker/`, `infra/`, `loadtest/`, `notebooks/`, and `docs/` are reserved placeholders and are not part of the primary measurement path.

---

## 9. Configuration

### 9.1 `configs/experiment.yaml`

Committed defaults:

- Model and endpoints: `qwen3:1.7b`, Ollama `127.0.0.1:11434`, Kepler `127.0.0.1:28282/metrics`, Prometheus `127.0.0.1:9090`.
- Paths: `data/processed/gsm8k_test.jsonl`, `results`.
- Sampling: temperature `0`, seed `42`, `top_p` `1.0`, `top_k` `1`, `num_predict` `2048`, `num_ctx` `4096`, `num_gpu` `0`.
- Profile sizes: test `5 × 1`, pilot `100 × 1`, full all rows `× 3`.
- Timing: idle window `1 s`, inter-request `1 s`, inter-repetition `60 s`, timeout `1800 s`, warm-up `2` per mode.
- Energy: zone `package`, monitor interval `100 ms`, scrape timeout `3 s`, low-confidence factor `5`.
- Difficulty bins `5`, sample seed `42`.

### 9.2 `configs/host.yaml` and `configs/host.local.yaml`

`host.yaml` holds committed defaults. `host.local.yaml` is generated by the installer and git-ignored. Non-empty local values override the experiment defaults for:

- `ollama_cpus`, `harness_cpus`
- `nproc`, `mem_gb`
- `kepler_zone`, `kepler_monitor_interval_ms`
- `rapl_zone`, `rapl_energy_file`
- `model_digest`

### 9.3 Configuration precedence

```text
built-in defaults < configs/experiment.yaml < configs/host.yaml < configs/host.local.yaml
```

CLI overrides (`--model`, `--kepler-zone`, `--seed`, `--limit`, `--reps`) take highest precedence.

---

## 10. Command-line interface

### 10.1 Make targets

| Target | Action |
| --- | --- |
| `make install` | Provision the Linux host (Docker, Kepler, Prometheus, native Ollama, venv, model, data) |
| `make verify` | Run the independent verifier |
| `make test` | Five-question end-to-end smoke run |
| `make pilot` | 100-question pilot, one repetition |
| `make run` | Full GSM8K run, three repetitions |
| `make analyze` | Analyze `results/latest` and write tables/plots |
| `make unit` | Run `pytest` |
| `make data` | Download and prepare GSM8K |
| `make preflight` | Check services, model, dataset, and Kepler |
| `make restore-cpu` | Restore the saved CPU governor/turbo state |
| `make clean` | Remove Python caches; results are kept |

### 10.2 `scripts/run_experiment.sh`

Before handing over to Python, the wrapper:

1. Verifies the project `.venv` exists.
2. Ensures the `kepler` and `ollama` systemd services are active, starting them with `sudo` when possible.
3. Ensures the existing `susco-prometheus` container is running and `/‑/ready` responds.
4. Waits until `kepler_node_cpu_joules_total` is exposed on `127.0.0.1:28282`.
5. Executes `python -m susco.runner` with the requested profile.

`python -m susco.runner` then performs the detailed preflight (dataset, model digest, Kepler, Prometheus, capacity, background load).

### 10.3 Runner flags

```text
--config PATH          experiment YAML
--host-config PATH     host overlay
--profile test|pilot|full
--limit N              override question count
--reps N               override repetitions
--out DIR              explicit run directory
--resume               resume newest run or --out
--no-progress          disable tqdm
--model TAG            override model
--kepler-zone ZONE     override energy zone
--seed N               override sampling seed
--skip-preflight       skip checks (not recommended)
```

### 10.4 Progress and failure behavior

- `tqdm` bar shows completed requests and live mode/correct/energy/low-confidence status.
- Three consecutive request errors abort the run with an actionable message.
- `Ctrl-C` stops cleanly; partial results and `summary.json` are written.
- Re-running with `--resume` continues without repeating completed requests.

---

## 11. Linux provisioning

### 11.1 `scripts/setup_linux.sh`

Ubuntu/Debian only. Re-executes itself with `sudo`, preserving the original arguments. Creates `/var/lib/susco`, `/var/log/susco`, and `/etc/susco`.

Responsibilities:

- Detect OS, architecture, virtualization, and cgroup v2.
- Install base packages, build tools, Python 3 + venv, `msr-tools`, `kmod`, and best-effort kernel tools.
- Load `msr`, `intel_rapl_common`, `intel_rapl_msr`, and `amd_energy`.
- Verify RAPL domains and that `energy_uj` increases.
- Save and apply CPU governor/turbo settings.
- Inspect CPU topology and choose/prompt for Ollama and harness CPU pinning.
- Install Docker, kubectl, Helm, and kind; optionally create a `susco` kind cluster.
- Install native Ollama and configure serialized, keep-loaded, CPU-only operation.
- Pull the model and record its digest.
- Create the project `.venv` and install the package.
- Optionally install CLUE and Locust.
- Install Kepler in host mode as a systemd service (default) or via Helm in kind mode.
- Install Prometheus as a host-network Docker container.
- Write `configs/host.local.yaml` with detected values.
- Prepare the GSM8K dataset.
- Run the verifier automatically.

Key flags: `--yes`, `--verify-only`, `--restore-cpu`, `--allow-virtualized`, `--allow-no-rapl`, `--allow-unsupported-arch`, `--no-cpu-tuning`, `--kepler-mode host|kind`, `--model`, `--ollama-cpus`, `--cluster-name`, `--install-dir`, and the `--skip-*` family.

### 11.2 `scripts/verify_linux.sh`

Independent PASS/FAIL/WARN/SKIP checks with a non-zero exit on required failures:

- OS, architecture, bare metal, cgroup v2, `jq`.
- RAPL domains and increasing `energy_uj`.
- hwmon fallback presence.
- CPU governor and turbo/boost state.
- Docker, kubectl, Helm, kind cluster, Ready nodes.
- Kepler service/DaemonSet, endpoint, and increasing joules.
- Prometheus readiness, `up{job="kepler"}`, and increasing energy series.
- Ollama binary, service, API, model presence, serialization/keep-alive, `think=false` vs `think=true`, `options.num_gpu=0`, and `ollama ps` reporting `100% CPU`.
- Project venv imports, `susco`, Locust.
- CLUE checkout, pipenv environment, core imports, and private `psc` warning.
- Disk, memory, and background load.

`--strict` treats warnings as failures. `--strict-clue` makes a `psc` import failure fatal. `--skip-kepler` and `--skip-prometheus` support partial installs.

### 11.3 Installer safety

- Refuses VMs unless `--allow-virtualized`.
- Refuses missing RAPL unless `--allow-no-rapl`.
- Refuses non-amd64 unless `--allow-unsupported-arch` (then skips Kepler/Prometheus).
- Uses `set -Eeuo pipefail` and an ERR trap with line numbers.
- Does not overwrite the saved CPU state on re-runs.
- Re-running is idempotent for installed components.
- The Kepler/RAPL cross-check is warn-only.

---

## 12. Reliability and reproducibility

- Every run stores the full experiment and host configuration, host environment, model digest, Kepler zone, and monitor interval.
- Request rows are append-only and flushed after each request.
- Run directories are unique; `latest` is a symlink.
- A filesystem lock prevents concurrent experiments.
- Resumption is keyed by `(rep, question_id, mode)`.
- Seeds determine question sampling, repetition order, and mode order.
- `temperature=0` makes repeat correctness stable; repetitions quantify system noise.
- Tests cover grading, data preparation, config overlays, metric parsing, analysis, Ollama client behavior, and runner record construction.

---

## 13. Limitations and threats to validity

1. **RAPL scope.** RAPL measures CPU package/psys domains, not wall-socket power. It excludes PSU losses, most DRAM on some platforms, fans, and peripherals. Results are suitable for relative mode comparison on the same host, not absolute energy claims.
2. **Kepler sampling.** A 100 ms interval still quantizes request windows. Very short requests are flagged low-confidence. Idle subtraction assumes idle power is stable during the window.
3. **Single model and task.** Findings apply to Qwen3-1.7B on GSM8K and may not generalize to other models or task families.
4. **Difficulty proxy.** The line/operator proxy is not an official difficulty label and may correlate imperfectly with human-perceived difficulty.
5. **Correctness.** Final-answer equivalence does not verify the reasoning process. A correct answer can come from flawed reasoning; an incorrect final answer can come from a mostly correct solution.
6. **Deterministic decoding.** At `temperature=0`, repetitions do not estimate answer-sampling variance. They estimate energy/latency/system variance.
7. **CPU-only.** GPU and Apple Silicon paths are excluded. `num_gpu=0` forces CPU inference; GPU energy is not measured.
8. **Host noise.** Background load, thermal throttling, and frequency scaling remain threats despite CPU tuning and low-confidence flags.
9. **Kepler version.** Kepler 0.13.0 is amd64-only. ARM64 hosts cannot use the accurate path.
10. **GSM8K size.** 1,319 test questions limit statistical power for small difficulty-bin effects; paired designs and bootstrap intervals mitigate but do not eliminate this.

---

## 14. Decision log

| # | Decision | Alternatives considered | Reason |
| --- | --- | --- | --- |
| 1 | Linux x86_64 bare metal | macOS, ARM64, VM | Accurate RAPL via Kepler; macOS and VMs cannot provide comparable hardware counters |
| 2 | Native Ollama | Ollama in Docker | Kepler sees the real process; lower overhead; CPU-only host |
| 3 | Kepler host systemd | Kepler in kind/Helm | Kind nodes usually cannot see host RAPL |
| 4 | Kepler only for energy | Direct RAPL, model-based estimates | Single normalized source, zone handling, process metrics |
| 5 | Direct scrape for per-request energy | Prometheus range queries | 100 ms windows vs 1 s scrape/query alignment |
| 6 | Serialized requests | Locust concurrency | Unambiguous energy attribution and stable frequency behavior |
| 7 | GSM8K only | MATH-500, TriviaQA, multi-task | Focused, tractable, reproducible scope |
| 8 | Paired modes per question | Independent groups | Controls question difficulty; enables McNemar and paired tests |
| 9 | Full run = 1,319 × 3 reps | More reps, fewer questions | Covers the benchmark; repetitions estimate system noise |
| 10 | Five proxy difficulty bins | Official labels (none exist) | Transparent, reproducible stratification |
| 11 | Symbolic final-answer grading | Exact match only, LLM judge | Robust to formatting without subjective judging |
| 12 | Report strict and symbolic accuracy | One correctness metric | Shows formatting sensitivity alongside semantic correctness |
| 13 | Idle baseline subtraction | Raw counter delta | Removes non-request background energy |
| 14 | 5× monitor interval low-confidence rule | No confidence flag | Makes quantization risk visible |
| 15 | Kepler/RAPL self-check warn-only | Fail install | Preserves measurement path while surfacing misconfiguration |
| 16 | CPU pinning chosen at install | Hard-coded cores | Host topology is unknown until installation |
| 17 | Model digest recorded and checked | Tag only | Tags are mutable; digest guarantees the measured model |
| 18 | No GPU path | GPU + CPU comparison | Scope and measurement-control complexity |
| 19 | Prometheus retained but not primary | Remove Prometheus | Useful for readiness/operational checks and dashboards |
| 20 | CLUE optional | CLUE as primary runner | CLUE targets TeaStore and 10 s sampling; not suitable for per-request GSM8K timing |

---

## 15. Implementation status and known gaps

Implemented and unit-tested:

- GSM8K preparation, difficulty bins, stratified sampling.
- Final-answer extraction, SymPy normalization, strict and symbolic grading.
- Ollama streaming client with `think` toggle and timing/token capture.
- Kepler metric parsing, non-overlapping zone selection, counter deltas.
- Direct pre/post-request energy measurement with idle subtraction and confidence flags.
- Paired runner with JSONL, resume, locking, warm-up, progress bar, and summaries.
- Analysis metrics: mode/difficulty/repetition aggregates, McNemar, Wilcoxon, bootstrap, marginal energy.
- Report generation with CSVs and plots.
- Linux setup and verification scripts.
- 31 unit tests passing on macOS (non-hardware logic).

Known gaps / pending Linux validation:

- The installer and verifier have not been executed end-to-end on the target Linux host in this environment.
- Setup must write `configs/host.local.yaml`, perform topology-based CPU pinning selection, and prepare GSM8K data as described in §11.1.
- The installer's Kepler/RAPL cross-check is specified but not yet implemented.
- A synthetic end-to-end analysis/report smoke test should be added.

---

## 16. Acceptance criteria

The project is complete when:

1. `make install` provisions the stack on a bare-metal x86_64 Linux host and exits zero after verification.
2. `make verify` reports no required failures, including increasing RAPL and Kepler counters.
3. `make test` completes five paired questions with valid energy and token records.
4. `make pilot` completes 100 questions and writes `summary.json`.
5. `make run` completes 7,914 requests with resume support and three repetitions.
6. `make analyze` produces mode/difficulty/repetition tables, paired tests, marginal energy ratios, and plots.
7. Every request row records model digest, zone, monitor interval, raw and idle-corrected energy, tokens, latency, correctness, and confidence flags.
8. Results answer both research questions with uncertainty estimates and explicit low-confidence reporting.
