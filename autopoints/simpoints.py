from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .paths import AutopointsPaths


@dataclass(frozen=True)
class SimPointRecord:
    simpoint_index: int
    interval: int
    cluster: int
    weight: float | None
    roi_start_instruction: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "simpoint_index": self.simpoint_index,
            "interval": self.interval,
            "cluster": self.cluster,
            "weight": self.weight,
            "roi_start_instruction": self.roi_start_instruction,
        }


@dataclass(frozen=True)
class CheckpointPoint:
    simpoint_index: int
    interval: int
    cluster: int
    weight: float | None
    roi_start_instruction: int
    checkpoint_instruction: int
    warmup_insts: int
    checkpoint_dir: Path

    def as_dict(self) -> dict[str, Any]:
        return {
            "simpoint_index": self.simpoint_index,
            "interval": self.interval,
            "cluster": self.cluster,
            "weight": self.weight,
            "roi_start_instruction": self.roi_start_instruction,
            "checkpoint_instruction": self.checkpoint_instruction,
            "warmup_insts": self.warmup_insts,
            "checkpoint_dir": str(self.checkpoint_dir),
        }


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def parse_weights(weights_path: Path) -> dict[int, float]:
    weights_by_cluster: dict[int, float] = {}
    if not weights_path.is_file():
        return weights_by_cluster

    for line_number, line in enumerate(
        weights_path.read_text(encoding="utf-8").splitlines(), start=1
    ):
        fields = line.split()
        if not fields:
            continue
        if len(fields) < 2:
            raise ValueError(
                f"invalid weights line {line_number} in {weights_path}: {line!r}"
            )
        weights_by_cluster[int(fields[1])] = float(fields[0])
    return weights_by_cluster


def parse_simpoints(
    simpoints_path: Path, weights_path: Path, interval_size: int
) -> list[SimPointRecord]:
    if not simpoints_path.is_file():
        raise FileNotFoundError(f"SimPoint file does not exist: {simpoints_path}")

    weights_by_cluster = parse_weights(weights_path)
    records: list[SimPointRecord] = []

    for line_number, line in enumerate(
        simpoints_path.read_text(encoding="utf-8").splitlines(), start=1
    ):
        fields = line.split()
        if not fields:
            continue
        if len(fields) < 2:
            raise ValueError(
                f"invalid simpoints line {line_number} in {simpoints_path}: {line!r}"
            )

        interval = int(fields[0])
        cluster = int(fields[1])
        records.append(
            SimPointRecord(
                simpoint_index=len(records),
                interval=interval,
                cluster=cluster,
                weight=weights_by_cluster.get(cluster),
                roi_start_instruction=interval * interval_size,
            )
        )

    if not records:
        raise ValueError(f"no SimPoints found in {simpoints_path}")
    return records


def build_checkpoint_points(
    records: list[SimPointRecord],
    paths: AutopointsPaths,
    requested_warmup_insts: int,
) -> list[CheckpointPoint]:
    points: list[CheckpointPoint] = []
    for record in records:
        checkpoint_instruction = max(
            0, record.roi_start_instruction - requested_warmup_insts
        )
        warmup_insts = record.roi_start_instruction - checkpoint_instruction
        points.append(
            CheckpointPoint(
                simpoint_index=record.simpoint_index,
                interval=record.interval,
                cluster=record.cluster,
                weight=record.weight,
                roi_start_instruction=record.roi_start_instruction,
                checkpoint_instruction=checkpoint_instruction,
                warmup_insts=warmup_insts,
                checkpoint_dir=paths.checkpoint_dir(
                    record.simpoint_index, checkpoint_instruction
                ),
            )
        )
    return points
