"""The MCP wire protocol with no SDK on the client side.

Speaks JSON-RPC 2.0 over the server's stdin/stdout by hand and prints every line in both
directions, so what the decorators in server.py actually produce is visible. Read this
before adding a tool. It is a diagnostic, not a test: nothing here asserts anything, and
tests/integration/test_client_smoke.py is the checked version of the same round trip.

    python tools/protocol_dump.py

Note the handshake below is the legacy `initialize` + `notifications/initialized` one at
protocolVersion 2025-06-18. The current revision replaced it with `server/discover` and
per-request `_meta`; the old path stays compatible but is on a clock.
"""

import json
import subprocess
import sys

SERVER = [sys.executable, '-m', 'casino_mcp.server']


def send(process, message):
    line = json.dumps(message)
    print(f'\n--> {line[:300]}')
    process.stdin.write(line + '\n')
    process.stdin.flush()


def receive(process, label):
    line = process.stdout.readline()
    if not line:
        raise SystemExit('server closed the connection')
    print(f'<-- {label}:')
    print(json.dumps(json.loads(line), indent=2)[:2000])
    return json.loads(line)


def main() -> int:
    process = subprocess.Popen(
        SERVER,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        bufsize=1,
    )

    send(
        process,
        {
            'jsonrpc': '2.0',
            'id': 1,
            'method': 'initialize',
            'params': {'protocolVersion': '2025-06-18', 'capabilities': {}, 'clientInfo': {'name': 'protocol-dump', 'version': '0'}},
        },
    )
    receive(process, 'initialize')

    send(process, {'jsonrpc': '2.0', 'method': 'notifications/initialized'})

    send(process, {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list'})
    listed = receive(process, 'tools/list')
    print('\ntool schemas generated from the Python signatures:')
    for tool in listed['result']['tools']:
        params = ', '.join(tool['inputSchema']['properties'])
        print(f'  {tool["name"]}({params})')

    send(process, {'jsonrpc': '2.0', 'id': 3, 'method': 'tools/call', 'params': {'name': 'casino_list_jobs', 'arguments': {'limit': 1}}})
    receive(process, 'tools/call casino_list_jobs')

    send(process, {'jsonrpc': '2.0', 'id': 4, 'method': 'tools/call', 'params': {'name': 'casino_status', 'arguments': {'job_id': 'does-not-exist'}}})
    receive(process, 'tools/call casino_status (unknown id)')

    process.stdin.close()
    process.wait(timeout=10)
    return 0


if __name__ == '__main__':
    sys.exit(main())
