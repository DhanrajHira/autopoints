# autopoints

`autopoints` automates SimPoint region discovery and gem5 checkpoint generation for later detailed simulation.

The workflow has three stages:

1. `collect`: run a target program under Valgrind `exp-bbv`, then run SimPoint on the generated basic block vectors.
2. `checkpoint`: use the selected SimPoints to run gem5 with `KVMCPU` through `sg kvm` and save warmup-adjusted checkpoints before each ROI.
3. `simulate`: restore every generated checkpoint and run detailed O3CPU ROI simulations.

## Requirements

- Valgrind with the `exp-bbv` tool available.
- A SimPoint binary, for example `../SimPoint.3.2/bin/simpoint`.
- A gem5 X86 build with KVM support, for example `../gem5/build/X86/gem5.opt`.
- The user must be able to run gem5 through `sg kvm`.
- `kernel.perf_event_paranoid` must be exactly `1` for precise KVM instruction-count exits.

Check and set the perf level with:

```bash
cat /proc/sys/kernel/perf_event_paranoid
sudo sysctl kernel.perf_event_paranoid=1
```

## Usage

From this directory, collect BBVs and SimPoints:

```bash
python -m autopoints collect \
  --bench my-benchmark \
  --output-dir . \
  --simpoint-bin ../SimPoint.3.2/bin/simpoint \
  --interval-size 100000000 \
  --max-k 30 \
  --num-init-seeds 1 \
  --program-cwd /path/to/run-dir \
  --stdout /path/to/stdout.txt \
  --stderr-append /path/to/stderr.txt \
  -- /path/to/program arg1 arg2
```

Then generate gem5 KVM checkpoints:

```bash
python -m autopoints checkpoint \
  --bench my-benchmark \
  --output-dir . \
  --gem5-bin ../gem5/build/X86/gem5.opt \
  --warmup-insts 30000000
```

Omit `--bench` to checkpoint every collected benchmark under `simpoints/`. These benchmark-level checkpoint jobs run in parallel by default:

```bash
python -m autopoints checkpoint \
  --output-dir . \
  --gem5-bin ../gem5/build/X86/gem5.opt \
  --warmup-insts 30000000
```

Limit checkpoint creation concurrency with `--jobs`:

```bash
python -m autopoints checkpoint \
  --output-dir . \
  --gem5-bin ../gem5/build/X86/gem5.opt \
  --warmup-insts 30000000 \
  --jobs 8
```

Then restore and simulate every checkpoint with the default detailed O3CPU config. You can pass one benchmark checkpoint directory:

```bash
python -m autopoints simulate checkpoints/my-benchmark \
  --gem5-bin ../gem5/build/X86/gem5.opt
```

Or pass the parent `checkpoints/` directory to discover every benchmark directory containing a `checkpoint.plan.json`:

```bash
python -m autopoints simulate checkpoints \
  --gem5-bin ../gem5/build/X86/gem5.opt
```

`simulate` also accepts multiple checkpoint paths. Each path may be either a specific benchmark checkpoint directory or a parent directory containing benchmark checkpoint directories. It runs all discovered checkpoints in parallel by default. Limit concurrency with `--jobs`:

```bash
python -m autopoints simulate checkpoints/my-benchmark \
  --gem5-bin ../gem5/build/X86/gem5.opt \
  --jobs 4
```

By default, simulation outputs are written under `<artifact-root>/simulations/<bench>/`. Use `--output` to choose a different simulation output root, which allows multiple simulation campaigns to reuse the same checkpoints without clobbering each other:

```bash
python -m autopoints simulate checkpoints/my-benchmark \
  --gem5-bin ../gem5/build/X86/gem5.opt \
  --output simulations-o3-baseline
```

This writes files such as `simulations-o3-baseline/<bench>/simpoint_00/m5out/stats.txt`.

At the start of `simulate`, autopoints copies the selected gem5 config into each benchmark simulation output directory with a content-hashed name such as `gem5-config-<sha>.py`, then runs gem5 using that copied config. This records the exact config used for reproducibility and prevents a config file edit during a parallel simulation campaign from affecting only some SimPoints.

Extract weighted metrics from completed simulations by passing a `simulations/` root or one benchmark under it, followed by one or more regex patterns matched against full gem5 stat names:

```bash
python -m autopoints metrics simulations/my-benchmark ipc memOrderViolation
```

Write the same JSON directly to a file with `--output`:

```bash
python -m autopoints metrics simulations/my-benchmark ipc --output metrics.json
```

The output is one JSON object keyed by benchmark and SimPoint. Each SimPoint entry contains its SimPoint `weight` and every stat whose name matches any requested regex. For example, `ipc` matches both `board.processor.cores.core.ipc` and `board.processor.cores.core.commitStats0.ipc`.

Aggregate collected metrics across SimPoints with one or more `--metric REGEX AGGREGATION` pairs. The metric regex is a partial match against collected metric names, but it must match exactly one metric across the input JSON. If it matches multiple metrics, the command errors and lists the ambiguous matches. Supported aggregations are `mean`, which computes `sum(weight * value) / sum(weight)` within each benchmark, and `max`, which ignores weights and returns the maximum value across SimPoints:

```bash
python -m autopoints aggregate metrics.json \
  --metric 'cores\.core\.ipc$' mean \
  --metric 'iew\.memOrderViolationEvents$' max
```

Write the aggregate JSON directly to a file with `--output`:

```bash
python -m autopoints aggregate metrics.json \
  --metric 'cores\.core\.ipc$' mean \
  --metric 'iew\.memOrderViolationEvents$' max \
  --output aggregate-metrics.json
```

Use `-` as the metrics file to read the metrics JSON from stdin:

```bash
python -m autopoints metrics simulations/my-benchmark ipc | \
  python -m autopoints aggregate - --metric 'cores\.core\.ipc$' mean
```

The aggregate JSON is keyed by benchmark, then exact matched stat name, then aggregation name.

Install in editable mode if you prefer the `autopoints` console command:

```bash
python -m pip install -e .
autopoints collect --bench my-benchmark --simpoint-bin ../SimPoint.3.2/bin/simpoint -- /path/to/program arg1 arg2
```

## KVM Hello Example

The repository includes a small KVM smoke workload under `../gem5/test-kvm/`.

Build it if needed:

```bash
gcc -static -O2 -o ../gem5/test-kvm/hello ../gem5/test-kvm/hello.c
```

Collect SimPoints for the workload:

```bash
python -m autopoints collect \
  --bench kvm-hello \
  --output-dir . \
  --simpoint-bin ../SimPoint.3.2/bin/simpoint \
  --interval-size 100 \
  --max-k 1 \
  --num-init-seeds 1 \
  -- ../gem5/test-kvm/hello
```

Create a KVM checkpoint with a one-instruction warmup:

```bash
python -m autopoints checkpoint \
  --bench kvm-hello \
  --output-dir . \
  --gem5-bin ../gem5/build/X86/gem5.opt \
  --warmup-insts 1
```

Expected artifacts are written under:

```text
bbv/kvm-hello/
simpoints/kvm-hello/
checkpoints/kvm-hello/
```

The checkpoint directory is named by the actual checkpoint instruction, for example:

```text
checkpoints/kvm-hello/cpt.simpoint_00_inst_.../
```

## Artifact Layout

`--output-dir` is the artifact root. All workflow stages share this stable per-benchmark layout:

```text
bbv/
  <bench>/
    simpoint.bb.gz
    bb.out.gz
    pc.out
    valgrind.log
simpoints/
  <bench>/
    simpoints
    weights
    simpoint.meta.json
    simpoint.log
checkpoints/
  <bench>/
    checkpoint.plan.json
    checkpoint.meta.json
    gem5.log
    m5out/
    cpt.simpoint_00_inst_.../
    cpt.simpoint_01_inst_.../
simulations/
  <bench>/
    gem5-config-<sha>.py
    simpoint_00/
      gem5.log
      simulation.meta.json
      m5out/
        stats.txt
        config.ini
        config.json
    simpoint_01/
      ...
```

Important files:

- `bbv/<bench>/simpoint.bb.gz`: gzipped frequency vectors used as SimPoint input.
- `bbv/<bench>/bb.out.gz`: gzipped raw Valgrind `exp-bbv` output.
- `bbv/<bench>/pc.out`: basic block address/function metadata emitted by `exp-bbv`.
- `simpoints/<bench>/simpoints`: selected SimPoint interval IDs.
- `simpoints/<bench>/weights`: SimPoint weights for combining detailed simulation results.
- `simpoints/<bench>/simpoint.meta.json`: reproducibility record for BBV collection and SimPoint selection.
- `checkpoints/<bench>/checkpoint.plan.json`: warmup-adjusted checkpoint plan consumed by gem5.
- `checkpoints/<bench>/checkpoint.meta.json`: reproducibility record for checkpoint generation.
- `checkpoints/<bench>/gem5.log`: gem5 stdout/stderr from checkpoint creation.
- `checkpoints/<bench>/m5out/`: gem5 output directory for the checkpoint run.
- `simulations/<bench>/simpoint_XX/simulation.meta.json`: reproducibility record for one detailed restore simulation.
- `simulations/<bench>/simpoint_XX/gem5.log`: gem5 stdout/stderr from one detailed restore simulation.
- `simulations/<bench>/simpoint_XX/m5out/`: gem5 `--outdir` for one detailed restore simulation. gem5 writes `stats.txt`, `config.ini`, `config.json`, and related outputs here.
- `simulations/<bench>/gem5-config-<sha>.py`: immutable copy of the gem5 config used by the simulation campaign. Each `simulation.meta.json` records the source config, copied config, and SHA-256 hash.

`collect` supports `--stdin`, `--stdout`, `--stdout-append`, `--stderr`, and
`--stderr-append`. These redirects are used during Valgrind collection, recorded
in `simpoint.meta.json`, propagated into `checkpoint.plan.json`, and applied to
the gem5 SE workload during checkpoint creation. gem5 SE opens output and error
redirect files in truncate mode, so append redirects are recorded but cannot be
faithfully appended during checkpoint creation.

## Warmup Semantics

SimPoint interval IDs are counted from program start. The ROI start for interval `N` is:

```text
roi_start_instruction = N * interval_size
```

Checkpoint generation applies warmup before each ROI:

```text
checkpoint_instruction = max(0, roi_start_instruction - requested_warmup_insts)
actual_warmup_insts = roi_start_instruction - checkpoint_instruction
```

`checkpoint.plan.json` records both the ROI start and the checkpoint instruction for every SimPoint. Future detailed restore workflows should restore from the checkpoint, run `actual_warmup_insts`, reset stats at the ROI boundary, then simulate `interval_size` instructions.

`simulate` follows that restore sequence by default. It runs each checkpoint's `warmup_insts`, resets stats, then runs `interval_size` ROI instructions. Override the ROI length with `--roi-insts`.

## Detailed Simulation Configs

The default `simulate` config is `autopoints/gem5_configs/se_o3_restore_simpoint.py`. It restores one SE-mode checkpoint, uses one detailed O3CPU, resets stats after warmup, and runs the requested ROI instruction count.

Use `--gem5-config` to supply a different detailed simulation config:

```bash
python -m autopoints simulate checkpoints/my-benchmark \
  --gem5-bin ../gem5/build/X86/gem5.opt \
  --gem5-config ./configs/my_o3_restore_simpoint.py
```

A custom config should accept the same restore interface as the default config:

```text
--checkpoint-plan <path>
--simpoint-index <index>
--checkpoint-dir <path>
--roi-insts <instructions>
```

To change O3CPU details such as branch predictor sizing, copy the default config and edit the Python directly. For example, after creating the `SimpleProcessor`, adjust the gem5 SimObject fields:

```python
from m5.objects import TournamentBP

processor = SimpleProcessor(cpu_type=CPUTypes.O3, isa=ISA.X86, num_cores=1)
for core in processor.get_cores():
    o3_cpu = core.get_simobject()
    o3_cpu.branchPred.conditionalBranchPred = TournamentBP(
        localPredictorSize=4096,
        globalPredictorSize=16384,
        choicePredictorSize=16384,
    )
```

Keep the checkpoint plan parsing, workload setup, checkpoint restore, warmup, stats reset, and ROI scheduling behavior unless you intentionally want to change the simulation protocol.

## Implementation Notes

- The checkpoint command launches gem5 as `sg kvm -c '<gem5 command>'`.
- The current checkpoint config targets X86 SE-mode workloads with one KVM core.
- The simulate command launches gem5 directly once per checkpoint and does not use `sg kvm`.
- The default simulation config targets X86 SE-mode checkpoints with one O3CPU core.
- KVM instruction stops rely on perf, so `autopoints checkpoint` fails early unless `/proc/sys/kernel/perf_event_paranoid` is `1`.
- The checkpoint config does not use gem5's `SimpointResource`. `autopoints` already computes exact warmup-adjusted checkpoint instruction counts in `checkpoint.plan.json`, so the config schedules those stops directly. This avoids gem5 recomputing warmup-adjusted starts internally and avoids a zero-warmup bug observed in this gem5 tree's `SimpointResource` path.
- All workflow code should use `autopoints.paths.AutopointsPaths` instead of constructing artifact paths directly.
