import pytest

from susco.energy.kepler import (
    EnergySample,
    KeplerError,
    KeplerMeter,
    parse_metrics,
    select_zone_samples,
)

METRICS = """
# HELP kepler_node_cpu_joules_total energy
kepler_node_cpu_joules_total{zone="package-0"} 100.5
kepler_node_cpu_joules_total{zone="package-1"} 50.25
kepler_node_cpu_joules_total{zone="core"} 10
kepler_node_cpu_joules_total{zone="dram"} 4
kepler_other_metric{zone="package-0"} 999
"""


def test_parse_metrics_skips_comments_and_parses_labels():
    samples = parse_metrics(METRICS)
    names = {name for name, _, _ in samples}
    assert "kepler_node_cpu_joules_total" in names
    labeled = [(labels, value) for name, labels, value in samples if name == "kepler_node_cpu_joules_total"]
    assert ({'zone': 'package-0'}, 100.5) in labeled


def test_select_package_family_sums_without_overlapping_zones():
    value, zones = select_zone_samples(parse_metrics(METRICS), "package")
    assert value == pytest.approx(150.75)
    assert zones == ("package-0", "package-1")


def test_select_exact_zone():
    value, zones = select_zone_samples(parse_metrics(METRICS), "package-1")
    assert value == pytest.approx(50.25)
    assert zones == ("package-1",)


def test_select_auto_prefers_psys():
    text = """
kepler_node_cpu_joules_total{zone="package-0"} 10
kepler_node_cpu_joules_total{zone="psys"} 20
"""
    value, zones = select_zone_samples(parse_metrics(text), None)
    assert value == pytest.approx(20)
    assert zones == ("psys",)


def test_unknown_zone_raises():
    with pytest.raises(KeplerError):
        select_zone_samples(parse_metrics(METRICS), "gpu")


def test_delta_and_counter_reset():
    meter = KeplerMeter(zone="package")
    before = EnergySample(100.0, ("package-0",), 10.0, 1.0)
    after = EnergySample(103.5, ("package-0",), 12.0, 2.0)
    delta = meter.delta(before, after)
    assert delta.valid
    assert delta.joules == pytest.approx(3.5)
    assert delta.duration_s == pytest.approx(2.0)
    assert delta.watts == pytest.approx(1.75)

    reset = meter.delta(after, EnergySample(1.0, ("package-0",), 13.0, 3.0))
    assert not reset.valid
    assert reset.joules == 0.0
