# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the versions follow
[semantic versioning](https://semver.org/) — pre-1.0, so the minor version may break things.

## [0.5.0] — 2026-08-30

A geminal wave function can be written now, which is the third file at the start of a chain
that no CASINO utility writes — and the tools that read a calculation say what the numbers
they hand back are, units and all, instead of answering with an untyped object.

### Added

- **The `GEMINAL` block of a `parameters.casl`**, which is the third file at the start of a
  chain that nothing else writes. `psi_s : geminal` replaces the Slater determinant with a sum
  of geminal determinants — the electrons are *paired* by `Phi(r,r') = sum_mk g_mk phi_m(r)
  phi_k(r')` rather than put in orbitals — and every `c` and `g` of it lives in that one block.
  `casino_prepare(source, dest, geminal=[])` writes the Hartree-Fock geminal, which is the
  Slater determinant exactly and the check the manual recommends making first;
  `geminal=['p:2', 'd:1']` adds a correlating geminal over the first two p levels and the first
  d level of the orbital file. `geminal_settings` holds the seeds, the anchors, the mirror
  geminal and the purity.
- The channels are *levels*, not orbitals, and that is the whole difficulty: a correlating
  geminal built out of one component of a degenerate level is not spherically symmetric, and
  optimizing it breaks the symmetry of the state it is meant to describe. So each level is tied
  together in `Constraints`, component by component and both ways round the diagonal. The levels
  are read off the orbital coefficients of `gwfn.data` — each MO classified by the (l, m-slot)
  carrying its weight, after the solid-harmonic constants CASINO premultiplies into d
  coefficients and (per `molden2qmc.py`) not into f and g ones are divided back out — and a
  level whose orbitals are not one clean component each is demoted to a diagonal-only tie with
  a warning rather than guessed at.
- The unpaired columns of an open shell go into *every* geminal with a non-zero `c`, not only
  the first: an empty unpaired column makes the geminal matrix singular at every configuration,
  which is `check_umat`'s errstop, and they are written fixed because `parse_umat_el` refuses an
  optimizable one. The occupation itself comes from `neu` and `ned` in the `input`, the only
  place that says which orbitals this calculation fills.
- Unlike a Jastrow factor, a geminal cannot start from zeros — a pairing matrix with an empty
  diagonal is singular — so the two leading correlating channels start at `seed` and `seed2`
  (−0.05 and −0.02, the values the committed examples start their cycles from).
- `casino-mcp prepare --geminal p:2,d:1 -g anchors=1 -g mirror=1`, the same for a shell.
- `tests/integration/test_geminal_casl.py`: every generated file put to the same `testrun : T`
  CASINO, which parses the block, resolves the constraint groups, checks them for contradictions
  and calls `check_umat`. Its last test is the one no unit test can make — a VMC run over the
  Hartree-Fock geminal against one over `psi_s : slater`, which is the only check that the
  orbital indices written mean what they are meant to.

### Changed

- **`casino_status`, `casino_results` and `casino_list_jobs` now declare what comes back.**
  Their return annotation was `dict[str, Any]`, out of which the SDK builds an output schema
  saying "an object" and nothing further — so a caller holding `acceptance : 50.1797` had
  nothing to tell it whether that is a per cent or a fraction, `correlation_time` steps or
  moves, `efficiency` per second of CPU time or per move. The three return models now, each
  field carrying its units and its meaning, and the schema travels with the tool listing. They
  are pydantic models rather than TypedDicts because the SDK builds a TypedDict's model through
  `get_type_hints` without `include_extras`, which drops every description on the way. Every
  field stays optional and every model allows extras: the runtime answers with the same plain
  dicts, an unknown job still answers with `error` alone, and a key that is not declared still
  reaches the caller. On the wire a validated reply now carries the fields it has no answer for
  as nulls instead of omitting them. `casino_run` and `casino_prepare` keep the free-form
  schema: what they answer is an account of what they did, which is not a fixed shape.
- `input_file.check_files` counts a fresh `parameters.casl` among the files the caller is about
  to write, as it already did a blank `correlation.data`: the file is prepared alongside the
  `input` it goes with, and refusing the input because it is not there yet would refuse the pair.

## [0.4.0] — 2026-08-28

The server can now read a calculation and write one, at both ends of a chain. Reading: a DMC
run takes hours, and until this release the only two moments it could be spoken about were
before it started and after it ended. Writing: the `input` for the next run, and — for the
first run, the one whose directory holds an orbital file and nothing else — the
`correlation.data` that no CASINO utility writes.

### Added

- **`casino_results(job_id)`** (`casino-mcp results`): the physics of one job as data —
  phases, energies, error bars, variance, per-block numbers, every value carrying the file
  and the line it was read from. `result` points at the number that is the run's answer.
- **A running DMC calculation can be read, and this is the only way to read one.** CASINO
  writes the mixed estimators into `out` once, at the very end; until then the current
  estimate lives in `dmc.status`, which it rewrites after every statistics block and
  *deletes* when the run finishes — copying the same text into `out` at that moment, so
  nothing is lost, but nothing is available either while it matters most. `parse_out` now
  reads that file when it is there (`parse_dmc_status`, one parser shared with the `out`
  section, because `write_dmc_status` in `dmc.f90` writes both), and points `result` at it.
  A run stopped by `casino_stop` keeps its `dmc.status`, so the last estimate it reached
  survives the stop.
- While a DMC run is still equilibrating there is no DMC energy anywhere yet, and `result`
  now says so instead of answering with the VMC phase — whose energy is the trial wave
  function's, not the calculation's.
- The reblock dump is parsed: the summary rows and the block-length table, with the row
  CASINO marked `*** BEST ***` — which is where the quoted error bar comes from, and whether
  the rows above it have flattened out is the whole question of whether it means anything.
- **`casino_prepare(source, dest, runtype, overrides)`** (`casino-mcp prepare`): copy a
  calculation into a new directory and write the `input` the next run needs. `runtype` fills
  in what that runtype requires and the source does not set — switching `vmc` to `vmc_dmc` is
  one keyword in the file and several more that CASINO then demands — while everything the
  source already says survives. Only what a calculation is *given* is copied: `input`, the
  orbital file, `correlation.data`, `parameters.casl`, the pseudopotentials and `config.in`;
  never `out`, the `.hist` files or `config.out`.
- `input_file`, the module under it: ESDF reading, an `apply` that edits rather than
  regenerates (hand comments, `%block`s and expert keywords all survive a rewrite), recipes
  for nine runtypes, and `check` for the combinations CASINO only rejects at run time — an
  optimisation sample smaller than the DMC population or larger than the number of steps that
  would write it, `opt_backflow` without `backflow`, the floor `opt_dtvmc` puts under
  `vmc_equil_nstep`, a missing mandatory keyword. Nothing is written unless the result would
  run; a refusal names the problems and creates no directory. What is legal but probably
  unintended comes back as `warnings`.
- The reblock dump is *not* an always-present field: `vmc.f90` prints it only inside the
  `derr > 0.1*err` branch, so a VMC phase has one exactly when its reblocking failed, while a
  DMC phase always does. `tools/refresh_examples.py` skips it in the field comparison for the
  same reason it skips `efficiency` — whether CASINO prints it is a property of the run, not of
  the output format.
- `tests/integration/test_recipes_check_only.py`: every recipe put to `runqmc --check-only`,
  which is the only oracle worth having for this — a recipe is right when CASINO says so, not
  when our own `check` does. It earned its place immediately: both `dmc_*_nstep` keywords are
  mandatory for *any* DMC runtype, including an equilibration-only one, and `dtdmc` and the
  `*_nblock`s are not mandatory at all. The tables now come from `runqmc`'s own.
- **A blank Jastrow factor and a blank backflow function**, for the calculation at the start of
  a chain: one that has an orbital file and nothing else, where `use_jastrow : T` and
  `backflow : T` need a `correlation.data` that does not exist yet. No CASINO utility writes one
  — nothing in `utils/` does, and the manual's own instruction is to copy an example and delete
  its parameter lines by hand — so
  `casino_prepare(source, dest, jastrow=['u', 'chi', 'f'], backflow=['eta', 'mu', 'phi'])`
  writes it, both blocks in the one file, each only if the input turns its keyword on.
- The backflow half is the Jastrow half twice over — a `mu` set is a `chi` set, `phi` is `f`
  with two more flags — with two differences that are not cosmetic. `eta` has a cutoff *per
  spin-pair channel*, and `read_cutoff_eta` errstops if the first line is missing. And the
  electron-nucleus cusp type of every `mu` and `phi` set is not a preference but a fact about
  the atom, 1 for a bare nucleus and 0 behind a pseudopotential — which CASINO reads and
  believes, checking it against nothing, so a wrong value there is not an errstop but a wrong
  wave function. It is derived from the `*_pp.data` files rather than defaulted.
- The one rule that could not be read off the CASINO source, and was found by putting orders to
  a `testrun : T` CASINO one at a time: an **all-electron `phi` set with `N_eN = 1` has no free
  parameters** once the cusp conditions are imposed, whatever `N_ee` is, while a pseudo-atom set
  at the same order is fine. Refused before the file is written, with the fix named.
- No `AE CUTOFFS` block is written: it is optional, and `init_pbackflow` gives every
  all-electron nucleus a set of its own with the length it would have defaulted to.
- `correlation_data`, the module under it: the geometry out of the orbital file's own header
  (`input` says how many electrons there are and never how many nuclei), one chi and f set per
  element with every atom labelled, and no parameter value anywhere — CASINO fills `alpha`,
  `beta` and `gamma` with zeros before it reads a line, and says "Not all coefficients supplied"
  when there are none. Which atoms are pseudo-atoms is read out of the `*_pp.data` files, each
  of which states its own atomic number, so the chi cusp is refused exactly where
  `read_chi_term` would errstop on it — on a pseudo-atom, and on a Slater-type basis.
- Cutoffs are written as zero, which CASINO reads as *use the default* and answers with
  `default_L_u`, `default_L_chi`, `default_L_f`. Writing a number instead would be
  reimplementing a choice that depends on the geometry; `warnings` says which values CASINO will
  take, and `jastrow_settings` sets one where the default is not wanted.
- `casino-mcp prepare --jastrow u,chi,f --backflow eta,mu,phi -j n_u=4`, the same for a shell.
- `tests/integration/test_blank_correlation.py`: every generated file put to a real CASINO with
  `testrun : T`, which reads the input files, imposes the cusp, no-duplication and no-cusp
  constraints, counts what is left free, checks that they hold, and stops — in a fraction of a
  second. `runqmc --check-only` is no oracle for this one: it never opens `correlation.data`.

### Changed

- The tool surface is six, where it was four. `casino_run`, `casino_status`, `casino_stop`,
  `casino_list_jobs`, and now `casino_results` and `casino_prepare`.
- A DMC recipe sets `popstats : T`. It is not CASINO's default, and it is what puts the
  statistical-efficiency section into `dmc.status` — the difference between a running
  calculation that can be read and one that cannot, for no cost.
- `input_file.check_files` takes `writing`, the files the caller is about to write. A blank
  `correlation.data` is prepared alongside the `input` it goes with, and refusing the input
  because the file is not there yet would refuse the pair.

### Fixed

- `casino_run(restart=true)` now deletes `dmc.status` along with the rest of what an earlier
  run left. Only an orderly end deletes it, so a killed DMC run leaves one behind, and a stale
  one next to a fresh `out` would have been read as the current estimate of a calculation that
  no longer exists.

## [0.3.0] — 2026-08-27

A packaging release. The server can now be run from an image rather than an installation,
it says which version of itself is running, and it carries the licence that scientific
software is most often redistributed under. Nothing about how CASINO is driven has changed.

### Added

- A `Dockerfile`: a `python:3.12-slim` image carrying nothing but the virtual environment,
  running as an unprivileged user, with `casino-mcp serve` as its default command. CASINO is
  not in it and does not need to be — the server starts and answers `tools/list` with no
  installation present, which is what an introspecting registry asks of it. To run a real
  calculation, mount the installation and point `CASINO_HOME` at the mount.

### Changed

- The licence is now Apache-2.0, where it was MIT. Same permissions, plus an explicit
  patent grant and the requirement to state modifications — the terms scientific-software
  users are most often required to redistribute under.

### Fixed

- The `initialize` handshake reported an empty server version. It now carries the one from
  `casino_mcp.__version__`, which the package metadata reads too — so the version is a single
  string in a single file rather than a copy in `pyproject.toml` to keep in step.

## [0.2.0] — 2026-08-26

Two things, one release. Stopping a calculation and continuing it now go through CASINO’s
own scripts — `haltqmc` and `runqmc` — instead of through signals and file moves of this
layer’s own devising. And the test data became a check on CASINO rather than a record of it:
every calculation the suite reads now lives in this repository, runs in minutes, and can be
re-run against a newly built CASINO to see whether the output format moved under the parser.

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

[Unreleased]: https://github.com/Konjkov/casino-mcp/compare/v0.3.0...HEAD
[0.3.0]: https://github.com/Konjkov/casino-mcp/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/Konjkov/casino-mcp/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/Konjkov/casino-mcp/releases/tag/v0.1.0
