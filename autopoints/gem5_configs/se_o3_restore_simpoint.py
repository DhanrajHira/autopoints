from __future__ import annotations

import argparse
import json
from pathlib import Path

from m5.stats import dump, reset

from gem5.components.boards.simple_board import SimpleBoard
from gem5.components.cachehierarchies.classic.private_l1_private_l2_walk_cache_hierarchy import (
    PrivateL1PrivateL2WalkCacheHierarchy,
)
from gem5.components.memory import SingleChannelDDR3_1600
from gem5.components.processors.cpu_types import CPUTypes
from gem5.components.processors.simple_processor import SimpleProcessor
from gem5.isas import ISA
from gem5.resources.resource import BinaryResource
from gem5.simulate.exit_event import ExitEvent
from gem5.simulate.simulator import Simulator
from gem5.utils.requires import requires


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Restore and simulate one autopoints SimPoint checkpoint."
    )
    parser.add_argument("--checkpoint-plan", type=Path, required=True)
    parser.add_argument("--simpoint-index", type=int, required=True)
    parser.add_argument("--checkpoint-dir", type=Path, required=True)
    parser.add_argument("--roi-insts", type=int, required=True)
    return parser.parse_args()


def load_plan(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def point_by_index(plan: dict, simpoint_index: int) -> dict:
    for point in plan["points"]:
        if int(point["simpoint_index"]) == simpoint_index:
            return point
    raise RuntimeError(f"checkpoint plan has no simpoint_index {simpoint_index}")


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


args = parse_args()
plan = load_plan(args.checkpoint_plan)
point = point_by_index(plan, args.simpoint_index)
warmup_insts = int(point["warmup_insts"])

if args.roi_insts <= 0:
    raise RuntimeError("--roi-insts must be positive")
if not args.checkpoint_dir.is_dir():
    raise RuntimeError(f"checkpoint directory does not exist: {args.checkpoint_dir}")

requires(isa_required=ISA.X86)

processor = SimpleProcessor(cpu_type=CPUTypes.O3, isa=ISA.X86, num_cores=1)
cache_hierarchy = PrivateL1PrivateL2WalkCacheHierarchy(
    l1d_size="32KiB",
    l1i_size="32KiB",
    l2_size="256KiB",
)
memory = SingleChannelDDR3_1600(size=plan["memory_size"])
board = SimpleBoard(
    clk_freq=plan["clock_frequency"],
    processor=processor,
    memory=memory,
    cache_hierarchy=cache_hierarchy,
)

target_command = plan["target_command"]
if not target_command:
    raise RuntimeError("checkpoint plan contains an empty target command")

board.set_se_binary_workload(
    binary=BinaryResource(local_path=target_command[0], architecture=ISA.X86),
    arguments=[str(argument) for argument in target_command[1:]],
    checkpoint=args.checkpoint_dir,
)

for core in processor.get_cores():
    for process in workload_processes(core.get_simobject().workload):
        if plan.get("program_cwd"):
            process.cwd = plan["program_cwd"]
        apply_redirects(process, plan.get("redirects", {}), plan.get("program_cwd"))


def max_inst_handler():
    if warmup_insts > 0:
        print("end of warmup, resetting stats and starting ROI")
        reset()
        simulator.schedule_max_insts(args.roi_insts)
        yield False

    print("end of ROI, dumping stats")
    dump()
    yield True


simulator = Simulator(
    board=board, on_exit_event={ExitEvent.MAX_INSTS: max_inst_handler()}
)

print(
    "Restoring simpoint "
    f"{args.simpoint_index} from {args.checkpoint_dir} and running "
    f"{warmup_insts} warmup instructions + {args.roi_insts} ROI instructions"
)
if warmup_insts > 0:
    simulator.schedule_max_insts(warmup_insts)
else:
    reset()
    simulator.schedule_max_insts(args.roi_insts)
simulator.run()
print(f"Exited: {simulator.get_last_exit_event_cause()}")
