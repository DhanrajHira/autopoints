from __future__ import annotations

import shlex
import shutil
import subprocess
from pathlib import Path
from typing import Sequence

PERF_EVENT_PARANOID = Path("/proc/sys/kernel/perf_event_paranoid")
PERF_EVENT_PARANOID_SUGGESTION = "sudo sysctl kernel.perf_event_paranoid=1"


class PerfEventParanoidError(RuntimeError):
    pass


def default_checkpoint_config(use_atomic_cpu: bool = False) -> Path:
    config_name = (
        "se_atomic_simpoint_checkpoints.py"
        if use_atomic_cpu
        else "se_kvm_simpoint_checkpoints.py"
    )
    return Path(__file__).resolve().parent / "gem5_configs" / config_name


def read_perf_event_paranoid(path: Path = PERF_EVENT_PARANOID) -> int:
    try:
        return int(path.read_text(encoding="utf-8").strip())
    except FileNotFoundError as error:
        raise PerfEventParanoidError(
            f"Cannot read {path}; this checkpoint flow requires Linux perf events."
        ) from error
    except ValueError as error:
        raise PerfEventParanoidError(
            f"Cannot parse {path}; expected an integer perf_event_paranoid value."
        ) from error


def ensure_perf_event_paranoid_is_one(path: Path = PERF_EVENT_PARANOID) -> None:
    value = read_perf_event_paranoid(path)
    if value != 1:
        raise PerfEventParanoidError(
            "kernel.perf_event_paranoid must be 1 for precise KVM instruction stops. "
            + f"Current value: {value}. Run: {PERF_EVENT_PARANOID_SUGGESTION}"
        )


def resolve_executable(value: str, label: str) -> str:
    path = Path(value).expanduser()
    if path.parent != Path(".") or path.is_absolute():
        resolved = path.resolve()
        if not resolved.is_file():
            raise FileNotFoundError(f"{label} does not exist: {resolved}")
        return str(resolved)

    resolved_name = shutil.which(value)
    if resolved_name is None:
        raise FileNotFoundError(f"{label} was not found on PATH: {value}")
    # Return an absolute path so the executable resolves regardless of the
    # working directory the gem5 subprocess is later launched from (e.g. the
    # simulate step runs gem5 from the workload's program_cwd). shutil.which
    # returns a relative path for a "./binary" form, which would otherwise fail.
    return str(Path(resolved_name).resolve())


def build_gem5_checkpoint_command(
    gem5_bin: str,
    config_path: Path,
    plan_path: Path,
    m5out_dir: Path,
    gem5_args: Sequence[str],
) -> list[str]:
    return [
        gem5_bin,
        "--outdir",
        str(m5out_dir),
        *gem5_args,
        str(config_path),
        "--checkpoint-plan",
        str(plan_path),
    ]


def wrap_with_sg_kvm(command: Sequence[str]) -> list[str]:
    return ["sg", "kvm", "-c", shlex.join(command)]


def run_logged(command: Sequence[str], log_path: Path, cwd: Path | None = None) -> int:
    with log_path.open("w", encoding="utf-8") as log_file:
        log_file.write(f"$ {shlex.join(command)}\n\n")
        log_file.flush()
        completed = subprocess.run(
            command,
            cwd=str(cwd) if cwd else None,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            check=False,
        )
    return completed.returncode
