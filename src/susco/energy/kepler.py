"""Direct Kepler metric scraping for per-request energy windows.

The experiment intentionally does not use Prometheus range queries for the
per-request counter. Kepler is scraped immediately before and after each
request, and the counter delta is attributed to that request.
"""

from __future__ import annotations

import math
import re
import time
from dataclasses import dataclass
from typing import Iterable

import requests

METRIC = "kepler_node_cpu_joules_total"
_LINE_RE = re.compile(
    r"^(?P<name>[a-zA-Z_:][a-zA-Z0-9_:]*)"
    r"(?:\{(?P<labels>.*)\})?"
    r"\s+(?P<value>[^\s]+)\s*$"
)
_LABEL_RE = re.compile(r'([a-zA-Z_][a-zA-Z0-9_]*)="((?:\\.|[^"\\])*)"')


class KeplerError(RuntimeError):
    """Raised when Kepler metrics cannot be read."""


@dataclass(frozen=True)
class EnergySample:
    joules: float
    zones: tuple[str, ...]
    monotonic: float
    wall_time: float


@dataclass(frozen=True)
class EnergyDelta:
    joules: float
    duration_s: float
    zones: tuple[str, ...]
    valid: bool
    reason: str = ""

    @property
    def watts(self) -> float | None:
        if not self.valid or self.duration_s <= 0:
            return None
        return self.joules / self.duration_s


def _unescape(value: str) -> str:
    return (
        value.replace(r"\\", "\\")
        .replace(r"\"", '"')
        .replace(r"\n", "\n")
    )


def parse_metrics(text: str) -> list[tuple[str, dict[str, str], float]]:
    """Parse Prometheus exposition text into (name, labels, value) tuples."""

    samples: list[tuple[str, dict[str, str], float]] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        match = _LINE_RE.match(line)
        if not match:
            continue
        labels: dict[str, str] = {}
        label_text = match.group("labels") or ""
        for key, value in _LABEL_RE.findall(label_text):
            labels[key] = _unescape(value)
        try:
            value = float(match.group("value"))
        except ValueError:
            continue
        if math.isnan(value) or math.isinf(value):
            continue
        samples.append((match.group("name"), labels, value))
    return samples


def select_zone_samples(
    samples: Iterable[tuple[str, dict[str, str], float]],
    zone: str | None = None,
) -> tuple[float, tuple[str, ...]]:
    """Select non-overlapping CPU-energy zones and sum them.

    ``zone`` may be an exact zone name (``package-0``, ``psys``) or a family
    prefix (``package``). An empty/unset zone picks ``psys`` when available,
    otherwise all package zones, otherwise the first available zone. This
    avoids the common mistake of summing overlapping package/core/dram zones.
    """

    by_zone: dict[str, float] = {}
    unlabeled: float | None = None
    for name, labels, value in samples:
        if name != METRIC:
            continue
        label_zone = labels.get("zone")
        if not label_zone:
            unlabeled = value
            continue
        by_zone[label_zone] = value

    if not by_zone:
        if unlabeled is None:
            return 0.0, ()
        return unlabeled, ("<unlabeled>",)

    def exact_or_family(family: str) -> list[str]:
        if family in by_zone:
            return [family]
        prefix = f"{family}-"
        return sorted(key for key in by_zone if key.startswith(prefix))

    requested = (zone or "").strip()
    if requested and requested != "auto":
        selected = exact_or_family(requested)
        if selected:
            return sum(by_zone[key] for key in selected), tuple(selected)
        raise KeplerError(
            f"Kepler zone '{requested}' not found; available zones: {sorted(by_zone)}"
        )

    if "psys" in by_zone:
        return by_zone["psys"], ("psys",)

    packages = exact_or_family("package")
    if packages:
        return sum(by_zone[key] for key in packages), tuple(packages)

    first = sorted(by_zone)[0]
    return by_zone[first], (first,)


class KeplerMeter:
    def __init__(
        self,
        url: str = "http://127.0.0.1:28282/metrics",
        zone: str | None = "package",
        timeout_s: float = 3.0,
    ) -> None:
        self.url = url
        self.zone = zone
        self.timeout_s = timeout_s

    def scrape(self) -> EnergySample:
        try:
            response = requests.get(self.url, timeout=self.timeout_s)
            response.raise_for_status()
            text = response.text
        except requests.RequestException as exc:
            raise KeplerError(f"cannot scrape Kepler at {self.url}: {exc}") from exc

        joules, zones = select_zone_samples(parse_metrics(text), self.zone)
        return EnergySample(
            joules=joules,
            zones=zones,
            monotonic=time.monotonic(),
            wall_time=time.time(),
        )

    def delta(self, before: EnergySample, after: EnergySample) -> EnergyDelta:
        duration = max(0.0, after.monotonic - before.monotonic)
        if after.joules < before.joules:
            return EnergyDelta(
                joules=0.0,
                duration_s=duration,
                zones=after.zones,
                valid=False,
                reason="counter decreased (reset or scrape race)",
            )
        return EnergyDelta(
            joules=after.joules - before.joules,
            duration_s=duration,
            zones=after.zones,
            valid=True,
        )

    def check(self, sleep_s: float = 1.5) -> bool:
        first = self.scrape()
        time.sleep(sleep_s)
        second = self.scrape()
        return second.joules > first.joules
