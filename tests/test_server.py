"""The MCP surface: which tools exist, what schema they present, and that they only delegate.

The wire-level check (a real stdio session with a real client) is
tests/integration/test_client_smoke.py. What is asserted here is the contract that breaks
silently: a renamed tool, an argument that stopped being optional, or a docstring that no
longer reaches the model -- the tool descriptions are the only documentation the model gets.
"""

import inspect

import pytest

from casino_mcp import runtime, server, settings

TOOLS = {'casino_run', 'casino_status', 'casino_stop', 'casino_list_jobs', 'casino_results', 'casino_prepare'}


def test_the_tool_surface_is_exactly_these_six():
    """The control plane, the tool that reads physics out of the files, and the one that writes an input."""
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
        ('casino_stop', 'stop', {'job_id': 'j'}),
        ('casino_list_jobs', 'listing', {}),
        ('casino_results', 'results', {'job_id': 'j'}),
        ('casino_prepare', 'prepare', {'source': '/tmp/a', 'dest': '/tmp/b'}),
    ],
)
def test_each_tool_only_delegates(monkeypatch, tool, function, arguments):
    """The protocol layer holds no logic of its own; that is why the runtime is testable."""
    seen = {}
    monkeypatch.setattr(runtime, function, lambda *args, **kwargs: seen.update(args=args, kwargs=kwargs) or {'ok': True})
    assert getattr(server, tool)(**arguments) == {'ok': True}
    assert seen


def test_run_passes_every_argument_through(monkeypatch):
    seen = {}
    monkeypatch.setattr(runtime, 'start', lambda workdir, **kwargs: seen.update(workdir=workdir, **kwargs) or {})
    server.casino_run('/tmp/calc', nproc=4, version='debug', restart=True, unlock=True)
    assert seen == {'workdir': '/tmp/calc', 'nproc': 4, 'version': 'debug', 'restart': True, 'resume': False, 'unlock': True}
