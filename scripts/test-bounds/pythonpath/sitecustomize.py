"""Bound a test-file process for its whole life, not just while a test runs.

Put this directory on ``PYTHONPATH`` and ``site`` imports it during interpreter
startup - before the test module is imported, before pytest is configured, and
still armed after pytest's last test finishes. That window is the point: it is
the one place a bound can sit and cover every phase of a test file.

Note [Why a fourth bound was needed]
    Three bounds already applied to a shard, and each one misses this:

    * ``pytest-timeout`` (``PYTEST_ADDOPTS``) arms around a test item's
      setup/call/teardown and is disarmed everywhere else - during import and
      collection, and after the final test while the interpreter and the CUDA
      context tear down.
    * ``run_test.py``'s own 30min per-file subprocess timeout would cover the
      whole invocation, but it cannot kill anything on Windows. Its expiry path
      calls ``p.send_signal(signal.SIGINT)``, and ``Popen.send_signal`` on
      Windows accepts only ``SIGTERM`` / ``CTRL_C_EVENT`` / ``CTRL_BREAK_EVENT``
      - ``SIGINT`` (2) raises ``ValueError``, which escapes before the
      ``p.kill()`` below it, leaving the handler's ``finally: p.wait()`` blocked
      forever on a live child. That is an upstream defect, not a setting we got
      wrong. (Absence corroborates it: ``retry_shell``'s "Command took >Nmin,
      returning 124" appears in none of ~9800 collected shard logs.)
    * the workflow's own watchdog does work, but it kills the shard. Every test
      that had not run yet is lost with it.

    Two of the shards lost to a hang froze in exactly the gap this closes: one
    ``test_meta`` printed its complete session summary and then never exited,
    and one ``test_sparse_csr`` logged "Retrying single test..." with the
    replacement pytest never reaching "test session starts". Both are stuck
    processes rather than truncated logs - ``run_test.py`` renames a file's
    ``*_toprint.log`` only once the subprocess returns, and both kept that name.

Note [Two timers, because either one alone has a hole]
    :func:`_arm` starts both a Python timer and faulthandler's C-level one, at
    slightly different deadlines, because their failure modes are complementary:

    * A ``threading.Timer`` can run arbitrary Python - so it can kill the whole
      process *tree*, which matters because pytest-xdist workers are children
      and an orphan holds the step's stdout pipe open (that alone can wedge a
      step even after the parent dies). But it needs the GIL. A C call that
      blocks while holding the GIL starves it, and never releasing the GIL is
      exactly the shape of the CUDA-driver deadlocks that started this.
    * ``faulthandler.dump_traceback_later(exit=True)`` runs on a native thread
      and fires regardless of the GIL, so it is immune to that. But its
      ``_exit()`` only ends *this* process and leaves children orphaned.

    So the Python timer goes first and does the thorough job; faulthandler is
    armed ``_BACKSTOP_GRACE_SEC`` later and only gets to act if the first was
    starved.

Deliberately inert unless ``PER_PROCESS_TIMEOUT_SEC`` is set to a positive
number, and deliberately silent when it arms. A ``sitecustomize`` is imported by
*every* python process in the job, so a stray print here would be several
hundred lines of noise per shard; the workflow echoes the setting once instead.
Nothing in here may raise either, for the same reason - a sitecustomize that
throws breaks every python invocation on the runner, so the whole body is
guarded.
"""

import os
import sys

_ENV_VAR = "PER_PROCESS_TIMEOUT_SEC"

# How long after the Python timer to let faulthandler's native timer fire. Only
# reached if the Python timer was starved of the GIL, so it needs to be long
# enough that a healthy timer has certainly finished, and short relative to the
# per-shard watchdog.
_BACKSTOP_GRACE_SEC = 60.0


def _configured_bound(environ=None):
    """Return the configured bound in seconds, or ``0.0`` when disabled.

    Anything unparseable is treated as "off" rather than raising: this runs at
    interpreter startup for every python process in the job, so a typo in the
    workflow must not be able to break the whole shard.
    """
    raw = (environ if environ is not None else os.environ).get(_ENV_VAR, "")
    try:
        seconds = float(str(raw).strip())
    except (TypeError, ValueError):
        return 0.0
    return seconds if seconds > 0 else 0.0


def _is_test_file_process(argv):
    """Is this the interpreter running a pytorch test file?

    ``run_test.py`` invokes each test file as ``python test_foo.py ...``, so
    ``argv[0]`` names the file. The parent orchestrator is ``run_test.py``
    itself and must never be armed - killing it would take down the shard,
    which is the outcome this whole module exists to avoid. pytest-xdist
    workers are not matched either; they are children of a matched process and
    the tree kill collects them.
    """
    if not argv or not argv[0]:
        return False
    name = os.path.basename(str(argv[0]))
    return name.startswith("test_") and name.endswith(".py")


def _kill_tree(pid):
    """Kill ``pid`` and its descendants. Returns True if a killer was launched.

    ``taskkill /T`` is what makes this worth doing over a plain ``_exit()``:
    ``/T`` takes the children too, so no orphaned xdist worker is left holding
    the test step's stdout pipe.
    """
    import subprocess

    if sys.platform == "win32":
        cmd = ["taskkill", "/T", "/F", "/PID", str(pid)]
    else:
        cmd = ["kill", "-9", "--", f"-{pid}"]
    try:
        subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
        )
        return True
    except Exception:
        return False


def _report(seconds, stream=None):
    """Announce the timeout and dump every thread's stack.

    The stacks are the whole point of reporting before dying: a shard lost to a
    hang otherwise records no failure and no stack at all, which is what made
    the original hangs so expensive to diagnose.
    """
    import faulthandler

    out = stream if stream is not None else sys.stderr
    try:
        out.write(
            f"\n::error::PER_PROCESS_TIMEOUT: {os.path.basename(str(sys.argv[0]))} "
            f"made no progress within {seconds:.0f}s of wall clock. This process "
            f"is being killed so the shard can continue with the remaining test "
            f"files. All thread stacks follow.\n"
        )
        out.flush()
    except Exception:
        pass
    try:
        faulthandler.dump_traceback(file=out, all_threads=True)
        out.flush()
    except Exception:
        pass


def _arm(seconds, argv=None, stream=None):
    """Arm both timers. Returns the ``threading.Timer`` so tests can inspect it.

    See Note [Two timers, because either one alone has a hole].
    """
    import threading

    def _fire():
        _report(seconds, stream=stream)
        if not _kill_tree(os.getpid()):
            os._exit(1)

    timer = threading.Timer(seconds, _fire)
    timer.daemon = True  # must never hold up a healthy interpreter exit
    timer.name = "per-process-timeout"
    timer.start()

    try:
        import faulthandler

        faulthandler.dump_traceback_later(
            seconds + _BACKSTOP_GRACE_SEC, exit=True
        )
    except Exception:
        pass

    return timer


def _main(argv=None, environ=None, stream=None):
    """Arm the bound if this process qualifies. Returns the timer, or ``None``."""
    seconds = _configured_bound(environ)
    if not seconds:
        return None
    if not _is_test_file_process(sys.argv if argv is None else argv):
        return None
    return _arm(seconds, stream=stream)


try:
    _main()
except Exception:
    # A sitecustomize that raises breaks every python process on the runner.
    # Losing the bound is recoverable - the per-shard watchdog still applies.
    pass
