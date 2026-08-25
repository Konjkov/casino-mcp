---
name: casino-mcp
description: >
  Use this skill when working on the casino-mcp MCP server (src/casino_mcp/ in this repo) — the
  external control layer that lets Claude Code start, watch, stop and read out real CASINO
  calculations instead of shelling out to runqmc by hand. Covers the architecture (launcher
  process + JSON job registry under XDG state, never inside the calculation directory), the
  tool surface (casino_run / casino_status / casino_stop / casino_list_jobs, later
  casino_results), the guardrails that stop a run from overwriting committed reference data
  in examples/, why the PID that matters is the launcher's process group and not runqmc's,
  and the staged plan with what was accepted and what was rejected from the original
  proposal. Trigger on: MCP, casino-mcp, mcp server, job manager, job id, casino_run,
  .mcp.json, FastMCP/MCPServer. See casino-run for how CASINO itself is driven and what its
  output files contain.
---

# casino-mcp

An MCP server that runs CASINO. `casino-run` documents *how CASINO is driven*; this skill
documents *the server that drives it*.

Since 0.1.0 it is a standalone pip-installable package with its own repository, not a
directory inside PyCasino. Layout:

```
pyproject.toml             casino-mcp 0.1.0, entry point `casino-mcp`. The only TOML here
src/casino_mcp/
    settings.py    where CASINO is, where our state goes: the environment, and constants
    parse_out.py   CASINO `out` -> structured phases (no MCP, no dependencies)
    jobs.py        job registry, state dir, process liveness
    runtime.py     start / status / stop over runqmc. No MCP in this module
    server.py      MCPServer + tool definitions. A thin wrapper over runtime
    launcher.py    the child process that actually waits on runqmc
    cli.py         `casino-mcp serve | config | run | status | stop | jobs | parse`
examples/          18 real calculations, a settings cover. The only tree the tests read
tests/             141 unit tests, no CASINO needed; tests/integration is opt-in
tools/protocol_dump.py     the JSON-RPC by hand, no SDK. Read before adding a tool
.mcp.json          registration for Claude Code (project scope)
```

The layering is the load-bearing part: `server.py` holds no logic, only the protocol surface
and the docstrings the model reads. Anything that would go in a tool body goes in `runtime.py`
instead, which is why the tools are testable without speaking MCP.

Nothing in `casino/` depends on it. CASINO and PyCasino keep working with the server absent —
it is an outer layer, never a dependency.

`references/roadmap.md` — the full staged plan (control plane → analysis primitives →
workflows → regression harness for CASINO development), what CASINO already automates and
where the gap is, and the five small changes in CASINO that would make the layer cleaner.
Written to be shown to the CASINO developers; read it before adding a stage.

---

## Position on the original proposal

The staged text was written by ChatGPT, which does not know this repo. What survives contact
with it:

**Accepted**

- Stdio server, one Python package, modern SDK. Local, single user, no daemon, no HTTP.
- No `execute_shell(command)`. Every tool is a named CASINO operation with typed arguments.
- Job IDs, and calculations that outlive the MCP call. Mandatory here: a DMC run in
  `examples/` takes 40+ minutes per block; a blocking tool call is unusable.
- Structured results instead of shipping `out` into the context. An `out` file is 800–4000
  lines and the interesting part is ~20 numbers.
- One directory per experiment, and never destroying an earlier one.

**Rejected or changed**

1. **Stage 1 as specified is not worth shipping.** `casino_run` + `casino_status` without
   persistence is strictly worse than `Bash("runqmc -p 4 -B")`, which Claude Code already
   has. The value is in the *registry*, so stage 3's persistence is folded into stage 1: a
   job record survives an MCP server restart from the first commit. What is genuinely
   deferred is analysis, not bookkeeping.

2. **PID is the wrong handle.** `runqmc` is a 3900-line bash script that execs
   `mpirun -np N casino`. The PID of the thing we spawn controls nothing on its own —
   `SIGTERM` to it orphans mpirun and casino. The server spawns a *launcher* in its own
   session (`start_new_session=True`), so `killpg` reaches the whole tree, and the launcher
   records the exit status to disk. `casino_status` therefore reports the truth after an
   MCP restart, which polling a bare PID cannot (PID reuse is guarded by comparing
   `/proc/<pid>` start time).

3. **No `casino_run_vmc` / `casino_run_dmc`.** The runtype is one keyword in `input`.
   A tool per runtype multiplies the surface without adding a capability. The pair that
   actually pays is `casino_run` plus a future `casino_prepare(source, dest, overrides)`
   that copies a directory and rewrites keywords — that is what "изменить параметр → новый
   каталог" needs, and it can lean on `casino/readers/validate.py`, which already knows all
   304 keywords, their types and which ones matter for a runtype.

4. **`casino_get_energy` and `casino_get_statistics` are one tool.** They read the same
   `FINAL RESULT` block of the same file. Two tools mean two parsers drifting apart. One
   `casino_results(job_id)` returning a dict; a separate reblocking tool only if the
   `out`-level numbers ever prove insufficient.

5. **Do not shell out to `envmc`/`endmc`.** `endmc` misparses numbers under the ru_RU
   locale (documented in `casino-run`) and prints garbage. `out` is parsed in Python.

6. **Stage 6 (build integration) needs no tools.** Claude Code already edits Fortran,
   runs `make`, runs pytest. Duplicating that in MCP is the "универсальный shell" mistake
   wearing a lab coat. The one thing MCP should add is *provenance*: every job record
   stamps the mtime and size of the `casino` binary it ran, so "изменил Fortran → собрал →
   запустил" produces results that can be told apart afterwards. Cheap, and it is the only
   part of stage 6 that MCP is the right place for.

7. **Stage 7 is mostly not MCP's job.** Comparing two energies, fitting a timestep
   extrapolation, deciding whether a variance moved — that is reasoning over ~20 numbers,
   and Claude does it better in the transcript than a tool would in Python. The server's
   job ends at returning clean numbers. A `casino_compare_jobs` is a formatting
   convenience, not a capability; add it only if the transcript actually gets clumsy.

8. **The real safety risk is not `shell=True`.** It is writing into
   `examples/**/` and destroying committed reference `out` files that other work is
   validated against. The guard is therefore semantic, not syntactic: refuse to run in a
   directory whose `out` is tracked by git unless explicitly overridden, and refuse to run
   in a directory another runqmc instance has locked.

---

## Architecture

```
Claude Code ──stdio──> server.py ──spawn──> launcher.py ──> runqmc ──> mpirun ──> casino
                          │                     │
                          │                     └─ writes status.json (exit code, end time)
                          └─ reads/writes jobs.json + per-job dir
```

**State lives outside the calculation.** `$XDG_STATE_HOME/casino-mcp/` (default
`~/.local/state/casino-mcp/`):

```
jobs.json              index: job_id -> record
jobs/<job_id>/meta.json      what was launched (frozen at spawn)
jobs/<job_id>/status.json    written by the launcher when the run ends
jobs/<job_id>/runqmc.log     stdout/stderr of runqmc itself (not CASINO's `out`)
```

The calculation directory only ever gets what CASINO puts there. That keeps
`source / build / calculations / results` separated the way the request asked, without a
copy of the tree.

**Why a launcher process.** Three properties fall out of it and none are available
otherwise: an exit code that survives the server dying, a process group that can be killed
as a unit, and a `runqmc.log` that is not interleaved with the MCP stdio stream (writing to
stdout would corrupt the protocol — the single most common way to break a stdio MCP server).

**Liveness.** `os.kill(pid, 0)` plus a start-time check against `/proc/<pid>/stat` field 22.
Without the second, a recycled PID reports a finished job as running.

---

## Tool surface

Stage 1 (implemented):

| tool | returns |
| --- | --- |
| `casino_run(workdir, nproc, …)` | job_id, pid, workdir, command, started |
| `casino_status(job_id)` | running/finished/failed, pid, runtime, exit code |
| `casino_stop(job_id)` | what was signalled, final status |
| `casino_list_jobs()` | every known job, newest first |

Deliberately *not* implemented yet: anything that reads physics out of `out`.

---

## Next step

**`parse_out` — a plain function, no MCP.** *(done: `src/casino_mcp/parse_out.py`,
`tests/integration/test_examples_envmc.py`. It was validated against all 526 calculations in
PyCasino's examples tree; the 18 kept under `examples/` are the settings cover of those.)*

What the file taught, and what the next parser-shaped thing should assume:

- **`out` is a sequence of phases, not one result.** `vmc_opt` writes a VMC and an
  OPTIMIZATION phase per cycle (so N+1 `FINAL RESULT` blocks), `vmc_dmc` writes VMC, DMC
  equilibration and DMC statistics accumulation. Anything that returns *the* energy of a run
  is wrong; `parse_out` returns `phases` and points `result` at the last one with an energy.
- **`envmc` is not a copy of `out`.** It shells out to a Fortran helper that *recomputes* the
  block averages and error bars, so its error differs from the printed one — by a per cent on
  good statistics, by up to 10× on the sample variance. It is the right oracle for what the
  numbers mean and the wrong one for byte equality. The test therefore asserts the central
  values to envmc's printed precision, asserts that the error `parse_out` reports is the
  correlation-time row (the one closest to envmc's, which is how envmc labels its default),
  and only *reports* the variance-error ratio.
- **Where reblocking failed, envmc cannot adjudicate anything** — 390 of the example phases
  carry `Bad reblock convergence`, and there envmc's own estimate lands between CASINO's
  rows. The flag is parsed and the row check is skipped on it.
- **CASINO omits the sample-variance error for a single-block run.** It is then the one
  block's own error; that is the single derived number in the parser, and it is labelled.
- **varmin phases report a variance and no energy**, emin phases report both. That is not a
  gap: varmin's target is the variance, and the cycle's energy is in the next VMC phase.
- Nine example runs are incomplete (no `Total CASINO CPU time`); one has no `FINAL RESULT` at
  all. `complete` distinguishes them, and no energy is invented for the interrupted phase
  even though `envmc` rebuilds one from the blocks.

Then, in order:

2. ~~`casino_runtime` — the registry plus an adapter over `runqmc`, as a library with **no
   mention of MCP in it**.~~ *(done: `runtime.py`.)*
3. ~~The MCP server becomes a thin wrapper over that library; a CLI would be a second wrapper
   over the same code.~~ *(done: `server.py` delegates, `cli.py` is the second wrapper.)*
4. ~~Split out as a shippable `casino-mcp` package.~~ *(done, 0.1.0: own repository,
   `pyproject.toml`, 141 unit tests that need no CASINO, CI on 3.11–3.13.)*
5. **`casino_results(job_id)`** — the tool that returns physics to the model. Everything it
   needs exists (`parse_out`, and `casino-mcp parse` as its CLI twin); what is left is
   deciding what a *job* returns as opposed to a *file*, and it is the next thing to build.
6. `casino_prepare(source, dest, overrides)` — copy a directory and rewrite keywords. That is
   what "изменить параметр → новый каталог" needs, and it can lean on
   `casino/readers/validate.py` in PyCasino, which knows all 304 keywords and their types.

Extracting the package before step 1 would have meant designing HTTP, SLURM and Tasks for
users who did not exist yet, instead of having a working parser. Steps 5 and 6 are the same
bet in reverse: build the tool only once the library under it is boring.

### Why this order (product-architecture verdict, 2026-08)

- **`runqmc` *is* the runtime.** A design that configures `executable = …/casino` and
  `mpirun = …` and calls them directly signs up to reimplement 3939 lines of bash: arch
  detection, MPI variants, PBS/SLURM/LSF submission, `--auto-continue`, `.runqmc.lock`, blip
  conversion. The adapter goes *over* `runqmc`, and HPC support comes with it for free.
- **Tasks do not replace the job registry.** The `io.modelcontextprotocol/tasks` extension
  gives `ttlMs`, `pollIntervalMs`, cooperative cancel, and support that varies by client and
  needs opt-in on both sides. A DMC run outlives any TTL, must be visible from another
  session and from the shell, and needs a fallback when the client lacks the extension — so
  the on-disk registry is written either way. Tasks are worth ~50 lines *on top* of it
  (`taskId → job_id`, `tasks/get` reads `status.json`, `tasks/cancel` is the existing
  `killpg`), and `input_required` is the natural home for a Horizon C gate.
- **Stateless is neutral, not a win.** The state is a directory on disk, so every instance
  behind a load balancer must see the same filesystem anyway. It costs nothing here because
  the registry is already on disk.
- **Protocol drift.** `protocol_dump.py` speaks the legacy `initialize` +
  `notifications/initialized` handshake at `protocolVersion 2025-06-18`. The current revision
  (`2026-07-28`) replaced it with `server/discover` and per-request `_meta`; the old path stays
  compatible but is on a clock. Check what `mcp` 2.0.0 actually implements before packaging.
- **`Logging` is deprecated** alongside Roots and Sampling. Diagnostics go to stderr or a
  file; `notifications/message` is not an option any more, which happens to be what the stdio
  server already required.

---

## Conventions that keep it honest

- Return JSON-able dicts, never prose. Claude formats; the server reports.
- Never write to stdout in the server process. Logging goes to stderr or a file.
- Every tool that can refuse should refuse loudly with the reason and the fix, not silently
  do something safer than what was asked.
- Absolute paths in every record. A job outlives the cwd it was started from.
- `vmc_nstep` in `input` is the total over MPI processes, so changing `nproc` changes wall
  time and *not* the statistics — that is why `nproc` is a tool argument and not something
  the server should ever write into `input`.

---

## Configuration

**There is no configuration file, and adding one was tried and reverted.** An MCP server is
configured where it is registered — the `env` block of `.mcp.json` — and that file is already
being edited to make the server exist at all. A `~/.config/casino-mcp/config.toml` on top of
it is a second home for the same three values: a search order, a merge, and type validation,
~230 lines plus 30 tests, for what `CASINO_HOME` already does. `$CASINO_HOME` and
`$CASINO_ARCH` are CASINO's own variables, known to `runqmc` — setting them configures both
layers at once, and a private file would let the two disagree.

What is read: `CASINO_HOME`, `CASINO_ARCH`, `CASINO_RUNQMC`, `CASINO_MCP_STATE_DIR`,
`CASINO_MCP_FORBID` (`:`-separated like `PATH`). Everything else is a constant in
`settings.py`. Two rules when adding one:

- **A knob is added only when someone needs it to differ between two runs on the same
  machine.** Until then it is a default, and defaults belong in one place — which is why the
  tool signatures read `nproc: int = settings.NPROC` and `tests/test_server.py` asserts that
  the protocol layer never restates a literal.
- **Settings are read at call time, not cached at import.** No config object, no reset hook;
  a test just monkeypatches the environment.

`casino-mcp config` prints what the server would use right now and which variable said so.
It is the first thing to run when a tool call refuses.

---

## Testing

```
pytest                     # 141 tests, ~2 s, no CASINO
pytest -m integration      # needs runqmc/envmc, but nothing outside this repository
```

The unit suite runs anywhere, including CI. What stands in for CASINO is a fake `runqmc`
shell script (`fake_runqmc` in `tests/conftest.py`), which is enough to exercise the parts
that are ours: the launcher, the process group, the exit code, the log file, the guardrails.
The parser is checked field by field against five real `out` files under `tests/data/` — one
per shape: single VMC, varmin, emin, a DMC run split over 2 equilibration and 20 statistics
blocks, and a run that was killed — and, in `tests/test_examples.py`, against all 18
calculations in `examples/`. Each fixture keeps the `input` that produced it, so its asserted
numbers can be reproduced rather than trusted; all but the interrupted one were regenerated on
CASINO v3.1.24. `examples/` is where breadth lives, `tests/data/` is where precision does —
the assertions there pin exact values *and line numbers*, which is how a claim made from a
parsed number stays checkable against the file it came from.

`examples/` is the tree the integration suites read, and it is deliberately in-repo: pip
installing casino-mcp does not install PyCasino, so no test may point at its examples. The 18
are a *cover*, chosen out of PyCasino's 526 so that every runtype, basis type, sampling
method, optimiser and wavefunction option appears at least once, at the smallest total size
that achieves it. `tests/test_examples.py` asserts the cover still holds, so a calculation
cannot be deleted without the suite naming the setting that went with it. Paths are kept as
they are upstream, so a failure here is looked up there unchanged.

The runs were then cut to tens of seconds each (the tree is ~10 minutes on 4 processes) and
given `random_seed : standard`. That turns it from test data into a check on CASINO:
`tools/refresh_examples.py` re-runs the tree and reports shape changes (a line renamed, a
block dropped -- the parser has silently stopped reading something) apart from value changes,
and only the shape is asserted. Two examples are cut below the point where CASINO can reblock,
so the tree also holds the output shape of a run with too little data. What cannot be re-run
are the two interrupted files; they keep the `out` they were committed with, and every suite
skips them by asking `parse_out` whether the run completed rather than by keeping a list.

Two things learned from building it, both of which the naive version of this test gets wrong:

- **The seed does not pin an optimisation.** A plain VMC run repeats digit for digit under
  `random_seed : standard`; `backflow/3_1_1/25` does not, because a `vmc_opt` cycle
  redistributes configurations across MPI processes in an order the seed has no say in. Values
  are therefore never asserted.
- **`efficiency` is a clock reading, not a format.** CASINO derives it from a measured time, so
  a block that took 0.00 s has none, and on these short runs the line appears and disappears
  between two runs of the same binary. It is excluded from the field comparison; everything
  else is compared.

The integration suite needs a real installation and is deselected by default:

- `tests/integration/test_examples_envmc.py` — `parse_out` against CASINO's own `envmc`, one
  test per calculation under `examples/`. 17 pass, 1 skips (the Kr run that stopped before
  printing an energy, where envmc rebuilds one CASINO never wrote). ~1 s.
- `tests/integration/test_client_smoke.py` — real stdio MCP against the installed
  `casino-mcp serve`: the tool schemas, the guardrail, a short VMC to completion, a long one
  stopped, and a job that outlives the server that started it.

Two things that cost an hour each if rediscovered:

- The autouse `isolated` fixture strips the environment so no test can read the developer's
  installation or write into the real registry — but an integration test *keeps* it, because
  `runqmc` and `envmc` need `$CASINO_HOME` to find anything. A test that asserts "runqmc is
  not found" must also point `$CASINO_HOME` at nothing, or the developer's own install
  answers.
- An MCP client session cannot be a pytest fixture: anyio cancel scopes must be entered and
  left in the same task, and an async-generator fixture is torn down in a different one. Use
  the `mcp_session()` context manager inside the test.

The server still cannot be exercised through Claude Code's own tools in the session that
creates it — `.mcp.json` is read at startup. `tools/protocol_dump.py` prints every JSON-RPC
line in both directions with no SDK on the client side; read it before adding a tool.
