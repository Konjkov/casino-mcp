# casino-mcp

[![PyPI](https://img.shields.io/pypi/v/casino-mcp.svg)](https://pypi.org/project/casino-mcp/)
[![Python](https://img.shields.io/pypi/pyversions/casino-mcp.svg)](https://pypi.org/project/casino-mcp/)
[![CI](https://github.com/Konjkov/casino-mcp/actions/workflows/ci.yml/badge.svg)](https://github.com/Konjkov/casino-mcp/actions/workflows/ci.yml)
[![Licence](https://img.shields.io/pypi/l/casino-mcp.svg)](LICENSE)

An MCP control plane over the Fortran [CASINO](https://vallico.net/casinoqmc/) quantum Monte
Carlo code: start runs, know what is running, stop them, and read an `out` file as structured
data instead of shipping 4000 lines of text into a model's context.

> **Beta (0.2.0).** The four control tools and the `out` parser are done and tested against a
> real CASINO; the tool that returns physics to the model is not shipped yet. Interfaces may
> still move before 1.0.

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
| `casino_run(workdir, nproc, version, restart, resume, unlock)` | job_id, pid, workdir, command, binary stamp, what `restart` removed |
| `casino_status(job_id)` | running / finished / failed / stopped / unknown, pid, runtime, exit code |
| `casino_stop(job_id, timeout)` | what was signalled, final status, what `haltqmc` did |
| `casino_list_jobs(limit)` | every known job, newest first |

The runtype (`vmc`, `vmc_opt`, `vmc_dmc`, …) comes from the `input` file in `workdir`; there
is no tool per runtype, because that multiplies the surface without adding a capability.

Starting, stopping and continuing a calculation all go through CASINO's own scripts, and only
through them: `runqmc` starts, `haltqmc` ends and tidies, `runqmc --continue` or a plain
`runqmc` over the `input` that `haltqmc -u` rewrote carries on. Nothing here moves a config
file, edits an `input`, or decides what a half-finished calculation should do next.

### A directory that already holds an `out`

`runqmc` appends to `out`, `vmc.hist` and `dmc.hist` rather than replacing them, so running
twice in one directory produces files that are two runs glued together. That is refused by
default, and there are two ways past it — opposites, so pass one:

| | |
| --- | --- |
| `restart=true` | delete what the earlier run left and start over. `out`, `out_part.N`, the `.hist` files, `config.in`/`config.out`, `correlation.out.N`, `parameters.N.casl`, `saved_part_N/`. The inputs stay: `input`, the wave function, the pseudopotentials, `correlation.data`, `parameters.casl`. Every deleted name comes back in the reply, under `removed`. |
| `resume=true` | carry the interrupted run on from where it stopped. Which of CASINO's two continuation routes that takes is read out of `out`, not chosen here — see below. |

On the command line these are `--restart` and `--resume`, and `--continue` is accepted for
the latter, which is what `runqmc` calls it. The tool parameter cannot be spelled that way:
`continue` is a Python keyword.

### Stopping a run, and continuing it

`casino_stop` sends SIGTERM to that job's `casino` processes and to nothing else — the same
signal `haltqmc -k` sends, except that haltqmc's is a `pkill -x casino` over the whole
account, which would take down every other job on the machine. `mpirun` puts each rank in a
process group of its own, so the ranks are found by session id: the session is the launcher's,
and the whole tree shares it. `runqmc` itself is left alive to finish its epilogue, and only a
job still running after `timeout` has its process group signalled and then killed.

Then the directory goes to `haltqmc -f -u`, which is the part that makes a stopped run
continuable: `config.out` becomes `config.in`, the lock and marker files go, and `input` is
rewritten for the work that is left — `newrun : F`, the finished blocks subtracted, the
runtype moved on to the next stage. The reply carries what it did under `halt`. The `input`
as it was before that is copied into the job directory, and `halt.input_saved` says where.

Which continuation route `resume=true` then takes is decided by the last run in `out`:

| | |
| --- | --- |
| `CONTINUATION INFO:` in `out` | `runqmc --continue`. CASINO writes that block only when it stops itself on `max_cpu_time` or `max_real_time`; runqmc applies it and archives the finished segment into `saved_part_N/`. |
| no such block | a plain `runqmc` over the `input` haltqmc rewrote. This is how an interrupted run continues — `--continue` would only errstop on the missing continuation info. |
| the run reached its own end | refused: there is nothing to continue. |

`restart=true` is refused on a directory whose `input` says `newrun : F`, because restarting
deletes the `config.in` that CASINO then demands. Put back the saved input first.

## Command line

The same runtime without a model in the loop — which is also how you debug the server:

```bash
casino-mcp config                  # the resolved configuration, and the files it came from
casino-mcp run ./calc -p 4         # start a calculation
casino-mcp run ./calc --restart    # ... after deleting what an earlier run left there
casino-mcp run ./calc --continue   # ... or carrying that run on instead
casino-mcp status 20260823-164511-qobn
casino-mcp stop   20260823-164511-qobn   # stop the run, then hand the directory to haltqmc
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
| `CASINO_HALTQMC` | explicit path to `haltqmc`; otherwise `PATH`, then `$CASINO_HOME/bin_qmc/haltqmc` |
| `CASINO_MCP_STATE_DIR` | the job registry; otherwise `$XDG_STATE_HOME/casino-mcp` |
| `CASINO_MCP_FORBID` | directories no run may ever touch, `:`-separated like `PATH` |

Everything else — one MPI process, the `opt` binary, twenty seconds for a stopped job to end
on its own, a minute for haltqmc to tidy, two hundred job records kept — is a constant in
`settings.py`. `casino-mcp config`
prints what the server would use right now and which variable said so; run it first when a
tool call refuses.

`CASINO_MCP_FORBID` is the one guard with no per-call override. `restart=true`/`resume=true`
and `unlock=true` unlock the other two; a directory listed here cannot be run in at all, which
is what makes it the right place for a tree of committed reference calculations.

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
jobs/<job_id>/input.before_halt   the input as it was, kept when a stop rewrites it
```

The calculation directory only ever gets what CASINO puts there.

**Why a launcher process.** `runqmc` is a bash script that execs `mpirun -np N casino`;
signalling its pid orphans the tree. The launcher runs in its own session, which is what makes
the tree identifiable — `killpg` reaches runqmc and mpirun, and the session id finds the ranks
that mpirun put in process groups of their own — its exit code survives the MCP server being
restarted, and runqmc's output goes to a log instead of the JSON-RPC stream. A recycled pid
cannot pass for a live job: `/proc/<pid>` start time is compared, and a zombie does not count
as running.

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
pytest                      # 158 tests, ~5 s, no CASINO needed
```

The unit suite runs anywhere: the parser is checked field by field against five real `out`
files under `tests/data/` — each with the `input` that produced it — and over all eighteen
calculations under `examples/`, while the launcher, the process group and the guardrails are
exercised against a fake `runqmc` shell script.

```bash
pytest -m integration
```

The integration suite needs a real CASINO, but nothing outside this repository. It checks
`parse_out` against CASINO's own `envmc` over every `out` in `examples/`, re-runs the whole
tree against the installed binary, and drives the server over real stdio MCP, running and
stopping actual VMC calculations.

`examples/` holds eighteen calculations chosen as a cover of the settings CASINO can be run
with — every runtype, basis type, optimiser and wavefunction option appears at least once, and
so do the two files a parser gets wrong quietly: a run that never printed an energy, and one
interrupted between optimisation cycles. `examples/README.md` says what each is there for.

They are short and seeded, so the tree doubles as a check on CASINO itself: re-run it on a new
release and any line the parser reads that has been renamed or dropped is named, rather than
silently becoming a `None`. Only that is asserted — moved numbers are reported for a person to
judge.

```bash
python tools/refresh_examples.py --nproc 4     # run the tree, report, touch nothing
python tools/refresh_examples.py --nproc 4 --write   # adopt the new output
```

`tools/protocol_dump.py` speaks the JSON-RPC by hand with no SDK and prints every line in
both directions. Read it before adding a tool.

## Licence

MIT.
