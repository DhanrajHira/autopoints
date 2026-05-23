from __future__ import annotations

import argparse
import gzip
import shlex
import shutil
import subprocess
import sys
from contextlib import ExitStack, contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Sequence

from .aggregate import aggregate_metrics_json, format_aggregate_json
from .checkpoint import DEFAULT_WARMUP_INSTS, create_checkpoint_plan
from .gem5 import (
    PerfEventParanoidError,
    build_gem5_checkpoint_command,
    default_checkpoint_config,
    ensure_perf_event_paranoid_is_one,
    resolve_executable,
    run_logged,
    wrap_with_sg_kvm,
)
from .metrics import collect_simulation_metrics, format_metrics_json
from .paths import AutopointsPaths, benchmark_name_from_command, sanitize_benchmark_name
from .simulate import default_simulation_config, simulate_checkpoints
from .simpoints import parse_simpoints, write_json

DEFAULT_INTERVAL_SIZE = 100_000_000
DEFAULT_MAX_K = 30
DEFAULT_NUM_INIT_SEEDS = 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="autopoints",
        description="Collect SimPoints and create gem5 checkpoints for their regions of interest.",
    )
    subparsers = parser.add_subparsers(dest="subcommand", required=True)

    collect = subparsers.add_parser(
        "collect",
        help="Collect Valgrind exp-bbv vectors and run SimPoint.",
        description=(
            "Run a program under Valgrind exp-bbv, then run SimPoint on the "
            "collected basic block vectors."
        ),
    )
    add_artifact_args(collect, bench_required=False)
    collect.add_argument(
        "--simpoint-bin",
        required=True,
        help="Path to the SimPoint binary, or a binary name available on PATH.",
    )
    collect.add_argument(
        "--interval-size",
        type=positive_int,
        default=DEFAULT_INTERVAL_SIZE,
        help=f"Instruction interval size for exp-bbv. Default: {DEFAULT_INTERVAL_SIZE}.",
    )
    collect.add_argument(
        "--max-k",
        type=positive_int,
        default=DEFAULT_MAX_K,
        help=f"Maximum number of SimPoint clusters to search. Default: {DEFAULT_MAX_K}.",
    )
    collect.add_argument(
        "--num-init-seeds",
        type=positive_int,
        default=DEFAULT_NUM_INIT_SEEDS,
        help=f"Number of SimPoint initialization seeds. Default: {DEFAULT_NUM_INIT_SEEDS}.",
    )
    collect.add_argument(
        "--coverage-pct",
        type=float,
        help="Optional SimPoint coverage percentage, for example 0.9 for 90%% coverage.",
    )
    collect.add_argument(
        "--valgrind-bin",
        default="valgrind",
        help="Valgrind binary to execute. Default: valgrind.",
    )
    collect.add_argument(
        "--program-cwd",
        type=Path,
        help="Working directory for the target program while running under Valgrind and gem5.",
    )
    add_redirect_args(collect)
    collect.add_argument(
        "--valgrind-arg",
        action="append",
        default=[],
        help="Additional Valgrind argument. May be passed multiple times.",
    )
    collect.add_argument(
        "--simpoint-arg",
        action="append",
        default=[],
        help="Additional SimPoint argument. May be passed multiple times.",
    )
    collect.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the commands and write the metadata without running them.",
    )
    collect.add_argument(
        "target_command",
        nargs=argparse.REMAINDER,
        help="Target program and arguments. Separate with '--'.",
    )
    collect.set_defaults(func=run_collect)

    checkpoint = subparsers.add_parser(
        "checkpoint",
        help="Create gem5 KVM checkpoints for collected SimPoints.",
        description="Use generated SimPoints to create warmup-adjusted gem5 KVM checkpoints.",
    )
    add_artifact_args(checkpoint, bench_required=False)
    checkpoint.add_argument(
        "--gem5-bin",
        required=True,
        help="Path to the gem5 binary, for example ../gem5/build/X86/gem5.opt.",
    )
    checkpoint.add_argument(
        "--gem5-config",
        type=Path,
        help="Override the gem5 checkpoint config script. Defaults to autopoints' SE KVM config.",
    )
    checkpoint.add_argument(
        "--gem5-arg",
        action="append",
        default=[],
        help="Additional gem5 argument placed before the config script. May be passed multiple times.",
    )
    checkpoint.add_argument(
        "--warmup-insts",
        type=non_negative_int,
        default=DEFAULT_WARMUP_INSTS,
        help=f"Instructions before ROI where checkpoints are taken. Default: {DEFAULT_WARMUP_INSTS}.",
    )
    checkpoint.add_argument(
        "--memory-size",
        default="2GiB",
        help="Memory size for checkpoint creation. Detailed restores must use the same size. Default: 2GiB.",
    )
    checkpoint.add_argument(
        "--clock-frequency",
        default="3GHz",
        help="Board clock frequency for checkpoint creation. Default: 3GHz.",
    )
    checkpoint.add_argument(
        "--dry-run",
        action="store_true",
        help="Write the checkpoint plan and print the gem5 command without running it.",
    )
    checkpoint.set_defaults(func=run_checkpoint)

    simulate = subparsers.add_parser(
        "simulate",
        help="Restore and simulate checkpoints from one or more checkpoint directories.",
        description=(
            "Run detailed gem5 simulations for every checkpoint listed in an "
            "autopoints checkpoint.plan.json and present on disk. Each input may be "
            "a benchmark checkpoint directory or a parent checkpoints/ directory."
        ),
    )
    simulate.add_argument(
        "checkpoint_paths",
        nargs="+",
        type=Path,
        help="checkpoint directory/directories to scan for checkpoint.plan.json files.",
    )
    simulate.add_argument(
        "--gem5-bin",
        required=True,
        help="Path to the gem5 binary, for example ../gem5/build/X86/gem5.opt.",
    )
    simulate.add_argument(
        "--gem5-config",
        type=Path,
        help=f"Detailed simulation config. Default: {default_simulation_config()}.",
    )
    simulate.add_argument(
        "--gem5-arg",
        action="append",
        default=[],
        help="Additional gem5 argument placed before the config script. May be passed multiple times.",
    )
    simulate.add_argument(
        "--roi-insts",
        type=positive_int,
        help="ROI instructions to simulate after warmup. Default: checkpoint plan interval_size.",
    )
    simulate.add_argument(
        "--jobs",
        type=positive_int,
        help="Number of checkpoint simulations to run in parallel. Default: all checkpoints.",
    )
    simulate.add_argument(
        "--force",
        action="store_true",
        help="Run even when simulation.meta.json already says completed.",
    )
    simulate.add_argument(
        "--dry-run",
        action="store_true",
        help="Print gem5 commands and write planned metadata without running them.",
    )
    simulate.set_defaults(func=run_simulate)

    metrics = subparsers.add_parser(
        "metrics",
        help="Extract weighted metrics from detailed simulation outputs.",
        description=(
            "Read autopoints simulation.meta.json files and gem5 stats.txt files, "
            "then emit one JSON object containing SimPoint weights and all stats "
            "whose names match the requested regex patterns."
        ),
    )
    metrics.add_argument(
        "simulation_path",
        type=Path,
        help="simulations/ root, simulations/<benchmark>, a simpoint directory, or simulation.meta.json.",
    )
    metrics.add_argument(
        "metric_patterns",
        nargs="+",
        help="Regex pattern matched against full gem5 stat names, for example 'ipc'.",
    )
    metrics.add_argument(
        "--output",
        type=Path,
        help="Write JSON metrics to this file instead of stdout.",
    )
    metrics.set_defaults(func=run_metrics)

    aggregate = subparsers.add_parser(
        "aggregate",
        help="Aggregate collected metrics across weighted SimPoints.",
        description=(
            "Read JSON produced by autopoints metrics and write a new JSON file "
            "containing weighted per-benchmark aggregate metric values."
        ),
    )
    aggregate.add_argument(
        "metrics_json",
        type=Path,
        help="JSON file produced by autopoints metrics.",
    )
    aggregate.add_argument(
        "--metric",
        action="append",
        nargs=2,
        required=True,
        metavar=("REGEX", "AGGREGATION"),
        help="Metric regex and aggregation. Supported aggregations: mean (weighted arithmetic mean), max. May be repeated.",
    )
    aggregate.add_argument(
        "--output",
        type=Path,
        required=True,
        help="Write aggregated JSON metrics to this file.",
    )
    aggregate.set_defaults(func=run_aggregate)
    return parser


def add_artifact_args(parser: argparse.ArgumentParser, bench_required: bool) -> None:
    parser.add_argument(
        "--bench",
        required=bench_required,
        help="Benchmark name used under bbv/, simpoints/, and checkpoints/. For checkpoint, omit to discover all collected benchmarks.",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("."),
        help="Artifact root containing bbv/, simpoints/, and checkpoints/. Default: current directory.",
    )


def add_redirect_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--stdin",
        type=Path,
        help="Redirect target stdin from this file while collecting and checkpointing.",
    )

    stdout = parser.add_mutually_exclusive_group()
    stdout.add_argument(
        "--stdout",
        type=Path,
        help="Redirect target stdout to this file, truncating it first.",
    )
    stdout.add_argument(
        "--stdout-append",
        type=Path,
        help="Redirect target stdout to this file in append mode.",
    )

    stderr = parser.add_mutually_exclusive_group()
    stderr.add_argument(
        "--stderr",
        type=Path,
        help="Redirect target stderr to this file, truncating it first.",
    )
    stderr.add_argument(
        "--stderr-append",
        type=Path,
        help="Redirect target stderr to this file in append mode.",
    )


def positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be positive")
    return parsed


def non_negative_int(value: str) -> int:
    parsed = int(value)
    if parsed < 0:
        raise argparse.ArgumentTypeError("must be non-negative")
    return parsed


def normalize_command(command: Sequence[str]) -> list[str]:
    normalized = list(command)
    if normalized and normalized[0] == "--":
        normalized = normalized[1:]
    if not normalized:
        raise ValueError("missing target command; pass it after '--'")
    return normalized


def resolve_target_command(
    command: Sequence[str], program_cwd: Path | None
) -> list[str]:
    resolved = list(command)
    program = Path(resolved[0]).expanduser()
    if program.parent != Path(".") or program.is_absolute():
        resolved_program = (
            program if program.is_absolute() else (program_cwd or Path.cwd()) / program
        )
        resolved_program = resolved_program.resolve()
        if not resolved_program.is_file():
            raise FileNotFoundError(
                f"target program does not exist: {resolved_program}"
            )
        resolved[0] = str(resolved_program)
        return resolved

    path_program = shutil.which(resolved[0])
    if path_program is None:
        raise FileNotFoundError(f"target program was not found on PATH: {resolved[0]}")
    resolved[0] = path_program
    return resolved


def resolve_redirect_path(path: Path, program_cwd: Path | None) -> Path:
    expanded = path.expanduser()
    if expanded.is_absolute():
        return expanded.resolve()
    return ((program_cwd or Path.cwd()) / expanded).resolve()


def collect_redirects(
    args: argparse.Namespace, program_cwd: Path | None
) -> dict[str, str]:
    redirects: dict[str, str] = {}
    if args.stdin:
        redirects["stdin"] = str(resolve_redirect_path(args.stdin, program_cwd))
    if args.stdout:
        redirects["stdout"] = str(resolve_redirect_path(args.stdout, program_cwd))
    if args.stdout_append:
        redirects["stdout-append"] = str(
            resolve_redirect_path(args.stdout_append, program_cwd)
        )
    if args.stderr:
        redirects["stderr"] = str(resolve_redirect_path(args.stderr, program_cwd))
    if args.stderr_append:
        redirects["stderr-append"] = str(
            resolve_redirect_path(args.stderr_append, program_cwd)
        )
    return redirects


@contextmanager
def opened_redirects(
    redirects: dict[str, str],
) -> Iterator[dict[str, object | None]]:
    with ExitStack() as stack:
        handles: dict[str, object | None] = {
            "stdin": None,
            "stdout": None,
            "stderr": None,
        }
        if redirects.get("stdin"):
            handles["stdin"] = stack.enter_context(Path(redirects["stdin"]).open("rb"))
        if redirects.get("stdout"):
            target = Path(redirects["stdout"])
            target.parent.mkdir(parents=True, exist_ok=True)
            handles["stdout"] = stack.enter_context(target.open("wb"))
        if redirects.get("stdout-append"):
            target = Path(redirects["stdout-append"])
            target.parent.mkdir(parents=True, exist_ok=True)
            handles["stdout"] = stack.enter_context(target.open("ab"))
        if redirects.get("stderr"):
            target = Path(redirects["stderr"])
            target.parent.mkdir(parents=True, exist_ok=True)
            handles["stderr"] = stack.enter_context(target.open("wb"))
        if redirects.get("stderr-append"):
            target = Path(redirects["stderr-append"])
            target.parent.mkdir(parents=True, exist_ok=True)
            handles["stderr"] = stack.enter_context(target.open("ab"))
        yield handles


def compress_file(source: Path, destination: Path) -> None:
    with source.open("rb") as input_file, gzip.open(destination, "wb") as output_file:
        shutil.copyfileobj(input_file, output_file)


def remove_collection_outputs(paths: AutopointsPaths) -> None:
    for path in (
        paths.raw_bbv,
        paths.raw_bbv_gz,
        paths.simpoint_bbv_gz,
        paths.pc_out,
        paths.simpoints,
        paths.weights,
    ):
        if path.exists():
            path.unlink()


def discover_collected_benchmarks(output_dir: Path) -> list[str]:
    simpoints_root = output_dir.expanduser().resolve() / "simpoints"
    if not simpoints_root.is_dir():
        return []

    benches = []
    for entry in sorted(simpoints_root.iterdir()):
        if entry.is_dir() and (entry / "simpoint.meta.json").is_file():
            benches.append(entry.name)
    return benches


def run_collect(args: argparse.Namespace) -> int:
    try:
        target_command = normalize_command(args.target_command)
        bench = (
            sanitize_benchmark_name(args.bench)
            if args.bench
            else benchmark_name_from_command(target_command)
        )
        simpoint_bin = resolve_executable(args.simpoint_bin, "SimPoint binary")
        valgrind_bin = resolve_executable(args.valgrind_bin, "Valgrind binary")
    except (FileNotFoundError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2

    if args.coverage_pct is not None and not 0 < args.coverage_pct <= 1:
        print(
            "error: --coverage-pct must be greater than 0 and less than or equal to 1",
            file=sys.stderr,
        )
        return 2

    program_cwd = args.program_cwd.expanduser().resolve() if args.program_cwd else None
    if program_cwd is not None and not program_cwd.is_dir():
        print(
            f"error: --program-cwd is not a directory: {program_cwd}", file=sys.stderr
        )
        return 2

    redirects = collect_redirects(args, program_cwd)

    try:
        target_command = resolve_target_command(target_command, program_cwd)
    except FileNotFoundError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2

    paths = AutopointsPaths.create(args.output_dir, bench)
    paths.ensure_collection_dirs()
    remove_collection_outputs(paths)

    valgrind_command = [
        valgrind_bin,
        "--tool=exp-bbv",
        f"--bb-out-file={paths.raw_bbv}",
        f"--pc-out-file={paths.pc_out}",
        f"--interval-size={args.interval_size}",
        f"--log-file={paths.valgrind_log}",
        *args.valgrind_arg,
        *target_command,
    ]

    simpoint_command = [
        simpoint_bin,
        "-maxK",
        str(args.max_k),
        "-numInitSeeds",
        str(args.num_init_seeds),
        "-loadFVFile",
        str(paths.simpoint_bbv_gz),
        "-inputVectorsGzipped",
        "-saveSimpoints",
        str(paths.simpoints),
        "-saveSimpointWeights",
        str(paths.weights),
        *args.simpoint_arg,
    ]
    if args.coverage_pct is not None:
        simpoint_command.extend(["-coveragePct", str(args.coverage_pct)])

    metadata: dict[str, object] = {
        "status": "planned",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "benchmark": bench,
        "interval_size": args.interval_size,
        "max_k": args.max_k,
        "num_init_seeds": args.num_init_seeds,
        "coverage_pct": args.coverage_pct,
        "program_cwd": str(program_cwd) if program_cwd else None,
        "target_command": target_command,
        "redirects": redirects,
        "valgrind_command": valgrind_command,
        "simpoint_command": simpoint_command,
        "paths": paths.as_dict(),
        "selected_points": [],
    }
    write_json(paths.simpoint_meta, metadata)

    print(f"Artifact root: {paths.root}")
    print(f"Benchmark: {paths.bench}")
    print(f"Valgrind command: {shlex.join(valgrind_command)}")
    print(f"SimPoint command: {shlex.join(simpoint_command)}")

    if args.dry_run:
        print(f"Dry run complete. Metadata: {paths.simpoint_meta}")
        return 0

    print("Running target under Valgrind exp-bbv...")
    try:
        with opened_redirects(redirects) as redirect_handles:
            valgrind_result = subprocess.run(
                valgrind_command,
                cwd=str(program_cwd) if program_cwd else None,
                stdin=redirect_handles["stdin"],
                stdout=redirect_handles["stdout"],
                stderr=redirect_handles["stderr"],
                check=False,
            )
    except (OSError, ValueError) as error:
        metadata["status"] = "valgrind_failed"
        metadata["error"] = str(error)
        write_json(paths.simpoint_meta, metadata)
        print(f"Valgrind run failed before launch: {error}", file=sys.stderr)
        return 1
    if valgrind_result.returncode != 0:
        metadata["status"] = "valgrind_failed"
        metadata["valgrind_returncode"] = valgrind_result.returncode
        write_json(paths.simpoint_meta, metadata)
        print(
            f"Valgrind run failed with exit code {valgrind_result.returncode}. See {paths.valgrind_log}.",
            file=sys.stderr,
        )
        return valgrind_result.returncode

    if not paths.raw_bbv.is_file() or paths.raw_bbv.stat().st_size == 0:
        metadata["status"] = "bbv_missing"
        write_json(paths.simpoint_meta, metadata)
        print(
            f"Valgrind did not produce a non-empty BBV file at {paths.raw_bbv}.",
            file=sys.stderr,
        )
        return 1

    compress_file(paths.raw_bbv, paths.raw_bbv_gz)
    shutil.copy2(paths.raw_bbv_gz, paths.simpoint_bbv_gz)
    paths.raw_bbv.unlink()
    metadata["status"] = "bbv_collected"
    write_json(paths.simpoint_meta, metadata)

    print("Running SimPoint clustering...")
    simpoint_result = run_logged(
        simpoint_command, paths.simpoint_log, cwd=paths.simpoints_dir
    )
    if simpoint_result != 0:
        metadata["status"] = "simpoint_failed"
        metadata["simpoint_returncode"] = simpoint_result
        write_json(paths.simpoint_meta, metadata)
        print(
            f"SimPoint failed with exit code {simpoint_result}. See {paths.simpoint_log}.",
            file=sys.stderr,
        )
        return simpoint_result

    records = parse_simpoints(paths.simpoints, paths.weights, args.interval_size)
    metadata["status"] = "completed"
    metadata["selected_points"] = [record.as_dict() for record in records]
    metadata["completed_at"] = datetime.now(timezone.utc).isoformat()
    write_json(paths.simpoint_meta, metadata)

    print(f"SimPoint BBV: {paths.simpoint_bbv_gz}")
    print(f"Raw BBV: {paths.raw_bbv_gz}")
    print(f"PC map: {paths.pc_out}")
    print(f"SimPoints: {paths.simpoints}")
    print(f"Weights: {paths.weights}")
    print(f"Metadata: {paths.simpoint_meta}")
    return 0


def run_checkpoint(args: argparse.Namespace) -> int:
    if args.bench:
        try:
            bench = sanitize_benchmark_name(args.bench)
        except ValueError as error:
            print(f"error: {error}", file=sys.stderr)
            return 2
        return run_checkpoint_for_bench(args, bench)

    benches = discover_collected_benchmarks(args.output_dir)
    if not benches:
        print(
            f"error: no collected benchmarks found under {(args.output_dir.expanduser().resolve() / 'simpoints')}",
            file=sys.stderr,
        )
        return 2

    print(f"Discovered {len(benches)} collected benchmark(s).")
    failures: list[tuple[str, int]] = []
    for bench in benches:
        print(f"\n=== checkpoint {bench} ===")
        returncode = run_checkpoint_for_bench(args, bench)
        if returncode != 0:
            failures.append((bench, returncode))

    if failures:
        print("\nCheckpoint failures:", file=sys.stderr)
        for bench, returncode in failures:
            print(f"  {bench}: exit {returncode}", file=sys.stderr)
        return 1
    return 0


def run_checkpoint_for_bench(args: argparse.Namespace, bench: str) -> int:
    try:
        gem5_bin = resolve_executable(args.gem5_bin, "gem5 binary")
        gem5_config = (
            (args.gem5_config or default_checkpoint_config()).expanduser().resolve()
        )
        if not gem5_config.is_file():
            raise FileNotFoundError(f"gem5 config does not exist: {gem5_config}")
    except (FileNotFoundError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2

    paths = AutopointsPaths.create(args.output_dir, bench)
    paths.ensure_checkpoint_dirs()

    try:
        plan = create_checkpoint_plan(
            paths=paths,
            requested_warmup_insts=args.warmup_insts,
            memory_size=args.memory_size,
            clock_frequency=args.clock_frequency,
            allow_existing_checkpoints=args.dry_run,
        )
    except (FileNotFoundError, FileExistsError, KeyError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2

    gem5_command = build_gem5_checkpoint_command(
        gem5_bin=gem5_bin,
        config_path=gem5_config,
        plan_path=paths.checkpoint_plan,
        m5out_dir=paths.checkpoint_m5out,
        gem5_args=args.gem5_arg,
    )
    launch_command = wrap_with_sg_kvm(gem5_command)

    metadata: dict[str, object] = {
        "status": "planned",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "benchmark": bench,
        "checkpoint_plan": str(paths.checkpoint_plan),
        "gem5_command": gem5_command,
        "launch_command": launch_command,
        "points": plan["points"],
        "paths": paths.as_dict(),
    }
    write_json(paths.checkpoint_meta, metadata)

    print(f"Artifact root: {paths.root}")
    print(f"Benchmark: {paths.bench}")
    print(f"Checkpoint plan: {paths.checkpoint_plan}")
    print(f"gem5 command: {shlex.join(gem5_command)}")
    print(f"Launch command: {shlex.join(launch_command)}")

    if args.dry_run:
        print(f"Dry run complete. Metadata: {paths.checkpoint_meta}")
        return 0

    try:
        ensure_perf_event_paranoid_is_one()
    except PerfEventParanoidError as error:
        metadata["status"] = "preflight_failed"
        metadata["preflight_error"] = str(error)
        write_json(paths.checkpoint_meta, metadata)
        print(f"error: {error}", file=sys.stderr)
        return 1

    print("Running gem5 checkpoint creation through sg kvm...")
    returncode = run_logged(launch_command, paths.checkpoint_log, cwd=paths.root)
    metadata["gem5_returncode"] = returncode
    if returncode != 0:
        metadata["status"] = "gem5_failed"
        write_json(paths.checkpoint_meta, metadata)
        print(
            f"gem5 failed with exit code {returncode}. See {paths.checkpoint_log}.",
            file=sys.stderr,
        )
        return returncode

    produced = [
        point for point in plan["points"] if Path(point["checkpoint_dir"]).is_dir()
    ]
    metadata["produced_checkpoints"] = produced
    metadata["completed_at"] = datetime.now(timezone.utc).isoformat()
    if len(produced) != len(plan["points"]):
        metadata["status"] = "checkpoint_missing"
        write_json(paths.checkpoint_meta, metadata)
        print(
            f"gem5 exited successfully but produced {len(produced)} of {len(plan['points'])} checkpoints. "
            f"See {paths.checkpoint_log}.",
            file=sys.stderr,
        )
        return 1

    metadata["status"] = "completed"
    write_json(paths.checkpoint_meta, metadata)
    print(f"Checkpoints: {paths.checkpoints_dir}")
    print(f"Metadata: {paths.checkpoint_meta}")
    return 0


def run_simulate(args: argparse.Namespace) -> int:
    return simulate_checkpoints(
        checkpoint_paths=args.checkpoint_paths,
        gem5_bin_value=args.gem5_bin,
        gem5_config=args.gem5_config,
        gem5_args=args.gem5_arg,
        roi_insts=args.roi_insts,
        jobs=args.jobs,
        force=args.force,
        dry_run=args.dry_run,
    )


def run_metrics(args: argparse.Namespace) -> int:
    try:
        payload, warnings = collect_simulation_metrics(
            simulation_path=args.simulation_path,
            metric_patterns=args.metric_patterns,
        )
    except (FileNotFoundError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2

    for warning in warnings:
        print(warning, file=sys.stderr)
    output = format_metrics_json(payload)
    if args.output:
        output_path = args.output.expanduser().resolve()
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(output, encoding="utf-8")
        print(f"Metrics JSON: {output_path}")
    else:
        print(output, end="")
    return 0


def run_aggregate(args: argparse.Namespace) -> int:
    try:
        payload, warnings = aggregate_metrics_json(
            metrics_json=args.metrics_json,
            raw_specs=args.metric,
        )
    except (OSError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2

    for warning in warnings:
        print(warning, file=sys.stderr)
    output = format_aggregate_json(payload)
    output_path = args.output.expanduser().resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(output, encoding="utf-8")
    print(f"Aggregated metrics JSON: {output_path}")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
