"""Starting, watching and stopping CASINO runs. No MCP in this module.

`runqmc` is the runtime: it is a 3900-line bash script that already knows arch detection,
MPI variants, batch-queue submission, `--auto-continue` and the lock file. This layer goes
*over* it and adds only what it does not do -- a record of what was run, an exit code that
survives the caller, and a session of its own by which the run's processes can be found.

Every state change to a calculation goes through CASINO's own scripts, and only through
them: `runqmc` starts a run, `haltqmc` ends one and tidies the directory afterwards, and
either `runqmc --continue` or a plain `runqmc` over the `input` that `haltqmc -u` rewrote
carries it on. Nothing here moves a config file, edits an `input` or decides what a
half-finished calculation should do next -- CASINO has programs for all of that.

Every function returns a JSON-able dict. A refusal is `{'error': ...}` carrying the reason
and the fix, never a silently safer action than the one that was asked for.
"""

import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from casino_mcp import correlation_data, geminal, input_file, jobs, parse_out, settings

LOCK_NAME = '.runqmc.lock'
UPDATE_HELPER = 'haltqmc_update_input'  # the program `haltqmc -u` rewrites `input` with
CASINO_COMM = 'casino'  # what a CASINO process is called in /proc, and what `haltqmc -k` pkills
ESCALATION_GRACE = 5.0  # seconds each further signal gets once the polite one has been ignored

# Markers in `out`. `Started` opens every CASINO run, so it is what splits a file that
# several runs have been appended to; the other two say how the last of them ended.
SEGMENT_MARKER = 'Started '
CONTINUATION_MARKER = 'CONTINUATION INFO:'
FINISHED_MARKER = 'Total CASINO CPU time'

# What an earlier run left behind, and what `restart` therefore deletes. `runqmc` appends to
# `out` and to the `.hist` files rather than replacing them, so a re-run in a dirty directory
# produces a file that is two runs glued together -- the flag exists to prevent exactly that.
#
# The list is named, not "everything that is not an input": the same directory holds the wave
# function, the pseudopotentials and a `correlation.data` that is often hand-edited, and any
# glob wide enough to catch every product would catch those too. Where a bare name is an input
# and a numbered one is a product, only the numbered form is here: `parameters.4.casl` is what
# an optimisation cycle wrote, `parameters.casl` is what it started from.
DEBRIS = (
    'out',
    'out_part.[0-9]*',  # earlier segments, put aside by a previous --continue
    '.out_proc*',  # per-process output a killed run never got to concatenate into `out`
    'vmc.hist',
    'dmc.hist',
    '*.hist.[0-9]*',
    # Only an orderly end deletes this one, so a killed DMC run leaves it behind. It has to go
    # with the rest: `parse_out` reads it as the current estimate, and a stale one left next to
    # a fresh `out` would answer for a calculation that no longer exists.
    parse_out.STATUS_NAME,
    'config.in',  # continuation state: deleting it is what makes this a restart and not a resume
    'config.out',
    'config.in_*',  # _fixed / _nofixed, written by a fixed-node run
    'config.out_*',
    'config.out.[0-9]*',
    'correlation.out',
    'correlation.out.[0-9]*',
    'parameters.[0-9]*.casl',
    'saved_part_[0-9]*',  # a directory: what runqmc moved aside when it set up a continuation
)


def find_script(name: str, override: str) -> str | None:
    """The named CASINO script: its own variable, else one on PATH, else $CASINO_HOME/bin_qmc.

    An explicit override that is not executable is not silently replaced by another one:
    falling back to PATH there would run a different script than the one named.
    """
    if override:
        explicit = Path(override).expanduser()
        return str(explicit) if os.access(explicit, os.X_OK) else None
    found = shutil.which(name)
    if found is not None:
        return found
    fallback = settings.casino_home() / 'bin_qmc' / name
    if os.access(fallback, os.X_OK):
        return str(fallback)
    return None


def find_runqmc() -> str | None:
    """$CASINO_RUNQMC, else one on PATH, else the one under $CASINO_HOME."""
    return find_script('runqmc', settings.runqmc_override())


def find_haltqmc() -> str | None:
    """$CASINO_HALTQMC, else one on PATH, else the one under $CASINO_HOME."""
    return find_script('haltqmc', settings.haltqmc_override())


def git_tracked(path: Path) -> bool:
    """Whether git tracks this file -- i.e. whether it is committed reference data."""
    try:
        result = subprocess.run(
            ['git', '-C', str(path.parent), 'ls-files', '--error-unmatch', path.name],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    except OSError:
        return False
    return result.returncode == 0


def clear_debris(path: Path) -> list[str]:
    """Delete every product of an earlier run in `path` and name what went.

    Only the patterns in DEBRIS, so `input`, the wave function, the pseudopotentials,
    `correlation.data`, `parameters.casl` and the lock file all survive.
    """
    removed = []
    for pattern in DEBRIS:
        for victim in sorted(path.glob(pattern)):
            if victim.is_dir():
                shutil.rmtree(victim, ignore_errors=True)
            else:
                victim.unlink(missing_ok=True)
            removed.append(victim.name)
    return removed


def last_run(out: Path) -> list[str]:
    """The lines of the last CASINO run in `out`.

    Both continuation routes append rather than replace -- `runqmc --continue` puts the
    finished segment aside as `out_part.N` but a halted-and-continued run leaves several
    CASINO runs in one file -- and only the last of them says how the calculation stands.
    """
    lines = out.read_text(errors='replace').splitlines()
    starts = [i for i, line in enumerate(lines) if line.strip().startswith(SEGMENT_MARKER)]
    return lines[starts[-1] :] if starts else lines


def resume_mode(out: Path) -> str:
    """How the last run in `out` ended, and therefore how CASINO continues it.

    'continue': CASINO stopped itself on max_cpu_time / max_real_time and wrote the
        continuation info that `runqmc --continue` reads -- `Set NEWRUN : F`, which files to
        move -- and applies for us.
    'halted': the run was interrupted before it could write any of that. What continues it
        is the `input` that `haltqmc -u` rewrote when the job was stopped, run by a plain
        `runqmc`; `--continue` would only errstop on the missing continuation info.
    'complete': it ran to its own end. There is nothing to continue.
    """
    segment = last_run(out)
    if any(CONTINUATION_MARKER in line for line in segment):
        return 'continue'
    if any(line.strip().startswith(FINISHED_MARKER) for line in segment):
        return 'complete'
    return 'halted'


def set_to_continue(inp: Path) -> bool:
    """Whether `input` says NEWRUN : F, i.e. is set up to carry an earlier run on.

    That is what `haltqmc -u` leaves behind, and it is the one thing `restart` cannot undo:
    CASINO refuses NEWRUN : F without the `config.in` that restarting has just deleted.
    """
    for line in inp.read_text(errors='replace').splitlines():
        keyword, _, rest = line.partition(':')
        if keyword.strip().lower() == 'newrun':
            return rest.split('#')[0].strip().upper() in ('F', 'FALSE', '.FALSE.')
    return False


def check_workdir(path: Path, restart: bool, resume: bool, unlock: bool) -> str:
    """The reason this directory must not be run in, or '' if it may be.

    The guard is semantic, not syntactic: the risk is not a shell metacharacter, it is
    destroying an `out` file that other work is validated against.
    """
    if restart and resume:
        return 'restart and resume are opposites: restart deletes what the earlier run left, resume carries on from it. Pass one.'
    if not path.is_dir():
        return f'not a directory: {path}'
    if not (path / 'input').is_file():
        return f'no CASINO `input` file in {path}'

    forbidden = settings.forbidden(path)
    if forbidden:
        return f'{path} is under {forbidden}, which $CASINO_MCP_FORBID lists. No override exists; copy the directory elsewhere and run there.'

    if restart and set_to_continue(path / 'input'):
        return (
            f'{path / "input"} says NEWRUN : F -- it is set up to continue an earlier run, which is what '
            f'haltqmc leaves behind when a job is stopped. Restarting deletes the `config.in` that CASINO '
            f'then requires, and the run would fail. Put back the input this calculation started from '
            f'(casino_stop keeps a copy of it in the job directory), or pass resume=true to carry it on.'
        )

    lock = path / LOCK_NAME
    if lock.exists() and not unlock:
        return f'{lock} exists: another run holds this directory. Pass unlock=true if it is stale.'

    out = path / 'out'
    if resume and not out.is_file():
        return f'no `out` in {path}: resume continues an interrupted run from the continuation info its output ends with, and there is none here.'
    if out.exists() and not (restart or resume):
        if git_tracked(out):
            return f'{out} is committed reference data. Copy the directory and run there, or pass restart=true to delete it and start over.'
        return (
            f'{out} holds an earlier run, and runqmc appends to it rather than replacing it. '
            f'Run in a fresh directory, or pass restart=true to delete what that run left, or resume=true to carry it on.'
        )
    return ''


# What a calculation is *given*, as opposed to what a run leaves behind (DEBRIS). Named for
# the same reason DEBRIS is: the wave function, the pseudopotentials and a hand-edited
# `correlation.data` share a directory with the products, and no glob separates them.
#
# `config.in` is on the list because for a DMC-only or `opt` runtype it is an input like any
# other -- the population an earlier VMC wrote. `config.out` is not: it is where the run being
# copied from left off, and carrying it over would make the new calculation continue the old
# one by accident.
INPUTS = (
    'input',
    'gwfn.data',
    'stowfn.data',
    'pwfn.data',
    'bwfn.data',
    'bwfn.data.bin',
    'awfn.data',
    'dwfn.data',
    'correlation.data',
    'parameters.casl',
    '*_pp.data',
    'expot.data',
    'mpc.data',
    'config.in',
)


def copy_inputs(source: Path, dest: Path) -> list[str]:
    """Copy what the calculation reads, and nothing a run wrote. Symlinks are followed.

    An orbital file in an examples tree is usually a symlink to one shared by a dozen
    calculations; copying the link would leave it dangling one directory further away, so the
    content is copied instead and the new directory stands on its own.
    """
    copied = []
    for pattern in INPUTS:
        for path in sorted(source.glob(pattern)):
            if path.is_file():
                shutil.copy2(path, dest / path.name, follow_symlinks=True)
                copied.append(path.name)
    return copied


def carry_configurations(source: Path, dest: Path, keywords: dict) -> str:
    """Bring the source's `config.out` over as `config.in`, when the new runtype needs it.

    A `dmc_dmc` or `opt` run starts from a population an earlier run wrote, and where that
    population sits is `config.out` -- `config.in` is the name it has *while being read*, and a
    finished run leaves the other one. `runqmc` renames it in place for exactly this reason;
    doing the rename here would edit the calculation being copied from, so the copy is made
    under the name the new run will read, and the name change is reported in `copied`.

    Not done when the runtype does not ask for configurations: there, a stray `config.out`
    would silently make the new calculation continue the old one instead of starting.
    """
    runtype = keywords.get('runtype', '').strip()
    if runtype not in input_file.NEEDS_CONFIGS and input_file.truthy(keywords.get('newrun', 'T')):
        return ''
    if (dest / 'config.in').is_file() or not (source / 'config.out').is_file():
        return ''
    shutil.copy2(source / 'config.out', dest / 'config.in', follow_symlinks=True)
    return 'config.out -> config.in'


def blank_correlation(
    source: Path,
    dest: Path,
    keywords: dict,
    terms,
    backflow=(),
    overrides: dict | None = None,
) -> tuple[str, dict, list[str], list[str]]:
    """The `correlation.data` a calculation with no wave function parameters needs: text, what it
    says, what is wrong with asking for it, and what it does not say out loud.

    The geometry has to come from the orbital file -- `input` knows how many electrons there are
    and never how many nuclei -- so which file that is comes from `atom_basis_type`, the same
    table `check_files` refuses a run over.

    Both blocks go in one file, because that is where CASINO looks for both, and each is written
    only if its own keyword in the `input` is on: `use_jastrow` for the Jastrow factor,
    `backflow` for the backflow function. Writing a block the run would not read, or turning on
    a keyword whose block is missing, are the same mistake from opposite ends.
    """
    basis = keywords.get('atom_basis_type', '').strip().lower()
    orbitals = input_file.ORBITAL_FILE.get(basis)
    if orbitals is None:
        return '', {}, [f'atom_basis_type {basis or "(unset)"} has no orbital file to read the atoms out of, and a chi or mu term needs them'], []
    if basis in correlation_data.UNREADABLE_GEOMETRY:
        return (
            '',
            {},
            [
                f'atom_basis_type {basis} keeps its geometry in {correlation_data.UNREADABLE_GEOMETRY[basis]}, '
                f'whose layout this does not read: it is written by a periodic code, and a periodic Jastrow wants a P term this does not write'
            ],
            [],
        )
    try:
        settings_ = correlation_data.settings_for(overrides)
        geometry = correlation_data.read_geometry(source / orbitals)
    except KeyError as problem:
        return '', {}, [problem.args[0]], []
    except (OSError, ValueError, UnicodeDecodeError) as problem:
        return '', {}, [str(problem)], []

    pseudo = correlation_data.pseudo_species(source)
    errors = correlation_data.check(geometry, terms, settings_, pseudo=pseudo, basis=basis, backflow=backflow)
    errors.extend(check_wanted(keywords, terms, backflow))
    if errors:
        return '', {}, errors, []

    text = correlation_data.blank(geometry, terms=terms, backflow=backflow, title=dest.name, settings=settings_, pseudo=pseudo)
    description = {
        'terms': list(terms),
        'backflow': list(backflow),
        'atoms': len(geometry['atomic_numbers']),
        'sets': [{'atomic_number': group['z'], 'atoms': group['labels']} for group in geometry['sets']],
        'pseudo': sorted(pseudo & set(geometry['atomic_numbers'])),
    }
    return text, description, [], correlation_data.describe(geometry, terms, settings_, pseudo=pseudo, basis=basis, backflow=backflow)


def blank_geminal(
    source: Path,
    dest: Path,
    keywords: dict,
    channels,
    overrides: dict | None = None,
) -> tuple[str, dict, list[str], list[str]]:
    """The `parameters.casl` a `psi_s : geminal` calculation needs: text, what it says, what is
    wrong with asking for it, and what it does not say out loud.

    The same shape as `blank_correlation` and for the same reason -- CASINO reads this file and
    no CASINO utility writes one -- but the two differ in what they can leave out. A Jastrow
    factor starts from zeros; a geminal cannot, because a pairing matrix with an empty diagonal
    is singular, so this has to know which orbitals the reference determinant fills (`neu` and
    `ned`, out of the input) and, when a correlating geminal is asked for, which orbitals form
    a degenerate level (out of the orbital file).

    With no channels there is no orbital file to read: Geminal 1 alone is the Hartree-Fock
    determinant written as a geminal, it needs nothing but the electron counts, and it is the
    same for a gaussian, Slater-type or numerical basis.
    """
    try:
        settings_ = geminal.settings_for(overrides)
    except KeyError as problem:
        return '', {}, [problem.args[0]], []
    wanted, errors = geminal.parse_channels(channels)

    orbitals, levels, notes = None, {}, []
    if wanted:
        basis = keywords.get('atom_basis_type', '').strip().lower()
        if basis != geminal.READABLE_BASIS:
            errors.append(
                f'the levels a channel names are read off the orbital coefficients, and this reads them out of a '
                f'{geminal.READABLE_BASIS} gwfn.data; atom_basis_type is {basis or "(unset)"}. Ask for no channels to write the '
                f'Hartree-Fock geminal, which needs no orbital file.'
            )
        else:
            try:
                orbitals = geminal.read_orbitals(source / input_file.ORBITAL_FILE[basis])
            except (OSError, ValueError, UnicodeDecodeError, KeyError) as problem:
                errors.append(str(problem))
    if errors:
        return '', {}, errors, []

    shells, diag_shells = [], []
    if orbitals is not None:
        levels = geminal.mo_levels(orbitals, purity=settings_['purity'])
        shells, diag_shells, problems, notes = geminal.select(levels, wanted)
        errors.extend(problems)
        notes.extend(geminal.spin_check(orbitals, settings_['purity'], wanted))

    neu, ned = int(input_file.number(keywords.get('neu')) or 0), int(input_file.number(keywords.get('ned')) or 0)
    occupied, unpaired, anchors = geminal.occupation(neu, ned, shells, diag_shells, settings_)
    errors.extend(geminal.check(keywords, wanted, orbitals, occupied, unpaired, anchors, settings_))
    if errors:
        return '', {}, errors, []

    text = geminal.casl(
        geminal.geminal_section(occupied, unpaired, anchors, shells, diag_shells, settings_),
        provenance=f'{dest.name}: written by casino-mcp, {len(shells) + len(diag_shells)} correlated level(s)',
    )
    description = {
        'channels': [f'{geminal.channel_name(l)}:{count}' for l, count in wanted],
        'geminals': (2 if shells or diag_shells else 1) + (1 if settings_['mirror'] and (shells or diag_shells) else 0),
        'occupied': occupied,
        'unpaired': unpaired,
        'anchors': anchors,
        'shells': shells,
        'diagonal_shells': diag_shells,
        'levels': {geminal.channel_name(l): len(found) for l, found in sorted(levels.items())},
        'orbitals': orbitals['norb'] if orbitals else None,
    }
    return text, description, [], notes + geminal.describe(keywords, orbitals, occupied, unpaired, anchors, shells, diag_shells, settings_)


def check_wanted(keywords: dict, terms, backflow) -> list[str]:
    """Whether the input asks for the blocks being written, and asks for none that are not.

    A block CASINO is told to use and cannot find is an errstop; a block it is not told to use is
    dead text in the file. Both are worth catching before the directory exists, and the second is
    the more insidious -- an optimisation that silently has nothing to optimize.
    """
    errors = []
    for names, keyword, block in ((terms, 'use_jastrow', 'JASTROW'), (backflow, 'backflow', 'BACKFLOW')):
        wanted = input_file.truthy(keywords.get(keyword))
        if names and not wanted:
            errors.append(f'the input this would write does not set {keyword} : T, so CASINO would not read the {block} block at all')
        if wanted and not names:
            errors.append(
                f'{keyword} is T and no {block} block was asked for: CASINO errstops on the keyword without the block. Name the terms it should have'
            )
    return errors


def prepare(
    source: str,
    dest: str,
    runtype: str = '',
    overrides: dict[str, str | None] | None = None,
    jastrow: list[str] | None = None,
    backflow: list[str] | None = None,
    jastrow_settings: dict | None = None,
    geminal: list[str] | None = None,  # NB: shadows the module of that name for the body of this function
    geminal_settings: dict | None = None,
) -> dict[str, Any]:
    """Copy a calculation into a new directory and write the `input` the next run needs.

    This is the "change a parameter, get a new directory" step, and it is a copy rather than an
    edit on purpose: a result whose input was overwritten in place cannot be reproduced, and
    every guardrail in `start` exists because a directory that already holds a run is not a
    place to put another one.

    `runtype` fills in the keywords that runtype requires and the source input does not have --
    switching a `vmc` calculation to `vmc_dmc` is one keyword in the file and eight more that
    CASINO then demands. What the source already says is kept, so the electron count, the basis
    and any hand tuning survive; `overrides` wins over both. A value of null deletes a keyword,
    and a value with newlines in it is written as a `%block`.

    `jastrow` and `backflow` name the terms of a blank `correlation.data` to write --
    `['u', 'chi', 'f']` and `['eta', 'mu', 'phi']` for the usual ones -- for the case the source
    has none at all, which is every calculation that has just come out of an orbital code. Both
    blocks go in the one file, each written only if the `input` turns its keyword on. Every
    coefficient starts at zero, because starting them anywhere else is what the optimisation is
    for. `jastrow_settings` holds the shape of both: expansion orders, spin dependence, cutoffs,
    the truncation orders.

    `geminal` does the same for the GEMINAL block of a `parameters.casl`, which is what
    `psi_s : geminal` reads: an empty list writes the Hartree-Fock geminal alone, and a list of
    channels (`['p:2', 'd:1']`) adds a correlating geminal over the levels they name. It is a
    list and not a flag because the two are the same file with one geminal or three in it;
    `None` writes no file at all. `geminal_settings` holds the rest of the decisions -- the
    seeds, the anchors, the mirror geminal.

    Nothing is written unless the result would run: the keywords are checked for the
    combinations CASINO only rejects at run time, and the directory for the files the input
    tells it to read.
    """
    source_path, dest_path = Path(source).expanduser().resolve(), Path(dest).expanduser().resolve()
    if not (source_path / 'input').is_file():
        return {'error': f'no CASINO `input` in {source_path}: there is nothing to copy from'}
    if source_path == dest_path:
        return {'error': 'source and dest are the same directory: preparing a run never edits the calculation it came from'}
    if dest_path.exists() and any(dest_path.iterdir()):
        return {'error': f'{dest_path} already exists and is not empty. One directory is one calculation; name a new one.'}
    forbidden = settings.forbidden(dest_path)
    if forbidden:
        return {'error': f'{dest_path} is under {forbidden}, which $CASINO_MCP_FORBID lists. No override exists; prepare the run elsewhere.'}
    if (jastrow or backflow) and (source_path / 'correlation.data').is_file():
        return {
            'error': f'{source_path} already has a correlation.data, and it is an input: it would be copied over. '
            f'A blank one would throw away whatever it holds.',
            'fix': 'prepare from a directory that has no correlation.data, or leave jastrow and backflow unset to keep the one there is',
        }
    if geminal is not None and (source_path / 'parameters.casl').is_file():
        return {
            'error': f'{source_path} already has a parameters.casl, and it is an input: it would be copied over. '
            f'A freshly written GEMINAL block would throw away whatever that one holds, optimized parameters included.',
            'fix': 'prepare from a directory that has no parameters.casl, or leave geminal unset to keep the one there is',
        }

    current = input_file.read(source_path / 'input')
    values = {name.lower(): value for name, value in (overrides or {}).items()}
    if runtype:
        if runtype not in input_file.RECIPES:
            return {'error': f'no recipe for runtype {runtype}. Known: {", ".join(sorted(input_file.RECIPES))}'}
        # Blocks count as present too: an `opt_plan` in the file is what `opt_cycles` would
        # otherwise be, and adding both would leave CASINO to resolve a contradiction silently.
        present = {**current['keywords'], **dict.fromkeys(current['blocks'], '')}
        filled, missing = input_file.recipe(runtype, values, present=present)
        if missing:
            return {
                'error': f'runtype {runtype} needs {", ".join(missing)}, which {source_path / "input"} does not set and no default can supply',
                'fix': 'pass them in overrides',
            }
        values = filled
    text = input_file.apply(current['text'], values) if values else current['text']
    keywords, blocks = input_file.parse_text(text)

    correlation_text, described, correlation_errors, correlation_notes = '', {}, [], []
    if jastrow or backflow:
        correlation_text, described, correlation_errors, correlation_notes = blank_correlation(
            source_path, dest_path, keywords, jastrow or (), backflow or (), jastrow_settings
        )

    casl_text, described_geminal, geminal_errors, geminal_notes = '', {}, [], []
    if geminal is not None:
        casl_text, described_geminal, geminal_errors, geminal_notes = blank_geminal(source_path, dest_path, keywords, geminal, geminal_settings)

    writing = (('correlation.data',) if (jastrow or backflow) else ()) + (('parameters.casl',) if geminal is not None else ())
    errors = input_file.check(keywords, blocks) + input_file.check_files(source_path, keywords, writing=writing) + correlation_errors + geminal_errors
    if errors:
        return {'error': 'the input this would write does not describe a run CASINO can do', 'problems': errors}

    dest_path.mkdir(parents=True, exist_ok=True)
    copied = copy_inputs(source_path, dest_path)
    carried = carry_configurations(source_path, dest_path, keywords)
    if carried:
        copied.append(carried)
    (dest_path / 'input').write_text(text)
    if correlation_text:
        (dest_path / 'correlation.data').write_text(correlation_text)
        copied.append('correlation.data (written blank)')
    if casl_text:
        (dest_path / 'parameters.casl').write_text(casl_text)
        copied.append('parameters.casl (written)')
    changed = {name: value for name, value in values.items() if current['keywords'].get(name, object()) != value}
    return {
        'workdir': str(dest_path),
        'source': str(source_path),
        'runtype': keywords.get('runtype', '').strip(),
        'copied': copied,
        'changed': changed,
        'correlation_data': described or None,
        'geminal': described_geminal or None,
        'warnings': input_file.advise(keywords, blocks) + input_file.advise_files(dest_path, keywords) + correlation_notes + geminal_notes,
    }


def build_command(runqmc: str, nproc: int, version: str, unlock: bool, resume: bool = False, extra: tuple[str, ...] = ()) -> list[str]:
    command = [runqmc, '-p', str(nproc)]
    if version != settings.VERSION:
        command.append(f'--version={version}')
    if unlock:
        command.append('--unlock')
    if resume:
        # runqmc reads the CONTINUATION INFO block at the end of `out`, moves the finished
        # segment into saved_part_N/ and edits `input` itself. None of that is ours to do.
        command.append('--continue')
    return command + list(extra)


def start(
    workdir: str,
    nproc: int = settings.NPROC,
    version: str = settings.VERSION,
    restart: bool = False,
    resume: bool = False,
    unlock: bool = False,
    store: jobs.JobStore | None = None,
) -> dict[str, Any]:
    """Spawn a launcher for one runqmc run and return its job record. Does not wait."""
    store = store or jobs.JobStore()
    if nproc < 1:
        return {'error': f'nproc must be at least 1, got {nproc}'}

    path = Path(workdir).expanduser().resolve()
    refusal = check_workdir(path, restart, resume, unlock)
    if refusal:
        return {'error': refusal}

    runqmc = find_runqmc()
    if runqmc is None:
        return {'error': f'runqmc not found: not in $CASINO_RUNQMC, not on PATH, not in {settings.casino_home() / "bin_qmc"}'}

    # Which of CASINO's two continuation routes this directory is in is not ours to choose:
    # `out` already says, and asking for the wrong one is an errstop from runqmc.
    mode = resume_mode(path / 'out') if resume else ''
    if mode == 'complete':
        return {
            'error': (
                f'the last run in {path / "out"} reached its own end, so there is nothing to continue. '
                f'Pass restart=true to run it again from the beginning, or run in a fresh directory.'
            )
        }

    # After the last thing that can refuse, never before: a directory is not emptied for a run
    # that then fails to start.
    removed = clear_debris(path) if restart else []
    command = build_command(runqmc, nproc, version, unlock, resume=mode == 'continue')
    job_id, job_dir, meta = jobs.create(command, path, nproc, version)

    process = subprocess.Popen(
        [sys.executable, '-m', 'casino_mcp.launcher', str(job_dir)],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
        env=os.environ.copy(),
    )
    meta = jobs.record_pid(job_dir, meta, process.pid)
    store.add(meta)

    started = {
        'job_id': job_id,
        'pid': process.pid,
        'workdir': str(path),
        'command': command,  # the argv, as the job record and casino_status also give it
        'started': meta['created'],
        'binary': meta['binary'],
        'removed': removed,  # named, so a restart that ate more than expected is visible in the reply
    }
    if resume:
        started['resume'] = mode
        if mode == 'halted':
            started['note'] = (
                'no continuation info in `out`: this run was interrupted rather than stopped by a time limit, '
                'so it continues from the `input` that `haltqmc -u` rewrote, not with `runqmc --continue`. '
                'If the job was not stopped through casino_stop, run haltqmc -f -u there first.'
            )
    return started


class UnknownJob(Exception):
    """Neither a job id the registry knows nor a directory anything has run in."""


def find(job_id: str, store: jobs.JobStore) -> dict[str, Any]:
    """The state of a job named either by its id or by the directory it ran in.

    A campaign holds directories: the scan's own loop variable is the directory it prepared,
    and the id of the run in it is a string it never saw. Rather than have every caller pair
    the two up through the listing by eye, the registry does it -- the newest job of that
    directory, which is the run that produced what is in it now.
    """
    try:
        return store.status(job_id)
    except KeyError:
        pass
    path = Path(job_id).expanduser()
    if path.is_dir():
        latest = store.latest(path)
        if latest is None:
            raise UnknownJob(f'no job has run in {path.resolve()}: prepared directories have no job until casino_run starts one')
        return store.status(latest)
    raise UnknownJob(f'unknown job {job_id}: not a job id in the registry, and not a directory')


def status(job_id: str, store: jobs.JobStore | None = None) -> dict[str, Any]:
    store = store or jobs.JobStore()
    try:
        return find(job_id, store)
    except UnknownJob as e:
        return {'error': str(e)}


def results(job_id: str, fields=None, store: jobs.JobStore | None = None) -> dict[str, Any]:
    """What one job's files say: the parsed `out`, and the live estimate beside it.

    A job, not a file: the state of the run is what tells the caller whether these numbers are
    final, and a job knows where its directory is after the cwd that started it is gone. The
    physics all comes from `parse_out`, which reads `out` and, if the run is a DMC one that has
    not ended, the `dmc.status` next to it -- so a running calculation answers with the estimate
    as of its last block instead of with nothing.

    `fields` projects the report down to the paths asked for (`parse_out.select`), which is what
    a scan wants: six numbers a point rather than the 16 kB the whole of one is. The projection
    runs over the job's state as well as the physics, so `status` and `workdir` are paths too.
    """
    store = store or jobs.JobStore()
    try:
        state = find(job_id, store)
    except UnknownJob as e:
        return {'error': str(e)}
    job_id = state['job_id']

    workdir = Path(state['workdir'])
    out = workdir / 'out'
    if not out.is_file():
        return {
            'error': f'no `out` in {workdir}',
            'job_id': job_id,
            'status': state['status'],
            'note': (
                'runqmc writes `out` once CASINO starts, so a job that has only just been launched, or one that '
                'failed in runqmc itself, has none. Its own log is in the job directory.'
            ),
        }
    try:
        parsed = parse_out.parse_out(out)
    except OSError as e:
        return {'error': f'cannot read {out}: {e}', 'job_id': job_id, 'status': state['status']}

    report = {key: state[key] for key in ('job_id', 'status', 'workdir', 'nproc', 'started', 'runtime') if key in state}
    for key in ('exit_code', 'finished'):
        if key in state:
            report[key] = state[key]
    report.update(parsed)
    if state['status'] == 'running' and parsed['complete']:
        # The launcher is still up while runqmc finishes its epilogue; the physics is done.
        report['note'] = (
            'CASINO has written its timing report, so these numbers are final; the job is still marked running because runqmc has not exited yet'
        )
    if fields:
        return project(report, fields)
    return report


def project(report: dict[str, Any], fields) -> dict[str, Any]:
    """The report cut down to the paths asked for, or the paths that do not exist.

    A path that is not in the run is a mistake in the question, and answering the rest of it as
    if nothing were wrong is how a scan ends up with a column of nulls it believes.
    """
    values, reasons, problems = parse_out.select(report, fields)
    answer = {key: report[key] for key in ('job_id', 'status', 'workdir', 'path', 'complete') if key in report}
    if problems:
        answer['error'] = f'{len(problems)} of {len(fields)} fields are not in this run'
        answer['problems'] = problems
        return answer
    answer['fields'] = values
    if reasons:
        answer['reasons'] = reasons
    return answer


def calculation_input(job_id: str, store: jobs.JobStore | None = None) -> dict[str, Any]:
    """The `input` of a calculation: what it was told to do, not what it did.

    A reading of its own and not a corner of `results`, because the two answer different
    questions. `results` carries CASINO's own echo of the keywords, which is neither the file
    nor a superset of it: it holds the defaults CASINO applied -- 70 entries where the file has
    23 -- and drops the keywords it does not print, of which `random_seed` is the one that
    decides whether a run can be reproduced at all.

    A directory nothing has ever run in is read too, which is half the point of taking one: it
    is how a prepared calculation is checked before there is a job to name it by.
    """
    store = store or jobs.JobStore()
    state = None
    try:
        state = find(job_id, store)
    except UnknownJob as e:
        if not Path(job_id).expanduser().is_dir():
            return {'error': str(e)}
    workdir = Path(state['workdir']) if state else Path(job_id).expanduser()
    path = workdir / 'input'
    if not path.is_file():
        return {'error': f'no CASINO `input` in {workdir.resolve()}', 'workdir': str(workdir.resolve())}
    try:
        parsed = input_file.read(path)
    except OSError as e:
        return {'error': f'cannot read {path}: {e}'}

    answer: dict[str, Any] = {'workdir': str(workdir.resolve()), 'path': parsed['path'], 'runtype': parsed['runtype']}
    if state:
        answer['job_id'] = state['job_id']
        answer['status'] = state['status']
    answer['keywords'] = parsed['keywords']
    answer['blocks'] = parsed['blocks']
    if state:
        # What the run was started from, when haltqmc has since rewritten the file in place.
        # Without it "what was this asked to do" has no answer for a job that was stopped and
        # continued: haltqmc's rewrite is what makes the continuation work, and it is lossy.
        before = jobs.jobs_dir() / state['job_id'] / 'input.before_halt'
        if before.is_file():
            saved = input_file.read(before)
            answer['before_halt'] = {'path': saved['path'], 'keywords': saved['keywords'], 'blocks': saved['blocks']}
            answer['note'] = 'casino_stop let haltqmc rewrite `input`; `before_halt` is the file this job was started from'
    return answer


def listing(limit: int = 20, workdir: str = '', store: jobs.JobStore | None = None) -> dict[str, Any]:
    store = store or jobs.JobStore()
    found = store.all_status()
    if workdir:
        wanted = Path(workdir).expanduser().resolve()
        found = [state for state in found if Path(state['workdir']).resolve() == wanted]
    return {'jobs': found[:limit]}


def casino_processes(session: int) -> list[int]:
    """The `casino` processes belonging to one job, by session id.

    This is `haltqmc -k`'s own target -- it runs `pkill -x -u $USER casino` -- narrowed from
    every CASINO process the user owns to the ones this job started, because a server that
    runs several calculations at once must not have one stop take the others down with it.

    The session, not the process group: `mpirun` puts every rank in a process group of its
    own, so the group that `killpg` reaches holds runqmc and mpirun but not one `casino`.
    The session is the launcher's, and it is what the whole tree shares.
    """
    found = []
    for entry in Path('/proc').iterdir():
        if not entry.name.isdigit():
            continue
        try:
            stat = (entry / 'stat').read_text()
        except OSError:
            continue  # the process ended between the listing and the read
        comm = stat[stat.index('(') + 1 : stat.rindex(')')]
        fields = stat[stat.rindex(')') + 2 :].split()  # state, ppid, pgrp, session, ...
        if comm == CASINO_COMM and int(fields[3]) == session:
            found.append(int(entry.name))
    return sorted(found)


def clear_lock(path: Path) -> bool:
    """Remove a lock file haltqmc did not get to, and say whether there was one."""
    lock = path / LOCK_NAME
    if not lock.exists():
        return False
    lock.unlink()
    return True


def tail(text: str, lines: int = 12) -> str:
    return '\n'.join(text.strip().splitlines()[-lines:])


def halt(path: Path, keep_input: Path | None = None, timeout: float = settings.HALT_TIMEOUT) -> dict[str, Any]:
    """Hand the directory to CASINO's own haltqmc once the job has ended, and report what it did.

    `-f` because the killed run's `.runqmc.lock` is still there, `-u` because rewriting
    `input` -- NEWRUN to F, the blocks already done subtracted, the runtype moved on -- is
    what makes the next `runqmc` continue this calculation instead of redoing it. Not `-k`:
    the job is already dead by the time this runs, and haltqmc's kill is a `pkill casino`
    over the whole account. Not `-r` either: reblocking is a results question, and haltqmc
    errstops on a run with no statistics to reblock.

    haltqmc looks `haltqmc_update_input` up on PATH, so its own directory goes on there --
    a CASINO installation that is not on PATH still has the helper next to the script.

    `keep_input` is where the `input` haltqmc is about to rewrite is copied first. It belongs
    in the job directory and never in the calculation directory: keeping the file the run was
    started from is provenance, which is this layer's business, while what the calculation
    directory holds stays CASINO's.
    """
    haltqmc = find_haltqmc()
    if haltqmc is None:
        return {
            'error': f'haltqmc not found: not in $CASINO_HALTQMC, not on PATH, not in {settings.casino_home() / "bin_qmc"}',
            'note': 'the directory was left as the killed run left it; run haltqmc -f -u there by hand before continuing it',
            'lock_cleared': clear_lock(path),
        }

    env = os.environ.copy()
    env['PATH'] = str(Path(haltqmc).resolve().parent) + os.pathsep + env.get('PATH', '')
    helper = shutil.which(UPDATE_HELPER, path=env['PATH'])
    command = [haltqmc, '-f'] + (['-u'] if helper else [])
    saved = None
    if helper and keep_input is not None and (path / 'input').is_file():
        shutil.copy2(path / 'input', keep_input)
        saved = str(keep_input)
    try:
        result = subprocess.run(command, cwd=path, env=env, capture_output=True, text=True, timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired) as e:
        return {'command': command, 'error': f'haltqmc did not run: {e}', 'lock_cleared': clear_lock(path)}

    report = {
        'command': command,
        'exit_code': result.returncode,
        'updated_input': helper is not None and result.returncode == 0,
        'input_saved': saved,  # the input as it was before haltqmc rewrote it, or None
        'output': tail(result.stdout + result.stderr),
    }
    if helper is None:
        report['note'] = (
            f'{UPDATE_HELPER} is not on PATH and not beside haltqmc, so `input` was not updated: this run can be restarted but not continued.'
        )
    elif result.returncode != 0:
        report['error'] = 'haltqmc exited nonzero: the directory may not have been tidied and `input` may not have been updated'
    report['lock_cleared'] = clear_lock(path)  # only if haltqmc left one, which it does not when it succeeds
    return report


def wait_while_running(job_id: str, store: jobs.JobStore, deadline: float) -> dict[str, Any]:
    while time.time() < deadline:
        state = store.status(job_id)
        if state['status'] != 'running':
            return state
        time.sleep(0.5)
    return store.status(job_id)


def wait(job_id: str, timeout: float = settings.WAIT_TIMEOUT, store: jobs.JobStore | None = None) -> dict[str, Any]:
    """Block until this job is no longer running, and answer with what it became.

    The alternative a caller is left with otherwise is a polling loop of its own -- `while pgrep
    -x casino; do sleep 5; done`, or a status call every few seconds, each one a round trip
    through the model. The wait ends when the job does, or when `timeout` runs out, and
    `timed_out` says which happened.
    """
    store = store or jobs.JobStore()
    try:
        state = find(job_id, store)
    except UnknownJob as e:
        return {'error': str(e)}
    started = time.time()
    if state['status'] == 'running':
        state = wait_while_running(state['job_id'], store, started + timeout)
    state['waited'] = round(time.time() - started, 1)
    state['timed_out'] = state['status'] == 'running'
    if state['timed_out']:
        state['note'] = f'still running after {timeout:.0f} s; call again to keep waiting'
    return state


def stop(job_id: str, timeout: float = settings.STOP_TIMEOUT, store: jobs.JobStore | None = None) -> dict[str, Any]:
    """Stop a running calculation the way CASINO stops one, then let haltqmc tidy up.

    SIGTERM goes to this job's `casino` processes and to nothing else -- the same signal
    `haltqmc -k` sends -- which leaves `runqmc` alive to finish its own epilogue: the
    per-node output concatenated into `out`, its lock file removed. Only if the job is still
    running after `timeout` is the whole process group signalled, and then killed.

    Once the job has ended, `haltqmc -f -u` gets the directory: config.out to config.in, the
    marker files, and `input` rewritten so that a later casino_run(resume=true) continues
    this calculation rather than starting it again.
    """
    store = store or jobs.JobStore()
    try:
        state = find(job_id, store)
    except UnknownJob as e:
        return {'error': str(e)}
    job_id = state['job_id']
    if state['status'] != 'running':
        return {'job_id': job_id, 'status': state['status'], 'note': 'not running, nothing to stop'}

    pid = state['pid']
    targets = casino_processes(pid)  # the launcher leads its own session, so its pid is the sid
    for target in targets:
        try:
            os.kill(target, signal.SIGTERM)
        except OSError:
            pass  # it ended on its own between the listing and the signal
    if not targets:
        # Nothing named `casino` in the session: the run is still in runqmc's setup -- arch
        # detection, the lock, copying files -- or it is already over. Either way the process
        # group is what there is to signal.
        try:
            os.killpg(pid, signal.SIGTERM)
        except OSError as e:
            return {'error': f'could not signal job {job_id} (pid {pid}): {e}'}

    state = wait_while_running(job_id, store, time.time() + timeout)
    for escalation in (signal.SIGTERM, signal.SIGKILL):
        if state['status'] != 'running':
            break
        try:
            os.killpg(pid, escalation)
        except OSError:
            pass
        state = wait_while_running(job_id, store, time.time() + ESCALATION_GRACE)

    store.mark_stopped(job_id)
    state = store.status(job_id)
    state['terminated'] = {'scope': 'casino' if targets else 'process group', 'pids': targets or [pid]}
    state['halt'] = halt(Path(state['workdir']), keep_input=jobs.jobs_dir() / job_id / 'input.before_halt')
    return state
