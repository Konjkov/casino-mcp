"""Where CASINO is, and where our state goes.

There is no configuration file: an MCP server is configured in the `env` block of the
`.mcp.json` that registers it, and CASINO's own variables keep their names. So what is worth
asserting is small — that each variable is read, that the fallbacks are the documented ones,
and that `$CASINO_MCP_FORBID` covers what it claims to.
"""

import os
from pathlib import Path

from casino_mcp import settings


def test_casino_home_falls_back_to_the_conventional_location(monkeypatch):
    assert settings.casino_home() == Path.home() / 'bin' / 'CASINO'
    monkeypatch.setenv('CASINO_HOME', '/opt/CASINO')
    assert settings.casino_home() == Path('/opt/CASINO')


def test_a_tilde_in_the_environment_is_expanded(monkeypatch):
    monkeypatch.setenv('CASINO_HOME', '~/elsewhere/CASINO')
    assert settings.casino_home() == Path.home() / 'elsewhere' / 'CASINO'


def test_binary_path_is_where_casino_puts_the_binary(monkeypatch):
    monkeypatch.setenv('CASINO_HOME', '/opt/CASINO')
    monkeypatch.setenv('CASINO_ARCH', 'linuxpc-gcc-parallel')
    assert settings.binary_path('debug') == Path('/opt/CASINO/bin_qmc/linuxpc-gcc-parallel/debug/casino')


def test_an_unset_arch_costs_provenance_not_correctness(monkeypatch):
    """Without $CASINO_ARCH the stamp points at a path that does not exist, and says so."""
    monkeypatch.setenv('CASINO_HOME', '/opt/CASINO')
    assert settings.casino_arch() == ''
    assert settings.binary_path('opt') == Path('/opt/CASINO/bin_qmc/opt/casino')


def test_state_dir_follows_xdg(tmp_path):
    assert settings.state_path() == tmp_path / 'state' / 'casino-mcp'


def test_state_dir_falls_back_to_the_xdg_default(monkeypatch):
    monkeypatch.delenv('XDG_STATE_HOME')
    assert settings.state_path() == Path.home() / '.local' / 'state' / 'casino-mcp'


def test_explicit_state_dir_wins_over_xdg(monkeypatch, tmp_path):
    monkeypatch.setenv('CASINO_MCP_STATE_DIR', str(tmp_path / 'elsewhere'))
    assert settings.state_path() == tmp_path / 'elsewhere'


def test_nothing_is_forbidden_by_default(tmp_path):
    assert settings.forbidden(tmp_path) == ''


def test_forbid_matches_a_directory_and_everything_under_it(monkeypatch, tmp_path):
    examples = tmp_path / 'examples'
    (examples / 'He' / 'HF').mkdir(parents=True)
    monkeypatch.setenv('CASINO_MCP_FORBID', str(examples))

    assert settings.forbidden(examples) == str(examples)
    assert settings.forbidden(examples / 'He' / 'HF') == str(examples)
    assert settings.forbidden(tmp_path / 'scratch') == ''


def test_forbid_takes_several_entries_like_path(monkeypatch, tmp_path):
    first, second = tmp_path / 'one', tmp_path / 'two'
    first.mkdir(), second.mkdir()
    monkeypatch.setenv('CASINO_MCP_FORBID', os.pathsep.join([str(first), '', str(second)]))

    assert settings.forbidden(second) == str(second)
    assert settings.forbidden(tmp_path / 'three') == ''  # an empty entry is not "everything"


def test_resolved_is_json_able_and_names_every_variable(monkeypatch, tmp_path):
    import json

    monkeypatch.setenv('CASINO_HOME', str(tmp_path))
    resolved = settings.resolved()

    assert json.loads(json.dumps(resolved))['casino_home'] == str(tmp_path)
    assert set(resolved['environment']) == {name for name, _ in settings.ENVIRONMENT}
    assert resolved['defaults'] == {
        'nproc': settings.NPROC,
        'version': settings.VERSION,
        'stop_timeout': settings.STOP_TIMEOUT,
        'halt_timeout': settings.HALT_TIMEOUT,
        'wait_timeout': settings.WAIT_TIMEOUT,
        'keep_jobs': settings.KEEP_JOBS,
    }


def test_every_documented_variable_is_actually_read():
    """The --help epilog is the only documentation of these; a stale entry there is a lie."""
    source = (Path(settings.__file__)).read_text()
    for name, description in settings.ENVIRONMENT:
        assert f"'{name}'" in source, f'{name} is documented but never read'
        assert description and description[0].islower()
