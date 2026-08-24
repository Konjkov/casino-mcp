# casino-mcp

An MCP control plane over the Fortran [CASINO](https://vallico.net/casinoqmc/) quantum Monte
Carlo code: start runs, know what is running, stop them, and read an `out` file as structured
data instead of shipping 4000 lines of text into a model's context.

> **Alpha (0.1.0).** The four control tools and the `out` parser are done and tested; the
> tool that returns physics to the model is not shipped yet. Interfaces may still move.

## What it is, and what it is not

CASINO already has the primitives — `opt_plan`, `runqmc --auto-continue`, `multirun`,
`envmc`, `make_E_v_dt`. What it has no place for is the layer between them: machine-readable
results, a memory of what was run, and the judgement calls that sit between the steps
("has the variance plateaued?", "is this timestep still in the linear regime?"). That layer
is what this package is, and three rules keep it honest:

1. **No number is produced by the model.** Every value a tool returns is read from a file and
   carries the line it came from. What CASINO did not print comes back as `null` with a
   reason, never a guess.
2. **Every result is reproducible from its record.** A job record freezes the command, the
   process count, and the path, size and mtime of the `casino` binary that ran.
3. **Nothing destructive is implicit.** A run refuses to start in a directory that already
   holds results, and refuses harder when that `out` is committed reference data.

There is deliberately no `execute_shell(command)` tool. Every tool is a named CASINO
operation with typed arguments.

## Install

```bash
pip install casino-mcp          # needs Python 3.11+ and a working CASINO installation
```

From a checkout:

```bash
pip install -e '.[dev]'
```

The package does not bundle, build or replace CASINO. It drives `runqmc`, which stays the
runtime: arch detection, MPI variants, batch-queue submission and the lock file are its job,
not ours.

## Register it with Claude Code

`.mcp.json`, project scope:

```json
{
  "mcpServers": {
    "casino": {
      "command": "casino-mcp",
      "args": ["serve"],
      "env": {
        "CASINO_HOME": "/home/you/bin/CASINO",
        "CASINO_ARCH": "linuxpc-gcc-parallel.openblas"
      }
    }
  }
}
```

## Tools

| tool | returns |
| --- | --- |
| `casino_run(workdir, nproc, version, overwrite, unlock)` | job_id, pid, workdir, command, binary stamp |
| `casino_status(job_id)` | running / finished / failed / stopped / unknown, pid, runtime, exit code |
| `casino_stop(job_id, timeout)` | what was signalled, final status |
| `casino_list_jobs(limit)` | every known job, newest first |

The runtype (`vmc`, `vmc_opt`, `vmc_dmc`, …) comes from the `input` file in `workdir`; there
is no tool per runtype, because that multiplies the surface without adding a capability.

## Command line

The same runtime without a model in the loop — which is also how you debug the server:

```bash
casino-mcp config                  # the resolved configuration, and the files it came from
casino-mcp run ./calc -p 4         # start a calculation
casino-mcp status 20260823-164511-qobn
casino-mcp stop   20260823-164511-qobn
casino-mcp jobs                    # the registry, newest first
casino-mcp parse ./calc            # the `out` file as JSON
casino-mcp serve                   # the MCP server on stdio
```

Every subcommand prints JSON and exits non-zero when that JSON carries an `error`.

## Configuration

There is no configuration file. An MCP server is configured where it is registered — the
`env` block of the `.mcp.json` above — and CASINO's own variables keep their names, so
setting them once configures both layers:

| variable | |
| --- | --- |
| `CASINO_HOME` | root of the CASINO installation (default `~/bin/CASINO`) |
| `CASINO_ARCH` | build target, the directory under `bin_qmc`; used to stamp which binary a job ran |
| `CASINO_RUNQMC` | explicit path to `runqmc`; otherwise `PATH`, then `$CASINO_HOME/bin_qmc/runqmc` |
| `CASINO_MCP_STATE_DIR` | the job registry; otherwise `$XDG_STATE_HOME/casino-mcp` |
| `CASINO_MCP_FORBID` | directories no run may ever touch, `:`-separated like `PATH` |

Everything else — one MPI process, the `opt` binary, twenty seconds between SIGTERM and
SIGKILL, two hundred job records kept — is a constant in `settings.py`. `casino-mcp config`
prints what the server would use right now and which variable said so; run it first when a
tool call refuses.

`CASINO_MCP_FORBID` is the one guard with no per-call override. `overwrite=true` and
`unlock=true` unlock the other two; a directory listed here cannot be run in at all, which is
what makes it the right place for a tree of committed reference calculations.

## How it works

```
Claude Code ──stdio──> server.py ──spawn──> launcher.py ──> runqmc ──> mpirun ──> casino
                          │                     │
                          │                     └─ writes status.json (exit code, end time)
                          └─ reads/writes jobs.json + one directory per job
```

**State lives outside the calculation**, under `$XDG_STATE_HOME/casino-mcp/`:

```
jobs.json                    index: job_id -> record
jobs/<job_id>/meta.json      what was launched, frozen at spawn
jobs/<job_id>/status.json    written by the launcher when the run ends
jobs/<job_id>/runqmc.log     runqmc's own output (not CASINO's `out`)
```

The calculation directory only ever gets what CASINO puts there.

**Why a launcher process.** `runqmc` is a bash script that execs `mpirun -np N casino`;
signalling its pid orphans the tree. The launcher runs in its own session, so `killpg`
reaches everything, its exit code survives the MCP server being restarted, and runqmc's
output goes to a log instead of the JSON-RPC stream. A recycled pid cannot pass for a live
job: `/proc/<pid>` start time is compared, and a zombie does not count as running.

## The `out` parser

`parse_out` is a plain function with no MCP and no dependencies. An `out` file is a *sequence
of phases*, not one result — `vmc_opt` writes a VMC and an OPTIMIZATION phase per cycle,
`vmc_dmc` writes VMC, DMC equilibration and DMC statistics accumulation — so it returns
`phases`, and `result` points at the last phase that carries an energy.

```python
from casino_mcp.parse_out import parse_out

parsed = parse_out('./calc')
parsed['result']['energy']  # {'value': -2.861829862553, 'error': 0.000659077167, 'line': 237}
```

The one derived number in it is the sample-variance error of a single-block run, which CASINO
does not print; it is taken from the one block exactly as `envmc` does, and labelled
`derived`. Nothing shells out to `envmc` or `endmc` at runtime — `endmc` misparses numbers
under a non-C locale.

## Tests

```bash
pytest                      # 102 tests, ~2 s, no CASINO needed
```

The unit suite runs anywhere: the parser is checked against five real `out` files committed
under `tests/data/`, and the launcher, the process group and the guardrails are exercised
against a fake `runqmc` shell script.

```bash
pytest -m integration --examples-dir ~/PycharmProjects/PyCasino/examples
```

The integration suite needs a real CASINO. It checks `parse_out` against CASINO's own `envmc`
over an entire examples tree (526 files, ~50 s), and drives the server over real stdio MCP,
running and stopping actual VMC calculations.

`tools/protocol_dump.py` speaks the JSON-RPC by hand with no SDK and prints every line in
both directions. Read it before adding a tool.

## Licence

MIT.
