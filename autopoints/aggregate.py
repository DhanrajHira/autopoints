from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Pattern, Sequence

AGGREGATION_ALIASES = {
    "mean": "mean",
    "arithmetic_mean": "mean",
    "arithmetic-mean": "mean",
    "max": "max",
}


@dataclass(frozen=True)
class AggregationSpec:
    metric_regex: str
    aggregation: str
    regex: Pattern[str]


def compile_aggregation_specs(
    raw_specs: Sequence[Sequence[str]],
) -> list[AggregationSpec]:
    if not raw_specs:
        raise ValueError("at least one --metric REGEX AGGREGATION pair is required")

    specs: list[AggregationSpec] = []
    for raw_spec in raw_specs:
        if len(raw_spec) != 2:
            raise ValueError("each --metric requires REGEX and AGGREGATION")

        metric_regex, aggregation_name = raw_spec
        aggregation = AGGREGATION_ALIASES.get(aggregation_name)
        if aggregation is None:
            supported = ", ".join(sorted(AGGREGATION_ALIASES))
            raise ValueError(
                f"unsupported aggregation {aggregation_name!r}; supported: {supported}"
            )

        try:
            regex = re.compile(metric_regex)
        except re.error as error:
            raise ValueError(
                f"invalid metric regex {metric_regex!r}: {error}"
            ) from error
        specs.append(
            AggregationSpec(
                metric_regex=metric_regex,
                aggregation=aggregation,
                regex=regex,
            )
        )
    return specs


def numeric_value(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(parsed):
        return None
    return parsed


def load_metrics_payload(metrics_json: Path) -> dict[str, Any]:
    path = metrics_json.expanduser().resolve()
    if not path.is_file():
        raise FileNotFoundError(f"metrics JSON does not exist: {path}")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"metrics JSON root must be an object: {path}")
    return payload


def metric_names_for_spec(
    benchmark_points: dict[str, Any], spec: AggregationSpec
) -> list[str]:
    names: set[str] = set()
    for point_metrics in benchmark_points.values():
        if not isinstance(point_metrics, dict):
            continue
        for metric_name in point_metrics:
            if metric_name == "weight":
                continue
            if spec.regex.search(metric_name):
                names.add(metric_name)
    return sorted(names)


def metric_names_for_payload(
    metrics_payload: dict[str, Any], spec: AggregationSpec
) -> list[str]:
    names: set[str] = set()
    for benchmark_points in metrics_payload.values():
        if isinstance(benchmark_points, dict):
            names.update(metric_names_for_spec(benchmark_points, spec))
    return sorted(names)


def unique_metric_name_for_spec(
    metrics_payload: dict[str, Any], spec: AggregationSpec
) -> str | None:
    metric_names = metric_names_for_payload(metrics_payload, spec)
    if len(metric_names) <= 1:
        return metric_names[0] if metric_names else None

    matches = "\n".join(f"  - {metric_name}" for metric_name in metric_names)
    raise ValueError(
        f"metric regex {spec.metric_regex!r} matched {len(metric_names)} metrics; "
        f"use a more specific regex. Matched metrics:\n{matches}"
    )


def weighted_mean_for_metric(
    benchmark_points: dict[str, Any], metric_name: str
) -> tuple[float | None, float, int]:
    weighted_sum = 0.0
    weight_sum = 0.0
    samples = 0

    for point_metrics in benchmark_points.values():
        if not isinstance(point_metrics, dict) or metric_name not in point_metrics:
            continue

        weight = numeric_value(point_metrics.get("weight"))
        value = numeric_value(point_metrics.get(metric_name))
        if weight is None or value is None:
            continue

        weighted_sum += weight * value
        weight_sum += weight
        samples += 1

    if samples == 0 or weight_sum == 0:
        return None, weight_sum, samples
    return weighted_sum / weight_sum, weight_sum, samples


def max_for_metric(
    benchmark_points: dict[str, Any], metric_name: str
) -> tuple[float | None, int]:
    maximum: float | None = None
    samples = 0

    for point_metrics in benchmark_points.values():
        if not isinstance(point_metrics, dict) or metric_name not in point_metrics:
            continue

        value = numeric_value(point_metrics.get(metric_name))
        if value is None:
            continue

        maximum = value if maximum is None else max(maximum, value)
        samples += 1

    return maximum, samples


def aggregate_value_for_metric(
    benchmark_points: dict[str, Any], metric_name: str, aggregation: str
) -> tuple[float | None, int]:
    if aggregation == "mean":
        value, _, samples = weighted_mean_for_metric(benchmark_points, metric_name)
        return value, samples
    if aggregation == "max":
        return max_for_metric(benchmark_points, metric_name)
    raise ValueError(f"unsupported aggregation {aggregation!r}")


def aggregate_metrics_payload(
    metrics_payload: dict[str, Any], raw_specs: Sequence[Sequence[str]]
) -> tuple[dict[str, dict[str, dict[str, float]]], list[str]]:
    specs = compile_aggregation_specs(raw_specs)
    warnings: list[str] = []
    spec_metric_names = [
        unique_metric_name_for_spec(metrics_payload, spec) for spec in specs
    ]
    output: dict[str, dict[str, dict[str, float]]] = {}

    for benchmark in sorted(metrics_payload):
        benchmark_points = metrics_payload[benchmark]
        if not isinstance(benchmark_points, dict):
            warnings.append(
                f"warning: skipping {benchmark}: benchmark payload is not an object"
            )
            continue

        benchmark_output: dict[str, dict[str, float]] = {}
        for spec, metric_name in zip(specs, spec_metric_names):
            if metric_name is None:
                continue

            value, _ = aggregate_value_for_metric(
                benchmark_points, metric_name, spec.aggregation
            )
            if value is None:
                warnings.append(
                    f"warning: {benchmark}: no numeric samples for {metric_name!r} "
                    f"with aggregation {spec.aggregation!r}"
                )
                continue
            benchmark_output.setdefault(metric_name, {})[spec.aggregation] = value

        if benchmark_output:
            output[benchmark] = benchmark_output

    for spec, metric_name in zip(specs, spec_metric_names):
        if metric_name is None:
            warnings.append(
                f"warning: metric regex {spec.metric_regex!r} matched no metrics"
            )

    if not output:
        raise ValueError("no metrics could be aggregated")
    return output, warnings


def aggregate_metrics_json(
    metrics_json: Path, raw_specs: Sequence[Sequence[str]]
) -> tuple[dict[str, dict[str, dict[str, float]]], list[str]]:
    return aggregate_metrics_payload(load_metrics_payload(metrics_json), raw_specs)


def format_aggregate_json(payload: dict[str, dict[str, dict[str, float]]]) -> str:
    return json.dumps(payload, indent=2, allow_nan=False) + "\n"
