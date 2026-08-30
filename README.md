# casino-mcp

[![PyPI](https://img.shields.io/pypi/v/casino-mcp.svg)](https://pypi.org/project/casino-mcp/)
[![Python](https://img.shields.io/pypi/pyversions/casino-mcp.svg)](https://pypi.org/project/casino-mcp/)
[![CI](https://github.com/Konjkov/casino-mcp/actions/workflows/ci.yml/badge.svg)](https://github.com/Konjkov/casino-mcp/actions/workflows/ci.yml)
[![Licence](https://img.shields.io/pypi/l/casino-mcp.svg)](LICENSE)

[![casino-mcp MCP server](https://glama.ai/mcp/servers/Konjkov/casino-mcp/badges/card.svg)](https://glama.ai/mcp/servers/Konjkov/casino-mcp)

An MCP control plane over the Fortran [CASINO](https://vallico.net/casinoqmc/) quantum Monte
Carlo code: write the `input` for the next calculation — and the blank Jastrow factor, backflow
function and geminal wave function for the first one — start it, know what is running, stop it, and read the
result as structured data instead of shipping 4000 lines of text into a model's context,
including from a DMC run that is still going, which has no energy in `out` at all until its
last block.

> **Beta (0.5.0).** Everything below is tested against a real CASINO: the recipes against
> `runqmc`'s own input check, and every file the wave function writer produces against a CASINO
> test run. Interfaces may still move before 1.0.

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
| `casino_run(workdir, nproc, version, restart, resume, unlock, allow_concurrent)` | job_id, pid, workdir, command, binary stamp, what `restart` removed |
| `casino_status(job_id)` | running / finished / failed / stopped / unknown, pid, runtime, exit code |
| `casino_wait(job_id, timeout)` | the same, once the calculation has ended — plus `waited` and `timed_out` |
| `casino_stop(job_id, timeout)` | what was signalled, final status, what `haltqmc` did |
| `casino_list_jobs(limit, workdir)` | every known job, newest first; `workdir` narrows it to one directory |
| `casino_results(job_id, fields)` | the physics: phases, energies, error bars, variance, acceptance, correlation time, efficiency, per-block numbers — each with the file and line it was read from; `fields` answers with just the paths asked for |
| `casino_input(job_id)` | the keywords and `%block`s a calculation was given — `random_seed` included, which CASINO never echoes into `out` |
| `casino_prepare(source, dest, runtype, overrides, jastrow, backflow, jastrow_settings, geminal, geminal_settings)` | a new calculation directory with the `input` — and, for a first run, the `correlation.data` and `parameters.casl` — the next run needs |

`casino_results` and `casino_input` answer different questions on purpose: what a run *did*
against what it was *told to do*. The `keywords` in a result are CASINO's own echo, which is
neither the file nor a superset of it — it holds every default CASINO applied, 70 entries
against the 23 a file typically sets, and drops what it does not print, `random_seed` among
them. `casino_input` also reads a directory nothing has run in, which is how a prepared
calculation is checked before there is a job to name it by, and keeps the `input` a stopped job
was started from, since `haltqmc -u` rewrites that file in place.

`fields` is what a scan wants from `casino_results`: a whole parsed run is 10–16 kB of JSON,
and 38 directories' worth of it is 600 kB to say six numbers a point. A path is written in the
run's own keys — `vmc`, `opt`, `dmc_equil`, `dmc_stats` mean the last phase of that kind,
`opt[3]` the cycle CASINO itself numbered, `phases[-1]` a position, and anything else is a key:
`keywords.DTVMC`, `cpu_time`, `vmc.energy.error`. A path that does not exist comes back in
`problems` naming what is there instead; a path that exists but holds a number CASINO never
printed comes back as null with its reason. `keywords.DTVMC` against `vmc.dtvmc` is the pair
worth asking for together — the step the input asked for against the step the run used.

Every `job_id` above is either the id `casino_run` returned or the calculation directory,
which is what a chain of runs actually holds: the registry knows which job ran where, so
nothing has to be written into the calculation directory to record it.

The runtype (`vmc`, `vmc_opt`, `vmc_dmc`, …) comes from the `input` file in `workdir`; there
is no tool per runtype, because that multiplies the surface without adding a capability.
What `casino_prepare` adds is the other half of that: it *writes* the `input`, filling in the
keywords a runtype requires and the source directory does not set, and refusing to write one
that CASINO would reject.

### Starting a chain: the blank wave function

The first calculation of a chain comes out of an orbital code with a wave function file and
nothing else, and `use_jastrow : T` needs a `correlation.data` that does not exist yet. No
CASINO utility writes one — the manual's own instruction is to copy an example and delete its
numbers by hand — so `casino_prepare(..., jastrow=['u', 'chi', 'f'], backflow=['eta', 'mu',
'phi'])` writes it, both blocks in the one file:

* the atoms come from the orbital file's own header, because `input` says how many electrons
  there are and never how many nuclei; one set per element, every atom labelled;
* which atoms are pseudo-atoms comes from the `*_pp.data` files, each of which states its own
  atomic number. In the Jastrow that decides where the chi cusp is *refused*, because CASINO
  errstops on it; in the backflow it decides the cusp *type* of every mu and phi set, which
  CASINO believes without checking — 1 for a bare nucleus, 0 behind a pseudopotential;
* every coefficient starts at zero, which is what the first optimisation cycle is for;
* the cutoffs are written as zero, which CASINO reads as *use your own default*: 2 or 5 a.u.
  for u, 4 for chi, 3 for f, 4.5 for mu and phi, and 1 or 4 for eta depending on whether the
  channel carries the e-e cusp. `warnings` says which values that will be. No AE CUTOFFS block
  is written either — it is optional, and CASINO picks those lengths itself.

`jastrow_settings` overrides any of the shape, for both blocks: `trunc_order`,
`bf_trunc_order`, `n_u`, `n_chi`, `n_f_en`, `n_f_ee`, `n_eta`, `n_mu`, `n_phi_en`, `n_phi_ee`,
every `spin_dep_*`, `cusp_chi`, `irrotational`, every `cutoff_*`, and `cusp_bf` for the rare
all-electron orbital set that does not satisfy the cusp condition. Finite systems so far: a
periodic Jastrow wants a P term, whose stars of reciprocal lattice vectors come from CASINO's
own `make_p_stars`.

A block is written only if the `input` turns its keyword on, and a keyword that is on with no
block is refused rather than left for CASINO to errstop over — the two halves of the same
mistake.

### The geminal wave function

`psi_s : geminal` replaces the Slater determinant with a sum of geminal determinants — the
electrons are *paired* by Φ(r,r′) = Σ g_mk φ_m(r) φ_k(r′) instead of put in orbitals — and
every parameter of it lives in the `GEMINAL` block of a `parameters.casl`. CASINO ships no
utility that writes one either, so `casino_prepare(..., geminal=[...])` does:

```python
casino_prepare('./hf', './gem', geminal=[])  # the Hartree-Fock geminal alone
casino_prepare(
    './hf',
    './gem',
    geminal=['p:2', 'd:1'],  # ... and a correlating one over
    geminal_settings={'anchors': [1]},
)  #     the first two p and first d levels
```

* **`geminal=[]` is the Hartree-Fock determinant, exactly.** `g_m,m = 1` over the doubly
  occupied orbitals and one `u_m,k` per singly occupied one; the manual recommends it as the
  check to make before correlating anything, and
  `tests/integration/test_geminal_casl.py` makes it — a VMC run over it has to land on the
  energy the same system gives with `psi_s : slater`. Being channel-less it reads no orbital
  file and works for any basis.
* **A channel is a degenerate level, not an orbital.** `p:2` means the first two p levels of
  the orbital file, and the whole of each is tied together in `Constraints`, component by
  component — a correlating geminal built out of one component of a level is not spherically
  symmetric, and optimizing it breaks the symmetry of the state it describes. The levels are
  read off the orbital coefficients of `gwfn.data`, so a channel needs a gaussian basis.
* **A level whose orbitals are not one clean m-component each is demoted, not guessed at.**
  It gets a diagonal-only tie and a line in `warnings` saying so, because component-wise
  off-diagonal ties between two levels that are mixed differently constrain orbitals that are
  not each other's counterparts.
* **The unpaired columns are not optional.** An open shell needs one `u_m,k` in *every*
  geminal with a non-zero `c`, since an empty unpaired column makes the geminal matrix
  singular at every configuration — CASINO's `check_umat` errstops on it — and they are
  written fixed, because `parse_umat_el` refuses an optimizable one.

`geminal_settings` holds the rest: `seed` and `seed2` (−0.05 and −0.02, the two leading
correlating diagonals, which start away from zero because a geminal holding only its anchors
is singular and has no gradient to move it), `anchors` (derived — every occupied orbital no
correlated level holds — unless given), `mirror` (a third geminal with `c = -1`, tied to the
second parameter for parameter), and `purity`.

### Reading a DMC run before it ends

A DMC calculation runs for hours and has **no energy in `out`** until the last block: CASINO
writes the mixed estimators once, at the end. Until then the current estimate lives in
`dmc.status`, which it rewrites after every statistics block and *deletes* when the run
finishes — copying the same text into `out` at that moment, so nothing is lost, but nothing is
available either while it matters most.

`casino_results` reads that file when it is there, and points `result` at it. So a running job
answers with the estimate as of its last block, and never with the VMC energy of the
configuration-generation phase — which is the trial wave function's, not the calculation's. A
run stopped by `casino_stop` keeps its `dmc.status`, so the last estimate it reached survives
the stop; a run still equilibrating has none, and `result` says so rather than reaching for an
earlier phase.

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

### One machine, one run at a time

A second run is refused while a job this server started is still going. Not because the
machine is busy — because of what sharing it does to the numbers. Two CASINO runs land on the
cores the scheduler gives them, and `Total CASINO CPU time` counts seconds of CPU across the
MPI processes: two jobs that ended up on the same core measured **97.2 s of CPU against
194.41 s of real time**, and `efficiency`, which is computed from the CPU time, was wrong by
the same factor. Nothing in the output says this happened. The ratio of `cpu_time` to
`real_time` does, which is one `casino_results(fields=['cpu_time', 'real_time'])`.

| | |
| --- | --- |
| another job of this server is running | refused; `allow_concurrent=true` starts it anyway, and the reply then names what it runs beside, under `concurrent` |
| a job of this server is running **in this directory** | refused, and nothing overrides it. One directory is one calculation. Stop it with `casino_stop` or wait for it with `casino_wait` |

Only jobs this server started are known here. A `pgrep casino` over the machine is
deliberately not done: someone else's process is someone else's business, and "something is
computing" with no owner and no job id is a refusal the caller has nothing to answer with. So
the reading afterwards is not a backstop but the check that actually holds — a run started
around the server spoils the timings just the same, and the registry never sees it.

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
casino-mcp status ./calc           # every job argument takes a directory: its newest job
casino-mcp wait   ./calc           # block until that calculation ends
casino-mcp stop   20260823-164511-qobn   # stop the run, then hand the directory to haltqmc
casino-mcp jobs                    # the registry, newest first
casino-mcp jobs -C ./calc          # ... or only what ran in one directory
casino-mcp results 20260823-164511-qobn   # the physics of that job, live runs included
casino-mcp prepare ./vmc ./dmc --runtype vmc_dmc -s dtdmc=0.005   # the next calculation
casino-mcp prepare ./hf ./opt --runtype vmc_opt --jastrow u,chi,f # ... and the first one
casino-mcp prepare ./hf ./bf --jastrow --backflow -s backflow=T   # ... with backflow in it
casino-mcp prepare ./hf ./gem --geminal p:2,d:1 -g anchors=1      # ... as a geminal wave function
casino-mcp input ./calc            # the keywords it was given, random_seed included
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

`parse_dmc_status` reads the `dmc.status` of a run that has not finished, through the same
parser: `write_dmc_status` in CASINO's `dmc.f90` writes that file and the `out` section from
one place, so reading them with two would be one more thing to keep in step. `parse_out` picks
it up on its own when the file is next to the `out` it was given.

## The `input` writer

`input_file` is the same shape in the other direction: text in, text out, no MCP.

```python
from casino_mcp import input_file

current = input_file.read('./vmc')
filled, missing = input_file.recipe('vmc_dmc', {'dtdmc': '0.02083'}, present=current['keywords'])
text = input_file.apply(current['text'], filled)  # edits; it does not regenerate
input_file.check(*input_file.parse_text(text))  # [] when CASINO would take it
```

`apply` only touches the lines it is named for, so hand comments, `%block`s and expert
keywords no recipe has heard of all survive a rewrite — a calculation's `input` is a document,
and the parts nobody can reconstruct are exactly the parts a template would drop. `build`
writes a whole file from a recipe for callers that have no source to start from.

The recipes and the rules come from `runqmc`'s own checks rather than from reading the manual,
and `tests/integration/test_recipes_check_only.py` puts every one of them back to
`runqmc --check-only`: a recipe is right when CASINO says the input is runnable, not when our
own `check` does.

## The `correlation.data` writer

`correlation_data` is the same shape again, and the layer under the `jastrow` and `backflow`
arguments above:

```python
from casino_mcp import correlation_data

geometry = correlation_data.read_geometry('./hf/gwfn.data')  # atoms, not orbitals
pseudo = correlation_data.pseudo_species('./hf')  # {8}, out of o_pp.data
problems = correlation_data.check(geometry, terms=('u', 'chi', 'f'), backflow=('eta', 'mu', 'phi'))
text = correlation_data.blank(geometry, backflow=('eta', 'mu', 'phi'), pseudo=pseudo)
```

Every label and every line of it is CASINO's own: the unit suite strips the numbers out of two
committed files — an optimised Jastrow and a hand-written blank backflow — and asserts that what
is left is exactly what this writes for the same atoms. `runqmc --check-only` is no oracle here,
it never opens the file, so `tests/integration/test_blank_correlation.py` uses `testrun : T`,
which makes CASINO read the input files, impose the cusp, no-duplication and no-cusp
constraints, count what is left free, check that they hold, and stop. That is also how the one
rule nobody could read off the source was found: an all-electron `phi` set with `N_eN = 1` has
no free parameters left, whatever `N_ee` is, while a pseudo-atom set at the same order is fine.

## The `parameters.casl` writer

`geminal` is the third writer of the same shape, and the layer under the `geminal` argument
above:

```python
from casino_mcp import geminal

orbitals = geminal.read_orbitals('./hf/gwfn.data')  # orbitals, not atoms
levels = geminal.mo_levels(orbitals)  # {1: [([3, 5, 4], True), ...], ...}
shells, diagonal, problems, notes = geminal.select(levels, [(1, 2)])  # the first two p levels
text = geminal.geminal_section([1, 2], [], [1], shells, diagonal)
```

CASL is *not* YAML — a constraint line reads `2^g_5,5=2^g_4,4`, which is a bare scalar no YAML
parser accepts — so the block is generated as plain text. Each MO is classified by the
(l, m-slot) its coefficients live on, after the solid-harmonic constants CASINO premultiplies
into d coefficients (and, per `molden2qmc.py`, *not* into f and g ones) are divided back out;
MOs of the same l are grouped into levels of 2l+1 in file order.

The oracle is again the committed examples plus a `testrun : T` CASINO: the unit suite asserts
that what this writes for the geminal calculations under `examples/` declares the same
parameters and imposes the same constraint groups as their hand-written `parameters.casl`, and
`tests/integration/test_geminal_casl.py` puts the files to CASINO itself, which parses the
block, resolves the constraint groups, checks them for contradictions and calls `check_umat`
before it stops.

## Tests

```bash
pytest                      # 337 tests, ~6 s, no CASINO needed
```

The unit suite runs anywhere: the parser is checked field by field against five real `out`
files under `tests/data/` — each with the `input` that produced it — and over all eighteen
calculations under `examples/`, while the launcher, the process group and the guardrails are
exercised against a fake `runqmc` shell script.

```bash
pytest -m integration
```

The integration suite needs a real CASINO, but nothing outside this repository. It checks
`parse_out` against CASINO's own `envmc` over every `out` in `examples/`, puts every input
recipe to `runqmc --check-only` and every blank `correlation.data` and `parameters.casl` to a
`testrun : T` CASINO, re-runs the whole tree against the installed binary, and drives the
server over real stdio MCP, running and stopping actual VMC calculations.

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

Apache-2.0 — see [LICENSE](LICENSE).
