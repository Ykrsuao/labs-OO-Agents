# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Windows-only process ownership, recovery, and PowerShell compatibility."""

from __future__ import annotations

import asyncio
import ctypes
import json
import os
import shutil
import sys
from contextlib import ExitStack
from ctypes import wintypes

import pytest

from nooa.tools._powershell_session import PowerShellSession

pytestmark = pytest.mark.skipif(os.name != "nt", reason="native Windows process tests")


class ProcessHandle:
    """Hold the actual test process object, avoiding PID-reuse races during cleanup."""

    def __init__(self, pid):
        self.api = ctypes.WinDLL("kernel32", use_last_error=True)
        self.api.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        self.api.OpenProcess.restype = wintypes.HANDLE
        self.api.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        self.api.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
        self.api.CloseHandle.argtypes = [wintypes.HANDLE]
        self.handle = self.api.OpenProcess(0x1000 | 0x0001, False, pid)
        assert self.handle, ctypes.WinError(ctypes.get_last_error())

    def running(self):
        code = wintypes.DWORD()
        assert self.api.GetExitCodeProcess(self.handle, ctypes.byref(code))
        return code.value == 259

    def __enter__(self):
        return self

    def __exit__(self, *args):
        if self.running():
            self.api.TerminateProcess(self.handle, 1)
        self.api.CloseHandle(self.handle)


async def wait_for_file(path):
    async with asyncio.timeout(15):
        while not path.exists():
            await asyncio.sleep(0.02)
    return json.loads(path.read_text(encoding="utf-8"))


def process_tree_script(tmp_path):
    script = tmp_path / "process_tree.py"
    script.write_text(
        "import json, os, subprocess, sys, time\n"
        "from pathlib import Path\n"
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'], "
        "creationflags=subprocess.CREATE_NO_WINDOW)\n"
        "Path(sys.argv[1]).write_text(json.dumps([os.getpid(), child.pid]), encoding='utf-8')\n"
        "print('tree-ready', flush=True)\n"
        "time.sleep(120)\n",
        encoding="utf-8",
    )
    return script


@pytest.fixture(params=["pwsh", "powershell"])
async def session(request, tmp_path):
    executable = shutil.which(request.param)
    if not executable:
        pytest.skip(f"{request.param} is not installed")
    value = PowerShellSession(tmp_path, executable=executable)
    await value.start()
    try:
        yield value
    finally:
        await value.close()


async def test_powershell_preserves_variables_and_functions(session):
    await session.run("$number = 41; function Get-NooaValue { $number + 1 }")
    stdout, stderr, code = await session.run("Get-NooaValue")
    assert code == 0, stderr
    assert stdout.strip() == "42"


async def test_powershell_syntax_error_does_not_poison_the_session(session):
    before = session._start_count
    _, stderr, code, timed_out = await session.run_with_timeout_flag('Write-Output "')
    assert code != 0
    assert stderr
    assert not timed_out
    assert (await session.run("Write-Output recovered"))[0] == "recovered"
    assert session._start_count == before


async def test_powershell_raw_console_output_is_separate_from_control(session):
    stdout, stderr, code = await session.run(
        "[Console]::Out.Write('raw-out'); [Console]::Error.Write('raw-error')"
    )
    assert code == 0
    assert stdout == "raw-out"
    assert stderr == "raw-error"


async def test_powershell_output_preserves_unicode_at_frame_boundaries(session):
    stdout, stderr, code = await session.run(
        "Write-Output (('x' * 4095) + [char]::ConvertFromUtf32(0x1f680) + 'tail')"
    )
    assert code == 0, stderr
    assert stdout == "x" * 4095 + "\U0001f680tail"


async def test_powershell_slow_consumer_receives_all_raw_output(session):
    chunks = []
    async for kind, text in session.run_stream("[Console]::Out.Write(('x' * 1000000))"):
        if kind == "stdout":
            chunks.append(text)
            await asyncio.sleep(0.01)
    assert "".join(chunks) == "x" * 1_000_000


async def test_powershell_host_output_and_noninteractive_prompts(session):
    stdout, stderr, code = await session.run("Write-Host 'host-output'; Write-Warning 'warning'")
    assert code == 0, stderr
    assert stdout.count("host-output") == 1
    assert "warning" in stderr
    _, error, code, timed_out = await session.run_with_timeout_flag("Read-Host 'input'", timeout=2)
    assert code != 0
    assert not timed_out
    assert "interactive" in error.lower() or "stdin" in error.lower()


@pytest.mark.parametrize("code", [0, 17, 124])
async def test_powershell_exit_code_is_not_a_timeout(session, code):
    _, _, actual, timed_out = await session.run_with_timeout_flag(f"exit {code}")
    assert actual == code
    assert not timed_out
    assert (await session.run("Write-Output alive"))[0] == "alive"


async def test_powershell_cancellation_reaps_descendants(session, tmp_path):
    marker = tmp_path / "children.json"
    script = process_tree_script(tmp_path)
    task = asyncio.create_task(
        session.run(session.command([sys.executable, str(script), str(marker)]))
    )
    try:
        pids = await wait_for_file(marker)
        with ExitStack() as stack:
            processes = [stack.enter_context(ProcessHandle(pid)) for pid in pids]
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, timeout=10)
            assert all(not process.running() for process in processes)
        assert (await session.run("Write-Output recovered"))[0] == "recovered"
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_powershell_timeout_retains_completed_directory_and_environment(session, tmp_path):
    (tmp_path / "saved").mkdir()
    await session.run("Set-Location saved; $env:NOOA_SAVED = 'value'")
    _, stderr, code, timed_out = await session.run_with_timeout_flag(
        "$env:NOOA_SAVED = 'uncommitted'; Start-Sleep -Seconds 30", timeout=0.3
    )
    assert code == 124
    assert timed_out
    assert "reset" in stderr
    assert session.cwd == tmp_path / "saved"
    assert (await session.run("Write-Output $env:NOOA_SAVED"))[0] == "value"


async def test_powershell_closing_a_stream_reaps_its_process_tree(session, tmp_path):
    marker = tmp_path / "stream-children.json"
    script = process_tree_script(tmp_path)
    stream = session.run_stream(session.command([sys.executable, str(script), str(marker)]))
    try:
        first = await asyncio.wait_for(anext(stream), timeout=10)
        assert first[0] == "stdout"
        assert "tree-ready" in first[1]
        pids = await wait_for_file(marker)
        with ExitStack() as stack:
            processes = [stack.enter_context(ProcessHandle(pid)) for pid in pids]
            await asyncio.wait_for(stream.aclose(), timeout=10)
            assert all(not process.running() for process in processes)
    finally:
        await stream.aclose()


async def test_powershell_timeout_reaps_descendants(session, tmp_path):
    marker = tmp_path / "timeout-children.json"
    script = process_tree_script(tmp_path)
    task = asyncio.create_task(
        session.run_with_timeout_flag(
            session.command([sys.executable, str(script), str(marker)]), timeout=3
        )
    )
    try:
        pids = await wait_for_file(marker)
        with ExitStack() as stack:
            processes = [stack.enter_context(ProcessHandle(pid)) for pid in pids]
            _, _, code, timed_out = await asyncio.wait_for(task, timeout=10)
            assert code == 124
            assert timed_out
            assert all(not process.running() for process in processes)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_powershell_close_interrupts_a_running_command(session, tmp_path):
    marker = tmp_path / "closing-children.json"
    script = process_tree_script(tmp_path)
    task = asyncio.create_task(
        session.run(session.command([sys.executable, str(script), str(marker)]))
    )
    try:
        pids = await wait_for_file(marker)
        with ExitStack() as stack:
            processes = [stack.enter_context(ProcessHandle(pid)) for pid in pids]
            await session.close()
            with pytest.raises(RuntimeError, match="closed|exited"):
                await asyncio.wait_for(task, timeout=10)
            assert all(not process.running() for process in processes)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_powershell_large_command_is_not_limited_by_windows_argv(session):
    command = "$value = '" + "x" * 100_000 + "'; Write-Output $value.Length"
    stdout, stderr, code = await session.run(command)
    assert code == 0, stderr
    assert stdout == "100000"


async def test_powershell_recovers_after_its_idle_worker_exits(session):
    await session.run("$env:NOOA_RECOVERY = 'saved'")
    process = session._process
    assert process is not None
    process.kill()
    await asyncio.wait_for(process.wait(), timeout=5)
    stdout, stderr, code = await session.run("Write-Output $env:NOOA_RECOVERY")
    assert code == 0
    assert stdout == "saved"
    assert "reset" in stderr


async def test_powershell_init_command_runs_again_after_reset(session):
    session._init_command = "$env:NOOA_INIT = 'initialized'"
    await session.reset()
    assert (await session.run("Write-Output $env:NOOA_INIT"))[0] == "initialized"
    await session.run_with_timeout_flag("Start-Sleep -Seconds 30", timeout=0.1)
    assert (await session.run("Write-Output $env:NOOA_INIT"))[0] == "initialized"


async def test_powershell_startup_can_be_cancelled(session):
    await session.close()
    session._init_command = "Start-Sleep -Seconds 30"
    task = asyncio.create_task(session.start())
    try:
        async with asyncio.timeout(15):
            while not session._started:
                await asyncio.sleep(0.02)
        process = session._process
        assert process is not None
        with ProcessHandle(process.pid) as handle:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, timeout=10)
            assert not handle.running()
        assert session._process is None
        assert session._job is None
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        session._init_command = None
