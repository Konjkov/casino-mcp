# casino-mcp: full roadmap

Written for two readers: this repo (what to build next) and the CASINO developers
(why it is worth their attention, and what small changes in CASINO would make it much
better). Nothing here requires a single line of Fortran to change — but five things in
CASINO would make the whole layer cleaner, and they are listed at the end.

---

## 1. What CASINO already automates

Any proposal that ignores this is not worth reading, so first the honest inventory.

| already in CASINO | what it does | what it does not do |
| --- | --- | --- |
| `%block opt_plan` | several optimization cycles inside one run | nothing across runs |
| `runqmc --auto-continue` | restarts a run until it finishes | one directory, one job |
| `multirun`, `runqmc dirA dirB` | several directories at once | no bookkeeping, no results |
| `make_E_v_dt` + `extrapolate_tau` | `DT E ERROR` table, τ→0 fit | text in, text out; the series must be built by hand |
| `envmc`, `quickblock`, `reblock` | energies and reblocking from `out`/`*.hist` | formatted text for a human; `endmc` breaks under a non-C locale |
| `runqmcmd` | DMC-MD with PWSCF | a specific physics workflow, not a general driver |

So CASINO has *primitives* and one specialised workflow. What is missing is the layer
between them: no machine-readable results, no memory of what was run, and no place for the
judgement calls that sit between the steps.

Those judgement calls are the actual bottleneck of a QMC study. "Has the variance
plateaued?" "Is this DMC timestep still in the linear regime?" "Is that 0.4 mHa shift
population-control bias or a real change?" A graduate student answers them by looking at
numbers and deciding. A shell script cannot, which is why the shell scripts stop where they
stop. That is the gap this layer fills — and it is filled by a model reasoning over
structured numbers, not by more heuristics baked into more bash.

---

## 2. The thesis, and its limits

**What the layer is.** A protocol-level (MCP) control plane over CASINO: start runs, know
what is running, read results as structured data with provenance, keep every experiment in
its own directory, never lose the record of what produced a number.

**What it is not.** It is not physics automation that replaces judgement, and it must never
look like one. Three rules make it trustworthy:

1. **No number is ever produced by the model.** Every value a tool returns is read from a
   file, and carries the file and the line it came from. A tool that cannot find a value
   returns `null` and says why. There is no interpolation, no "approximately", no memory of
   an earlier run standing in for a missing one.
2. **Every result is reproducible from its record.** Job records freeze the input file, the
   binary (path, size, mtime, and later a hash of `src`), the process count, and the random
   seed. A result that cannot be regenerated is a bug.
3. **Nothing destructive is implicit.** Runs refuse to start in a directory that already
   holds results, and hard-refuse in one whose `out` is committed reference data. The
   guardrail is semantic, not syntactic.

If those hold, the model in the loop is doing what a careful human collaborator does — and
every claim it makes is checkable against a file on disk.

---

## 3. Stages

Stage 1 is done and verified against real CASINO. The rest is ordered so that every stage
is useful on its own and nothing is built before the thing under it works.

### Horizon A — control plane

**1. Run, watch, stop, remember.** *(done)*
`casino_run` / `casino_status` / `casino_stop` / `casino_list_jobs`. Launcher process in its
own session so the whole `runqmc → mpirun → casino` tree is one signalling unit and the exit
code survives the server restarting. Registry in `$XDG_STATE_HOME/casino-mcp`, never inside
the calculation directory.
*Verified:* He VMC through real MCP stdio, stop kills the tree and leaves the partial `out`
intact, jobs survive a server restart, a run in a committed reference directory is refused.

**2. Results as data.** `casino_results(job_id)` → energy, standard error (all three
corrections CASINO prints), variance, acceptance ratio, correlation time, efficiency, steps,
block count, CPU and real time; for DMC the reference-energy trace, the best estimate, the
effective timestep and the acceptance at each level. One parser, one schema, every field
tagged with where it was read. ~20 numbers instead of 800–4000 lines of `out`.
*Done when:* the parser reproduces `envmc` on every `out` under `examples/`, including the
ones where CASINO reports bad reblock convergence.

**3. Derived experiments.** `casino_prepare(source, dest, overrides)` — copy a calculation
directory, apply keyword changes, validate. Validation is free: `casino/readers/validate.py`
already knows all 304 keywords, their types, defaults, and which are mandatory per runtype.
This is the tool that makes "change a parameter → new directory → run" safe: `dest` must not
exist, `source` is never written to.

**4. Series.** `casino_run_batch(prepare_spec)` — one parameter, a list of values, N
directories, N jobs, one call. Plus notification when a set completes, so a τ-series is
launched once and reported once rather than polled.

### Horizon B — analysis primitives

Still no judgement here: these tools compute, they do not conclude.

**5. Reblocking.** Error bars straight from `vmc.hist` / `dmc.hist` rather than from CASINO's
on-the-fly estimate: the full block-length series, the plateau, the correlation time and its
error. This is where CASINO's own `reblock` is interactive and therefore unusable from a
script, and where `endmc` is locale-broken.

**6. Optimization traces.** Per-cycle energy, variance, and parameter-vector norm from
`out` + `correlation.out.N`. This is what tells you whether varmin actually converged, and
it is currently read by eye. Note that a rising variance during emin is expected, not a
fault — a diagnostic that does not know this produces false alarms.

**7. Comparison.** `casino_compare(job_ids)` → differences in units of the combined
standard error. Deliberately thin: it subtracts and divides. The interpretation stays in the
conversation, where it belongs.

**8. Extrapolations.** τ→0 for DMC and target-weight→∞ for population control, as weighted
least squares with the fit uncertainty and the residuals returned, so a bad fit is visible
rather than averaged away. Same job as `extrapolate_tau`, but the series is assembled from
the job registry instead of by hand, and the output is data.

### Horizon C — workflows

The recipes are *documented procedures the model follows using the primitives*, not
monolithic tools. That distinction is the whole point: a `casino_full_study()` tool would
bury exactly the decisions that need to be visible.

**9. The standard chain.** Slater → Jastrow (varmin, then emin) → backflow → DMC over a τ
series → extrapolation, each step gated on the previous step's numbers, each in its own
directory, with the decision and the numbers behind it written into the record.

**10. Diagnostics with stated criteria.** Every gate is an explicit, citable rule rather than
a vibe: VMC acceptance near the measured efficiency optimum (≈0.7 for `vmc_method 1`, ≈0.6
for the position-dependent step) rather than the folklore 50%; variance plateau within its
own error; DMC timestep bias linear within the fitted range; population-control bias below
the statistical error. When a gate fails the run stops and says which number failed.

### Horizon D — for developing CASINO itself

This is the part that is directly useful to the people who maintain the code, and it costs
almost nothing once Horizon B exists.

**11. Regression harness.** N example directories × two binaries → a table of ΔE in units of
σ, with the wall time per system. Answers "did my change to `dmc.f90` move any energy beyond
its error bar, anywhere?" in one command instead of a week of manual comparison. Because job
records stamp the binary, the comparison is automatic and cannot silently compare a result
against itself.

**12. Provenance.** Every job record ties to the binary hash and, when the source tree is a
git checkout, the commit. A result is then bisectable: given a regression, the same harness
walks commits until the energy moves. This is the mechanical part of debugging a physics
code, and it is entirely automatable.

**13. Cross-validation against an independent implementation.** PyCasino reads the same
directories and computes the same quantities from an independent codebase. Two things fall
out: statistical agreement of energies and variances, and — using CASINO's `config.out`,
which stores configurations *and* their local energies — a bit-level comparison of the wave
function itself, independent of sampling (agreement to ~1e-14 when both are right). That
comparison is how the `figem` indexing bug, the `tol_log_softzero` threshold, and the four
backflow+geminal defects were found by hand. Automating it turns a one-off investigation
into a standing check.

### Horizon E — what it could become

Speculative but concrete, and this is the part meant to interest CASINO developers:

- **A nightly conscience for the code.** The whole `examples/` tree re-run against `master`,
  every energy compared to its reference with a proper error bar, a one-page report. CASINO
  has the reference data already; what is missing is the machinery to compare it without a
  human.
- **A QMC study that runs overnight instead of over a fortnight.** The mechanical labour of
  a study — build the series, watch the queue, reblock, extrapolate, tabulate — is exactly
  what this layer removes, leaving the physics decisions visible and logged.
- **Reproducibility artifacts for papers.** Every number in a table traceable to a job
  record: input, binary, commit, seed, raw output. Journals increasingly ask; CASINO users
  currently assemble this by hand if at all.
- **A lower barrier to entry.** Most of the cost of a first QMC calculation is not physics,
  it is knowing which of 304 keywords matter and in which order to do things. A layer that
  validates, explains the failure, and suggests the next step is a teaching tool as much as
  an automation one.
- **Protocol, not integration.** MCP is a published standard with multiple clients. This is
  not a bespoke bridge to one product — anything speaking MCP gets CASINO for free, and the
  server is ~400 lines of Python that CASINO does not have to maintain.

---

## 4. Five small things in CASINO that would make this much better

Ordered by ratio of benefit to effort. Each is independent, and none changes physics.

1. **A machine-readable summary.** A `write_json_summary` keyword making CASINO dump the
   `FINAL RESULT` block, the per-block statistics and the timings as JSON alongside `out`.
   Every consumer of CASINO output — this layer, `envmc`, plotting scripts, other people's
   pipelines — currently reimplements a parser for formatted Fortran text, and those parsers
   break whenever a column width changes.
2. **Meaningful exit codes.** Distinguishing "input error", "file missing", "converged",
   "ran out of time" from a blanket non-zero would let a driver react correctly instead of
   grepping stderr.
3. **A graceful halt file.** CASINO can stop cleanly at a block boundary only via
   `max_cpu_time`/`max_real_time`. A file the code checks between blocks (`.casino_halt`)
   would make stopping lossless — currently a stop costs the current block, so a driver that
   wants to reallocate resources has to choose between waiting and throwing away data.
4. **A machine-readable optimization trace.** Cycle, energy, variance, parameter norm, one
   line each. It is already printed for humans; emitting it as data would make convergence
   diagnostics reliable rather than regex-based.
5. **A header on `vmc.hist`/`dmc.hist`.** Column names and units in the file itself, so a
   reader does not have to know the layout by version.

Item 1 alone would remove most of the fragility of every third-party CASINO tool in
existence, this one included.

---

## 5. Non-goals and risks

- **Not a scheduler.** On HPC, `runqmc` already knows how to submit to the batch system. The
  job manager must eventually delegate to it and track queue IDs, not run a second queue of
  its own.
- **Not a shell.** There is no `execute_shell` and there never will be one. Every tool is a
  named CASINO operation with typed arguments.
- **Not a replacement for reading `out`.** The structured summary is for routine decisions;
  anything surprising is investigated in the file itself.
- **The model can be wrong.** The mitigation is not a better model, it is that every claim
  it makes points at a number in a file that a human can check in five seconds.
- **Scope creep is the real failure mode.** Each stage ships and is used before the next one
  starts. A tool is added when a workflow actually needs it, never in anticipation.
