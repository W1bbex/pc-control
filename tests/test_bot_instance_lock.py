"""Cross-process single-instance ownership without starting a bot or network client."""

import multiprocessing
import os
import queue
from pathlib import Path

import pytest


def _acquire_in_process(path, start, release, results, reads=None, first=None, slot=0):
    import vds_bot.bot as bot_mod

    real_path = Path(path)

    class CoordinatedPath:
        parent = real_path.parent

        def __fspath__(self):
            return str(real_path)

        def __str__(self):
            return str(real_path)

        def read_text(self, **kwargs):
            content = real_path.read_text(**kwargs)
            if reads is not None:
                reads.wait(timeout=10)
                if slot == 1:
                    assert first.wait(timeout=10)
            return content

        def unlink(self, **kwargs):
            return real_path.unlink(**kwargs)

    bot_mod.LOCK_PATH = CoordinatedPath()
    app = bot_mod.PcControlBotApp.__new__(bot_mod.PcControlBotApp)
    assert start.wait(timeout=10)
    try:
        app._acquire_lock()
    except SystemExit as error:
        results.put(("rejected", os.getpid(), error.code))
        return
    if first is not None and slot == 0:
        first.set()
    results.put(("acquired", os.getpid(), None))
    try:
        release.wait(timeout=15)
    finally:
        app._release_lock()


@pytest.mark.parametrize("contents", ["99999999", ""])
def test_stale_or_empty_lock_only_allows_one_process(tmp_path, contents):
    path = tmp_path / "bot.lock"
    path.write_text(contents, encoding="utf-8")
    ctx = multiprocessing.get_context("spawn")
    start, release, first = ctx.Event(), ctx.Event(), ctx.Event()
    results, reads = ctx.Queue(), ctx.Barrier(2)
    processes = [
        ctx.Process(target=_acquire_in_process, args=(str(path), start, release, results, reads, first, slot))
        for slot in range(2)
    ]
    try:
        for process in processes:
            process.start()
        start.set()
        outcomes = [results.get(timeout=12) for _ in processes]
        owners = [item for item in outcomes if item[0] == "acquired"]
        assert len(owners) == 1, outcomes
        assert path.read_text(encoding="utf-8").strip() == str(owners[0][1])
        assert sorted(item[0] for item in outcomes) == ["acquired", "rejected"]
    finally:
        release.set()
        for process in processes:
            process.join(timeout=5)
            if process.is_alive():
                process.terminate()
                process.join(timeout=5)
        results.close()


def test_owner_crash_releases_lock_for_next_process(tmp_path):
    path = tmp_path / "bot.lock"
    ctx = multiprocessing.get_context("spawn")
    owner_start, owner_release, owner_results = ctx.Event(), ctx.Event(), ctx.Queue()
    succ_start, succ_release, succ_results = ctx.Event(), ctx.Event(), ctx.Queue()
    owner = ctx.Process(target=_acquire_in_process, args=(str(path), owner_start, owner_release, owner_results))
    successor = None
    try:
        owner.start()
        owner_start.set()
        assert owner_results.get(timeout=12)[0] == "acquired"
        owner.terminate()
        owner.join(timeout=5)
        successor = ctx.Process(target=_acquire_in_process, args=(str(path), succ_start, succ_release, succ_results))
        successor.start()
        succ_start.set()
        status, pid, _ = succ_results.get(timeout=12)
        assert status == "acquired"
        assert path.read_text(encoding="utf-8").strip() == str(pid)
    finally:
        succ_release.set()
        for process in (owner, successor):
            if process is not None:
                process.join(timeout=5)
                if process.is_alive():
                    process.terminate()
                    process.join(timeout=5)
        owner_results.close()
        succ_results.close()

