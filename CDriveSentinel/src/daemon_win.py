#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Windowed entry point for the collector -- no console window to close (0.3.2).

Why this file exists
--------------------
``CDriveSentinel.exe`` is a console program: ``status`` / ``report`` / ``selftest``
must print to the terminal. The collector daemon, however, is a background
service, and when the scheduled task starts the console exe Windows hands the
process a real console window titled with the exe path. Closing that window is
CTRL_CLOSE_EVENT, and the default handler terminates the process immediately:
no traceback, no ``snapshot FAILED`` line in run.log, the task quietly returns
to Ready, and nothing scans again until someone starts it. Observed on
2026-11-25 17:09:07 -- ``LastTaskResult = 3221225786 (0xC000013A)`` seconds
after the window had been closed by hand, on a build whose log had otherwise
been healthy.

This module is compiled with ``console=False`` (see CDriveSentinel.spec), so the
process is created without a console at all. There is no window, therefore
nothing to close, and Ctrl+Break / window close cannot reach it. It knows
exactly one command:

    CDriveSentinelDaemon.exe              == CDriveSentinel.exe daemon
    CDriveSentinelDaemon.exe --no-usn     (daemon flags pass through)

From a source checkout:  python src\\daemon_win.py
"""
from __future__ import annotations

import sys
from pathlib import Path

if not getattr(sys, "frozen", False):
    # Source run: make `import main` resolve the way `src\main.py` does when the
    # exe is started from another working directory.
    sys.path.insert(0, str(Path(__file__).resolve().parent))

import main  # noqa: E402  (import after the path fix-up above)


def run(argv=None):
    """Run the collector daemon; extra arguments are forwarded verbatim."""
    extra = list(sys.argv[1:] if argv is None else argv)
    # main.main() dispatches on argv[1]; we are only ever the daemon.
    sys.argv = [sys.argv[0], "daemon"] + extra
    try:
        return main.main()
    except KeyboardInterrupt:
        return 130
    except SystemExit:
        raise
    except BaseException:
        # No console: the traceback must land in run.log or the death is silent.
        main._fatal_to_runlog(sys.exc_info()[1])
        return 1


if __name__ == "__main__":
    sys.exit(run())
