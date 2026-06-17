from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .paths import AutopointsPaths
from .simpoints import (
    build_checkpoint_points,
    load_json,
    parse_simpoints,
    write_json,
)

DEFAULT_WARMUP_INSTS = 10_000_000


def create_checkpoint_plan(
    paths: AutopointsPaths,
    requested_warmup_insts: int,
    memory_size: str,
    clock_frequency: str,
    allow_existing_checkpoints: bool = False,
) -> dict[str, Any]:
    if not paths.simpoint_meta.is_file():
        raise FileNotFoundError(
            f"SimPoint metadata does not exist: {paths.simpoint_meta}"
        )

    simpoint_meta = load_json(paths.simpoint_meta)
    interval_size = int(simpoint_meta["interval_size"])
    records = parse_simpoints(paths.simpoints, paths.weights, interval_size)
    points = build_checkpoint_points(records, paths, requested_warmup_insts)

    existing_checkpoints = [
        point.checkpoint_dir for point in points if point.checkpoint_dir.exists()
    ]
    if existing_checkpoints and not allow_existing_checkpoints:
        existing = "\n".join(str(path) for path in existing_checkpoints)
        raise FileExistsError(f"checkpoint directories already exist:\n{existing}")

    plan = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "benchmark": paths.bench,
        "paths": paths.as_dict(),
        "generated_from": str(paths.simpoint_meta),
        "interval_size": interval_size,
        "requested_warmup_insts": requested_warmup_insts,
        "memory_size": memory_size,
        "clock_frequency": clock_frequency,
        "program_cwd": simpoint_meta.get("program_cwd"),
        "target_command": simpoint_meta["target_command"],
        "redirects": simpoint_meta.get("redirects", {}),
        "points": [point.as_dict() for point in points],
    }
    write_json(paths.checkpoint_plan, plan)
    return plan
