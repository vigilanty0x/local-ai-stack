"""Synthetic subprocess output only; never read machine GPU or configuration."""
import os
from pathlib import Path
import sys
import time

assert sys.flags.isolated and sys.flags.no_site and sys.flags.dont_write_bytecode
state = Path(sys.argv[2]).resolve()
runtime = Path(sys.base_prefix).resolve()
def guard(event, args):
    if event == "open":
        if isinstance(args[0], int):
            return
        path = Path(os.fsdecode(args[0])).resolve()
        writing = (isinstance(args[1], str) and any(c in args[1] for c in "wax+")) or args[2] & (os.O_WRONLY | os.O_RDWR | os.O_CREAT)
        if writing or not (path.is_relative_to(runtime) or path == Path(__file__).resolve()):
            raise PermissionError("synthetic GPU child file boundary")
    elif event.startswith("socket.") or event in {"subprocess.Popen", "os.system"}:
        raise PermissionError("synthetic GPU child execution boundary")
sys.addaudithook(guard)
mode = sys.argv[1]
if mode == "ok":
    os.write(1, b"Synthetic GPU, 4096, 1024, 25\n")
elif mode == "partial":
    os.write(1, b"Synthetic GPU, 4096, N/A, N/A\n")
elif mode == "huge":
    for _ in range(100):
        os.write(1, b"x" * 4096)
elif mode == "stderr":
    for _ in range(100):
        os.write(2, b"s" * 4096)
elif mode == "slow":
    time.sleep(20)
elif mode == "nonzero":
    os.write(2, b"synthetic diagnostic must not escape\n")
    raise SystemExit(7)
elif mode == "badutf8":
    os.write(1, b"\xff\xfe")
elif mode != "empty":
    raise SystemExit(8)
