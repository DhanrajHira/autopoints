from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Pattern, Sequence

from .simpoints import load_json

JsonValue = int | float | str | None


@dataclass(frozen=True)
class MetricPattern:
    source: str
    regex: Pattern[str]


def compile_metric_patterns(patterns: Sequence[str]) -> list[MetricPattern]:
    if not patterns:
        raise ValueError("at least one metric regex is required")

    compiled: list[MetricPattern] = []
    for pattern in patterns:
        try:
            regex = re.compile(pattern)
        except re.error as error:
            raise ValueError(f"invalid metric regex {pattern!r}: {error}") from error
        compiled.append(MetricPattern(source=pattern, regex=regex))
    return compiled


def discover_simulation_metadata(simulation_path: Path) -> list[Path]:
    resolved = simulation_path.expanduser().resolve()
    if resolved.is_file():
        if resolved.name != "simulation.meta.json":
            raise ValueError(f"not a simulation metadata file: {resolved}")
        return [resolved]
    if not resolved.is_dir():
        raise FileNotFoundError(f"simulation path does not exist: {resolved}")

    metadata_paths: set[Path] = set()
    direct_metadata = resolved / "simulation.meta.json"
    if direct_metadata.is_file():
        metadata_paths.add(direct_metadata.resolve())

    for pattern in ("*/simulation.meta.json", "*/*/simulation.meta.json"):
        for metadata_path in resolved.glob(pattern):
            metadata_paths.add(metadata_path.resolve())
    return sorted(metadata_paths)


def parse_stat_value(raw_value: str) -> JsonValue:
    value = raw_value.strip()
    numeric = value[:-1] if value.endswith("%") else value
    lower = numeric.lower()
    if lower in {
        "nan",
        "+nan",
        "-nan",
        "inf",
        "+inf",
        "-inf",
        "infinity",
        "+infinity",
        "-infinity",
    }:
        return None

    if re.fullmatch(r"[+-]?\d+", numeric):
        return int(numeric)

    try:
        parsed = float(numeric)
    except ValueError:
        return value
    if not math.isfinite(parsed):
        return None
    return parsed


def parse_stats(stats_path: Path) -> dict[str, JsonValue]:
    stats: dict[str, JsonValue] = {}
    for line in stats_path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("----------"):
            continue

        fields = stripped.split("#", 1)[0].split()
        if len(fields) < 2:
            continue
        stats[fields[0]] = parse_stat_value(fields[1])
    return stats


def stats_path_from_metadata(metadata: dict[str, Any], metadata_path: Path) -> Path:
    m5out_dir = metadata.get("m5out_dir")
    if not m5out_dir:
        return metadata_path.parent / "m5out" / "stats.txt"

    path = Path(str(m5out_dir)).expanduser()
    if not path.is_absolute():
        path = metadata_path.parent / path
    return path / "stats.txt"


def point_from_metadata(metadata: dict[str, Any]) -> dict[str, Any]:
    point = metadata.get("point")
    return point if isinstance(point, dict) else {}


def simpoint_index_from_metadata(metadata: dict[str, Any], metadata_path: Path) -> int:
    value = metadata.get("simpoint_index")
    if value is None:
        value = point_from_metadata(metadata).get("simpoint_index")
    if value is not None:
        return int(value)

    match = re.fullmatch(r"simpoint_(\d+)", metadata_path.parent.name)
    if match:
        return int(match.group(1))
    raise ValueError(f"metadata does not include a simpoint index: {metadata_path}")


def finite_float_or_none(value: Any) -> float | None:
    if value is None:
        return None
    parsed = float(value)
    if not math.isfinite(parsed):
        return None
    return parsed


def matching_stats(
    stats: dict[str, JsonValue], patterns: Sequence[MetricPattern]
) -> tuple[dict[str, JsonValue], set[str]]:
    matches: dict[str, JsonValue] = {}
    matched_patterns: set[str] = set()
    for stat_name in sorted(stats):
        stat_patterns = [
            pattern.source for pattern in patterns if pattern.regex.search(stat_name)
        ]
        if not stat_patterns:
            continue
        matches[stat_name] = stats[stat_name]
        matched_patterns.update(stat_patterns)
    return matches, matched_patterns


def params_from_metadata(metadata: dict[str, Any]) -> dict[str, str]:
    raw = metadata.get("params")
    if not isinstance(raw, dict):
        return {}
    return {str(name): str(value) for name, value in raw.items()}


@dataclass(frozen=True)
class SimulationPointMetrics:
    benchmark: str
    simpoint_index: int
    simpoint_key: str
    metrics: dict[str, JsonValue]
    params: dict[str, str]
    matched_patterns: frozenset[str]


def extract_point_metrics(
    metadata_path: Path, patterns: Sequence[MetricPattern]
) -> tuple[SimulationPointMetrics | None, str | None]:
    """Read one simulation.meta.json and its stats.txt into a point record.

    Returns ``(record, None)`` on success or ``(None, warning)`` when the point
    should be skipped (missing/incomplete metadata, missing stats, etc.).
    """
    try:
        metadata = load_json(metadata_path)
    except (OSError, ValueError) as error:
        return None, f"warning: skipping {metadata_path}: {error}"

    status = metadata.get("status")
    if status != "completed":
        return None, f"warning: skipping {metadata_path}: status is {status!r}"

    stats_path = stats_path_from_metadata(metadata, metadata_path)
    if not stats_path.is_file():
        return (
            None,
            f"warning: skipping {metadata_path}: stats.txt does not exist: {stats_path}",
        )

    try:
        stats = parse_stats(stats_path)
        simpoint_index = simpoint_index_from_metadata(metadata, metadata_path)
        weight = finite_float_or_none(point_from_metadata(metadata).get("weight"))
    except (OSError, TypeError, ValueError) as error:
        return None, f"warning: skipping {metadata_path}: {error}"

    stats_matches, matched_patterns = matching_stats(stats, patterns)
    benchmark = str(metadata.get("benchmark") or metadata_path.parent.parent.name)
    point_metrics: dict[str, JsonValue] = {"weight": weight}
    point_metrics.update(stats_matches)
    record = SimulationPointMetrics(
        benchmark=benchmark,
        simpoint_index=simpoint_index,
        simpoint_key=f"simpoint_{simpoint_index:02d}",
        metrics=point_metrics,
        params=params_from_metadata(metadata),
        matched_patterns=frozenset(matched_patterns),
    )
    return record, None


def collect_simulation_metrics(
    simulation_path: Path, metric_patterns: Sequence[str]
) -> tuple[dict[str, dict[str, dict[str, JsonValue]]], list[str]]:
    patterns = compile_metric_patterns(metric_patterns)
    metadata_paths = discover_simulation_metadata(simulation_path)
    if not metadata_paths:
        raise ValueError(f"no simulation.meta.json files found under {simulation_path}")

    warnings: list[str] = []
    matched_patterns: set[str] = set()
    by_benchmark: dict[str, list[tuple[int, str, dict[str, JsonValue]]]] = {}

    for metadata_path in metadata_paths:
        record, warning = extract_point_metrics(metadata_path, patterns)
        if warning is not None:
            warnings.append(warning)
        if record is None:
            continue

        matched_patterns.update(record.matched_patterns)
        by_benchmark.setdefault(record.benchmark, []).append(
            (record.simpoint_index, record.simpoint_key, record.metrics)
        )

    if not by_benchmark:
        raise ValueError("no completed simulations with stats.txt were found")

    for pattern in patterns:
        if pattern.source not in matched_patterns:
            warnings.append(
                f"warning: metric regex {pattern.source!r} matched no stats"
            )

    payload: dict[str, dict[str, dict[str, JsonValue]]] = {}
    for benchmark in sorted(by_benchmark):
        payload[benchmark] = {}
        for _, simpoint_key, point_metrics in sorted(
            by_benchmark[benchmark], key=lambda item: (item[0], item[1])
        ):
            payload[benchmark][simpoint_key] = point_metrics
    return payload, warnings


def format_metrics_json(payload: dict[str, dict[str, dict[str, JsonValue]]]) -> str:
    return json.dumps(payload, indent=2, allow_nan=False) + "\n"
