from __future__ import annotations

import concurrent.futures
import hashlib
import shlex
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

from .gem5 import resolve_executable, run_logged
from .simpoints import load_json, write_json


def default_simulation_config() -> Path:
    return (
        Path(__file__).resolve().parent / "gem5_configs" / "se_o3_restore_simpoint.py"
    )


@dataclass(frozen=True)
class SimulationPoint:
    benchmark: str
    simpoint_index: int
    checkpoint_dir: Path
    m5out_dir: Path
    log_path: Path
    metadata_path: Path
    plan_path: Path
    checkpoint_meta_path: Path
    gem5_config_source: Path
    gem5_config_copy: Path
    gem5_config_sha256: str
    point: dict[str, Any]
    roi_insts: int


@dataclass(frozen=True)
class SimulationResult:
    benchmark: str
    simpoint_index: int
    status: str
    returncode: int
    message: str
    metadata_path: str
    log_path: str


def load_checkpoint_metadata(
    checkpoint_dir: Path,
) -> tuple[Path, dict[str, Any], dict[str, Any]]:
    plan_path = checkpoint_dir / "checkpoint.plan.json"
    meta_path = checkpoint_dir / "checkpoint.meta.json"
    if not plan_path.is_file():
        raise FileNotFoundError(f"checkpoint plan does not exist: {plan_path}")
    if not meta_path.is_file():
        raise FileNotFoundError(f"checkpoint metadata does not exist: {meta_path}")
    return plan_path, load_json(plan_path), load_json(meta_path)


def discover_checkpoint_dirs(paths: Sequence[Path]) -> list[Path]:
    checkpoint_dirs: set[Path] = set()
    for path in paths:
        resolved = path.expanduser().resolve()
        if not resolved.is_dir():
            raise FileNotFoundError(f"checkpoint path does not exist: {resolved}")

        if (resolved / "checkpoint.plan.json").is_file():
            checkpoint_dirs.add(resolved)

        for plan_path in sorted(resolved.glob("*/checkpoint.plan.json")):
            checkpoint_dirs.add(plan_path.parent.resolve())
    return sorted(checkpoint_dirs)


def artifact_root_from_plan(plan: dict[str, Any], checkpoint_dir: Path) -> Path:
    plan_root = plan.get("paths", {}).get("root")
    if plan_root:
        return Path(plan_root).expanduser().resolve()
    return checkpoint_dir.parent.parent.resolve()


def benchmark_from_plan(plan: dict[str, Any], checkpoint_dir: Path) -> str:
    return str(plan.get("benchmark") or checkpoint_dir.name)


def resolve_checkpoint_dir(checkpoint_dir: Path, point: dict[str, Any]) -> Path | None:
    planned = Path(point["checkpoint_dir"]).expanduser()
    if planned.is_dir():
        return planned.resolve()

    local = checkpoint_dir / planned.name
    if local.is_dir():
        return local.resolve()
    return None


def simulation_points(
    checkpoint_dir: Path,
    plan: dict[str, Any],
    plan_path: Path,
    checkpoint_meta_path: Path,
    benchmark: str,
    output_root: Path,
    gem5_config_source: Path,
    gem5_config_copy: Path,
    gem5_config_sha256: str,
    roi_insts: int,
) -> list[SimulationPoint]:
    points: list[SimulationPoint] = []
    for point in plan["points"]:
        actual_checkpoint_dir = resolve_checkpoint_dir(checkpoint_dir, point)
        if actual_checkpoint_dir is None:
            continue

        simpoint_index = int(point["simpoint_index"])
        point_dir = output_root / f"simpoint_{simpoint_index:02d}"
        points.append(
            SimulationPoint(
                benchmark=benchmark,
                simpoint_index=simpoint_index,
                checkpoint_dir=actual_checkpoint_dir,
                m5out_dir=point_dir / "m5out",
                log_path=point_dir / "gem5.log",
                metadata_path=point_dir / "simulation.meta.json",
                plan_path=plan_path,
                checkpoint_meta_path=checkpoint_meta_path,
                gem5_config_source=gem5_config_source,
                gem5_config_copy=gem5_config_copy,
                gem5_config_sha256=gem5_config_sha256,
                point=point,
                roi_insts=roi_insts,
            )
        )
    return sorted(points, key=lambda item: (item.benchmark, item.simpoint_index))


def metadata_completed(path: Path) -> bool:
    if not path.is_file():
        return False
    try:
        return load_json(path).get("status") == "completed"
    except ValueError:
        return False


def build_gem5_simulation_command(
    gem5_bin: str,
    gem5_args: Sequence[str],
    point: SimulationPoint,
) -> list[str]:
    return [
        gem5_bin,
        "--outdir",
        str(point.m5out_dir),
        *gem5_args,
        str(point.gem5_config_copy),
        "--checkpoint-plan",
        str(point.plan_path),
        "--simpoint-index",
        str(point.simpoint_index),
        "--checkpoint-dir",
        str(point.checkpoint_dir),
        "--roi-insts",
        str(point.roi_insts),
    ]


def write_simulation_metadata(
    point: SimulationPoint,
    command: Sequence[str],
    status: str,
    returncode: int | None = None,
) -> None:
    payload: dict[str, Any] = {
        "status": status,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "benchmark": point.benchmark,
        "checkpoint_plan": str(point.plan_path),
        "checkpoint_meta": str(point.checkpoint_meta_path),
        "checkpoint_dir": str(point.checkpoint_dir),
        "gem5_config_source": str(point.gem5_config_source),
        "gem5_config_copy": str(point.gem5_config_copy),
        "gem5_config_sha256": point.gem5_config_sha256,
        "simpoint_index": point.simpoint_index,
        "roi_insts": point.roi_insts,
        "warmup_insts": int(point.point["warmup_insts"]),
        "m5out_dir": str(point.m5out_dir),
        "gem5_log": str(point.log_path),
        "gem5_command": list(command),
        "point": point.point,
    }
    if returncode is not None:
        payload["gem5_returncode"] = returncode
    if status == "completed":
        payload["completed_at"] = datetime.now(timezone.utc).isoformat()
    write_json(point.metadata_path, payload)


def run_one_simulation(
    point: SimulationPoint,
    gem5_bin: str,
    gem5_args: Sequence[str],
    force: bool,
    dry_run: bool,
) -> SimulationResult:
    if metadata_completed(point.metadata_path) and not force:
        return SimulationResult(
            benchmark=point.benchmark,
            simpoint_index=point.simpoint_index,
            status="skipped_completed",
            returncode=0,
            message="existing completed simulation metadata found",
            metadata_path=str(point.metadata_path),
            log_path=str(point.log_path),
        )

    command = build_gem5_simulation_command(
        gem5_bin=gem5_bin,
        gem5_args=gem5_args,
        point=point,
    )
    point.metadata_path.parent.mkdir(parents=True, exist_ok=True)
    write_simulation_metadata(
        point=point,
        command=command,
        status="planned",
    )

    if dry_run:
        print(
            f"DRY-RUN {point.benchmark} simpoint {point.simpoint_index:02d}: "
            f"{shlex.join(command)}"
        )
        return SimulationResult(
            benchmark=point.benchmark,
            simpoint_index=point.simpoint_index,
            status="dry_run",
            returncode=0,
            message=shlex.join(command),
            metadata_path=str(point.metadata_path),
            log_path=str(point.log_path),
        )

    returncode = run_logged(command, point.log_path)
    status = "completed" if returncode == 0 else "gem5_failed"
    write_simulation_metadata(
        point=point,
        command=command,
        status=status,
        returncode=returncode,
    )

    return SimulationResult(
        benchmark=point.benchmark,
        simpoint_index=point.simpoint_index,
        status=status,
        returncode=returncode,
        message=(
            "completed" if returncode == 0 else f"gem5 failed; see {point.log_path}"
        ),
        metadata_path=str(point.metadata_path),
        log_path=str(point.log_path),
    )


def copy_gem5_config_snapshot(
    source: Path, source_bytes: bytes, source_sha256: str, simulation_root: Path
) -> Path:
    simulation_root.mkdir(parents=True, exist_ok=True)
    suffix = source.suffix or ".py"
    destination = simulation_root / f"gem5-config-{source_sha256[:16]}{suffix}"
    if not destination.exists() or destination.read_bytes() != source_bytes:
        destination.write_bytes(source_bytes)
        destination.chmod(source.stat().st_mode & 0o777)
    return destination


def simulate_checkpoints(
    checkpoint_paths: Sequence[Path],
    gem5_bin_value: str,
    gem5_config: Path | None,
    gem5_args: Sequence[str],
    roi_insts: int | None,
    output_dir: Path | None,
    jobs: int | None,
    force: bool,
    dry_run: bool,
) -> int:
    try:
        checkpoint_dirs = discover_checkpoint_dirs(checkpoint_paths)
        gem5_bin = resolve_executable(gem5_bin_value, "gem5 binary")
        config_path = (
            (gem5_config or default_simulation_config()).expanduser().resolve()
        )
        if not config_path.is_file():
            raise FileNotFoundError(
                f"gem5 simulation config does not exist: {config_path}"
            )
        config_bytes = config_path.read_bytes()
        config_sha256 = hashlib.sha256(config_bytes).hexdigest()
    except (FileNotFoundError, ValueError) as error:
        print(f"error: {error}")
        return 2

    if not checkpoint_dirs:
        print("error: no checkpoint.plan.json files found")
        return 1

    points: list[SimulationPoint] = []
    custom_output_root = output_dir.expanduser().resolve() if output_dir else None
    config_copies: dict[Path, Path] = {}
    for checkpoint_dir in checkpoint_dirs:
        try:
            plan_path, plan, checkpoint_meta = load_checkpoint_metadata(checkpoint_dir)
        except (FileNotFoundError, ValueError) as error:
            print(f"warning: skipping {checkpoint_dir}: {error}")
            continue

        benchmark = benchmark_from_plan(plan, checkpoint_dir)
        artifact_root = artifact_root_from_plan(plan, checkpoint_dir)
        simulation_root = (
            custom_output_root
            if custom_output_root is not None
            else artifact_root / "simulations"
        )
        output_root = (
            simulation_root / benchmark
            if custom_output_root is not None
            else simulation_root / benchmark
        )
        config_copy = config_copies.get(simulation_root)
        if config_copy is None:
            config_copy = copy_gem5_config_snapshot(
                source=config_path,
                source_bytes=config_bytes,
                source_sha256=config_sha256,
                simulation_root=simulation_root,
            )
            config_copies[simulation_root] = config_copy
        selected_roi_insts = (
            roi_insts if roi_insts is not None else int(plan["interval_size"])
        )
        checkpoint_meta_path = checkpoint_dir / "checkpoint.meta.json"
        benchmark_points = simulation_points(
            checkpoint_dir=checkpoint_dir,
            plan=plan,
            plan_path=plan_path,
            checkpoint_meta_path=checkpoint_meta_path,
            benchmark=benchmark,
            output_root=output_root,
            gem5_config_source=config_path,
            gem5_config_copy=config_copy,
            gem5_config_sha256=config_sha256,
            roi_insts=selected_roi_insts,
        )
        if len(benchmark_points) != len(plan["points"]):
            missing = len(plan["points"]) - len(benchmark_points)
            print(
                f"warning: {benchmark}: skipping {missing} planned checkpoints missing on disk"
            )
        if checkpoint_meta.get("status") != "completed":
            print(
                f"warning: {benchmark}: checkpoint metadata status is "
                f"{checkpoint_meta.get('status')!r}"
            )
        points.extend(benchmark_points)

    if not points:
        print("error: no checkpoint directories from discovered plans exist on disk")
        return 1

    workers = min(jobs or len(points), len(points))

    print(
        f"Simulating {len(points)} checkpoints from {len(checkpoint_dirs)} checkpoint plans; "
        f"parallel jobs: {workers}"
    )

    results: list[SimulationResult] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
        futures = [
            executor.submit(
                run_one_simulation,
                point,
                gem5_bin,
                gem5_args,
                force,
                dry_run,
            )
            for point in points
        ]
        completed = 0
        total = len(futures)
        for future in concurrent.futures.as_completed(futures):
            result = future.result()
            results.append(result)
            completed += 1
            print(
                f"[{completed}/{total}] {result.benchmark} simpoint "
                f"{result.simpoint_index:02d}: {result.status}"
            )

    failures = [result for result in results if result.returncode != 0]
    if failures:
        for failure in sorted(
            failures, key=lambda item: (item.benchmark, item.simpoint_index)
        ):
            print(
                f"{failure.benchmark} simpoint {failure.simpoint_index:02d}: {failure.message}"
            )
        return 1
    return 0
