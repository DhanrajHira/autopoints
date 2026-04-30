from __future__ import annotations

import argparse
import json
from pathlib import Path

import m5
from gem5.components.boards.simple_board import SimpleBoard
from gem5.components.cachehierarchies.classic.no_cache import NoCache
from gem5.components.memory.single_channel import SingleChannelDDR3_1600
from gem5.components.processors.cpu_types import CPUTypes
from gem5.components.processors.simple_processor import SimpleProcessor
from gem5.isas import ISA
from gem5.resources.resource import BinaryResource
from gem5.simulate.exit_event import ExitEvent
from gem5.simulate.simulator import Simulator
from gem5.utils.requires import requires


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Create KVM fast-forwarded SimPoint checkpoints."
    )
    parser.add_argument("--checkpoint-plan", type=Path, required=True)
    return parser.parse_args()


def load_plan(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def grouped_points(points: list[dict]) -> list[list[dict]]:
    groups: list[list[dict]] = []
    current_instruction: int | None = None
    for point in sorted(
        points,
        key=lambda item: (item["checkpoint_instruction"], item["simpoint_index"]),
    ):
        instruction = int(point["checkpoint_instruction"])
        if current_instruction != instruction:
            groups.append([])
            current_instruction = instruction
        groups[-1].append(point)
    return groups


def resolve_workload_path(path: str, cwd: str | None) -> str:
    candidate = Path(path)
    if candidate.is_absolute() or cwd is None:
        return str(candidate)
    return str(Path(cwd) / candidate)


def apply_redirects(workload, redirects: dict[str, str], cwd: str | None) -> None:
    if redirects.get("stdin"):
        workload.input = resolve_workload_path(redirects["stdin"], cwd)

    if redirects.get("stdout"):
        target = resolve_workload_path(redirects["stdout"], cwd)
        Path(target).parent.mkdir(parents=True, exist_ok=True)
        workload.output = target
    elif redirects.get("stdout-append"):
        target = resolve_workload_path(redirects["stdout-append"], cwd)
        Path(target).parent.mkdir(parents=True, exist_ok=True)
        workload.output = target
        print(
            f"warning: gem5 SE stdout redirection truncates instead of appending: {target}"
        )

    if redirects.get("stderr"):
        target = resolve_workload_path(redirects["stderr"], cwd)
        Path(target).parent.mkdir(parents=True, exist_ok=True)
        workload.errout = target
    elif redirects.get("stderr-append"):
        target = resolve_workload_path(redirects["stderr-append"], cwd)
        Path(target).parent.mkdir(parents=True, exist_ok=True)
        workload.errout = target
        print(
            f"warning: gem5 SE stderr redirection truncates instead of appending: {target}"
        )


def workload_processes(workload) -> list:
    try:
        return list(workload)
    except TypeError:
        return [workload]


def checkpoint_generator(points: list[dict]):
    groups = grouped_points(points)
    group_index = 0
    while True:
        if group_index >= len(groups):
            yield True
            continue

        for point in groups[group_index]:
            checkpoint_dir = Path(point["checkpoint_dir"])
            if checkpoint_dir.exists():
                raise RuntimeError(
                    f"checkpoint directory already exists: {checkpoint_dir}"
                )
            checkpoint_dir.parent.mkdir(parents=True, exist_ok=True)
            print(
                "Taking checkpoint for simpoint "
                f"{point['simpoint_index']} at instruction {point['checkpoint_instruction']} "
                f"before ROI {point['roi_start_instruction']}: {checkpoint_dir}"
            )
            m5.checkpoint(checkpoint_dir.as_posix())

        group_index += 1
        yield group_index >= len(groups)


args = parse_args()
plan = load_plan(args.checkpoint_plan)
points = plan["points"]
if not points:
    raise RuntimeError("checkpoint plan contains no SimPoints")

target_command = plan["target_command"]
if not target_command:
    raise RuntimeError("checkpoint plan contains an empty target command")

requires(isa_required=ISA.X86, kvm_required=True)

processor = SimpleProcessor(cpu_type=CPUTypes.KVM, isa=ISA.X86, num_cores=1)
for core in processor.get_cores():
    core.get_simobject().usePerf = True

board = SimpleBoard(
    clk_freq=plan["clock_frequency"],
    processor=processor,
    memory=SingleChannelDDR3_1600(size=plan["memory_size"]),
    cache_hierarchy=NoCache(),
)

binary = BinaryResource(local_path=target_command[0], architecture=ISA.X86)
board.set_se_binary_workload(
    binary=binary,
    arguments=[str(argument) for argument in target_command[1:]],
)

for core in processor.get_cores():
    for process in workload_processes(core.get_simobject().workload):
        if plan.get("program_cwd"):
            process.cwd = plan["program_cwd"]
        apply_redirects(process, plan.get("redirects", {}), plan.get("program_cwd"))

# Do not use SimpointResource here. autopoints has already converted SimPoint
# intervals into exact warmup-adjusted checkpoint instruction counts, and this
# config needs to checkpoint at those exact counts. SimpointResource also
# recalculates warmup-adjusted starts internally and this gem5 version has a
# zero-warmup bug in that path, so we set the CPU SimPoint stops directly.
processor.get_cores()[0]._set_simpoint(
    sorted({int(point["checkpoint_instruction"]) for point in points}),
    board_initialized=False,
)

simulator = Simulator(
    board=board,
    on_exit_event={ExitEvent.SIMPOINT_BEGIN: checkpoint_generator(points)},
)
simulator.run()
