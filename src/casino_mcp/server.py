"""MCP server driving the Fortran CASINO code.

Tools are named CASINO operations; there is deliberately no generic shell tool. Everything
these tools do lives in `runtime.py` and `parse_out.py`, which know nothing about MCP -- this
module is the protocol surface and the docstrings the model reads, and nothing else.

Never write to stdout from this process: stdout is the JSON-RPC stream, and printing into it
is the single most common way to break a stdio MCP server. Diagnostics go to stderr.
"""

from typing import Any, Literal

from mcp.server.mcpserver import MCPServer
from pydantic import BaseModel, ConfigDict, Field

from casino_mcp import __version__, runtime, settings

# The version travels in the initialize handshake, so a client reporting a misbehaving
# server names a release; it is the one in __init__.py, never a second copy.
server = MCPServer(
    'casino',
    instructions='Run and monitor Fortran CASINO calculations. One directory is one calculation.',
    version=__version__,
)


# The return models below are the other half of the documentation: a tool docstring says what a
# call does, an output schema says what the numbers coming back are, and a number whose units are
# not written down anywhere is a number a caller has to guess at. They are pydantic models rather
# than TypedDicts because the SDK builds a TypedDict's model through `get_type_hints` without
# `include_extras`, which drops every `Field(description=...)` on the way -- the one thing worth
# having here. Every field is optional and every model allows extras: the runtime answers with
# what it knows, an unknown job answers with `error` alone, and a key that is not declared here
# still reaches the caller instead of being validated away.
#
# The tools go on returning the runtime's plain dicts, which the SDK validates against these
# models on the way out. Returning a model instead would serialise every unset field as a null
# into the text half of the reply, which is the half a model reads -- hence the ignores below.


class Measured(BaseModel):
    """One number as CASINO printed it, with where it was read from."""

    model_config = ConfigDict(extra='allow')

    value: float | str | None = Field(None, description='the number itself, or null when CASINO did not print it, and then `reason` says why')
    error: float | None = Field(None, description='the standard error CASINO printed with it, in the units of `value`')
    line: int | None = Field(None, description='1-based line of the file the value was read from')
    reason: str | None = Field(None, description='why there is no value')
    derived: str | None = Field(None, description='set when the value was computed here rather than printed, naming what it was computed from')


class Phase(BaseModel):
    """One phase of a run. The fields a phase does not have are absent, not zero.

    Units are CASINO's own, as its `out` labels them: an acceptance ratio in per cent and not a
    fraction, a correlation time in steps of this run and not in moves, an efficiency in inverse
    variance per second of CPU time, which is comparable between runs only at equal process count.
    """

    model_config = ConfigDict(extra='allow')

    kind: Literal['vmc', 'opt', 'dmc_equil', 'dmc_stats'] | None = Field(None, description='what CASINO was doing in this phase')
    label: str | None = Field(None, description='the banner line of the phase in `out`')
    index: int | None = Field(None, description='the phase number CASINO gave it, counting from 1 within its kind')
    line: int | None = Field(None, description='1-based line of `out` the phase starts at')
    energy: Measured | None = Field(None, description='the total energy of this phase, au per simulation cell, with its standard error')
    variance: Measured | None = Field(None, description='the sample variance of the local energy, au^2')
    nblock: int | None = Field(None, description='number of blocks in the phase')
    blocks: list[dict[str, Any]] | None = Field(None, description='the same quantities block by block, each with its own `time` in seconds')
    acceptance: Measured | None = Field(None, description='mean acceptance ratio over the blocks, per cent')
    correlation_time: Measured | None = Field(None, description='mean correlation time over the blocks, in VMC steps')
    efficiency: Measured | None = Field(None, description='mean efficiency over the blocks, au^-2 s^-1: 1 / (variance of the mean * CPU seconds)')
    steps_per_process: Measured | None = Field(None, description='VMC steps one process took, which is vmc_nstep divided over the processes')
    dtvmc: Measured | None = Field(None, description='the VMC time step, au: the variance of the gaussian proposal, not its width')


class BinaryStamp(BaseModel):
    """The `casino` binary as it was at launch, frozen into the job record.

    Not which binary, but which build of it: this is what tells results from before and after a
    rebuild apart afterwards. `size` and `mtime` are absent when the binary was not there to stat.
    """

    model_config = ConfigDict(extra='allow')

    path: str | None = Field(None, description='the binary runqmc was given, under bin_qmc/<arch>/<version>')
    exists: bool | None = Field(None, description='whether it was there when the job was launched')
    size: int | None = Field(None, description='its size in bytes')
    mtime: str | None = Field(None, description='local time it was last built')


class JobState(BaseModel):
    """One job as the control plane sees it, which is what runqmc and /proc say and nothing about the physics."""

    model_config = ConfigDict(extra='allow')

    job_id: str | None = Field(None, description='the id every other tool takes')
    status: Literal['running', 'finished', 'failed', 'stopped', 'unknown'] | None = Field(
        None,
        description=(
            "'finished' means runqmc exited 0, not that the physics is any good; 'failed' that it did not; 'stopped' that casino_stop halted it; "
            "'unknown' that the launcher vanished without recording an exit code"
        ),
    )
    workdir: str | None = Field(None, description='the calculation directory: one directory is one calculation')
    command: list[str] | None = Field(None, description='the runqmc command line as it was launched')
    nproc: int | None = Field(None, description='MPI processes the job was given')
    pid: int | None = Field(None, description='pid of the launcher, not of the casino processes under it')
    binary: BinaryStamp | None = Field(None, description='the build of the CASINO binary this job ran: path, size and mtime as they were at launch')
    started: str | None = Field(None, description='local time the job was launched')
    finished: str | None = Field(None, description='local time the launcher exited')
    stopped: str | None = Field(None, description='local time casino_stop signalled it')
    runtime: float | None = Field(None, description='seconds of wall clock since the job started, or its whole life once it ended; not CPU time')
    exit_code: int | None = Field(None, description="runqmc's exit code")
    runqmc_log: str | None = Field(None, description="the launcher's own log, where a failure that never reached CASINO is explained")
    note: str | None = Field(None, description='something about this answer the caller would otherwise have to infer')
    error: str | None = Field(None, description='set instead of everything else when the call could not be answered')


class Waited(JobState):
    """What casino_wait answers with: the job's state, and what the waiting came to."""

    waited: float | None = Field(None, description='seconds this call blocked for')
    timed_out: bool | None = Field(None, description='true when the timeout ran out first and the job is still running')


class Input(BaseModel):
    """A calculation's `input` as data: what the run was told to do."""

    model_config = ConfigDict(extra='allow')

    workdir: str | None = Field(None, description='the calculation directory the file was read from')
    path: str | None = Field(None, description='the `input` itself')
    runtype: str | None = Field(None, description='RUNTYPE as this file sets it')
    job_id: str | None = Field(None, description='the newest job that ran here, when one has; absent for a directory nothing has run in')
    status: str | None = Field(None, description="that job's state")
    keywords: dict[str, str] | None = Field(None, description='every keyword the file sets, verbatim: names as written, values unparsed')
    blocks: dict[str, list[str]] | None = Field(None, description='every %block in the file, as its lines -- opt_plan, npcell, and the rest')
    before_halt: dict[str, Any] | None = Field(
        None, description='the same for the `input` this job was started from, when casino_stop has since let haltqmc rewrite it'
    )
    note: str | None = Field(None, description='something about this answer the caller would otherwise have to infer')
    error: str | None = Field(None, description='set instead of everything else when the call could not be answered')


class JobList(BaseModel):
    model_config = ConfigDict(extra='allow')

    jobs: list[JobState] | None = Field(None, description='every known job, newest first')


class Results(JobState):
    """A job's state and the physics in its files. Only this level is typed; a phase's blocks stay free-form."""

    path: str | None = Field(None, description='the `out` these numbers were read from')
    runtype: str | None = Field(None, description='RUNTYPE as the input set it: vmc, vmc_opt, vmc_dmc, ...')
    # CASINO's own `Started` line wins over the launch time, and it is a measured value, not a string
    started: Measured | str | None = Field(  # type: ignore[assignment]
        None, description='local time CASINO started, from `out`, falling back to the time the job was launched'
    )
    complete: bool | None = Field(None, description='true once CASINO wrote its timing report, the only sign that the run reached its own end')
    cpu_time: Measured | None = Field(None, description='Total CASINO CPU time, seconds, summed over the MPI processes')
    real_time: Measured | None = Field(None, description='Total CASINO real time, seconds of wall clock; twice the CPU time means shared cores')
    ended: Measured | None = Field(None, description='local time CASINO stopped, as it printed it')
    phases: list[Phase] | None = Field(None, description='the run as the sequence of phases it is')
    dmc_status: dict[str, Any] | None = Field(
        None, description='the estimate a running DMC job has reached, from `dmc.status`, which only an orderly end deletes'
    )
    result: dict[str, Any] | None = Field(
        None,
        description=(
            "the number that is this run's answer: {phase, kind, energy, variance}, or {source, kind, energy, variance} from `dmc.status`, "
            'or {value: null, reason} when this run has no answer yet'
        ),
    )
    messages: list[dict[str, Any]] | None = Field(None, description='the warnings and errors CASINO printed, each {line, text}')
    fields: dict[str, Any] | None = Field(None, description='what `fields` asked for, as one flat {path: value}; the rest of the report is not sent')
    reasons: dict[str, str] | None = Field(None, description='for a projected path that is null, why CASINO printed no number there')
    problems: list[str] | None = Field(None, description='the paths in `fields` that do not exist in this run, each naming what is there instead')


@server.tool()
def casino_run(
    workdir: str,
    nproc: int = settings.NPROC,
    version: str = settings.VERSION,
    restart: bool = False,
    resume: bool = False,
    unlock: bool = False,
) -> dict[str, Any]:
    """Start a CASINO calculation in workdir and return immediately.

    The runtype (vmc, vmc_opt, vmc_dmc, ...) comes from the `input` file in workdir;
    this tool only decides how the binary is launched. The calculation keeps running
    after the call returns and after this server is restarted.

    A directory that already holds an `out` is refused, because runqmc appends to it and
    the result is two runs in one file. `restart` and `resume` are the two ways past that,
    and they are opposites -- pass one.

    workdir: directory holding `input` and the wave function files.
    nproc: number of MPI processes (`vmc_nstep` in `input` is the total over all of them).
    version: binary flavour, 'opt' or 'debug'.
    restart: delete `out` and everything else the earlier run left -- `.hist` files, configs,
        optimisation output -- and start the calculation over. Inputs are kept. Destructive:
        `config.in` goes too, so what could have been continued no longer can be. Refused on a
        directory whose `input` haltqmc has set up to continue (NEWRUN : F), because CASINO
        then wants the `config.in` this would delete; casino_stop keeps a copy of the input as
        it was, and the reply to the stop says where.
    resume: carry the interrupted run on, keeping the work already done. Which of CASINO's
        two continuation routes that takes is read out of `out`, not chosen here: a run that
        CASINO stopped on max_cpu_time / max_real_time is continued by `runqmc --continue`,
        and a run that casino_stop halted is continued by a plain `runqmc` over the `input`
        that `haltqmc -u` rewrote. The reply says which one under `resume`. A run that
        reached its own end is refused -- there is nothing to continue.
    unlock: clear a stale .runqmc.lock left by a runqmc instance that died.
    """
    return runtime.start(workdir, nproc=nproc, version=version, restart=restart, resume=resume, unlock=unlock)


@server.tool()
def casino_prepare(
    source: str,
    dest: str,
    runtype: str = '',
    overrides: dict[str, str | None] | None = None,
    jastrow: list[str] | None = None,
    backflow: list[str] | None = None,
    jastrow_settings: dict[str, float] | None = None,
    geminal: list[str] | None = None,
    geminal_settings: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Copy a calculation into a new directory and write the `input` for the next run in it.

    This is how a calculation becomes the next one: optimise a wave function, then prepare a
    `vmc_dmc` directory beside it; halve the timestep into another; change the target weight
    into a third. A copy rather than an edit, because a number whose input was overwritten in
    place can no longer be reproduced, and because casino_run refuses a directory that already
    holds a run -- rightly.

    What is copied is what a calculation is given and never what a run produced: `input`, the
    orbital file, `correlation.data`, `parameters.casl`, the pseudopotentials, and `config.in`
    (which a dmc-only or `opt` runtype reads as an input). Not `out`, not the `.hist` files, not
    `config.out`. A symlinked orbital file is copied by content, so the new directory stands on
    its own.

    runtype: the runtype the new directory is for -- vmc, vmc_opt, opt, vmc_dmc, vmc_dmc_equil,
        dmc_dmc, dmc_equil, dmc_stats. Every keyword that runtype needs and the source input
        does not set is filled from a working default; every keyword the source does set is
        kept, so the electron count, the basis and any hand tuning survive. Leave it empty to
        keep the source's runtype and only apply `overrides`.
    overrides: keywords to set, as {name: value}, and they win over both the source and the
        defaults. A null value deletes the keyword; a value containing newlines is written as a
        `%block` (that is how `opt_plan` and `npcell` are set). Values are written verbatim, so
        booleans are 'T' and 'F' as CASINO spells them.
    jastrow: the terms of a blank Jastrow factor to write into the new directory -- ['u', 'chi',
        'f'] for the usual one, ['u'] for a system with no atoms. This is for the first
        calculation of a chain, the one whose directory holds an orbital file and nothing else:
        `use_jastrow : T` needs a `correlation.data`, no CASINO utility writes one, and the
        manual's own instruction is to copy an example and delete its numbers by hand. Every
        coefficient starts at zero, which is what the first optimisation cycle is for. Leave it
        unset when the source already has a `correlation.data`; asking for both is refused,
        because a blank Jastrow would discard an optimised one. Finite systems only so far: a
        periodic Jastrow wants a P term, whose stars come from CASINO's own `make_p_stars`.
    backflow: the terms of a blank backflow function, in the same file -- ['eta', 'mu', 'phi']
        for the usual one. It goes with `backflow : T` in the input, and the two blocks are
        written together for a calculation that wants both. The electron-nucleus cusp type of
        each set is not a setting: it is read off the pseudopotentials in the directory, 1 for a
        bare nucleus and 0 behind a pseudopotential, because CASINO believes the flag without
        checking it. No AE CUTOFFS section is written -- it is optional, and CASINO chooses the
        lengths itself.
    geminal: the GEMINAL block of a `parameters.casl`, which is what `psi_s : geminal` reads --
        a geminal wave function pairs the electrons instead of putting them in a determinant,
        and CASINO ships no utility that writes the file. An empty list writes the
        Hartree-Fock geminal alone: g_m,m = 1 over the occupied orbitals and one fixed
        unpaired column per singly occupied one, which is the Slater determinant exactly and
        the check to make before correlating anything. A list of channels -- ['p:2', 'd:1'],
        meaning the first two p levels and the first d level of the orbital file -- adds a
        second geminal that correlates them, with the whole of each degenerate level tied
        together component by component in the Constraints block, because a correlating
        geminal built out of one component of a level is not spherically symmetric. Leave it
        unset when the source already has a parameters.casl; asking for both is refused. The
        channels are read off the orbital coefficients of a gaussian gwfn.data, so they need
        one; the Hartree-Fock geminal needs no orbital file and works for any basis.
    geminal_settings: seed (-0.05) and seed2 (-0.02), the first two correlating diagonals,
        which start away from zero because a geminal holding only its anchors is singular and
        has no gradient to move it (the Be 2s-2p near-degeneracy wants -0.19); mirror (0, set
        it to 1 for a third geminal with c = -1 tied to the second parameter for parameter);
        anchors (derived: every occupied orbital no correlated level contains -- pass [1] for
        Be, whose 2s pair the p block replaces rather than sits beside); purity (0.98, the
        share of an orbital's weight that must sit on one m-component before its level counts
        as rotationally closed and can be tied off-diagonally).
    jastrow_settings: the shape of both blocks, where the defaults are not wanted. Jastrow:
        trunc_order (3), n_u (8), n_chi (8), n_f_en (3), n_f_ee (3), spin_dep_u (1),
        spin_dep_chi (0), spin_dep_f (1), cusp_chi (0), cutoff_u / cutoff_chi / cutoff_f (0,
        which CASINO reads as "use your own default"), no_dup_u (0), no_dup_chi (0),
        optimizable (1, the cutoffs). Backflow: bf_trunc_order (3), n_eta (9), n_mu (9),
        n_phi_en (3), n_phi_ee (3), spin_dep_eta (1), spin_dep_mu (0), spin_dep_phi (1),
        cutoff_eta / cutoff_mu / cutoff_phi (0), irrotational (0), cusp_bf (-1, meaning derive
        it from the pseudopotentials).

    Nothing is written unless the result would actually run: the keyword combinations CASINO
    only rejects at run time are checked first (an optimisation sample smaller than the DMC
    target weight, `opt_backflow` without `backflow`, a missing mandatory keyword), and so is
    the presence of every file the input tells CASINO to read. A refusal names the problems and
    creates no directory. What is legal but probably unintended -- a `dtdmc` still at CASINO's
    placeholder default, `dmc_stats_nstep` not divisible by its block count, keywords left over
    from the runtype this was copied from -- comes back in `warnings` and does not stop it.
    """
    return runtime.prepare(
        source,
        dest,
        runtype=runtype,
        overrides=overrides,
        jastrow=jastrow,
        backflow=backflow,
        jastrow_settings=jastrow_settings,
        geminal=geminal,
        geminal_settings=geminal_settings,
    )


@server.tool()
def casino_status(job_id: str) -> JobState:
    """State of one job: running / finished / failed / stopped, pid, runtime in seconds, exit code.

    job_id: the id casino_run returned, or the calculation directory -- then it is the newest
        job that ran there. Every tool that takes a job takes either.
    """
    return runtime.status(job_id)  # type: ignore[return-value]


@server.tool()
def casino_wait(job_id: str, timeout: float = settings.WAIT_TIMEOUT) -> Waited:
    """Wait for a running calculation to end, and answer with the state it ended in.

    This is how a chain is driven without a polling loop: run, wait, read the results, prepare
    the next directory from this one. `waited` is how long it took and `timed_out` says the job
    is still going -- the wait is bounded because this server answers one call at a time, so
    waiting is the whole control plane standing still. A caller that wants longer calls again.

    A job that has already ended returns at once. Waiting is not stopping: nothing is signalled
    and the calculation is not touched.

    job_id: the id casino_run returned, or the calculation directory.
    timeout: seconds to block before answering that the job is still running.
    """
    return runtime.wait(job_id, timeout=timeout)  # type: ignore[return-value]


@server.tool()
def casino_results(job_id: str, fields: list[str] | None = None) -> Results:
    """Everything a job's files say, as data: energies, error bars, variance, acceptance ratio,
    correlation time, efficiency, the optimized DTVMC, CPU and real time, per-block numbers.

    Reads `out` and returns it as phases, because a CASINO run is a sequence of them and not
    one result: `vmc_opt` writes a VMC and an optimization phase per cycle, `vmc_dmc` writes
    VMC, DMC equilibration and DMC statistics accumulation. `result` points at the number that
    is this run's answer, and every value carries the file and line it was read from. Nothing
    is computed here that CASINO did not print, and a value it did not print comes back as null
    with the reason.

    What is in a phase depends on what kind it is, and the numbers a scan usually wants are
    there without grepping the file for them -- the output schema carries their units:

        vmc        acceptance (per cent), correlation_time (steps), efficiency (au^-2 s^-1),
                   dtvmc (the optimized step, not the one asked for), steps_per_process,
                   energy, variance, energy_errors, reblock, reblock_converged, nblock,
                   blocks[] with the same quantities and the time each took
        opt        method (varmin / emin), nparam, energy, variance, return_code, halted
        dmc_equil  acceptance, energy, variance, nblock, blocks[]
        dmc_stats  the same, plus mixed_estimators, target_weight, average_population,
                   effective_population, time_step, correlation_length, correlation_time,
                   std_dev_local_energy, steps, effective_steps, stat_inefficiency_est,
                   stat_inefficiency_measured, data_points

    Beside the phases: `keywords`, CASINO's own echo of the input -- which is not the `input`
    file, since it holds the defaults CASINO applied and not the keywords it never prints --
    `runtype`, `cpu_time` and `real_time` in seconds, `ended`, `complete`, `messages`, and
    `dmc_status` while a DMC run is still going. The run itself is named by `path`, `version`,
    `build`, `host`, `mpi_processes` and `started`.

    A DMC run that has not ended is readable too, and this is the only way to read one: CASINO
    writes the mixed estimators into `out` at the very end, and until then the current estimate
    lives in `dmc.status`, which it rewrites after every statistics block and deletes when the
    run finishes. When that file is there it is parsed into `dmc_status` and `result` points at
    it, so a running job answers with the estimate as of its last block -- and never with the
    VMC energy of the configuration-generation phase, which is the trial wave function's and not
    the calculation's. A run stopped by casino_stop keeps its `dmc.status`, so the last estimate
    it reached survives the stop.

    While the run is still equilibrating there is no DMC energy anywhere yet, and `result` then
    says so rather than answering with an earlier phase.

    job_id: the id casino_run returned, or the calculation directory.
    fields: answer with just these paths, as one flat `fields` map, instead of the whole run --
        a scan over 38 directories wanting six numbers from each does not want 16 kB of JSON
        per point. A path is written in the keys above, with four rules: `vmc`, `opt`,
        `dmc_equil`, `dmc_stats` mean the last phase of that kind; `opt[3]` and `vmc[2]` the
        cycle CASINO itself numbered; `phases[-1]` and `phases[0]` by position; anything else
        is a key of the run -- `keywords.DTVMC`, `cpu_time`, `result.energy`, `vmc.energy.error`,
        `vmc.blocks[0].time`, and `status` or `workdir` from the job's own state. A path that
        lands on a measured value collapses to the number. A path that does not exist is a
        mistake in the question and comes back in `problems` naming what is there instead; a
        path that exists but holds a number CASINO never printed comes back as null, with why
        in `reasons`. (`keywords.DTVMC` against `vmc.dtvmc` is the pair worth asking for
        together: the step the input asked for against the step the run actually used.)
    """
    return runtime.results(job_id, fields=fields)  # type: ignore[return-value]


@server.tool()
def casino_input(job_id: str) -> Input:
    """The `input` of a calculation: the keywords and blocks it was given, as data.

    What a run was *told* to do, which `casino_results` does not answer and is not meant to.
    The `keywords` in a result are CASINO's own echo of the input, and that echo is neither the
    file nor a superset of it: it holds every default CASINO applied -- 70 entries against the
    23 a file typically sets -- and silently drops what it does not print. `random_seed` is one
    of those, and it is the keyword the question "can this number be reproduced" turns on.

    A directory that has never been run is read too: that is how a prepared calculation is
    checked before there is a job to name it by.

    job_id: the id casino_run returned, or a calculation directory -- one that has run, or one
        casino_prepare has only just written.
    """
    return runtime.calculation_input(job_id)  # type: ignore[return-value]


@server.tool()
def casino_list_jobs(limit: int = 20, workdir: str = '') -> JobList:
    """Every known job, newest first, with its current state.

    workdir: only the jobs that ran in this directory, newest first. What a chain of runs did
        in one place, and the way to see that a directory has been run twice.
    """
    return runtime.listing(limit, workdir=workdir)  # type: ignore[return-value]


@server.tool()
def casino_stop(job_id: str, timeout: float = settings.STOP_TIMEOUT) -> dict[str, Any]:
    """Stop a running calculation and leave its directory ready to be continued.

    SIGTERM goes to this job's `casino` processes, as `haltqmc -k` does for the whole
    account, so `runqmc` stays alive to finish writing `out`. CASINO has no graceful-halt
    signal: the blocks it had finished stay in `out`, `vmc.hist` and `dmc.hist`, the
    current block is lost. Then `haltqmc -f -u` tidies the directory -- config.out to
    config.in, the lock file, and `input` rewritten for the work that is left -- so
    casino_run(workdir, resume=true) carries this calculation on.

    job_id: the id casino_run returned, or the calculation directory.
    timeout: seconds the job gets to end on its own before the process group is killed.
    """
    return runtime.stop(job_id, timeout=timeout)


def main() -> None:
    server.run(transport='stdio')


if __name__ == '__main__':
    main()
