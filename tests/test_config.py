from pathlib import Path

from susco.config import HostConfig, load_config, resolve_path


def test_load_config_and_host_overlay(tmp_path):
    experiment = tmp_path / "experiment.yaml"
    experiment.write_text(
        """
model: qwen3:1.7b
kepler_zone: package
results_dir: results
""",
        encoding="utf-8",
    )
    host = tmp_path / "host.local.yaml"
    host.write_text(
        """
kepler_zone: psys
kepler_monitor_interval_ms: 50
model_digest: abc123
ollama_cpus: "1-7"
""",
        encoding="utf-8",
    )
    config, host_config = load_config(experiment, host)
    assert config.kepler_zone == "psys"
    assert config.kepler_monitor_interval_ms == 50
    assert config.model_digest == "abc123"
    assert host_config.ollama_cpus == "1-7"


def test_empty_host_values_do_not_override(tmp_path):
    experiment = tmp_path / "experiment.yaml"
    experiment.write_text("kepler_zone: package\n", encoding="utf-8")
    host = tmp_path / "host.local.yaml"
    host.write_text('kepler_zone: ""\nmodel_digest: ""\n', encoding="utf-8")
    config, _ = load_config(experiment, host)
    assert config.kepler_zone == "package"


def test_resolve_path_is_relative_to_project_root():
    path = resolve_path("data/processed/x.jsonl")
    assert path.is_absolute()
    assert path.name == "x.jsonl"
    assert path.parent.name == "processed"
    assert path.parent.parent.name == "data"


def test_host_config_defaults():
    host = HostConfig()
    assert host.kepler_zone == "package"
    assert host.kepler_monitor_interval_ms == 100
