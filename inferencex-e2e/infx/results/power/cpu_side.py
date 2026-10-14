"""Grace CPU-side power leg of the multinode srt-slurm package.

srt-slurm's ``cpu_power_exporter`` leg writes ``power/cpu/samples.csv`` and a
non-authoritative ``power/cpu/cpu_manifest.json`` beside the GPU DCGM package
(srt-slurm ``src/srtctl/core/power/cpu_session.py``). The pinned producer writes
the wide format: one row per (scrape, host, socket) whose ``sensor`` names the
Grace socket meter and whose ``cpu_rail_w`` / ``soc_w`` columns carry the ACPI
component rails of that socket.

Each socket's Grace socket series is integrated over the GPU leg's bound formal
window with the shared trapezoid and boundary interpolation and published as
additive metrics next to the GPU-board numbers. The CPU-only and SysIO rails
are integrated the same way when every socket reports them; a rail with gaps
drops only its own metrics. Any failure records ``cpu_power_valid=0`` with
reason codes and leaves every GPU field untouched. This module only judges the
leg; the caller decides whether an invalid leg fails the job.
"""

from __future__ import annotations

import csv
import itertools
import json
import math
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .common import _append_reason, _bracketing_sequence, _integrate_device, audit_metrics

CPU_DIRNAME = "cpu"
CPU_SAMPLES_FILENAME = "samples.csv"
CPU_MANIFEST_FILENAME = "cpu_manifest.json"

# srt-slurm contract.CPU_SAMPLES_HEADER: the wide format every pinned producer writes.
CPU_SAMPLES_HEADER = (
    "schema_version",
    "timestamp_unix",
    "hostname",
    "source",
    "sensor",
    "socket_id",
    "power_w",
    "cpu_rail_w",
    "soc_w",
    "dram_w",
    "total_power_w",
)
CPU_SAMPLES_SCHEMA_VERSION = 2

# One GB200/GB300 compute tray per worker host, two Grace sockets per tray.
SOCKETS_PER_HOST = 2
# Shared with the GPU leg (srt-slurm contract.MAX_SAMPLE_GAP_SECONDS).
MAX_SAMPLE_GAP_SECONDS = 3.0

# The headline meter: "Grace Power Socket N", the whole socket (CPU rail, SysIO,
# LPDDR5X and regulator loss). srt-slurm names its wide-format rows
# ``CPU<socket>:cpuSidePowerUsageW`` (cpu_rails.SENSOR_SUFFIXES).
SENSOR_GRACE = "grace_socket"
_GRACE_SENSOR_SUFFIX = "cpuSidePowerUsageW"
# Wide-format rail column -> published rail family: "CPU Power Socket N" (cores,
# no SysIO) and "SysIO Power Socket N". dram_w stays blank on NVL72 firmware.
RAIL_COLUMNS = {"cpu_rail_w": "cpu_rail", "soc_w": "cpu_sysio"}

_INTEGRATION_METHOD = "per_socket_trapezoidal_with_linear_boundary_interpolation"


@dataclass(frozen=True)
class CpuSampleRow:
    timestamp_unix: float
    hostname: str
    source: str
    sensor: str
    socket_id: int
    power_w: float
    rails: tuple[tuple[str, float], ...] = ()


@dataclass
class CpuPowerAudit:
    """Everything the sidecar needs to explain the CPU-side verdict."""

    valid: bool = False
    sensor_kind: str | None = None
    source: str | None = None
    expected_sockets: int = 0
    observed_sockets: int = 0
    sample_row_count: int = 0
    reason_codes: list[str] = field(default_factory=list)
    per_series_energy_j: dict[str, float] = field(default_factory=dict)
    per_series_max_sample_gap_s: dict[str, float] = field(default_factory=dict)
    metrics: dict[str, float] = field(default_factory=dict)

    def invalidate(self, *reasons: str) -> None:
        for reason in reasons:
            _append_reason(self.reason_codes, reason)
        self.valid = False
        self.metrics = {}

    def to_payload(self) -> dict[str, Any]:
        return {
            "cpu_power_valid": self.valid,
            "reason_codes": list(self.reason_codes),
            "sensor_kind": self.sensor_kind,
            "source": self.source,
            "sockets_per_host": SOCKETS_PER_HOST,
            "expected_sockets": self.expected_sockets,
            "observed_sockets": self.observed_sockets,
            "sample_row_count": self.sample_row_count,
            "integration_method": _INTEGRATION_METHOD,
            "max_sample_gap_seconds": MAX_SAMPLE_GAP_SECONDS,
            "per_series_max_sample_gap_s": dict(self.per_series_max_sample_gap_s),
            # Overflowed integrations reach here as inf; keep the sidecar strict JSON.
            "per_series_energy_j": {
                key: value if math.isfinite(value) else None
                for key, value in self.per_series_energy_j.items()
            },
            "metrics": audit_metrics(self.metrics),
        }


def is_grace_sensor(sensor: str) -> bool:
    """True for the Grace socket meter's ``CPU<socket>:cpuSidePowerUsageW`` sensor name."""
    return sensor.partition(":")[2] == _GRACE_SENSOR_SUFFIX


def _parse_row(raw: list[str]) -> CpuSampleRow | None:
    if len(raw) != len(CPU_SAMPLES_HEADER):
        return None
    try:
        schema_version = int(raw[0])
        timestamp_unix = float(raw[1])
        socket_id = int(raw[5])
        power_w = float(raw[6])
    except ValueError:
        return None
    hostname, source, sensor = raw[2], raw[3], raw[4]
    if schema_version != CPU_SAMPLES_SCHEMA_VERSION or not hostname or not source or not sensor:
        return None
    if not math.isfinite(timestamp_unix) or not math.isfinite(power_w) or power_w < 0:
        return None
    if socket_id < 0:
        return None
    rails = []
    for column, family in RAIL_COLUMNS.items():
        cell = raw[CPU_SAMPLES_HEADER.index(column)]
        if not cell:
            continue
        try:
            watts = float(cell)
        except ValueError:
            return None
        if not math.isfinite(watts) or watts < 0:
            return None
        rails.append((family, watts))
    return CpuSampleRow(
        timestamp_unix=timestamp_unix,
        hostname=hostname,
        source=source,
        sensor=sensor,
        socket_id=socket_id,
        power_w=power_w,
        rails=tuple(rails),
    )


def read_cpu_samples(path: Path) -> tuple[tuple[CpuSampleRow, ...], tuple[str, ...]]:
    """Strictly parse the wide sample file; malformed rows invalidate the leg."""
    if not path.is_file():
        return (), ("cpu_samples_missing",)
    rows: list[CpuSampleRow] = []
    reasons: list[str] = []
    try:
        with open(path, newline="", encoding="utf-8") as handle:
            reader = csv.reader(handle)
            if next(reader, None) != list(CPU_SAMPLES_HEADER):
                return (), ("cpu_samples_header_mismatch",)
            for raw in reader:
                row = _parse_row(raw)
                if row is None:
                    _append_reason(reasons, "cpu_samples_malformed")
                    continue
                rows.append(row)
    except (OSError, UnicodeDecodeError, csv.Error):
        _append_reason(reasons, "cpu_samples_malformed")
    return tuple(rows), tuple(reasons)


def _manifest_is_object(path: Path) -> bool:
    try:
        return isinstance(json.loads(path.read_text(encoding="utf-8")), dict)
    except (OSError, UnicodeDecodeError, ValueError):
        return False


SocketKey = tuple[str, int]
# (host, socket) -> family -> {timestamp: watts}; the Grace meter and its rails.
SocketSeries = dict[SocketKey, dict[str, dict[float, float]]]


def _grace_series(rows: Iterable[CpuSampleRow]) -> tuple[SocketSeries, set[str], bool]:
    """Group the Grace socket rows per socket, with the sources seen and a duplicate flag."""
    series: SocketSeries = {}
    sources: set[str] = set()
    duplicate = False
    for row in rows:
        if not is_grace_sensor(row.sensor):
            continue
        by_family = series.setdefault((row.hostname, row.socket_id), {})
        points = by_family.setdefault(SENSOR_GRACE, {})
        if row.timestamp_unix in points:
            duplicate = True
        points[row.timestamp_unix] = row.power_w
        sources.add(row.source)
        for family, watts in row.rails:
            by_family.setdefault(family, {})[row.timestamp_unix] = watts
    return series, sources, duplicate


def _max_sample_gap(samples: list[tuple[float, float]], start: float, end: float) -> float | None:
    """The widest gap between the samples bracketing ``[start, end]``; None when not bracketed."""
    sequence = _bracketing_sequence(tuple(t for t, _ in samples), start, end)
    if sequence is None:
        return None
    return max((later - earlier for earlier, later in itertools.pairwise(sequence)), default=0.0)


def validate_cpu_leg(
    cpu_dir: Path,
    *,
    window: tuple[float, float] | None,
    expected_hosts: Iterable[str],
    expected_source: str | None = None,
) -> CpuPowerAudit | None:
    """Validate and integrate the CPU sub-package; None when the run carried none."""
    if not cpu_dir.is_dir():
        if expected_source is None:
            return None
        audit = CpuPowerAudit()
        audit.invalidate("cpu_artifacts_missing")
        return audit
    audit = CpuPowerAudit()
    if not _manifest_is_object(cpu_dir / CPU_MANIFEST_FILENAME):
        audit.invalidate("cpu_manifest_invalid")
    rows, sample_reasons = read_cpu_samples(cpu_dir / CPU_SAMPLES_FILENAME)
    audit.sample_row_count = len(rows)
    audit.invalidate(*sample_reasons)
    series, sources, duplicate = _grace_series(rows)
    if duplicate or len(sources) > 1:
        audit.invalidate("cpu_samples_malformed")
    expected_keys = {
        (host, socket) for host in expected_hosts for socket in range(SOCKETS_PER_HOST)
    }
    audit.expected_sockets = len(expected_keys)
    audit.observed_sockets = len(series)
    if window is None:
        audit.invalidate("cpu_window_unavailable")
    if audit.reason_codes:
        return audit
    if not expected_keys or set(series) != expected_keys:
        audit.invalidate("cpu_socket_count_mismatch")
        return audit
    audit.sensor_kind = SENSOR_GRACE
    audit.source = next(iter(sources))
    if expected_source is not None and audit.source != expected_source:
        audit.invalidate("cpu_sensor_source_mismatch")
        return audit

    # A rail is published only when every socket reports it, gap-free; the Grace
    # meter must be gap-free on every socket or the whole leg is invalid.
    families = [SENSOR_GRACE] + [
        rail
        for rail in RAIL_COLUMNS.values()
        if all(rail in by_family for by_family in series.values())
    ]
    start, end = window
    energy: dict[str, float] = {}
    dropped_rails: set[str] = set()
    for (host, socket), by_family in sorted(series.items()):
        for family in families:
            samples = sorted(by_family[family].items())
            label = f"{host}/socket{socket}/{family}"
            gap = _max_sample_gap(samples, start, end)
            if gap is not None:
                audit.per_series_max_sample_gap_s[label] = gap
            if gap is None or gap > MAX_SAMPLE_GAP_SECONDS:
                if family != SENSOR_GRACE:
                    dropped_rails.add(family)
                elif gap is None:
                    audit.invalidate("cpu_window_not_bracketed")
                else:
                    audit.invalidate("cpu_sample_gap_exceeded")
                continue
            joules = _integrate_device(samples, start_unix=start, end_unix=end)
            audit.per_series_energy_j[label] = joules
            energy[family] = energy.get(family, 0.0) + joules
    if audit.reason_codes:
        audit.per_series_energy_j = {}
        return audit

    duration = end - start
    metrics = {
        "avg_cpu_socket_power_w": energy[SENSOR_GRACE] / duration / len(expected_keys),
        "avg_total_cpu_power_w": energy[SENSOR_GRACE] / duration,
        "total_cpu_energy_j": energy[SENSOR_GRACE],
    }
    for rail in RAIL_COLUMNS.values():
        if rail in energy and rail not in dropped_rails:
            metrics[f"avg_total_{rail}_power_w"] = energy[rail] / duration
            metrics[f"total_{rail}_energy_j"] = energy[rail]
    if any(not math.isfinite(value) for value in metrics.values()):
        audit.invalidate("non_finite_power_metric")
        return audit
    audit.metrics = metrics
    audit.valid = True
    return audit
