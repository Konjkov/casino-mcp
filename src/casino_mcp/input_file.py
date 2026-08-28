"""Reading, checking and writing a CASINO `input` file.

Text in, text out: no MCP, no dependencies, and no keyword invented that the caller did not
ask for. The format is ESDF -- `keyword : value`, `#` starts a comment, `%block name` /
`%endblock name` for the multi-line ones, order irrelevant, names case-insensitive.

What this module knows that a text editor does not is which keywords a *runtype* needs. That
is the whole difficulty of writing an input by hand: `vmc` and `vmc_dmc` are one keyword
apart in the file and a dozen apart in what CASINO then demands, and the failure mode is an
errstop after the queue has already given you the node. So `RECIPES` holds, per runtype, the
keywords CASINO requires and a working default for each, and `check` holds the combinations
that are legal one keyword at a time and wrong together -- an optimisation sample smaller than
the DMC population, `opt_backflow` without `backflow`, a basis type whose orbital file is not
in the directory.

Three operations, and they compose:

    read(path)                  -> the keywords and blocks in a file
    apply(text, overrides)      -> the same file with some keywords changed, added or removed
    build(values)               -> a whole file, in the layout CASINO's own examples use

`apply` edits rather than regenerates: a real calculation directory's `input` carries hand
comments, a `%block npcell`, keywords no recipe knows about, and rewriting it from a template
would silently drop all of it. Only the lines named in `overrides` are touched.
"""

import re
from pathlib import Path

REQUIRED = None  # a recipe entry with no default: CASINO needs it and only the caller knows it

HEADER = '#-------------------#\n# CASINO input file #\n#-------------------#\n'

# `keyword : value  #*! description`, as CASINO's own examples are laid out. Everything after
# the first `:` up to a `#` is the value; ESDF itself allows `=` too, but CASINO writes `:`.
ENTRY = re.compile(r'^\s*([A-Za-z][A-Za-z_0-9]*)\s*[:=]\s*([^#]*?)\s*(#.*)?$')
BLOCK_START = re.compile(r'^\s*%block\s+([A-Za-z][A-Za-z_0-9]*)', re.IGNORECASE)
BLOCK_END = re.compile(r'^\s*%endblock\s+([A-Za-z][A-Za-z_0-9]*)', re.IGNORECASE)

# Section name -> the keywords that belong under it, in the order CASINO's examples print them.
# A keyword not listed here still round-trips through `apply`; it is only the layout of a file
# this module writes itself that the table decides.
SECTIONS = (
    ('SYSTEM', ('neu', 'ned', 'periodic', 'atom_basis_type', 'psi_s', 'complex_wf')),
    ('RUN', ('runtype', 'newrun', 'testrun')),
    ('VMC', ('vmc_method', 'vmc_equil_nstep', 'vmc_nstep', 'vmc_nblock', 'vmc_nconfig_write', 'vmc_decorr_period', 'dtvmc', 'opt_dtvmc')),
    ('OPTIMIZATION', ('opt_method', 'opt_cycles', 'opt_jastrow', 'opt_backflow', 'opt_det_coeff', 'opt_geminal', 'opt_fixnl', 'opt_plan')),
    (
        'DMC',
        (
            'dmc_method',
            'dmc_equil_nstep',
            'dmc_equil_nblock',
            'dmc_stats_nstep',
            'dmc_stats_nblock',
            'dmc_target_weight',
            'dtdmc',
            'use_tmove',
            'popstats',
            'dmc_trip_weight',
        ),
    ),
    ('GENERAL PARAMETERS', ('use_jastrow', 'backflow', 'random_seed')),
)

DESCRIPTIONS = {
    'neu': 'Number of up electrons (Integer)',
    'ned': 'Number of down electrons (Integer)',
    'periodic': 'Periodic boundary conditions (Boolean)',
    'atom_basis_type': 'Basis set type (Text)',
    'psi_s': 'Type of [anti]symmetrizing wfn (Text)',
    'complex_wf': 'Wave function real or complex (Boolean)',
    'runtype': 'Type of calculation (Text)',
    'newrun': 'New run or continue old (Boolean)',
    'testrun': 'Test run flag (Boolean)',
    'vmc_method': 'VMC algorithm (1 - EBES, 3 - CBCS)',
    'vmc_equil_nstep': 'Number of equilibration steps (Integer)',
    'vmc_nstep': 'Number of steps, over all processes (Integer)',
    'vmc_nblock': 'Number of checkpoints (Integer)',
    'vmc_nconfig_write': 'Number of configs to write (Integer)',
    'vmc_decorr_period': 'VMC decorrelation period (0 - auto)',
    'dtvmc': 'VMC time step (Real)',
    'opt_dtvmc': 'VMC time-step optimization (0-2)',
    'opt_method': 'Opt method (varmin/madmin/emin/...)',
    'opt_cycles': 'Number of optimization cycles (Integer)',
    'opt_jastrow': 'Optimize Jastrow factor (Boolean)',
    'opt_backflow': 'Optimize backflow parameters (Boolean)',
    'opt_det_coeff': 'Optimize determinant coefficients (Boolean)',
    'opt_geminal': 'Optimize geminal parameters (Boolean)',
    'opt_fixnl': 'Fix nonlocal energies in optimization',
    'opt_plan': 'Multi-cycle optimization plan (Block)',
    'dmc_method': 'DMC algorithm (1 - EBES, 2 - CBCS)',
    'dmc_equil_nstep': 'Number of steps per process (Integer)',
    'dmc_equil_nblock': 'Number of checkpoints (Integer)',
    'dmc_stats_nstep': 'Number of steps per process (Integer)',
    'dmc_stats_nblock': 'Number of checkpoints (Integer)',
    'dmc_target_weight': 'Total target weight in DMC (Real)',
    'dtdmc': 'DMC time step (Real)',
    'use_tmove': 'Casula nl pp for DMC (Boolean)',
    'popstats': 'Collect population statistics (Boolean)',
    'dmc_trip_weight': 'DMC catastrophe threshold (Real)',
    'use_jastrow': 'Use a Jastrow function (Boolean)',
    'backflow': 'Use backflow corrections (Boolean)',
    'random_seed': 'Random number seed (Text or Integer)',
}

SYSTEM = {'neu': REQUIRED, 'ned': REQUIRED, 'periodic': 'F', 'atom_basis_type': REQUIRED}
WAVEFUNCTION = {'use_jastrow': 'T', 'backflow': 'F'}
VMC = {'vmc_equil_nstep': '5000', 'vmc_nstep': '10000', 'vmc_nblock': '1', 'vmc_nconfig_write': '0', 'vmc_decorr_period': '0'}

# The DMC defaults are deliberately small: they are a shape that runs, not a production
# calculation. `dtdmc` in particular is a physics choice -- CASINO's own default of 0.01 is far
# too coarse for an all-electron heavy atom and far finer than a pseudopotential run needs --
# so it is defaulted, never derived, and `advise` says so out loud.
#
# `popstats : T` is not CASINO's default and is set here on purpose: it is what puts the
# statistical-efficiency section into `dmc.status`, which is the only readable estimate a
# running DMC calculation has (see parse_out). It costs nothing.
DMC = {
    'dmc_equil_nstep': '1000',
    'dmc_equil_nblock': '1',
    'dmc_stats_nstep': '10000',
    'dmc_stats_nblock': '10',
    'dmc_target_weight': '1024.0',
    'dtdmc': '0.01',
    'popstats': 'T',
}

# What each runtype needs in the file, and what it gets if the caller says nothing. A value of
# REQUIRED has no sensible default -- CASINO cannot guess the number of electrons either.
#
# Every DMC recipe carries the whole DMC group, equilibration-only and statistics-only included:
# CASINO wants `dmc_equil_nstep` and `dmc_stats_nstep` both, whichever half it is about to run.
RUN = {'newrun': 'T', 'testrun': 'F'}
CONFIG_GEN = {'vmc_nstep': '1024', 'vmc_nconfig_write': '1024', 'vmc_decorr_period': '1'}

RECIPES = {
    'vmc': {**SYSTEM, 'runtype': 'vmc', **RUN, **VMC, **WAVEFUNCTION},
    'vmc_opt': {
        **SYSTEM,
        'runtype': 'vmc_opt',
        **RUN,
        **VMC,
        # the optimisation sample is what the VMC phase writes, so it must write some
        'vmc_nconfig_write': '10000',
        'vmc_decorr_period': '10',
        'opt_method': 'emin',
        'opt_cycles': '4',
        'opt_jastrow': 'T',
        'opt_backflow': 'F',
        **WAVEFUNCTION,
    },
    'opt_vmc': {
        **SYSTEM,
        'runtype': 'opt_vmc',
        **RUN,
        **VMC,
        'vmc_nconfig_write': '10000',
        'vmc_decorr_period': '10',
        'opt_method': 'emin',
        'opt_cycles': '4',
        'opt_jastrow': 'T',
        'opt_backflow': 'F',
        **WAVEFUNCTION,
    },
    'opt': {**SYSTEM, 'runtype': 'opt', **RUN, 'opt_method': 'emin', 'opt_jastrow': 'T', **WAVEFUNCTION},
    # The VMC phase of a vmc_dmc run exists to generate the DMC starting population: it writes
    # one configuration per unit of target weight and stops.
    'vmc_dmc': {**SYSTEM, 'runtype': 'vmc_dmc', **RUN, **VMC, **CONFIG_GEN, **DMC, **WAVEFUNCTION},
    'vmc_dmc_equil': {**SYSTEM, 'runtype': 'vmc_dmc_equil', **RUN, **VMC, **CONFIG_GEN, **DMC, **WAVEFUNCTION},
    'dmc_dmc': {**SYSTEM, 'runtype': 'dmc_dmc', **RUN, **DMC, **WAVEFUNCTION},
    'dmc_equil': {**SYSTEM, 'runtype': 'dmc_equil', **RUN, **DMC, **WAVEFUNCTION},
    'dmc_stats': {**SYSTEM, 'runtype': 'dmc_stats', **RUN, **DMC, **WAVEFUNCTION},
}

# Which phases a runtype has, and whether it starts from configurations somebody else wrote.
# This is `runqmc`'s own table (the `case $runtype` at the top of `check_input_files`): every
# rule below hangs off it, and it is the one thing here that must be copied from CASINO rather
# than reasoned out. `req_config` means a `config.in` is an input to the run exactly as the
# orbital file is; `req_config_gen` means the VMC phase exists to write one and so must.
PHASES = {
    'vmc': {'vmc'},
    'opt': {'opt', 'config'},
    'vmc_opt': {'vmc', 'opt', 'config_gen'},
    'opt_vmc': {'vmc', 'opt', 'config', 'config_gen'},
    'dmc': {'dmc', 'config'},
    'dmc_equil': {'dmc', 'config'},
    'dmc_stats': {'dmc', 'config'},
    'dmc_dmc': {'dmc', 'config'},
    'vmc_dmc': {'vmc', 'dmc', 'config_gen'},
    'vmc_dmc_equil': {'vmc', 'dmc', 'config_gen'},
}

NEEDS_CONFIGS = tuple(sorted(name for name, phases in PHASES.items() if 'config' in phases))

# What CASINO errstops without -- kept apart from RECIPES on purpose. A recipe answers "what
# should a new input of this kind say"; this answers "what will CASINO refuse to run without",
# and the second is a fact about CASINO that a change of house style must not be able to alter.
#
# It is shorter than it looks like it should be, and every absence is deliberate.
# `vmc_equil_nstep`, `vmc_nblock`, the two `*_nblock`s and `opt_method` all have working
# defaults inside CASINO and are `optional` in runqmc's own checks; `dtdmc` has one too (0.01),
# which is why it is in the recipes and in `advise` but not here. Both `dmc_*_nstep` are
# mandatory for *any* DMC runtype, including an equilibration-only or statistics-only one --
# CASINO asks for the whole pair whichever half it is about to run.
EVERY_RUNTYPE = ('neu', 'ned', 'atom_basis_type')
BY_PHASE = {
    'vmc': ('vmc_nstep',),
    'dmc': ('dmc_equil_nstep', 'dmc_stats_nstep', 'dmc_target_weight'),
    'config_gen': ('vmc_nconfig_write',),
}

MANDATORY = {
    runtype: EVERY_RUNTYPE + tuple(name for phase, names in BY_PHASE.items() if phase in phases for name in names)
    for runtype, phases in PHASES.items()
}

# Keywords that are alternatives: setting the second makes the first redundant, and CASINO
# resolves the pair silently (`opt_plan` sets opt_cycles to its own line count), so a recipe
# must not add one on top of the other.
ALTERNATIVES = {'opt_cycles': 'opt_plan'}

ORBITAL_FILE = {
    'gaussian': 'gwfn.data',
    'slater-type': 'stowfn.data',
    'plane-wave': 'pwfn.data',
    'blip': 'bwfn.data',
    'numerical': 'awfn.data',
    'dimer': 'dwfn.data',
}

TRUE = ('t', 'true', '.true.')


def truthy(value) -> bool:
    return str(value).strip().lower() in TRUE


def number(value):
    try:
        return float(str(value).replace('d', 'e').replace('D', 'E'))
    except (TypeError, ValueError):
        return None


def parse_text(text: str) -> tuple[dict, dict]:
    """The keywords and the blocks of an input file, keyed by lower-case name.

    A block is kept as the lines between its `%block` and `%endblock`, unparsed: what is inside
    depends on the block, and this module has no business interpreting `opt_plan` or `npcell`.
    """
    keywords, blocks = {}, {}
    block = None
    for line in text.splitlines():
        if block is not None:
            end = BLOCK_END.match(line)
            if end and end.group(1).lower() == block:
                block = None
            else:
                blocks[block].append(line.rstrip())
            continue
        start = BLOCK_START.match(line)
        if start:
            block = start.group(1).lower()
            blocks[block] = []
            continue
        if line.lstrip().startswith('#'):
            continue
        entry = ENTRY.match(line)
        if entry:
            keywords[entry.group(1).lower()] = entry.group(2)
    return keywords, blocks


def read(path) -> dict:
    """One input file as data: its keywords, its blocks, and the text they came from."""
    path = Path(path)
    if path.is_dir():
        path = path / 'input'
    text = path.read_text(errors='replace')
    keywords, blocks = parse_text(text)
    return {'path': str(path.resolve()), 'text': text, 'keywords': keywords, 'blocks': blocks, 'runtype': keywords.get('runtype', '').strip()}


def line_for(name: str, value) -> str:
    """One `keyword : value #*! description` line, in the columns CASINO's own examples use."""
    description = DESCRIPTIONS.get(name)
    body = f'{name:<18}: {value}'
    return f'{body:<35}#*! {description}' if description else body


def block_for(name: str, value) -> list[str]:
    return [f'%block {name}', *str(value).strip('\n').splitlines(), f'%endblock {name}']


def rendered(name: str, value) -> list[str]:
    return block_for(name, value) if '\n' in str(value) else [line_for(name, value)]


def block_bounds(lines: list[str], start: int, name: str) -> int:
    """The index of the `%endblock` closing the block that opens at `start`, or the last line."""
    for i in range(start + 1, len(lines)):
        end = BLOCK_END.match(lines[i])
        if end and end.group(1).lower() == name:
            return i
    return len(lines) - 1


def apply(text: str, overrides: dict) -> str:
    """`text` with the named keywords changed, added or removed. Everything else survives.

    A value of None deletes the keyword. A value containing a newline is written as a block --
    that is how `opt_plan` and `npcell` are set, and setting one over an existing block replaces
    it whole. An existing keyword is rewritten where it stands and keeps its own trailing
    comment, so the file keeps its order, its comments and whatever it holds that no recipe
    knows about; a keyword the file did not have is added under its section, and the section
    header is written if there is none.

    Editing rather than regenerating is the point. A calculation directory's `input` is a
    document -- hand comments, an `npcell` block, expert keywords a template never lists -- and
    rewriting it from a recipe would drop exactly the parts nobody can reconstruct.
    """
    changes = {name.lower(): value for name, value in overrides.items()}
    lines = text.splitlines()
    written, out = set(), []
    i = 0
    while i < len(lines):
        line = lines[i]
        start = BLOCK_START.match(line)
        if start:
            name = start.group(1).lower()
            end = block_bounds(lines, i, name)
            if name in changes:
                written.add(name)
                if changes[name] is not None:
                    out.extend(block_for(name, changes[name]))
            else:
                out.extend(lines[i : end + 1])
            i = end + 1
            continue
        entry = None if line.lstrip().startswith('#') else ENTRY.match(line)
        if entry and entry.group(1).lower() in changes:
            name = entry.group(1).lower()
            value = changes[name]
            written.add(name)
            if value is None:
                i += 1
                continue
            if '\n' in str(value):
                out.extend(block_for(name, value))
            else:
                comment = entry.group(3)
                body = f'{name:<18}: {value}'
                out.append(f'{body:<35}{comment}' if comment else line_for(name, value))
            i += 1
            continue
        out.append(line)
        i += 1

    remaining = {name: value for name, value in changes.items() if name not in written and value is not None}
    return '\n'.join(insert_new(out, remaining)) + '\n'


def insert_new(lines: list[str], remaining: dict) -> list[str]:
    """Put keywords the file did not have under their section, adding the header if need be."""
    if not remaining:
        return lines
    lines = list(lines)
    sections = dict(SECTIONS)
    headers = {line.strip('# ').upper(): i for i, line in enumerate(lines) if line.lstrip().startswith('#') and line.strip('# ').upper() in sections}
    placements, tail = [], []
    for section, names in SECTIONS:
        here = [name for name in names if name in remaining]
        if not here:
            continue
        flat = [line for name in here for line in rendered(name, remaining[name])]
        if section in headers:
            end = headers[section] + 1
            while end < len(lines) and lines[end].strip() and not lines[end].lstrip().startswith('#'):
                end += 1
            placements.append((end, flat))
        else:
            tail.extend(['', f'# {section}', *flat])
    for index, flat in sorted(placements, key=lambda placement: placement[0], reverse=True):
        lines[index:index] = flat

    # A keyword no section claims still gets written -- this module knows which keywords go
    # where, not which keywords exist, and CASINO has 304 of them.
    known = {name for _, names in SECTIONS for name in names}
    unknown = [line for name, value in remaining.items() if name not in known for line in rendered(name, value)]
    while lines and not lines[-1].strip():
        lines.pop()
    return lines + tail + (['', *unknown] if unknown else [])


def recipe(runtype: str, values: dict | None = None, present: dict | None = None) -> tuple[dict, list[str]]:
    """The keywords a runtype needs, filled from `values`, then `present`, then the defaults.

    `present` is what the file already says, so switching an existing calculation to another
    runtype keeps the electron count and the basis it was written for and only adds what the
    new runtype introduces. What no one supplied and has no default comes back as the second
    element: CASINO cannot guess it either.
    """
    if runtype not in RECIPES:
        raise KeyError(runtype)
    values, present = {k.lower(): v for k, v in (values or {}).items()}, present or {}
    filled, missing = {}, []
    for name, default in RECIPES[runtype].items():
        if name in values:
            filled[name] = values[name]
        elif name in present:
            continue  # the file already answers this one; leave its line alone
        elif ALTERNATIVES.get(name) in present:
            continue  # and this one is answered by the keyword that supersedes it
        elif default is not REQUIRED:
            filled[name] = default
        else:
            missing.append(name)
    filled.update({name: value for name, value in values.items() if name not in filled})
    filled['runtype'] = values.get('runtype', runtype)
    return filled, missing


def build(runtype: str, values: dict | None = None, title: str = '') -> str:
    """A whole input file for one runtype, in the layout CASINO's own examples use."""
    filled, missing = recipe(runtype, values)
    if missing:
        raise ValueError(f'runtype {runtype} needs {", ".join(missing)}, and there is no default for them')
    text = HEADER + (f'\n# {title}\n' if title else '')
    return apply(text, filled)


def check(keywords: dict, blocks: dict | None = None) -> list[str]:
    """Everything wrong with this input that can be seen without opening the directory.

    Each of these is legal one keyword at a time and wrong in combination, which is why CASINO
    only finds them at run time -- and by then a queue slot has been spent on it.
    """
    blocks = blocks or {}
    errors = []
    runtype = keywords.get('runtype', '').strip()
    if not runtype:
        errors.append('runtype is not set')
    for name in MANDATORY.get(runtype, ()):
        if name not in keywords:
            errors.append(f'{name} is required for runtype {runtype}')

    phases = PHASES.get(runtype, set())
    written, steps = number(keywords.get('vmc_nconfig_write')), number(keywords.get('vmc_nstep'))
    weight = number(keywords.get('dmc_target_weight'))
    if 'config_gen' in phases and not written:
        errors.append(f'vmc_nconfig_write must be greater than zero for runtype {runtype}: its VMC phase is there to write configurations')
    if written and steps and written > steps:
        errors.append(f'vmc_nconfig_write {written:.0f} is above vmc_nstep {steps:.0f}: a step cannot write more than one configuration')
    if 'dmc' in phases and 'vmc' in phases and written and weight and written < weight:
        errors.append(
            f'vmc_nconfig_write {written:.0f} is below dmc_target_weight {weight:.0f}: '
            f'the VMC phase would not write enough configurations to start DMC from'
        )
    if truthy(keywords.get('opt_backflow')) and not truthy(keywords.get('backflow')):
        errors.append('opt_backflow needs backflow : T')
    if truthy(keywords.get('opt_jastrow')) and not (truthy(keywords.get('use_jastrow')) or truthy(keywords.get('use_gjastrow'))):
        errors.append('opt_jastrow needs use_jastrow : T')
    if runtype == 'opt' and len(blocks.get('opt_plan', [''])) != 1:
        errors.append(f'runtype opt is a single optimization, and opt_plan has {len(blocks["opt_plan"])} cycles')
    errors.extend(check_equilibration(keywords))
    return errors


def check_equilibration(keywords: dict) -> list[str]:
    """The floor `opt_dtvmc` puts under `vmc_equil_nstep`, which is easy to walk into.

    Automatic VMC time-step optimisation is on by default, and it needs enough equilibration
    moves to measure an acceptance ratio on: 2000 for the configuration-by-configuration
    algorithm, and 500/electron (never below 100) for electron-by-electron. Shortening a run by
    cutting the equilibration is exactly how a run gets stopped before it starts.
    """
    if 'vmc' not in PHASES.get(keywords.get('runtype', '').strip(), set()):
        return []
    if number(keywords.get('opt_dtvmc', '1')) == 0:
        return []
    equilibration = number(keywords.get('vmc_equil_nstep', '5000')) or 0
    if number(keywords.get('vmc_method', '1')) == 3:
        floor, why = 2000, 'vmc_method 3'
    else:
        electrons = (number(keywords.get('neu')) or 0) + (number(keywords.get('ned')) or 0)
        floor, why = max(100, int(500 / electrons)) if electrons else 500, f'{electrons:.0f} electrons'
    if equilibration < floor:
        return [f'vmc_equil_nstep {equilibration:.0f} is below the {floor} that opt_dtvmc needs for {why}; set opt_dtvmc : 0 or raise it']
    return []


def advise(keywords: dict, blocks: dict | None = None) -> list[str]:
    """What is legal, will run, and is probably not what was meant."""
    blocks = blocks or {}
    notes = []
    runtype = keywords.get('runtype', '').strip()
    for phase in ('equil', 'stats'):
        steps, nblock = number(keywords.get(f'dmc_{phase}_nstep')), number(keywords.get(f'dmc_{phase}_nblock'))
        if steps and nblock and steps % nblock:
            notes.append(
                f'dmc_{phase}_nstep {steps:.0f} is not divisible by dmc_{phase}_nblock {nblock:.0f}; CASINO rounds it up to the next multiple'
            )
    if runtype.endswith('dmc') or runtype.startswith('dmc'):
        if not truthy(keywords.get('popstats')):
            notes.append(
                'popstats is not T: CASINO then writes no statistical-efficiency section, and a run that is still going has no dmc.status to read'
            )
        if keywords.get('dtdmc') is not None and number(keywords['dtdmc']) == 0.01:
            notes.append("dtdmc is at CASINO's default of 0.01, which is a placeholder rather than a choice: it has to be set for the system")
    if 'opt_plan' in blocks and keywords.get('opt_cycles') and len(blocks['opt_plan']) != (number(keywords['opt_cycles']) or 0):
        notes.append(
            f'opt_plan has {len(blocks["opt_plan"])} cycles and opt_cycles says {keywords["opt_cycles"].strip()}: '
            f'CASINO takes the block, so the run will do {len(blocks["opt_plan"])}'
        )
    if keywords.get('opt_method', '').strip() == 'emin' and 'opt_plan' not in blocks and (number(keywords.get('opt_cycles')) or 0) > 1:
        notes.append(
            'opt_method is emin with no opt_plan: energy minimization from an unoptimized wave function usually opens with one varmin cycle, '
            'which is written as a block -- %block opt_plan / 1 method=varmin fix_cutoffs=T / 2 / 3 / %endblock opt_plan'
        )
    if 'vmc_nstep' in keywords:
        notes.append('vmc_nstep is the total over all MPI processes, unlike dmc_equil_nstep and dmc_stats_nstep, which are per process')
    notes.extend(unused(keywords))
    return notes


def unused(keywords: dict) -> list[str]:
    """Keywords in the file that this runtype has no phase for.

    CASINO does not mind them -- it reads the file and uses what its runtype needs -- but a
    `dmc_stats_nstep` left in a `vmc_opt` input is usually the residue of the calculation this
    one was copied from, and reading it as if it were in force is how a run gets misremembered.
    """
    runtype = keywords.get('runtype', '').strip()
    if runtype not in PHASES:
        return []
    phases = PHASES[runtype]
    notes = []
    for phase, prefix in (('dmc', 'dmc_'), ('opt', 'opt_')):
        stale = sorted(name for name in keywords if name.startswith(prefix))
        if phase not in phases and stale:
            notes.append(f'runtype {runtype} has no {phase.upper()} phase, so CASINO will not read {", ".join(stale)}')
    return notes


def check_files(directory, keywords: dict) -> list[str]:
    """The files this input tells CASINO to read, and whether the directory has them."""
    directory = Path(directory)
    errors = []
    basis = keywords.get('atom_basis_type', '').strip().lower()
    orbitals = ORBITAL_FILE.get(basis)
    if orbitals is not None and not (directory / orbitals).is_file():
        errors.append(f'atom_basis_type {basis} reads {orbitals}, and there is none in {directory}')
    if (truthy(keywords.get('use_jastrow')) or truthy(keywords.get('backflow'))) and not (directory / 'correlation.data').is_file():
        errors.append(f'use_jastrow / backflow read correlation.data, and there is none in {directory}')
    if (truthy(keywords.get('use_gjastrow')) or keywords.get('psi_s', '').strip() == 'geminal') and not (directory / 'parameters.casl').is_file():
        errors.append(f'psi_s : geminal / use_gjastrow read parameters.casl, and there is none in {directory}')
    runtype = keywords.get('runtype', '').strip()
    if (runtype in NEEDS_CONFIGS or not truthy(keywords.get('newrun', 'T'))) and not configurations(directory):
        errors.append(f'runtype {runtype} starts from the configurations in config.in, and there is none in {directory}')
    return errors


def configurations(directory) -> Path | None:
    """The file this directory's configurations are in: `config.in`, else `config.out`.

    Both count, because `runqmc` renames the second to the first itself when the first is
    absent (`[ ! -s config.in ] && [ -s config.out ] && move_config=1`, and the manual documents
    it under `config.out`). Treating only `config.in` as the answer would refuse a directory
    CASINO is perfectly willing to run in -- which is every directory a finished
    configuration-generating run left behind.

    Empty does not count, and that is runqmc's rule too: it tests for size, not existence.
    """
    directory = Path(directory)
    for name in ('config.in', 'config.out'):
        path = directory / name
        if path.is_file() and path.stat().st_size > 0:
            return path
    return None


def advise_files(directory, keywords: dict) -> list[str]:
    directory = Path(directory)
    notes = []
    pseudopotentials = sorted(path.name for path in directory.glob('*_pp.data'))
    runtype = keywords.get('runtype', '').strip()
    if pseudopotentials and 'dmc' in runtype and 'use_tmove' not in keywords:
        notes.append(
            f'{", ".join(pseudopotentials)} is a pseudopotential and use_tmove is not set: it defaults to T, '
            f'and whether the Casula scheme is wanted is a decision, not a default'
        )
    if not pseudopotentials and truthy(keywords.get('use_tmove')):
        notes.append('use_tmove is T but there is no *_pp.data in the directory: it does nothing without a pseudopotential')
    return notes
