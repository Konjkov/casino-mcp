"""End-to-end check of the casino MCP server over real stdio MCP, driving real CASINO.

    pytest -m integration --examples-dir ~/PycharmProjects/PyCasino/examples

Copies a He example into a scratch directory, shortens the run, then drives
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
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

pytestmark = pytest.mark.integration

EXAMPLE = Path('stowfn/He/HF/QZ4P/EBES/Slater')


@pytest.fixture(scope='module')
def example(request):
    root = request.config.getoption('--examples-dir')
    if not root:
        pytest.skip('needs --examples-dir (or $CASINO_EXAMPLES)')
    path = Path(root).expanduser() / EXAMPLE
    if not (path / 'input').is_file():
        pytest.skip(f'no {EXAMPLE} under {root}')
    if shutil.which('runqmc') is None and not os.environ.get('CASINO_HOME'):
        pytest.skip('needs runqmc on the PATH or $CASINO_HOME')
    return path


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


async def test_the_advertised_tools_are_the_four(example):
    async with mcp_session() as session:
        listed = await session.list_tools()

    assert {tool.name for tool in listed.tools} == {'casino_run', 'casino_status', 'casino_stop', 'casino_list_jobs'}
    run = next(tool for tool in listed.tools if tool.name == 'casino_run')
    assert set(run.input_schema['properties']) == {'workdir', 'nproc', 'version', 'overwrite', 'unlock'}
    assert run.input_schema['required'] == ['workdir']


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
    assert result == {'error': 'unknown job does-not-exist'}
