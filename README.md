# autopoints

`autopoints` automates SimPoint region discovery and gem5 checkpoint generation for later detailed simulation.

The workflow has two stages:

1. `collect`: run a target program under Valgrind `exp-bbv`, then run SimPoint on the generated basic block vectors.
2. `checkpoint`: use the selected SimPoints to run gem5 with `KVMCPU` through `sg kvm` and save warmup-adjusted checkpoints before each ROI.

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

## Implementation Notes

- The checkpoint command launches gem5 as `sg kvm -c '<gem5 command>'`.
- The current checkpoint config targets X86 SE-mode workloads with one KVM core.
- KVM instruction stops rely on perf, so `autopoints checkpoint` fails early unless `/proc/sys/kernel/perf_event_paranoid` is `1`.
- The checkpoint config does not use gem5's `SimpointResource`. `autopoints` already computes exact warmup-adjusted checkpoint instruction counts in `checkpoint.plan.json`, so the config schedules those stops directly. This avoids gem5 recomputing warmup-adjusted starts internally and avoids a zero-warmup bug observed in this gem5 tree's `SimpointResource` path.
- All workflow code should use `autopoints.paths.AutopointsPaths` instead of constructing artifact paths directly.
