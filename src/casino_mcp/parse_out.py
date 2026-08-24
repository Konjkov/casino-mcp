"""Structured data out of a CASINO `out` file.

A path in, a dict out: no MCP, no dependencies, nothing invented. Every number carries the
1-based line it was read from, and anything CASINO did not print comes back as None with a
reason instead of a guess.

An `out` file is a sequence of phases, not one result: `vmc_opt` writes a VMC and an
OPTIMIZATION phase per cycle, `vmc_dmc` writes VMC, DMC equilibration and DMC statistics
accumulation. So the phases are returned as a list, and `result` points at the last one that
carries an energy.

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
    for phase, following in zip(phases, phases[1:] + [{'start': len(lines)}], strict=True):
        phase['end'] = following['start']
    return phases


def block_bounds(lines, start, end):
    starts = [i for i in range(start, end) if lines[i].strip().startswith('In block :')]
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
    result = {'energy': missing('no FINAL RESULT block in this phase'), 'variance': missing('no FINAL RESULT block in this phase')}
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
        if stripped.startswith('Bad reblock convergence'):
            result['reblock_converged'] = False
    result.setdefault('reblock_converged', True)
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
    parsed = {'blocks': blocks, 'nblock': len(blocks), 'energy': missing('no mixed estimators in this phase')}
    mixed = next((i for i in range(start, end) if lines[i].strip().startswith('Mixed estimators of the energies')), None)
    if mixed is not None:
        parsed['mixed_estimators'] = {}
        for i in range(mixed, end):
            stripped = lines[i].strip()
            for key, label in MIXED:
                if stripped.startswith(label) and '+/-' in stripped:
                    values = rhs_values(lines[i])
                    parsed['mixed_estimators'][key] = value(values[0], i, values[1] if len(values) > 1 else None)
        parsed['energy'] = parsed['mixed_estimators'].get('total_energy', parsed['energy'])
    parsed['variance'] = missing('no statistical-efficiency analysis in this phase')
    for i in range(start, end):
        stripped = lines[i].strip()
        for key, label in EFFICIENCY:
            if stripped.startswith(label):
                values = rhs_values(lines[i])
                parsed[key if key != 'variance' else 'variance'] = value(values[0], i, values[1] if len(values) > 1 else None)
        if stripped.startswith('Number of data points collected'):
            parsed['data_points'] = measured(lines, i)
    if blocks:
        parsed['acceptance'] = derived(mean([b.get('acceptance', missing('')) for b in blocks]), f'mean over {len(blocks)} blocks')
    return parsed


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
    header = {'keywords': {}}
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


def parse_out(path):
    path = Path(path)
    if path.is_dir():
        path = path / 'out'
    lines = path.read_text(errors='replace').split('\n')
    phases = split_phases(lines)
    parsed = {'path': str(path.resolve())}
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

    final = next((p for p in reversed(parsed['phases']) if p.get('energy', {}).get('value') is not None), None)
    if final is None:
        parsed['result'] = missing('no phase in this file reports an energy')
    else:
        parsed['result'] = {'phase': parsed['phases'].index(final), 'kind': final['kind'], 'energy': final['energy'], 'variance': final['variance']}
    parsed['messages'] = parse_messages(lines)
    return parsed


if __name__ == '__main__':
    print(json.dumps(parse_out(sys.argv[1]), indent=2))
