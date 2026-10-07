"""Offline tests for the game supervisor (`scripts.supervise`): a game stuck on its connection is killed at
its deadline, with what it started, and the other games still come back. Real processes, no StarCraft II."""

import os
import socket
import subprocess
import sys
import time

import pytest

from scripts import supervise


def quick(value):
    return value * 2


def stuck_on_a_socket(pid_file):
    """Like python-sc2 waiting on a frozen game: blocked reading a connection that never answers, with a
    child process of its own (the game)."""
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(600)"])
    with open(pid_file, "w") as f:
        f.write(str(child.pid))
    a, _b = socket.socketpair()
    a.recv(1)  # never returns


def fails():
    raise ValueError("boom")


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    # A killed child not yet reaped by its (dead) parent is a zombie: gone for our purposes
    with open(f"/proc/{pid}/stat") as f:
        return f.read().split()[2] != "Z"


def game(kind, value):
    return stuck_on_a_socket(value) if kind == "stuck" else quick(value)


@pytest.mark.skipif(not hasattr(os, "killpg"), reason="process groups: Linux and macOS")
def test_a_stuck_game_is_killed_with_its_children_and_the_others_still_finish(tmp_path):
    pid_file = tmp_path / "child.pid"
    tasks = [("stuck", str(pid_file)), ("quick", 21), ("quick", 4)]
    started = time.monotonic()
    outcomes = {o.index: o for o in supervise.run(game, tasks, 2, lambda i: 3.0)}
    assert time.monotonic() - started < 20
    assert outcomes[0].result is None and "no result after 3s" in outcomes[0].error
    child = int(pid_file.read_text())
    gone_by = time.monotonic() + 3  # a kill signal lands a moment later
    while alive(child) and time.monotonic() < gone_by:
        time.sleep(0.05)
    assert not alive(child)  # the game it started went too
    assert outcomes[1].result == 42 and outcomes[2].result == 8


def test_results_come_back_as_games_finish_and_errors_carry_the_traceback():
    outcomes = list(supervise.run(quick, [(1,), (2,), (3,)], 3, lambda i: 30.0))
    assert sorted(o.result for o in outcomes) == [2, 4, 6]
    (failed,) = supervise.run(fails, [()], 1, lambda i: 30.0)
    assert failed.result is None and "ValueError: boom" in failed.error
