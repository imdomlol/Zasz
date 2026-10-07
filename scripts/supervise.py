"""Run games in their own processes, at most `jobs` at once, each under a real-time deadline enforced from
outside: the benchmark's and the scenario runner's workers.

A frozen StarCraft II leaves python-sc2 waiting on its connection forever, and nothing inside that process
can interrupt the wait reliably. So each game runs in a fresh process (python-sc2 keeps per-process state,
and a fresh process is the only way to give a game a fixed hash seed) that starts its own process group,
which the game it launches joins; past its deadline the parent kills the whole group, StarCraft II
included. Results come back as games finish, not in submission order, so one stuck game doesn't hold up
the others' results.
"""

import multiprocessing
import os
import signal
import time
import traceback
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from multiprocessing.connection import Connection, wait


@dataclass
class Outcome:
    index: int  # position in the task list
    result: object = None  # what the function returned (when it did)
    error: str | None = None  # why there's no result: its traceback, a timeout, or the process dying
    real_time: float = 0.0


def _child(conn: Connection, func: Callable, args: tuple) -> None:
    if hasattr(os, "setsid"):
        os.setsid()  # own process group: the game it starts joins it, and goes with it if killed
    try:
        conn.send((func(*args), None))
    except BaseException:  # noqa: BLE001 - report anything, including KeyboardInterrupt, to the parent
        conn.send((None, traceback.format_exc()))
    finally:
        conn.close()


def _kill(process: multiprocessing.Process) -> None:
    """The process and everything it started (its process group); on Windows, the process only."""
    if process.pid is None:
        return
    try:
        if hasattr(os, "killpg"):
            os.killpg(process.pid, signal.SIGKILL)
        else:
            process.kill()
    except (ProcessLookupError, PermissionError):
        pass
    process.join(5)


def run(func: Callable, tasks: list[tuple], jobs: int, deadline: Callable[[int], float]) -> Iterator[Outcome]:
    """`func(*task)` for every task, `jobs` at once, each in a fresh process given `deadline(index)` real
    seconds. Yields an Outcome per task as it finishes (any order). Ctrl-C kills every running game."""
    context = multiprocessing.get_context("spawn")
    waiting = list(enumerate(tasks))
    running: dict[Connection, tuple[int, multiprocessing.Process, float]] = {}
    try:
        while waiting or running:
            while waiting and len(running) < jobs:
                index, task = waiting.pop(0)
                parent, child = context.Pipe(duplex=False)
                process = context.Process(target=_child, args=(child, func, task), daemon=False)
                process.start()
                child.close()
                running[parent] = (index, process, time.monotonic())
            for conn in wait(list(running), timeout=1.0):
                index, process, started = running.pop(conn)
                try:
                    result, error = conn.recv()
                except EOFError:
                    process.join(5)
                    result, error = None, f"worker died (exit code {process.exitcode})"
                conn.close()
                process.join(5)
                yield Outcome(index, result, error, time.monotonic() - started)
            now = time.monotonic()
            for conn, (index, process, started) in list(running.items()):
                if now - started > deadline(index):
                    _kill(process)
                    del running[conn]
                    conn.close()
                    yield Outcome(
                        index, None, f"no result after {deadline(index):.0f}s real time (killed)", now - started
                    )
    finally:
        for _, process, _ in running.values():
            _kill(process)
