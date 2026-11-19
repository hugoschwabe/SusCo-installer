"""Pre-run checks for services, model, dataset, energy and host noise."""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from .config import Config, HostConfig, load_config, resolve_path
from .energy.kepler import KeplerError, KeplerMeter
from .energy.prometheus import PrometheusClient, PrometheusError
from .ollama_client import OllamaClient, OllamaError


@dataclass(frozen=True)
class Check:
    name: str
    ok: bool
    detail: str
    required: bool = True

    @property
    def status(self) -> str:
        if self.ok:
            return "PASS"
        return "FAIL" if self.required else "WARN"


def _service_active(unit: str) -> bool | None:
    try:
        result = subprocess.run(
            ["systemctl", "is-active", "--quiet", unit],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except FileNotFoundError:
        return None
    return result.returncode == 0


def check_dataset(config: Config) -> Check:
    path = config.dataset_path
    if not path.exists():
        return Check("dataset", False, f"{path} missing; run 'make data'")
    try:
        first_line = path.read_text(encoding="utf-8").splitlines()[0]
    except (OSError, IndexError) as exc:
        return Check("dataset", False, f"cannot read {path}: {exc}")
    if '"gold"' not in first_line:
        return Check("dataset", False, f"{path} is not a prepared MATH-500 file")
    count = sum(1 for line in path.read_text(encoding="utf-8").splitlines() if line.strip())
    return Check("dataset", True, f"{count} rows at {path}")


def check_ollama(config: Config) -> Check:
    client = OllamaClient(config.ollama_url, config.model, config.request_timeout_s)
    try:
        info = client.check_model(config.model_digest)
    except OllamaError as exc:
        return Check("ollama/model", False, str(exc))
    digest = str(info.get("digest", ""))[:16]
    return Check("ollama/model", True, f"{config.model} digest={digest or 'unknown'}")


def check_kepler(config: Config) -> Check:
    meter = KeplerMeter(
        config.kepler_url,
        zone=config.kepler_zone,
        timeout_s=config.kepler_timeout_s,
    )
    try:
        before = meter.scrape()
        first = meter.scrape()
        time.sleep(0.5)
        second = meter.scrape()
    except KeplerError as exc:
        return Check("kepler", False, str(exc))
    if not second.joules > first.joules and not second.joules > before.joules:
        return Check(
            "kepler/energy",
            False,
            f"joules did not increase ({first.joules:.3f} -> {second.joules:.3f}); RAPL visible?",
        )
    zones = ", ".join(second.zones) or "unknown"
    return Check("kepler/energy", True, f"zones={zones} joules={second.joules:.3f}")


def check_prometheus(config: Config) -> Check:
    client = PrometheusClient(config.prometheus_url)
    if not client.ready():
        return Check("prometheus", False, f"{config.prometheus_url} not ready")
    try:
        target = client.kepler_target()
    except PrometheusError as exc:
        return Check("prometheus/kepler", False, str(exc))
    if not target.up:
        return Check("prometheus/kepler", False, f"kepler scrape target not up ({target.detail})")
    return Check("prometheus/kepler", True, "kepler target up")


def check_services() -> list[Check]:
    checks: list[Check] = []
    for unit in ("kepler", "ollama"):
        active = _service_active(unit)
        if active is None:
            checks.append(Check(f"service/{unit}", False, "systemctl not available", required=False))
        elif active:
            checks.append(Check(f"service/{unit}", True, "active"))
        else:
            checks.append(Check(f"service/{unit}", False, "not active; start it or run 'make install'"))
    return checks


def check_capacity(config: Config) -> list[Check]:
    checks: list[Check] = []
    target = config.results_path
    target.mkdir(parents=True, exist_ok=True)
    usage = shutil.disk_usage(target)
    free_gb = usage.free / (1024**3)
    checks.append(
        Check(
            "disk",
            free_gb >= 5,
            f"{free_gb:.1f} GiB free",
            required=free_gb >= 2,
        )
    )

    try:
        page_size = os.sysconf("SC_PAGE_SIZE")
        pages = os.sysconf("SC_PHYS_PAGES")
        mem_gb = page_size * pages / (1024**3)
    except (ValueError, OSError):
        mem_gb = 0.0
    if mem_gb:
        checks.append(
            Check("memory", mem_gb >= 8, f"{mem_gb:.1f} GiB", required=mem_gb >= 4)
        )

    try:
        load1 = os.getloadavg()[0]
        ncpu = os.cpu_count() or 1
        checks.append(
            Check(
                "background-load",
                load1 <= ncpu * 0.5,
                f"load1={load1:.2f} on {ncpu} CPUs",
                required=False,
            )
        )
    except OSError:
        pass
    return checks


def run_preflight(
    config: Config,
    host: HostConfig,
    *,
    include_services: bool = True,
    include_energy: bool = True,
) -> list[Check]:
    checks = [check_dataset(config), check_ollama(config)]
    if include_energy:
        checks.append(check_kepler(config))
        checks.append(check_prometheus(config))
    if include_services:
        checks.extend(check_services())
    checks.extend(check_capacity(config))
    return checks


def print_checks(checks: list[Check]) -> None:
    for check in checks:
        print(f"[{check.status:4}] {check.name:22} {check.detail}")


def raise_for_failures(checks: list[Check]) -> None:
    failures = [check for check in checks if not check.ok and check.required]
    if failures:
        details = "; ".join(f"{check.name}: {check.detail}" for check in failures)
        raise SystemExit(f"preflight failed: {details}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check the SusCo host before a run")
    parser.add_argument("--config", default="configs/experiment.yaml")
    parser.add_argument("--host-config", default="configs/host.local.yaml")
    parser.add_argument("--no-energy", action="store_true")
    parser.add_argument("--no-services", action="store_true")
    args = parser.parse_args(argv)

    config, host = load_config(args.config, args.host_config)
    checks = run_preflight(
        config,
        host,
        include_services=not args.no_services,
        include_energy=not args.no_energy,
    )
    print_checks(checks)
    raise_for_failures(checks)
    print("Preflight passed.")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
