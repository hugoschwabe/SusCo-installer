"""Configuration loading for the SusCo measurement pipeline."""

from __future__ import annotations

from dataclasses import dataclass, fields, replace
from pathlib import Path
from typing import Any, Mapping

import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class HostConfig:
    """Host-specific values written by the installer."""

    ollama_cpus: str = ""
    harness_cpus: str = ""
    nproc: int = 0
    mem_gb: int = 0
    kepler_zone: str = "package"
    kepler_monitor_interval_ms: int = 100
    rapl_zone: str = ""
    rapl_energy_file: str = ""
    model_digest: str = ""


@dataclass(frozen=True)
class Config:
    """Experiment configuration.

    Values map one-to-one to ``configs/experiment.yaml``. Paths may be
    absolute or relative to the repository root.
    """

    model: str = "qwen3:1.7b"
    model_digest: str = ""
    ollama_url: str = "http://127.0.0.1:11434"
    kepler_url: str = "http://127.0.0.1:28282/metrics"
    prometheus_url: str = "http://127.0.0.1:9090"
    dataset: str = "data/processed/math500_test.jsonl"
    results_dir: str = "results"

    temperature: float = 0.0
    seed: int = 42
    top_p: float = 1.0
    top_k: int = 1
    num_predict: int = 2048
    num_ctx: int = 4096
    num_gpu: int = 0
    system_prompt: str = (
        "Solve the problem carefully. Put your final answer in \\boxed{...}. "
        "Do not put anything after the boxed answer."
    )

    test_questions: int = 5
    test_repetitions: int = 1
    pilot_questions: int = 100
    pilot_repetitions: int = 1
    full_repetitions: int = 3

    idle_window_s: float = 1.0
    inter_request_s: float = 1.0
    inter_repetition_s: float = 60.0
    request_timeout_s: float = 1800.0
    warmup_prompts: int = 2

    kepler_zone: str = "package"
    kepler_monitor_interval_ms: int = 100
    kepler_timeout_s: float = 3.0
    low_confidence_window_factor: float = 5.0

    sample_seed: int = 42

    @property
    def dataset_path(self) -> Path:
        return resolve_path(self.dataset)

    @property
    def results_path(self) -> Path:
        return resolve_path(self.results_dir)

    def with_overrides(self, **overrides: Any) -> "Config":
        clean = {key: value for key, value in overrides.items() if value is not None}
        unknown = set(clean) - {field.name for field in fields(self)}
        if unknown:
            raise ValueError(f"unknown configuration keys: {sorted(unknown)}")
        return replace(self, **clean)


def resolve_path(value: str | Path) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    return path


def _read_yaml(path: Path | None) -> dict[str, Any]:
    if path is None or not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle) or {}
    if not isinstance(data, dict):
        raise ValueError(f"{path} must contain a YAML mapping")
    return data


def _filter_known(data: Mapping[str, Any], cls: type) -> dict[str, Any]:
    known = {field.name for field in fields(cls)}
    return {key: value for key, value in data.items() if key in known}


def load_host_config(path: str | Path | None = None) -> HostConfig:
    """Load host values, optionally overlaying a generated local file."""

    defaults = _filter_known(_read_yaml(PROJECT_ROOT / "configs" / "host.yaml"), HostConfig)
    local: dict[str, Any] = {}
    if path is not None:
        local = _filter_known(_read_yaml(resolve_path(path)), HostConfig)
    merged = {**defaults, **{key: value for key, value in local.items() if value != ""}}
    return HostConfig(**merged)


def load_config(
    path: str | Path = "configs/experiment.yaml",
    host_config_path: str | Path | None = None,
) -> tuple[Config, HostConfig]:
    """Load the experiment config and its host overlay.

    Host values are applied only when they are present and non-empty. This
    keeps the committed experiment defaults in charge while allowing the
    installer to record the detected RAPL zone, core layout and model digest.
    """

    host = load_host_config(host_config_path)
    data = _filter_known(_read_yaml(resolve_path(path)), Config)
    config = Config(**data)

    host_overrides: dict[str, Any] = {}
    if host.kepler_zone:
        host_overrides["kepler_zone"] = host.kepler_zone
    if host.kepler_monitor_interval_ms:
        host_overrides["kepler_monitor_interval_ms"] = host.kepler_monitor_interval_ms
    if host.model_digest:
        host_overrides["model_digest"] = host.model_digest
    if config.model_digest:
        host_overrides["model_digest"] = config.model_digest

    return config.with_overrides(**host_overrides), host


def apply_profile(config: Config, profile: str) -> Config:
    """Return a config adjusted for a named run profile."""

    if profile == "test":
        return config.with_overrides(
            test_questions=config.test_questions,
            test_repetitions=config.test_repetitions,
        )
    if profile == "pilot":
        return config.with_overrides(pilot_repetitions=config.pilot_repetitions)
    if profile == "full":
        return config.with_overrides(full_repetitions=config.full_repetitions)
    raise ValueError(f"unknown profile: {profile}")
