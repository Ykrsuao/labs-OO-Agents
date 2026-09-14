# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""POSIX shell recovery and process ownership contracts."""

from __future__ import annotations

import asyncio
import os
import shlex
import sys
import textwrap
from unittest.mock import Mock

import pytest

from nooa.tools import ShellTools
from nooa.tools._bash_session import BashSession

pytestmark = pytest.mark.skipif(os.name == "nt", reason="POSIX shell lifecycle")


@pytest.fixture
async def background_child():
    connected = asyncio.get_running_loop().create_future()
    writers = []

    async def accept(reader, writer):
        writers.append(writer)
        if not connected.done():
            connected.set_result(reader)
        else:
            writer.close()

    server = await asyncio.start_server(accept, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    child_code = (
        "import signal,socket,sys;"
        "signal.signal(signal.SIGTERM,signal.SIG_IGN);"
        "connection=socket.create_connection(('127.0.0.1',int(sys.argv[1])));"
        "connection.sendall(b'ready\\n');"
        "connection.recv(1)"
    )
    command = shlex.join([sys.executable, "-I", "-S", "-c", child_code, str(port)])
    try:
        yield command, connected
    finally:
        connected.cancel()
        for writer in writers:
            writer.close()
            await writer.wait_closed()
        server.close()
        await server.wait_closed()


def test_concurrent_commands_remain_serialized_after_loop_restart(tmp_path):
    shell = ShellTools(cwd=tmp_path)

    async def run_round(prefix, *, close=False):
        labels = [f"{prefix}_{index}" for index in range(3)]
        try:
            results = await asyncio.wait_for(
                asyncio.gather(
                    *(shell.run(f"sleep 0.02; printf '%s' {label}") for label in labels)
                ),
                timeout=15,
            )
            assert [result.stdout for result in results] == labels
            assert all(result.returncode == 0 for result in results)
        finally:
            if close:
                await shell.close()

    try:
        asyncio.run(run_round("first"))
        asyncio.run(run_round("second", close=True))
    finally:
        asyncio.run(shell.close())


@pytest.mark.parametrize("streaming", [False, True])
async def test_file_tools_follow_cwd_after_command_timeout(tmp_path, streaming):
    child = tmp_path / "child"
    child.mkdir()
    parent_file = tmp_path / "result.txt"
    child_file = child / "result.txt"
    parent_file.write_text("parent original", encoding="utf-8")
    child_file.write_text("child original", encoding="utf-8")
    shell = ShellTools(cwd=tmp_path)
    try:
        if streaming:
            events = [event async for event in shell.run_stream("cd child; sleep 30", timeout=0.2)]
            result = events[-1]
        else:
            result = await shell.run("cd child; sleep 30", timeout=0.2)
        assert result.timed_out

        await shell.write_file("result.txt", "updated")

        assert child_file.read_text(encoding="utf-8") == "updated"
        assert parent_file.read_text(encoding="utf-8") == "parent original"
        assert shell.cwd == child
    finally:
        await shell.close()


async def test_close_does_not_signal_a_reused_shell_pid(tmp_path, monkeypatch):
    session = BashSession(cwd=tmp_path)
    try:
        await session.run("exit 0", timeout=5)
        process = session._process
        assert process is not None
        await process.wait()

        foreign_group = Mock(return_value=271828)
        signals = Mock()
        with monkeypatch.context() as context:
            context.setattr(os, "getpgid", foreign_group)
            context.setattr(os, "killpg", signals)
            await session.close()

        foreign_group.assert_not_called()
        signals.assert_not_called()
    finally:
        await session.close()


async def test_cancelled_startup_reaps_the_launched_process(tmp_path, monkeypatch):
    launched = asyncio.get_running_loop().create_future()
    release = asyncio.Event()
    create_process = asyncio.create_subprocess_exec

    async def delayed_launch(*args, **kwargs):
        process = await create_process(*args, **kwargs)
        launched.set_result(process)
        await release.wait()
        return process

    session = BashSession(cwd=tmp_path)
    process = None
    with monkeypatch.context() as context:
        context.setattr(asyncio, "create_subprocess_exec", delayed_launch)
        startup = asyncio.create_task(session.start())
        try:
            process = await asyncio.wait_for(asyncio.shield(launched), timeout=10)
            startup.cancel()
            await asyncio.sleep(0)
            release.set()

            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(startup, timeout=10)
            await asyncio.wait_for(process.wait(), timeout=5)
        finally:
            release.set()
            startup.cancel()
            await asyncio.gather(startup, return_exceptions=True)
            await session.close()
            if process is not None and process.returncode is None:
                process.kill()
                await asyncio.wait_for(process.wait(), timeout=5)

    try:
        assert await session.run("printf recovered") == ("recovered", "", 0)
    finally:
        await session.close()


async def test_shell_can_start_with_high_numbered_pipes(tmp_path, monkeypatch):
    import fcntl
    import resource

    if resource.getrlimit(resource.RLIMIT_NOFILE)[0] < 1100:
        pytest.skip("This process cannot open descriptors above select()'s 1024 limit")
    create_pipe = os.pipe

    def high_pipe():
        original = create_pipe()
        duplicated = []
        try:
            for fd in original:
                duplicated.append(fcntl.fcntl(fd, fcntl.F_DUPFD_CLOEXEC, 1024))
            return tuple(duplicated)
        except BaseException:
            for fd in duplicated:
                os.close(fd)
            raise
        finally:
            for fd in original:
                os.close(fd)

    with monkeypatch.context() as context:
        context.setattr(os, "pipe", high_pipe)
        async with BashSession(cwd=tmp_path) as session:
            assert await session.run("sleep 0.05; printf ready") == ("ready", "", 0)


@pytest.mark.parametrize("shell_exits_first", [False, True])
async def test_close_reaps_background_children_that_ignore_term(
    tmp_path, background_child, shell_exits_first
):
    command, connected = background_child
    session = BashSession(cwd=tmp_path)
    try:
        _, stderr, code = await session.run(command + " &")
        assert code == 0, stderr
        reader = await asyncio.wait_for(connected, timeout=10)
        assert await asyncio.wait_for(reader.readline(), timeout=5) == b"ready\n"

        if shell_exits_first:
            await session.run("exit 0", timeout=5)
            await asyncio.sleep(0.05)
        await session.close()

        assert await asyncio.wait_for(reader.read(), timeout=5) == b""
    finally:
        await session.close()


async def test_owner_exit_reaps_background_children(tmp_path, background_child):
    command, connected = background_child
    owner_code = textwrap.dedent(
        """
        import asyncio
        import sys
        from nooa.tools import ShellTools

        async def main():
            shell = ShellTools(cwd=sys.argv[1])
            result = await shell.run(sys.argv[2] + " &")
            assert result.returncode == 0, result.stderr
            print("ready", flush=True)
            await asyncio.Event().wait()

        asyncio.run(main())
        """
    )
    owner = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        owner_code,
        str(tmp_path),
        command,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
    )
    try:
        assert await asyncio.wait_for(owner.stdout.readline(), timeout=20) == b"ready\n"
        reader = await asyncio.wait_for(connected, timeout=10)
        assert await asyncio.wait_for(reader.readline(), timeout=5) == b"ready\n"

        owner.kill()
        await asyncio.wait_for(owner.wait(), timeout=5)

        assert await asyncio.wait_for(reader.read(), timeout=5) == b""
    finally:
        if owner.returncode is None:
            owner.kill()
        await asyncio.wait_for(owner.communicate(), timeout=5)
