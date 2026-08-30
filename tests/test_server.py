"""The MCP surface: which tools exist, what schema they present, and that they only delegate.

The wire-level check (a real stdio session with a real client) is
tests/integration/test_client_smoke.py. What is asserted here is the contract that breaks
silently: a renamed tool, an argument that stopped being optional, or a docstring that no
longer reaches the model -- the tool descriptions are the only documentation the model gets.
"""

import inspect
import json

import pytest

from casino_mcp import jobs, runtime, server, settings
from casino_mcp.parse_out import parse_out

TOOLS = {
    'casino_run',
    'casino_status',
    'casino_wait',
    'casino_stop',
    'casino_list_jobs',
    'casino_results',
    'casino_input',
    'casino_prepare',
}


def test_the_tool_surface_is_exactly_these_eight():
    """The control plane, the two that read a calculation -- what it did and what it was given -- and the one that writes an input."""
    assert {name for name in dir(server) if name.startswith('casino_')} == TOOLS


@pytest.mark.parametrize('name', sorted(TOOLS))
def test_every_tool_has_a_docstring_the_model_can_act_on(name):
    doc = inspect.getdoc(getattr(server, name))
    assert doc and len(doc.split('\n')[0]) > 20


def test_no_generic_shell_tool_exists():
    """Every tool is a named CASINO operation with typed arguments. That is the safety model."""
    for name in dir(server):
        assert 'shell' not in name and 'exec' not in name and 'command' not in name


def test_run_signature_matches_the_runtime():
    parameters = inspect.signature(server.casino_run).parameters
    assert list(parameters) == ['workdir', 'nproc', 'version', 'restart', 'resume', 'unlock']
    # these defaults are in the schema the model reads, so they must be the real ones
    assert parameters['nproc'].default == settings.NPROC
    assert parameters['version'].default == settings.VERSION
    assert parameters['restart'].default is False
    assert parameters['resume'].default is False
    assert parameters['unlock'].default is False


def test_defaults_are_never_restated_in_the_protocol_layer():
    """One default, one place. A literal here would drift from the runtime's own."""
    assert inspect.signature(server.casino_stop).parameters['timeout'].default == settings.STOP_TIMEOUT
    assert inspect.signature(runtime.start).parameters['nproc'].default == settings.NPROC
    assert inspect.signature(runtime.stop).parameters['timeout'].default == settings.STOP_TIMEOUT


def test_tools_are_registered_with_the_server():
    assert server.server.name == 'casino'
    assert 'CASINO' in (server.server.instructions or '')


@pytest.mark.parametrize(
    'tool,function,arguments',
    [
        ('casino_run', 'start', {'workdir': '/tmp/x'}),
        ('casino_status', 'status', {'job_id': 'j'}),
        ('casino_wait', 'wait', {'job_id': 'j'}),
        ('casino_stop', 'stop', {'job_id': 'j'}),
        ('casino_list_jobs', 'listing', {}),
        ('casino_results', 'results', {'job_id': 'j', 'fields': ['vmc.energy']}),
        ('casino_input', 'calculation_input', {'job_id': 'j'}),
        ('casino_prepare', 'prepare', {'source': '/tmp/a', 'dest': '/tmp/b'}),
    ],
)
def test_each_tool_only_delegates(monkeypatch, tool, function, arguments):
    """The protocol layer holds no logic of its own; that is why the runtime is testable."""
    seen = {}
    monkeypatch.setattr(runtime, function, lambda *args, **kwargs: seen.update(args=args, kwargs=kwargs) or {'ok': True})
    assert getattr(server, tool)(**arguments) == {'ok': True}
    assert seen


def test_prepare_passes_the_wave_function_blocks_through(monkeypatch):
    """The three files a first calculation may need, each written only when it is asked for."""
    seen = {}
    monkeypatch.setattr(runtime, 'prepare', lambda source, dest, **kwargs: seen.update(source=source, dest=dest, **kwargs) or {})
    server.casino_prepare('/tmp/a', '/tmp/b', geminal=['p:2'], geminal_settings={'mirror': 1})
    assert seen['geminal'] == ['p:2'] and seen['geminal_settings'] == {'mirror': 1}
    assert seen['jastrow'] is None and seen['backflow'] is None and seen['jastrow_settings'] is None


def test_run_passes_every_argument_through(monkeypatch):
    seen = {}
    monkeypatch.setattr(runtime, 'start', lambda workdir, **kwargs: seen.update(workdir=workdir, **kwargs) or {})
    server.casino_run('/tmp/calc', nproc=4, version='debug', restart=True, unlock=True)
    assert seen == {'workdir': '/tmp/calc', 'nproc': 4, 'version': 'debug', 'restart': True, 'resume': False, 'unlock': True}


def test_the_binary_stamp_survives_the_output_model(tmp_path, monkeypatch):
    """A declared field is a claim about what the runtime returns, and `binary` was the one that lied.

    jobs.binary_stamp has answered with a dict since 0.1.0 while JobState declared a str, so
    casino_status, casino_wait and casino_list_jobs all failed at serialization -- after the
    calculation had run. Nothing caught it: extra='allow' waves through every key the model does
    not name, casino_run declares a plain dict and casino_results never fills the field in, and
    the delegation tests above stub the runtime out, so no unit test ever put a real stamp in a model.
    """
    present, missing = tmp_path / 'casino', tmp_path / 'gone' / 'casino'
    present.write_bytes(b'x')
    for path in (present, missing):
        monkeypatch.setattr(settings, 'binary_path', lambda _version, path=path: path)
        stamp = jobs.binary_stamp('opt')
        state = server.JobState.model_validate({'job_id': 'j', 'status': 'running', 'binary': stamp})
        assert state.model_dump(exclude_none=True)['binary'] == stamp


def test_every_declared_field_says_what_it_is():
    """An output schema exists to carry the units. A field without a description carries nothing."""
    for model in (server.Measured, server.Phase, server.JobState, server.JobList, server.Results):
        for name, field in model.model_fields.items():
            assert field.description, f'{model.__name__}.{name} declares a type and says nothing'


def test_the_units_of_a_vmc_phase_are_written_down():
    """The four numbers a scan reads, and the four ways a caller could otherwise get them wrong."""
    phase = server.Phase.model_json_schema()['properties']
    assert 'per cent' in phase['acceptance']['description']
    assert 'steps' in phase['correlation_time']['description']
    assert 'au^-2 s^-1' in phase['efficiency']['description']
    assert 'variance of the gaussian proposal' in phase['dtvmc']['description']


@pytest.mark.parametrize('name', ['vmc_single', 'vmc_opt_emin', 'vmc_opt_varmin', 'vmc_dmc', 'interrupted'])
def test_a_parsed_run_survives_the_output_model_whole(out_file, name):
    """Validation must not become a filter: every key the parser produced still reaches the caller."""
    parsed = parse_out(out_file(name))
    dumped = server.Results.model_validate(parsed).model_dump(mode='json')
    assert [key for key in parsed if key not in dumped] == []
    for phase, validated in zip(parsed['phases'], dumped['phases'], strict=True):
        assert [key for key in phase if key not in validated] == []


async def test_the_schemas_reach_the_wire():
    tools = {tool.name: tool for tool in await server.server.list_tools()}
    assert 'au^-2 s^-1' in json.dumps(tools['casino_results'].output_schema)
    assert set(tools['casino_status'].output_schema['properties']) >= {'job_id', 'status', 'workdir', 'runtime', 'error'}
    assert tools['casino_list_jobs'].output_schema['properties']['jobs']['anyOf'][0]['items']['$ref'].endswith('JobState')
    # the two tools that write answer with what they did, which is not a fixed shape
    assert 'properties' not in tools['casino_run'].output_schema
    assert 'properties' not in tools['casino_prepare'].output_schema


def test_the_results_docstring_names_what_the_parser_returns():
    """The description is the only documentation the model gets, and it was silent about the
    numbers a scan reads: `acceptance`, `efficiency`, `correlation_time`, `dtvmc`, CPU time."""
    doc = inspect.getdoc(server.casino_results)
    for kind in ('vmc', 'opt', 'dmc_equil', 'dmc_stats'):
        assert kind in doc, kind
    for name in ('acceptance', 'correlation_time', 'efficiency', 'dtvmc', 'steps_per_process', 'nparam', 'mixed_estimators', 'cpu_time', 'keywords'):
        assert name in doc, name
