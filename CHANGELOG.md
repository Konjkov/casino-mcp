# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the versions follow
[semantic versioning](https://semver.org/) — pre-1.0, so the minor version may break things.

## [Unreleased]

### Added

- `casino_run(resume=true)` (`casino-mcp run --continue`): carry an interrupted run on instead
  of starting it again. Which of CASINO's two continuation routes that takes is read out of
  `out` rather than chosen here: a run CASINO stopped on `max_cpu_time` / `max_real_time` left
  a `CONTINUATION INFO:` block and is continued by `runqmc --continue`, and a run that was
  interrupted is continued by a plain `runqmc` over the `input` that `haltqmc -u` rewrote —
  `--continue` errstops there, on continuation info that was never written. A run that reached
  its own end is refused: there is nothing to continue. The reply says which route was taken.
- `CASINO_HALTQMC`, alongside `CASINO_RUNQMC`: an explicit path to `haltqmc`, otherwise `PATH`,
  then `$CASINO_HOME/bin_qmc/haltqmc`. `casino-mcp config` reports it and says when it is
  missing — a job can still be stopped without it, but its directory will not be tidied.

### Changed

- `casino_stop` no longer signals the whole process tree and clears the lock file itself. It
  sends SIGTERM to that job's `casino` processes and to nothing else — the same signal
  `haltqmc -k` sends, except that haltqmc's own kill is a `pkill -x casino` over every CASINO
  process the account owns, which on a machine running several jobs would take the others down
  too. The ranks are found by session id, because `mpirun` puts each of them in a process group
  of its own and `killpg` therefore never reaches them. `runqmc` is left alive to finish its
  epilogue — the per-node output concatenated into `out`, its own lock file removed — and only
  a job still running after `timeout` has its process group signalled and then killed.
- Everything a stop then does to the directory is `haltqmc -f -u`: `config.out` to `config.in`,
  the lock and marker files, and `input` rewritten for the work that is left. The reply carries
  what it did under `halt`. Since that rewrite is the one thing `restart` cannot undo — CASINO
  refuses `newrun : F` without the `config.in` restarting deletes — the `input` as it was is
  copied into the job directory first, and `restart=true` on a directory whose `input` is set
  up to continue is refused with that copy's location.
- **Breaking**: `overwrite` is replaced by `restart`, which does what `overwrite` only claimed
  to. `overwrite=true` lifted the refusal to start in a directory that already held an `out`
  and then deleted nothing, so `runqmc` — which appends — produced an `out` containing two
  runs, and left the previous `.hist` files and configs to be appended to as well.
  `restart=true` deletes them: `out`, `out_part.N`, `.out_proc*`, `vmc.hist`, `dmc.hist` and
  their numbered backups, `config.in`/`config.out` and their `_fixed`/`_nofixed` forms,
  `correlation.out.N`, `parameters.N.casl`, `saved_part_N/`. Inputs are never touched — the
  list is named rather than derived, because the same directory holds the wave function, the
  pseudopotentials and a `correlation.data` that is usually hand-edited. The reply carries
  `removed`, the names that went. Nothing is deleted until every check that could refuse the
  run has passed.

## [0.2.0] — 2026-08-25

The test data became a check on CASINO rather than a record of it. Nothing in the tool surface
changed; what changed is that every calculation the suite reads now lives in this repository,
runs in minutes, and can be re-run against a newly built CASINO to see whether the output
format moved under the parser.

### Added

- `examples/`: eighteen real CASINO calculations committed with the `out` files they produced,
  and enough of their inputs to be re-run. They are a settings cover, not a sample — chosen out
  of PyCasino's 526 so that every runtype, basis type, sampling method, optimiser and
  wavefunction option appears at least once, at the smallest total size that achieves it,
  together with a run that never printed an energy and one interrupted mid-optimisation.
- `tests/test_examples.py`: the tree parses, and it still covers every setting it was
  assembled to cover. Runs without CASINO, so a calculation cannot be dropped from `examples/`
  without the suite naming the setting that went with it.
- `tools/refresh_examples.py` and `tests/integration/test_examples_rerun.py`: the tree is
  re-run against the installed CASINO and compared with what was committed. The test asserts
  only that no phase, keyword or number `parse_out` reads has disappeared, which is what a
  changed output format looks like from a parser's side. Moved values are reported, never
  asserted: a new release may legitimately produce different numbers, and `random_seed` does
  not pin an optimisation run anyway — it redistributes configurations across MPI processes and
  lands somewhere slightly different each time. Efficiency is excluded from the comparison
  entirely, being computed from a measured time that rounds to zero on a short block.

### Changed

- The parser fixtures under `tests/data/` were regenerated on CASINO v3.1.24 and now each keep
  the `input` that produced them, so their asserted values can be reproduced rather than
  trusted. The DMC fixture moved from krypton to beryllium — 4 electrons instead of 36 — and
  from a single statistics block to 2 equilibration and 20 statistics blocks, which is the
  shape a wandering DMC population actually shows up in and which nothing was asserting
  before. The interrupted fixture was left alone; it was already v3.1.24, and a run that was
  killed cannot be reproduced by running.
- The integration suite reads `examples/` and nothing else: `--examples-dir`, `$CASINO_EXAMPLES`
  and the dependency on a PyCasino checkout are gone. Installing casino-mcp does not install
  PyCasino, so `pytest -m integration` now needs only CASINO itself.
- The example calculations were shortened and re-run in one pass, so the tree takes minutes
  rather than days to reproduce. This costs statistics and no output format: the longest run
  went from 2.5 hours to a couple of minutes with every phase, keyword and printed number
  intact. Two examples are cut below the point where CASINO can reblock, which is the one
  output shape the tree previously had no instance of.

## [0.1.0] — 2026-08-23

First packaged release. The control plane works and is tested; the tools that return physics
to the model are not shipped yet.

### Added

- MCP server over stdio with four tools: `casino_run`, `casino_status`, `casino_stop`,
  `casino_list_jobs`. No generic shell tool, by design.
- An on-disk job registry under `$XDG_STATE_HOME/casino-mcp`, so a calculation outlives the
  server that started it, and a launcher process per job, so the whole tree
  (runqmc → mpirun → casino) can be signalled as one group and its exit code recorded.
- `parse_out`: a CASINO `out` file as structured phases, every number carrying the line it
  was read from. No MCP, no dependencies. Checked against `envmc` over 526 example files.
- Configuration through the environment, which is what `.mcp.json` can set per server
  registration: `CASINO_HOME`, `CASINO_ARCH`, `CASINO_RUNQMC`, `CASINO_MCP_STATE_DIR`,
  `CASINO_MCP_FORBID`. Every other default is a constant in `settings.py`.
- A `casino-mcp` command: `serve`, `config`, `run`, `status`, `stop`, `jobs`, `parse`.
- Guardrails against destroying results: a directory that already holds an `out`, one whose
  `out` is tracked by git, one another runqmc has locked, and a `$CASINO_MCP_FORBID` list
  that has no per-call override.
- Provenance: every job record freezes the command and the path, size and mtime of the
  `casino` binary it ran.
- Tests: 102 unit tests that need no CASINO (a fake `runqmc` stands in for the real one), plus
  an opt-in integration suite that drives real calculations over real stdio MCP.

### Notes

- `parse_out` derives exactly one number: the sample-variance error of a single-block run,
  which CASINO does not print. It is taken from the one block as `envmc` does, and labelled.
- Nothing shells out to `envmc`/`endmc` at runtime; `endmc` misparses numbers under a non-C
  locale.

[Unreleased]: https://github.com/Konjkov/casino-mcp/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/Konjkov/casino-mcp/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/Konjkov/casino-mcp/releases/tag/v0.1.0
