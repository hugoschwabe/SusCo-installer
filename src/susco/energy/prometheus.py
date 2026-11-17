"""Small Prometheus HTTP API client used for installation/run health checks."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import requests


class PrometheusError(RuntimeError):
    """Raised when Prometheus cannot be queried."""


@dataclass(frozen=True)
class TargetStatus:
    up: bool
    value: float | None
    detail: str


class PrometheusClient:
    def __init__(self, base_url: str = "http://127.0.0.1:9090", timeout_s: float = 5.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout_s = timeout_s

    def ready(self) -> bool:
        try:
            response = requests.get(f"{self.base_url}/-/ready", timeout=self.timeout_s)
            return response.status_code == 200
        except requests.RequestException:
            return False

    def query(self, expression: str) -> list[dict[str, Any]]:
        try:
            response = requests.get(
                f"{self.base_url}/api/v1/query",
                params={"query": expression},
                timeout=self.timeout_s,
            )
            response.raise_for_status()
            payload = response.json()
        except (requests.RequestException, ValueError) as exc:
            raise PrometheusError(f"Prometheus query failed: {exc}") from exc
        if payload.get("status") != "success":
            raise PrometheusError(f"Prometheus query error: {payload}")
        return payload.get("data", {}).get("result", [])

    def scalar(self, expression: str) -> float | None:
        result = self.query(expression)
        if not result:
            return None
        try:
            return float(result[0]["value"][1])
        except (KeyError, IndexError, TypeError, ValueError):
            return None

    def kepler_target(self, job_name: str = "kepler") -> TargetStatus:
        expression = f'up{{job="{job_name}"}}'
        result = self.query(expression)
        if not result:
            return TargetStatus(False, None, f"no series for {expression}")
        try:
            value = float(result[0]["value"][1])
        except (KeyError, IndexError, TypeError, ValueError):
            return TargetStatus(False, None, f"malformed series for {expression}")
        return TargetStatus(value == 1.0, value, expression)
