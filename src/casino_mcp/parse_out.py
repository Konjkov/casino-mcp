"""Structured data out of a CASINO `out` file, and out of the `dmc.status` beside it.

A path in, a dict out: no MCP, no dependencies, nothing invented. Every number carries the
1-based line it was read from, and anything CASINO did not print comes back as None with a
reason instead of a guess.

An `out` file is a sequence of phases, not one result: `vmc_opt` writes a VMC and an
OPTIMIZATION phase per cycle, `vmc_dmc` writes VMC, DMC equilibration and DMC statistics
accumulation. So the phases are returned as a list, and `result` points at the last one that
carries an energy.

**A running DMC calculation has no energy in `out` at all.** The mixed estimators are written
once, at the very end of the run; until then the only place the current estimate exists is
`dmc.status`, which CASINO rewrites at the end of every statistics block and deletes when the
run finishes -- the same text is copied into `out` at that moment, so nothing is lost, but a
job that is still running is read from `dmc.status` or not at all. It is written by
`write_dmc_status` in `dmc.f90`, which is also what writes the `out` section, so the two have
the same shape and `parse_dmc_status` and `parse_dmc` share their parser.

The single derived quantity is the sample-variance error, which CASINO prints only for a
multi-block run and which is otherwise taken from the one block, exactly as `envmc` does.
Block means are labelled `derived` and never overwrite a printed value.

    python casino_mcp/parse_out.py <dir-or-out-file>
"""

import json
import re
import sys
from pathlib import Path

NUMBER = re.compile(r'[-+]?(?:\d+\.?\d*|\.\d+)(?:[EeDd][-+]?\d+)?|Infinity|NaN')

BANNERS = (
    ('vmc', 'PERFORMING A SINGLE VMC CALCULATION.'),
    ('vmc', 'PERFORMING A VMC CONFIGURATION-GENERATION CALCULATION.'),
    ('vmc', 'PERFORMING VMC CONFIGURATION-GENERATION CALCULATION No.'),
    ('vmc', 'PERFORMING POST-FIT VMC CALCULATION.'),
    ('opt', 'PERFORMING OPTIMIZATION CALCULATION No.'),
    ('dmc_equil', 'PERFORMING A DMC EQUILIBRATION CALCULATION.'),
    ('dmc_stats', 'PERFORMING A DMC STATISTICS-ACCUMULATION CALCULATION.'),
)

CORRECTIONS = (('no_correction', 'No correction'), ('correlation_time', 'Correlation time method'), ('reblock', 'On-the-fly reblocking method'))

KEYWORD = re.compile(r'^\s*([A-Z][A-Z_0-9]*)\s*(?:\([^)]*\))?\s*:\s*(\S.*?)\s*$')

MIXED = (
    ('total_energy', 'Total energy'),
    ('kinetic_ti', 'Kinetic energy (TI)'),
    ('kinetic_kei', 'Kinetic energy (KEI)'),
    ('kinetic_fisq', 'Kinetic energy (FISQ)'),
    ('ee_interaction', 'e-e interac.'),
    ('ei_interaction', 'e-i interaction'),
)

MIXED_HEADING = 'Mixed estimators of the energies'
UNITS_PREFIX = '[All energies given in '
STATUS_NAME = 'dmc.status'

# How CASINO says the error bar it just printed cannot be trusted. The first is what a phase
# with too little data gets, the second what a reblocking that has not plateaued gets; both
# are wordwrapped, so only the opening of the message is matched.
UNRELIABLE = ('Reblocking not converged', 'Bad reblock convergence')

REBLOCK_SCALARS = (('mean', 'mean:'), ('stderr', 'stderr:'), ('errfac', 'errfac:'), ('n_corr', 'N_corr:'))
REBLOCK_DUMP = 'Dumping reblock data for energy:'
REBLOCK_HEADER = 'Block len'
REBLOCK_BEST = '*** BEST ***'

EFFICIENCY = (
    ('correlation_length', 'Int corr length (steps)'),
    ('time_step', 'DMC time step (au)'),
    ('correlation_time', 'Int correlation time (au)'),
    ('variance', 'Var of loc en (au / simcell)'),
    ('std_dev_local_energy', 'Std dev of local energy'),
    ('steps', 'Number of steps of accum data'),
    ('effective_steps', 'Effective number of steps'),
    ('target_weight', 'Target weight'),
    ('average_population', 'Average population'),
    ('effective_population', 'Effective population'),
    ('stat_inefficiency_est', 'Stat inefficiency (est)'),
    ('stat_inefficiency_measured', 'Stat inefficiency (measured)'),
)


def to_float(token):
    if token[0] in '+-.0123456789':
        return float(token.replace('D', 'E').replace('d', 'e'))
    return None


def rhs_values(line):
    for sep in ('=', ':'):
        if sep in line:
            line = line.split(sep, 1)[1]
            break
    return [to_float(token) for token in NUMBER.findall(line)]


def value(x, i, error=None):
    v = {'value': x}
    if error is not None:
        v['error'] = error
    v['line'] = i + 1
    if x is None:
        v['reason'] = 'CASINO printed a non-numeric value here'
    return v


def missing(reason):
    return {'value': None, 'reason': reason}


def derived(x, source):
    return {'value': x, 'derived': source}


def measured(lines, i):
    values = rhs_values(lines[i])
    if not values:
        return missing('no number on the line')
    error = None
    if i + 1 < len(lines) and 'Standard error' in lines[i + 1]:
        following = rhs_values(lines[i + 1])
        error = following[0] if following else None
    return value(values[0], i, error)


def mean(values):
    numbers = [v['value'] for v in values if v['value'] is not None]
    if not numbers:
        return None
    return sum(numbers) / len(numbers)


def parse_reblock(lines, start, end) -> dict | None:
    """The reblock dump: the four summary rows and the block-length table under them.

    This is where the error bar CASINO quotes actually comes from -- it is the row marked
    `*** BEST ***`, and whether the rows above it have flattened out is the whole question of
    whether the error bar means anything. The table is small (a dozen rows) and no substitute
    exists at the `out` level, so it is returned whole rather than summarised.

    **Whether it is printed at all differs between the two places it appears.** The DMC mixed
    estimators always carry it (`dmc.f90` calls `reblock_dump` unconditionally). A VMC
    `FINAL RESULT` carries it only when the reblocking did *not* converge -- `vmc.f90` prints it
    inside the `derr > 0.1*err` branch, right after the "Bad reblock convergence" line -- so in
    a VMC phase it is a diagnostic that appears exactly when the error bar is in doubt, and its
    absence is the good case. Nothing downstream may treat it as a field that is always there.
    """
    first = next((i for i in range(start, end) if lines[i].strip().startswith(REBLOCK_DUMP)), None)
    if first is None:
        return None
    dump: dict = {'line': first + 1, 'rows': []}
    in_table = False
    for i in range(first + 1, end):
        stripped = lines[i].strip()
        for key, label in REBLOCK_SCALARS:
            if stripped.startswith(label):
                values = rhs_values(stripped)
                dump[key] = value(values[0], i, values[1] if len(values) > 1 else None)
        if stripped.startswith(REBLOCK_HEADER):
            in_table = True
            continue
        if in_table:
            values = rhs_values(stripped)
            if len(values) < 3:
                break  # the closing rule of dashes, or whatever follows the table
            length, stderr, error_in_error = values[:3]
            if length is None or stderr is None or error_in_error is None:
                break
            row = {'length': int(length), 'stderr': stderr, 'error_in_error': error_in_error, 'line': i + 1}
            if REBLOCK_BEST in stripped:
                row['best'] = True
                dump['best_block_length'] = row['length']
            dump['rows'].append(row)
    return dump


def parse_mixed(lines, start, end) -> dict:
    """The `Mixed estimators of the energies` section, wherever it was found.

    One parser for two files: `write_dmc_status` in CASINO's `dmc.f90` writes this text into
    `dmc.status` after every statistics block, and the identical text into `out` at the end of
    the run. A second parser would be a second thing to keep in step with CASINO.
    """
    parsed: dict = {
        'energy': missing('no mixed estimators in this section'),
        'variance': missing('no statistical-efficiency analysis in this section'),
    }
    heading = next((i for i in range(start, end) if lines[i].strip().startswith(MIXED_HEADING)), None)
    if heading is not None:
        parsed['mixed_estimators'] = {}
        for i in range(heading, end):
            stripped = lines[i].strip()
            if stripped.startswith(UNITS_PREFIX):
                parsed['units'] = value(stripped[len(UNITS_PREFIX) :].rstrip(']'), i)
            for key, label in MIXED:
                if stripped.startswith(label) and '+/-' in stripped:
                    values = rhs_values(lines[i])
                    parsed['mixed_estimators'][key] = value(values[0], i, values[1] if len(values) > 1 else None)
        parsed['energy'] = parsed['mixed_estimators'].get('total_energy', parsed['energy'])
    for i in range(start, end):
        stripped = lines[i].strip()
        for key, label in EFFICIENCY:
            if stripped.startswith(label):
                values = rhs_values(lines[i])
                parsed[key] = value(values[0], i, values[1] if len(values) > 1 else None)
        if stripped.startswith('Number of data points collected'):
            parsed['data_points'] = measured(lines, i)
        if stripped.startswith(UNRELIABLE):
            parsed['reblock_converged'] = False
    reblock = parse_reblock(lines, start, end)
    if reblock is not None:
        parsed['reblock'] = reblock
    if heading is not None:
        parsed.setdefault('reblock_converged', True)
    return parsed


def split_phases(lines):
    phases = []
    for i, line in enumerate(lines):
        stripped = line.strip()
        for kind, banner in BANNERS:
            if stripped.startswith(banner):
                index = rhs_values(stripped[len(banner) :]) if banner.endswith('No.') else []
                number = int(index[0]) if index and index[0] is not None else None
                phases.append({'kind': kind, 'label': stripped, 'index': number, 'start': i})
                break
    # Each phase runs to where the next one starts, and the last to the end of the file. With no
    # phase at all there is nothing to pair -- a run that errstopped before its first one, or a
    # file that is not an `out` -- and the empty list is the honest answer, not an exception.
    if not phases:
        return phases
    for phase, following in zip(phases, phases[1:] + [{'start': len(lines)}], strict=True):
        phase['end'] = following['start']
    return phases


def block_bounds(lines, start, end):
    starts = [i for i in range(start, end) if lines[i].strip().startswith('In block :')]
    if not starts:
        return []  # a phase cut off before it finished a block, for the same reason as above
    return list(zip(starts, starts[1:] + [end], strict=True))


def parse_vmc_block(lines, start, end):
    block = {'line': start + 1, 'acceptance_levels': []}
    for i in range(start, end):
        stripped = lines[i].strip()
        if stripped.startswith('Acceptance ratio'):
            block['acceptance_levels'].append(measured(lines, i))
        elif stripped.startswith('Diffusion constant'):
            block['diffusion_constant'] = measured(lines, i)
        elif stripped.startswith('Correlation time'):
            values = rhs_values(lines[i])
            block['correlation_time'] = value(values[0], i, values[1] if len(values) > 1 else None)
        elif stripped.startswith('Efficiency'):
            block['efficiency'] = measured(lines, i)
        elif stripped.startswith('No. of VMC steps per'):
            block['steps_per_process'] = measured(lines, i)
        elif stripped.startswith('Total energy'):
            block['energy'] = measured(lines, i)
        elif stripped.startswith('Variance of local energy'):
            block['variance'] = measured(lines, i)
        elif stripped.startswith('Time taken in block'):
            block['time'] = measured(lines, i)
    block['acceptance'] = block['acceptance_levels'][-1] if block['acceptance_levels'] else missing('no acceptance ratio in the block')
    return block


def parse_final_result(lines, start, end, blocks):
    result: dict = {'energy': missing('no FINAL RESULT block in this phase'), 'variance': missing('no FINAL RESULT block in this phase')}
    first = next((i for i in range(start, end) if lines[i].strip().startswith('FINAL RESULT:')), None)
    if first is None:
        return result
    result['energy_errors'] = {}
    for i in range(first, end):
        stripped = lines[i].strip()
        for key, label in CORRECTIONS:
            if stripped.endswith(label) and '+/-' in stripped:
                values = rhs_values(stripped)
                result['energy_errors'][key] = value(values[1], i)
                if key == 'correlation_time':
                    result['energy'] = value(values[0], i, values[1])
        if stripped.startswith('Sample variance of E_L'):
            values = rhs_values(lines[i])
            error = values[1] if len(values) > 1 else None
            result['variance'] = value(values[0], i, error)
            if error is None and len(blocks) == 1:
                result['variance']['error'] = blocks[0].get('variance', {}).get('error')
                result['variance']['derived'] = 'error taken from the only block, as envmc does'
        if stripped.startswith(UNRELIABLE):
            result['reblock_converged'] = False
    result.setdefault('reblock_converged', True)
    reblock = parse_reblock(lines, first, end)
    if reblock is not None:
        result['reblock'] = reblock
    return result


def parse_vmc(lines, phase):
    start, end = phase['start'], phase['end']
    blocks = [parse_vmc_block(lines, *bounds) for bounds in block_bounds(lines, start, end)]
    parsed = {'blocks': blocks, 'nblock': len(blocks)}
    dtvmc = next((i for i in range(start, end) if lines[i].strip().startswith('Optimized DTVMC:')), None)
    parsed['dtvmc'] = measured(lines, dtvmc) if dtvmc is not None else missing('no time-step optimization in this phase')
    parsed.update(parse_final_result(lines, start, end, blocks))
    if blocks:
        parsed['acceptance'] = derived(mean([b['acceptance'] for b in blocks]), f'mean over {len(blocks)} blocks')
        parsed['correlation_time'] = derived(mean([b.get('correlation_time', missing('')) for b in blocks]), f'mean over {len(blocks)} blocks')
        parsed['efficiency'] = derived(mean([b.get('efficiency', missing('')) for b in blocks]), f'mean over {len(blocks)} blocks')
        parsed['steps_per_process'] = blocks[0].get('steps_per_process', missing('not printed'))
    return parsed


def parse_dmc_block(lines, start, end):
    fields = (
        ('moves', 'Number of moves in block'),
        ('load_balancing', 'Load-balancing efficiency'),
        ('config_transfers', 'Number of config transfers'),
        ('acceptance', 'Acceptance ratio'),
        ('best_energy', 'New best estimate of DMC energy'),
        ('effective_time_step', 'New best estimate of effective time step'),
        ('time', 'Time taken in block'),
    )
    block = {'line': start + 1}
    for i in range(start, end):
        stripped = lines[i].strip()
        for key, label in fields:
            if stripped.startswith(label):
                block[key] = measured(lines, i)
    return block


def parse_dmc(lines, phase):
    start, end = phase['start'], phase['end']
    blocks = [parse_dmc_block(lines, *bounds) for bounds in block_bounds(lines, start, end)]
    parsed = {'blocks': blocks, 'nblock': len(blocks)}
    parsed.update(parse_mixed(lines, start, end))
    if parsed['energy']['value'] is None:
        # Not a gap in the parser: CASINO writes the mixed estimators once, when the run ends.
        # While it runs they exist only in `dmc.status`, which parse_dmc_status reads.
        parsed['energy'] = missing('no mixed estimators in this phase: CASINO writes them when the run ends, and until then they are in dmc.status')
    if blocks:
        parsed['acceptance'] = derived(mean([b.get('acceptance', missing('')) for b in blocks]), f'mean over {len(blocks)} blocks')
    return parsed


def parse_dmc_status(path) -> dict:
    """The current best estimate of a DMC run, from the `dmc.status` file of a live run.

    The file exists only between the end of the first statistics block and the end of the run,
    and holds the complete evaluation as it would be written if the run stopped now. It is
    rewritten whole after every block (`status='replace'`), so what is read here is a
    consistent snapshot and never a half-written one. `iaccum` gates it: an equilibration-only
    run, a run still equilibrating, and a `do_twist` run never have one, and the
    statistical-efficiency section needs `popstats : T`.
    """
    path = Path(path)
    if path.is_dir():
        path = path / STATUS_NAME
    lines = path.read_text(errors='replace').split('\n')
    return {
        'path': str(path.resolve()),
        'kind': 'dmc_status',
        **parse_mixed(lines, 0, len(lines)),
        'note': (
            'the estimate as of the last completed statistics block; CASINO rewrites this file after every '
            'block and deletes it when the run ends, copying the same text into `out`'
        ),
    }


def parse_opt(lines, phase):
    start, end = phase['start'], phase['end']
    parsed = {'method': missing('no optimization configuration header')}
    for i in range(start, end):
        stripped = lines[i].strip()
        if stripped.startswith('Variance minimization configuration'):
            parsed['method'] = value('varmin', i)
        elif stripped.startswith('Energy minimization configuration'):
            parsed['method'] = value('emin', i)
        elif stripped.startswith(('Number of variable parameters', 'No. of variable parameters')):
            parsed['nparam'] = measured(lines, i)
        elif stripped.startswith('There are ') and stripped.endswith('optimizable parameters.'):
            parsed['nparam'] = value(rhs_values(stripped)[0], i)
        elif stripped.startswith('Variance reduced to'):
            parsed['variance'] = measured(lines, i)
        elif stripped.startswith('Energy (a.u.) :'):
            parsed['energy'] = value(rhs_values(lines[i])[0], i)
        elif stripped.startswith('Error (a.u.) :') and 'energy' in parsed:
            parsed['energy'].setdefault('error', rhs_values(lines[i])[0])
        elif stripped.startswith('Variance (a.u.) :'):
            parsed['variance'] = value(rhs_values(lines[i])[0], i)
        elif stripped.startswith('NL2SOL return code'):
            parsed['return_code'] = measured(lines, i)
        elif stripped.startswith('Optimization halted'):
            parsed['halted'] = value(stripped, i)
    parsed.setdefault('energy', missing('optimization phase reports no final energy'))
    parsed.setdefault('variance', missing('optimization phase reports no final variance'))
    return parsed


def parse_header(lines, until):
    header: dict = {'keywords': {}}
    if lines and lines[0].startswith('CASINO'):
        header['version'] = value(lines[0].strip(), 0)
    for i in range(min(until, len(lines))):
        stripped = lines[i].strip()
        if stripped.startswith('Running on '):
            header['host'] = value(stripped[len('Running on ') :], i)
        elif stripped.startswith('Binary compiled in '):
            header['build'] = value(stripped.split()[3], i)
        elif stripped.startswith('Running in parallel using'):
            header['mpi_processes'] = measured(lines, i)
        elif stripped.startswith('Started '):
            header['started'] = value(stripped[len('Started ') :], i)
        else:
            match = KEYWORD.match(lines[i])
            if match:
                header['keywords'].setdefault(match.group(1), match.group(2))
    header.setdefault('mpi_processes', missing('no parallel banner'))
    header['runtype'] = header['keywords'].get('RUNTYPE', None)
    return header


def parse_messages(lines):
    markers = ('Warning', 'ERROR', 'Bad reblock convergence', 'Optimization halted')
    return [{'line': i + 1, 'text': line.strip()} for i, line in enumerate(lines) if line.strip().startswith(markers)]


PHASE_KINDS = ('vmc', 'opt', 'dmc_equil', 'dmc_stats')
SEGMENT = re.compile(r'^([A-Za-z_][A-Za-z_0-9]*)?(?:\[(-?\d+)\])?$')


class NoSuchField(Exception):
    """A path that does not exist in this run. Not the same as a number CASINO did not print."""


def what_is_here(node) -> str:
    if isinstance(node, dict):
        return 'has ' + ', '.join(sorted(node)) if node else 'is empty'
    if isinstance(node, list):
        return f'is a list of {len(node)}'
    return f'is {node!r}, which has nothing under it'


def pick_phase(parsed: dict, kind: str, index):
    """A phase by kind: the last one of that kind, or the cycle CASINO numbered `index`."""
    phases = [phase for phase in parsed.get('phases', []) if phase.get('kind') == kind]
    if not phases:
        kinds = sorted({phase.get('kind') for phase in parsed.get('phases', [])})
        raise NoSuchField(f'this run has no {kind} phase; its phases are {", ".join(kinds) or "none"}')
    if index is None:
        return phases[-1]
    numbered = [phase for phase in phases if phase.get('index') == index]
    if not numbered:
        available = ', '.join(str(phase.get('index')) for phase in phases)
        raise NoSuchField(f'this run has no {kind} phase numbered {index}; the {kind} phases are numbered {available}')
    return numbered[0]


def step(node, name, index, seen: str):
    here = seen
    if name is not None:
        if not isinstance(node, dict) or name not in node:
            raise NoSuchField(f'no {name} under {seen or "the top level"}, which {what_is_here(node)}')
        node = node[name]
        here = f'{seen}.{name}' if seen else name
    if index is not None:
        if not isinstance(node, list):
            raise NoSuchField(f'{here} is not a list, so {here}[{index}] means nothing')
        try:
            node = node[index]
        except IndexError:
            raise NoSuchField(f'{here} has {len(node)} entries, so there is no {here}[{index}]') from None
    return node


def walk(parsed: dict, path: str):
    """One path into a parsed run. See `select` for what a path may say."""
    node, seen = parsed, ''
    for position, segment in enumerate(path.split('.')):
        match = SEGMENT.match(segment)
        if not match or (match.group(1) is None and match.group(2) is None):
            raise NoSuchField(f'{segment!r} in {path!r} is not a name, a name[i], or a [i]')
        name, index = match.group(1), None if match.group(2) is None else int(match.group(2))
        if position == 0 and name in PHASE_KINDS:
            node = pick_phase(parsed, name, index)
        else:
            node = step(node, name, index, seen)
        seen = segment if not seen else f'{seen}.{segment}'
    return node


def select(parsed: dict, fields) -> tuple[dict, dict, list]:
    """The few numbers a caller came for, as a flat {path: value}.

    A whole parsed run is 10-16 kB of JSON, and a scan over 38 directories that wants six
    numbers from each does not want 600 kB of them. A path is written in the parsed run's own
    keys, so there is nothing to learn beyond four rules:

        vmc, opt, dmc_equil, dmc_stats   the last phase of that kind
        opt[3], vmc[2]                   the cycle CASINO itself numbered 3, 2
        phases[-1], phases[0]            by position, when the kind does not matter
        keywords.DTVMC, cpu_time.value   anything else is a key of the parsed run

    and one distinction. A path that does not exist is a mistake in the question and comes back
    in `problems`, naming what is there instead; a path that exists but holds a number CASINO
    did not print comes back as null with its reason, which is a fact about the run.

    A path that lands on a measured value collapses to the number, since that is what was being
    asked for -- `vmc.energy.error` and `vmc.energy.line` reach the rest of it.
    """
    values, reasons, problems = {}, {}, []
    for path in fields:
        try:
            node = walk(parsed, path)
        except NoSuchField as e:
            problems.append(f'{path}: {e}')
            continue
        if isinstance(node, dict) and 'value' in node:
            values[path] = node['value']
            if node['value'] is None and node.get('reason'):
                reasons[path] = node['reason']
        else:
            values[path] = node
    return values, reasons, problems


def parse_out(path) -> dict:
    path = Path(path)
    if path.is_dir():
        path = path / 'out'
    lines = path.read_text(errors='replace').split('\n')
    phases = split_phases(lines)
    parsed: dict = {'path': str(path.resolve())}
    parsed.update(parse_header(lines, phases[0]['start'] if phases else len(lines)))

    parsers = {'vmc': parse_vmc, 'opt': parse_opt, 'dmc_equil': parse_dmc, 'dmc_stats': parse_dmc}
    parsed['phases'] = []
    for phase in phases:
        entry = {'kind': phase['kind'], 'label': phase['label'], 'index': phase['index'], 'line': phase['start'] + 1}
        entry.update(parsers[phase['kind']](lines, phase))
        parsed['phases'].append(entry)

    parsed['complete'] = False
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith('Total CASINO CPU time'):
            parsed['cpu_time'] = measured(lines, i)
            parsed['complete'] = True
        elif stripped.startswith('Total CASINO real time'):
            parsed['real_time'] = measured(lines, i)
        elif stripped.startswith('Ends '):
            parsed['ended'] = value(stripped[len('Ends ') :], i)
    for key, reason in (
        ('cpu_time', 'run did not reach the timing report'),
        ('real_time', 'run did not reach the timing report'),
        ('ended', 'run did not finish'),
    ):
        parsed.setdefault(key, missing(reason))

    # A DMC run that has not reached its end has no energy in `out`, but the estimate it would
    # print if it stopped now is in `dmc.status` next to it -- while it runs, and also after it
    # was killed, since only an orderly end deletes that file. Reading it here rather than in
    # each caller is what keeps `result` from pointing at the VMC phase of a running DMC job:
    # that number is the trial wave function's energy, not the calculation's.
    status_file = path.parent / STATUS_NAME
    if status_file.is_file():
        parsed['dmc_status'] = parse_dmc_status(status_file)

    final = next((p for p in reversed(parsed['phases']) if p.get('energy', {}).get('value') is not None), None)
    if 'dmc_status' in parsed:
        current = parsed['dmc_status']
        parsed['result'] = {'source': STATUS_NAME, 'kind': 'dmc_status', 'energy': current['energy'], 'variance': current['variance']}
    elif final is None:
        parsed['result'] = missing('no phase in this file reports an energy')
    elif not parsed['complete'] and parsed['phases'][-1]['kind'].startswith('dmc') and final['kind'] != parsed['phases'][-1]['kind']:
        parsed['result'] = missing(
            f'this run is in its {parsed["phases"][-1]["kind"]} phase and no DMC energy exists yet: CASINO writes the mixed '
            f'estimators when the run ends, and dmc.status only from the end of the first statistics block. '
            f"The energy in phase {parsed['phases'].index(final)} is the {final['kind']} one and is not this run's result."
        )
    else:
        parsed['result'] = {'phase': parsed['phases'].index(final), 'kind': final['kind'], 'energy': final['energy'], 'variance': final['variance']}
    parsed['messages'] = parse_messages(lines)
    return parsed


if __name__ == '__main__':
    print(json.dumps(parse_out(sys.argv[1]), indent=2))
