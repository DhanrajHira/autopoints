from __future__ import annotations

import itertools
import re
from pathlib import Path
from typing import Sequence

from .simulate import (
    SimulationPoint,
    build_points_for_dir,
    prepare_campaign,
    run_simulation_pool,
)

_UNSAFE_SWEEP_CHARS = re.compile(r"[^A-Za-z0-9._-]+")


def normalize_param_name(raw: str) -> str:
    """Drop any leading dashes the user supplied; autopoints adds --<name>."""
    name = raw.lstrip("-").strip()
    if not name:
        raise ValueError(f"sweep parameter name is empty: {raw!r}")
    return name


def parse_param_specs(
    param_specs: Sequence[Sequence[str]],
) -> tuple[list[str], list[list[str]]]:
    """Split --param specs into parallel lists of names and value lists.

    Each spec is ``[name, value1, value2, ...]``. Returns ``(names, values)``
    where ``names[i]`` aligns with ``values[i]``. Raises ValueError on a spec
    without values or a duplicate parameter name.
    """
    names: list[str] = []
    value_lists: list[list[str]] = []
    seen: set[str] = set()
    for spec in param_specs:
        if len(spec) < 2:
            raise ValueError(
                "each --param needs a name and at least one value, "
                f"for example --param l2-size 1MB 2MB; got {list(spec)!r}"
            )
        name = normalize_param_name(spec[0])
        if name in seen:
            raise ValueError(f"duplicate --param name: {name}")
        seen.add(name)
        names.append(name)
        value_lists.append(list(spec[1:]))
    return names, value_lists


def sweep_dir_component(name: str, value: str) -> str:
    """A filesystem-safe '<name>_<value>' directory component for one param."""
    safe_name = _UNSAFE_SWEEP_CHARS.sub("_", name)
    safe_value = _UNSAFE_SWEEP_CHARS.sub("_", value)
    return f"{safe_name}_{safe_value}"


def combo_subpath(params: Sequence[tuple[str, str]]) -> Path:
    """Nested '<name>_<value>/...' path, one directory level per parameter."""
    return Path(*(sweep_dir_component(name, value) for name, value in params))


def sweep_checkpoints(
    checkpoint_paths: Sequence[Path],
    gem5_bin_value: str,
    gem5_config: Path | None,
    gem5_args: Sequence[str],
    roi_insts: int | None,
    output_dir: Path | None,
    jobs: int | None,
    force: bool,
    dry_run: bool,
    param_specs: Sequence[Sequence[str]],
) -> int:
    try:
        inputs = prepare_campaign(checkpoint_paths, gem5_bin_value, gem5_config)
        param_names, value_lists = parse_param_specs(param_specs)
    except (FileNotFoundError, ValueError) as error:
        print(f"error: {error}")
        return 2

    if not inputs.checkpoint_dirs:
        print("error: no checkpoint.plan.json files found")
        return 1

    combinations = list(itertools.product(*value_lists))
    custom_output_root = output_dir.expanduser().resolve() if output_dir else None
    config_copies: dict[Path, Path] = {}
    points: list[SimulationPoint] = []
    for values in combinations:
        params = tuple(zip(param_names, values))
        subpath = combo_subpath(params)
        for checkpoint_dir in inputs.checkpoint_dirs:
            points.extend(
                build_points_for_dir(
                    checkpoint_dir=checkpoint_dir,
                    inputs=inputs,
                    custom_output_root=custom_output_root,
                    roi_insts=roi_insts,
                    config_copies=config_copies,
                    combo_subpath=subpath,
                    params=params,
                )
            )

    if not points:
        print("error: no checkpoint directories from discovered plans exist on disk")
        return 1

    print(
        f"Sweeping {len(param_names)} parameter(s) over {len(combinations)} "
        f"combination(s) across {len(inputs.checkpoint_dirs)} checkpoint plan(s)."
    )
    return run_simulation_pool(
        points=points,
        gem5_bin=inputs.gem5_bin,
        gem5_args=gem5_args,
        jobs=jobs,
        force=force,
        dry_run=dry_run,
    )
