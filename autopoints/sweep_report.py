from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Sequence

from .aggregate import aggregate_value_for_metric, compile_aggregation_specs
from .metrics import compile_metric_patterns, extract_point_metrics


def discover_sweep_metadata(sweep_path: Path) -> list[Path]:
    """Recursively find every simulation.meta.json under a sweep output root.

    Unlike the shallow-glob discovery used by ``metrics``, this walks the full
    tree so it reaches points nested under any number of ``<name>_<value>/``
    parameter-combination directories.
    """
    resolved = sweep_path.expanduser().resolve()
    if resolved.is_file():
        if resolved.name != "simulation.meta.json":
            raise ValueError(f"not a simulation metadata file: {resolved}")
        return [resolved]
    if not resolved.is_dir():
        raise FileNotFoundError(f"sweep path does not exist: {resolved}")
    return sorted(
        {path.resolve() for path in resolved.rglob("simulation.meta.json")}
    )


def combo_label(params: dict[str, str]) -> str:
    if not params:
        return "(no params)"
    return ",".join(f"{name}={value}" for name, value in params.items())


def collect_sweep_report(
    sweep_path: Path, metric_specs: Sequence[Sequence[str]]
) -> tuple[dict[str, Any], list[str]]:
    """Collect and aggregate sweep statistics into one sweep-aware payload.

    Groups every completed simulation by the parameter combination recorded in
    its ``params`` metadata, then aggregates each ``(combination, benchmark)``
    across its SimPoints with the requested ``--metric REGEX AGGREGATION``
    specs. Returns a tidy ``{"param_names": [...], "results": [...]}`` payload.
    """
    specs = compile_aggregation_specs(metric_specs)
    patterns = compile_metric_patterns([spec.metric_regex for spec in specs])
    metadata_paths = discover_sweep_metadata(sweep_path)
    if not metadata_paths:
        raise ValueError(f"no simulation.meta.json files found under {sweep_path}")

    warnings: list[str] = []
    # combination key -> {"params": ordered dict, "payload": {bench: {simpoint: metrics}}}
    groups: dict[tuple[tuple[str, str], ...], dict[str, Any]] = {}
    param_names: list[str] = []
    seen_param_names: set[str] = set()
    all_metric_names: set[str] = set()

    for metadata_path in metadata_paths:
        record, warning = extract_point_metrics(metadata_path, patterns)
        if warning is not None:
            warnings.append(warning)
        if record is None:
            continue

        for name in record.params:
            if name not in seen_param_names:
                seen_param_names.add(name)
                param_names.append(name)
        for metric_name in record.metrics:
            if metric_name != "weight":
                all_metric_names.add(metric_name)

        key = tuple(sorted(record.params.items()))
        group = groups.setdefault(
            key, {"params": dict(record.params), "payload": {}}
        )
        group["payload"].setdefault(record.benchmark, {})[
            record.simpoint_key
        ] = record.metrics

    if not groups:
        raise ValueError("no completed simulations with stats.txt were found")

    # Resolve each spec to a single metric name across the whole sweep so an
    # ambiguous regex fails fast instead of being silently skipped per combo.
    spec_metric_names: list[str | None] = []
    for spec in specs:
        matches = sorted(
            name for name in all_metric_names if spec.regex.search(name)
        )
        if not matches:
            warnings.append(
                f"warning: metric regex {spec.metric_regex!r} matched no stats"
            )
            spec_metric_names.append(None)
        elif len(matches) > 1:
            joined = "\n".join(f"  - {name}" for name in matches)
            raise ValueError(
                f"metric regex {spec.metric_regex!r} matched {len(matches)} metrics; "
                f"use a more specific regex. Matched metrics:\n{joined}"
            )
        else:
            spec_metric_names.append(matches[0])

    def sort_key(key: tuple[tuple[str, str], ...]) -> tuple[str, ...]:
        params = groups[key]["params"]
        return tuple(params.get(name, "") for name in param_names)

    results: list[dict[str, Any]] = []
    for key in sorted(groups, key=sort_key):
        group = groups[key]
        ordered_params = {
            name: group["params"][name]
            for name in param_names
            if name in group["params"]
        }
        payload = group["payload"]
        for benchmark in sorted(payload):
            benchmark_points = payload[benchmark]
            metrics_out: dict[str, dict[str, float]] = {}
            for spec, metric_name in zip(specs, spec_metric_names):
                if metric_name is None:
                    continue
                value, _ = aggregate_value_for_metric(
                    benchmark_points, metric_name, spec.aggregation
                )
                if value is None:
                    warnings.append(
                        f"warning: {combo_label(ordered_params)} {benchmark}: "
                        f"no numeric samples for {metric_name!r} with aggregation "
                        f"{spec.aggregation!r}"
                    )
                    continue
                metrics_out.setdefault(metric_name, {})[spec.aggregation] = value
            if metrics_out:
                results.append(
                    {
                        "params": ordered_params,
                        "benchmark": benchmark,
                        "metrics": metrics_out,
                    }
                )

    if not results:
        raise ValueError("no metrics could be aggregated")

    return {"param_names": param_names, "results": results}, warnings


def format_sweep_report_json(payload: dict[str, Any]) -> str:
    return json.dumps(payload, indent=2, allow_nan=False) + "\n"
