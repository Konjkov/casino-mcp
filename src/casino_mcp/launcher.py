"""Waits on a single runqmc process and records how it ended.

Spawned by `runtime.start` as `python -m casino_mcp.launcher <job_dir>` in its own session,
which buys three things and nothing else does:

  * the whole process tree (runqmc -> mpirun -> casino) shares one session, which is how a
    stop finds the `casino` ranks -- mpirun puts each of them in a process group of its own,
    so `killpg` reaches runqmc and mpirun and stops there;
  * the exit code survives the MCP server being restarted, because it is written to disk;
  * runqmc's own output goes to a log file instead of the server's stdio stream.
"""

import json
import signal
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

signalled = False


def on_signal(signum, frame):
    global signalled
    signalled = True


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 1:
        print('usage: python -m casino_mcp.launcher <job_dir>', file=sys.stderr)
        return 2
    job_dir = Path(argv[0])
    meta = json.loads((job_dir / 'meta.json').read_text())

    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)

    with open(job_dir / 'runqmc.log', 'wb') as log:
        process = subprocess.Popen(meta['command'], cwd=meta['workdir'], stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT)
        while True:
            try:
                exit_code = process.wait(timeout=1.0)
                break
            except subprocess.TimeoutExpired:
                if signalled and process.poll() is None:
                    process.terminate()
                    try:
                        exit_code = process.wait(timeout=10.0)
                        break
                    except subprocess.TimeoutExpired:
                        process.kill()

    finished = time.time()
    status = {
        'exit_code': exit_code,
        'signalled': signalled,
        'finished': datetime.fromtimestamp(finished, UTC).astimezone().isoformat(timespec='seconds'),
        'finished_epoch': finished,
    }
    (job_dir / 'status.json').write_text(json.dumps(status, indent=2))
    return 0


if __name__ == '__main__':
    sys.exit(main())
