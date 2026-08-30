"""End-to-end check of the casino MCP server over real stdio MCP, driving real CASINO.

    pytest -m integration

Copies a He example out of `examples/` into a scratch directory, shortens the run, then drives
casino_run / casino_status / casino_stop / casino_list_jobs as a client would. This is the
only test that proves the tool schemas, the JSON round trip and the process handling work
together; everything else in tests/ stops at the Python call.

The server cannot be exercised through Claude Code's own tools in the session that creates
it -- `.mcp.json` is read at startup -- which is why this drives it as a client instead.
"""

import asyncio
import os
import re
import shutil
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from conftest import EXAMPLES
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

pytestmark = pytest.mark.integration

# The cheapest calculation in the tree: two electrons, one Slater determinant, no Jastrow.
EXAMPLE = EXAMPLES / 'stowfn/He/HF/QZ4P/EBES/Slater'


@pytest.fixture(scope='module')
def example():
    if shutil.which('runqmc') is None and not os.environ.get('CASINO_HOME'):
        pytest.skip('needs runqmc on the PATH or $CASINO_HOME')
    return EXAMPLE


@pytest.fixture
def prepare(example, tmp_path):
    """A copy of the example with the run shortened, so a VMC finishes in seconds."""

    def build(name: str, nstep: int, nblock: int) -> Path:
        scratch = tmp_path / name
        scratch.mkdir()
        shutil.copy(example / 'stowfn.data', scratch)
        text = (example / 'input').read_text()
        text = re.sub(r'vmc_nstep\s+:\s+\d+', f'vmc_nstep         : {nstep}', text)
        text = re.sub(r'vmc_nblock\s+:\s+\d+', f'vmc_nblock        : {nblock}', text)
        text = re.sub(r'vmc_equil_nstep\s+:\s+\d+', 'vmc_equil_nstep   : 1000', text)
        (scratch / 'input').write_text(text)
        return scratch

    return build


@asynccontextmanager
async def mcp_session():
    """A real stdio MCP session against the installed server.

    Deliberately not a fixture: anyio's cancel scopes have to be entered and left in the
    same task, and an async-generator fixture is torn down in a different one.
    """
    params = StdioServerParameters(command='casino-mcp', args=['serve'], env=os.environ.copy())
    async with stdio_client(params) as (read, write), ClientSession(read, write) as client:
        await client.initialize()
        yield client


async def call(session, name, **arguments):
    return (await session.call_tool(name, arguments)).structured_content


async def test_the_advertised_tools_are_the_six(example):
    async with mcp_session() as session:
        listed = await session.list_tools()

    assert {tool.name for tool in listed.tools} == {
        'casino_run',
        'casino_status',
        'casino_stop',
        'casino_list_jobs',
        'casino_results',
        'casino_prepare',
    }
    run = next(tool for tool in listed.tools if tool.name == 'casino_run')
    assert set(run.input_schema['properties']) == {'workdir', 'nproc', 'version', 'restart', 'resume', 'unlock'}
    assert run.input_schema['required'] == ['workdir']

    # `overrides` is a mapping whose values may be null -- that is what deletes a keyword -- and
    # the schema has to say so, or a client rejects the call before it reaches us.
    prepare = next(tool for tool in listed.tools if tool.name == 'casino_prepare')
    assert prepare.input_schema['required'] == ['source', 'dest']
    overrides = prepare.input_schema['properties']['overrides']
    assert {'type': 'null'} in overrides['anyOf'][0]['additionalProperties']['anyOf']
    # and the blank Jastrow is a list of term names, not a flag: a client that sends one has to
    # be able to say which terms it wants
    assert prepare.input_schema['properties']['jastrow']['anyOf'][0] == {'type': 'array', 'items': {'type': 'string'}}


async def test_committed_reference_data_is_refused_over_the_protocol(example):
    """The guardrail that matters most has to hold through the protocol, not just in Python."""
    async with mcp_session() as session:
        result = await call(session, 'casino_run', workdir=str(example))

    assert 'error' in result
    assert 'committed reference data' in result['error'] or 'earlier run' in result['error']


async def test_a_short_vmc_runs_to_completion(prepare):
    scratch = prepare('short', nstep=20000, nblock=2)
    async with mcp_session() as session:
        started = await call(session, 'casino_run', workdir=str(scratch), nproc=2)
        assert 'job_id' in started, started

        state = started
        for _ in range(120):
            state = await call(session, 'casino_status', job_id=started['job_id'])
            if state['status'] != 'running':
                break
            await asyncio.sleep(2)

    assert state['status'] == 'finished', state
    assert 'FINAL RESULT:' in (scratch / 'out').read_text()


async def test_a_long_run_can_be_stopped_and_leaves_no_lock(prepare):
    scratch = prepare('long', nstep=400000000, nblock=10)
    async with mcp_session() as session:
        started = await call(session, 'casino_run', workdir=str(scratch), nproc=2)
        await asyncio.sleep(10)
        assert (await call(session, 'casino_status', job_id=started['job_id']))['status'] == 'running'
        stopped = await call(session, 'casino_stop', job_id=started['job_id'])

    assert stopped['status'] == 'stopped'
    assert not (scratch / '.runqmc.lock').exists()
    # only the ranks were signalled, so runqmc lived to finish writing `out`
    assert stopped['terminated']['scope'] == 'casino'
    assert stopped['halt']['exit_code'] == 0, stopped['halt']


async def test_a_stopped_run_is_continued_by_the_route_casino_left_open(prepare):
    """The whole cycle against the real scripts: runqmc, then haltqmc -f -u, then runqmc again.

    A run interrupted by hand has no continuation info in `out` -- CASINO writes that only
    against a time limit -- so what continues it is the `input` haltqmc rewrote, and the second
    run has to end up in the same `out` as the first.
    """
    scratch = prepare('halted', nstep=4000000, nblock=4)
    async with mcp_session() as session:
        started = await call(session, 'casino_run', workdir=str(scratch), nproc=2)
        for _ in range(60):  # stop it once a block is behind it, which is what makes it continuable
            await asyncio.sleep(2)
            if 'Time taken in block' in (scratch / 'out').read_text(errors='replace'):
                break
        stopped = await call(session, 'casino_stop', job_id=started['job_id'])
        assert stopped['halt']['updated_input'] is True, stopped['halt']
        assert (scratch / 'config.in').is_file()  # haltqmc moved config.out here
        assert 'newrun            : F' in (scratch / 'input').read_text()

        resumed = await call(session, 'casino_run', workdir=str(scratch), nproc=2, resume=True)
        assert resumed['resume'] == 'halted', resumed
        assert '--continue' not in resumed['command']

        state = resumed
        for _ in range(120):
            state = await call(session, 'casino_status', job_id=resumed['job_id'])
            if state['status'] != 'running':
                break
            await asyncio.sleep(2)

    assert state['status'] == 'finished', state
    out = (scratch / 'out').read_text(errors='replace')
    assert out.count(' Started ') == 2  # both runs in one file, which is how CASINO continues
    assert 'Total CASINO CPU time' in out


async def test_a_job_outlives_the_server_that_started_it(prepare):
    """The registry is on disk, so a second server sees a run the first one started."""
    scratch = prepare('outlives', nstep=400000000, nblock=10)
    async with mcp_session() as session:
        started = await call(session, 'casino_run', workdir=str(scratch), nproc=2)
        await asyncio.sleep(5)

    async with mcp_session() as session:  # a different server process
        listed = await call(session, 'casino_list_jobs', limit=5)
        assert listed['jobs'][0]['job_id'] == started['job_id']
        assert listed['jobs'][0]['status'] == 'running'
        stopped = await call(session, 'casino_stop', job_id=started['job_id'])

    assert stopped['status'] == 'stopped'


async def test_an_unknown_job_is_an_error_payload_not_a_protocol_error(example):
    async with mcp_session() as session:
        result = await call(session, 'casino_status', job_id='does-not-exist')
    assert result['error'] == 'unknown job does-not-exist'
    # the reply is validated against the output schema on the way out, so every field the runtime
    # had no answer for arrives as a null rather than as an absent key
    assert {key for key, value in result.items() if value is not None} == {'error'}
