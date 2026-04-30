from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

_UNSAFE_BENCH_CHARS = re.compile(r"[^A-Za-z0-9._-]+")


def sanitize_benchmark_name(name: str) -> str:
    """Return a filesystem-safe benchmark name for artifact directories."""
    stripped = name.strip()
    if not stripped:
        raise ValueError("benchmark name cannot be empty")

    sanitized = _UNSAFE_BENCH_CHARS.sub("_", stripped).strip("._-")
    if not sanitized:
        raise ValueError(f"benchmark name has no usable characters: {name!r}")
    return sanitized


def benchmark_name_from_command(command: Sequence[str]) -> str:
    if not command:
        raise ValueError("cannot infer benchmark name from an empty command")
    return sanitize_benchmark_name(Path(command[0]).name or "program")


@dataclass(frozen=True)
class AutopointsPaths:
    """Shared artifact paths for a single benchmark workflow."""

    root: Path
    bench: str

    @classmethod
    def create(cls, root: Path, bench: str) -> "AutopointsPaths":
        return cls(
            root=root.expanduser().resolve(), bench=sanitize_benchmark_name(bench)
        )

    @property
    def bbv_dir(self) -> Path:
        return self.root / "bbv" / self.bench

    @property
    def simpoints_dir(self) -> Path:
        return self.root / "simpoints" / self.bench

    @property
    def checkpoints_dir(self) -> Path:
        return self.root / "checkpoints" / self.bench

    @property
    def raw_bbv(self) -> Path:
        return self.bbv_dir / "bb.out"

    @property
    def raw_bbv_gz(self) -> Path:
        return self.bbv_dir / "bb.out.gz"

    @property
    def simpoint_bbv_gz(self) -> Path:
        return self.bbv_dir / "simpoint.bb.gz"

    @property
    def pc_out(self) -> Path:
        return self.bbv_dir / "pc.out"

    @property
    def valgrind_log(self) -> Path:
        return self.bbv_dir / "valgrind.log"

    @property
    def simpoints(self) -> Path:
        return self.simpoints_dir / "simpoints"

    @property
    def weights(self) -> Path:
        return self.simpoints_dir / "weights"

    @property
    def simpoint_meta(self) -> Path:
        return self.simpoints_dir / "simpoint.meta.json"

    @property
    def simpoint_log(self) -> Path:
        return self.simpoints_dir / "simpoint.log"

    def checkpoint_dir(self, simpoint_index: int, start_instruction: int) -> Path:
        return (
            self.checkpoints_dir
            / f"cpt.simpoint_{simpoint_index:02d}_inst_{start_instruction}"
        )

    @property
    def checkpoint_plan(self) -> Path:
        return self.checkpoints_dir / "checkpoint.plan.json"

    @property
    def checkpoint_meta(self) -> Path:
        return self.checkpoints_dir / "checkpoint.meta.json"

    @property
    def checkpoint_log(self) -> Path:
        return self.checkpoints_dir / "gem5.log"

    @property
    def checkpoint_m5out(self) -> Path:
        return self.checkpoints_dir / "m5out"

    def ensure_collection_dirs(self) -> None:
        self.bbv_dir.mkdir(parents=True, exist_ok=True)
        self.simpoints_dir.mkdir(parents=True, exist_ok=True)
        self.checkpoints_dir.mkdir(parents=True, exist_ok=True)

    def ensure_checkpoint_dirs(self) -> None:
        self.checkpoints_dir.mkdir(parents=True, exist_ok=True)

    def as_dict(self) -> dict[str, str]:
        return {
            "root": str(self.root),
            "bbv_dir": str(self.bbv_dir),
            "simpoints_dir": str(self.simpoints_dir),
            "checkpoints_dir": str(self.checkpoints_dir),
            "raw_bbv_gz": str(self.raw_bbv_gz),
            "simpoint_bbv_gz": str(self.simpoint_bbv_gz),
            "pc_out": str(self.pc_out),
            "valgrind_log": str(self.valgrind_log),
            "simpoints": str(self.simpoints),
            "weights": str(self.weights),
            "simpoint_meta": str(self.simpoint_meta),
            "simpoint_log": str(self.simpoint_log),
            "checkpoint_plan": str(self.checkpoint_plan),
            "checkpoint_meta": str(self.checkpoint_meta),
            "checkpoint_log": str(self.checkpoint_log),
            "checkpoint_m5out": str(self.checkpoint_m5out),
        }
