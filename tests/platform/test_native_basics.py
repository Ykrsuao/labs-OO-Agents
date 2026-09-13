# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Native-platform contracts that do not require a shell or a model provider."""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import textwrap

import pytest
from pydantic import BaseModel

from nooa import Agent, strategy
from nooa.events import Task
from nooa.storage import sqlite
from nooa.strategies import PredictStrategy
from nooa.tools import ShellTools
from nooa.unifiedllm import FakeLLMClient, LLMResponse, ToolCall


class NativeStateAgent(Agent):
    value: int = 0

    async def total(self, values: list[int]) -> int:
        """Return the sum of the values."""
        ...


class NativeResult(BaseModel):
    total: int


class NativePredictAgent(Agent):
    @strategy(PredictStrategy())
    async def summarize(self, values: list[int]) -> NativeResult:
        """Return the total."""
        ...


def run_python(
    source: str,
    *args: str,
    env_overrides: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    env = {
        **os.environ,
        "PYTHONUTF8": "1",
        "PYTHONIOENCODING": "utf-8",
        "LITELLM_LOCAL_MODEL_COST_MAP": "True",
        **(env_overrides or {}),
    }
    return subprocess.run(
        [sys.executable, "-c", textwrap.dedent(source), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=45,
        env=env,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )


def test_native_public_imports():
    result = run_python(
        """
        import nooa
        import nooa_acp
        import nooa_bench
        import nooa_cli
        import nooa_memory
        print("native imports ready")
        """
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "native imports ready"


@pytest.mark.parametrize("command", ["nooa", "nooa-acp", "trace-explorer"])
def test_native_command_help(command):
    result = run_python(
        """
        import sys
        from importlib.metadata import entry_points
        command = sys.argv[1]
        sys.argv = [command, "--help"]
        entry_point = next(iter(entry_points(group="console_scripts", name=command)))
        entry_point.load()()
        """,
        command,
    )
    assert result.returncode == 0, result.stderr
    assert "usage:" in result.stdout.lower()


def test_debug_dumps_do_not_require_a_unix_signal(tmp_path, monkeypatch):
    from nooa.runtime import debug_handler

    monkeypatch.delattr(debug_handler.signal, "SIGUSR2", raising=False)
    monkeypatch.setattr(debug_handler, "_handler_installed", False)
    monkeypatch.setattr(debug_handler, "_dump_dir", tmp_path)
    debug_handler.install_debug_handler()
    output = io.StringIO()
    debug_handler.dump_debug_info(output)
    assert "Manual debug dump" in output.getvalue()
    assert not debug_handler._handler_installed


def test_session_lock_rejects_a_second_owner(tmp_path):
    path = tmp_path / "session.sqlite"
    with sqlite.SQLiteStorageManager(path):
        with pytest.raises(sqlite.SessionAlreadyActiveError) as exc:
            sqlite.SQLiteStorageManager(path)
        assert exc.value.owner_pid == os.getpid()
        assert exc.value.session_id == "session"


def test_session_lock_reports_owner_to_another_process(tmp_path):
    path = tmp_path / "session.sqlite"
    with sqlite.SQLiteStorageManager(path):
        result = run_python(
            """
            import json
            import sys
            from nooa.storage.sqlite import SessionAlreadyActiveError, SQLiteStorageManager
            try:
                with SQLiteStorageManager(sys.argv[1]):
                    raise AssertionError("opened an active session")
            except SessionAlreadyActiveError as exc:
                print(json.dumps({"pid": exc.owner_pid, "session": exc.session_id}))
            """,
            str(path),
        )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {"pid": os.getpid(), "session": "session"}
    with sqlite.SQLiteStorageManager(path):
        pass


def test_session_lock_is_released_when_its_process_exits(tmp_path):
    path = tmp_path / "session.sqlite"
    result = run_python(
        """
        import os
        import sys
        from nooa.storage.sqlite import SQLiteStorageManager
        storage = SQLiteStorageManager(sys.argv[1])
        os._exit(23)
        """,
        str(path),
    )
    assert result.returncode == 23, result.stderr
    with sqlite.SQLiteStorageManager(path):
        assert sqlite._read_lock_pid(str(path.with_suffix(".lock"))) == os.getpid()


def test_failed_owner_metadata_write_releases_the_lock(tmp_path, monkeypatch):
    path = tmp_path / "session.lock"

    def fail_write(fd, data):
        raise OSError("cannot write owner")

    with monkeypatch.context() as patch:
        patch.setattr(sqlite.os, "write", fail_write)
        with pytest.raises(OSError, match="cannot write owner"):
            sqlite._acquire_session_lock(str(path))

    fd = sqlite._acquire_session_lock(str(path))
    try:
        assert sqlite._read_lock_pid(str(path)) == os.getpid()
    finally:
        sqlite._release_session_lock(fd)


def test_session_deletion_preserves_the_lock_target(tmp_path):
    path = tmp_path / "session.sqlite"
    with sqlite.SQLiteStorageManager(path):
        with pytest.raises(sqlite.SessionAlreadyActiveError):
            sqlite.delete_sqlite_database(path)
        assert path.is_file()
    assert sqlite.delete_sqlite_database(path)
    assert not path.exists()
    assert path.with_suffix(".lock").is_file()


def test_file_tools_use_utf8_independently_of_system_encoding(tmp_path):
    result = run_python(
        r"""
        import asyncio
        import sys
        from pathlib import Path
        from nooa.tools import ShellTools

        async def main():
            shell = ShellTools(cwd=sys.argv[1])
            path = Path(sys.argv[1]) / "notes.txt"
            original = "\u4e2d\u6587 \U0001f680"
            changed = "\u7ed3\u679c \U0001f680"
            try:
                await shell.write_file("notes.txt", original)
                assert path.read_bytes() == original.encode("utf-8")
                assert (await shell.read("notes.txt")).text == original
                await shell.replace("notes.txt", original, changed)
                assert path.read_bytes() == changed.encode("utf-8")
                region = await shell.read("notes.txt")
                await shell.replace(region, original)
                assert path.read_bytes() == original.encode("utf-8")
            finally:
                await shell.close()

        asyncio.run(main())
        """,
        str(tmp_path),
        env_overrides={
            "PYTHONUTF8": "0",
            "PYTHONCOERCECLOCALE": "0",
            "LC_ALL": "C",
        },
    )
    assert result.returncode == 0, result.stderr


async def test_file_tools_accept_unicode_paths_and_line_ranges(tmp_path):
    shell = ShellTools(cwd=str(tmp_path))
    path = "\u4e2d\u6587 directory/notes.txt"
    try:
        await shell.write_file(path, "first\nsecond\nthird\n")
        region = await shell.read(path, lines=(2, 2))
        assert region.text == "second\n"
        await shell.replace(region, "updated")
        assert (await shell.read(path)).text == "first\nupdated\nthird\n"
    finally:
        await shell.close()


async def test_codeact_shares_locals_between_cells_without_a_shell():
    responses = [
        LLMResponse(
            tool_calls=[
                ToolCall(
                    id=f"cell-{index}",
                    name="execute_python",
                    arguments=json.dumps({"code": code}),
                )
            ],
            finish_reason="tool_calls",
        )
        for index, code in enumerate(["total = sum(values)", "return_result(total)"])
    ]
    agent = NativeStateAgent(llm=FakeLLMClient(responses))
    assert await agent.total([2, 3, 5]) == 10


async def test_predict_returns_a_typed_value():
    agent = NativePredictAgent(llm=FakeLLMClient.simple_message('{"total": 10}'))
    assert await agent.summarize([2, 3, 5]) == NativeResult(total=10)


def test_snapshot_roundtrip_with_a_unicode_database_path(tmp_path):
    path = tmp_path / "\u4f1a\u8bdd.sqlite"
    with sqlite.SQLiteStorageManager(path) as store:
        agent = NativeStateAgent(llm=FakeLLMClient(), storage=store)
        agent.value = 42
        tag = agent.event_manager.add(Task(prompt="saved task"))
        snapshot_id = store.save_snapshot(agent)
    with sqlite.SQLiteStorageManager(path) as store:
        restored = NativeStateAgent(llm=FakeLLMClient(), storage=store)
        store.restore_snapshot(snapshot_id, restored)
        assert restored.value == 42
        event = restored.event_manager[tag]
        assert isinstance(event, Task)
        assert event.prompt == "saved task"


def test_sandbox_capabilities_can_be_queried_on_every_platform():
    from nooa.runtime.sandbox.guards import probe_capabilities

    caps = probe_capabilities()
    assert isinstance(caps.rlimit, bool)
    if os.name == "nt":
        assert not caps.rlimit
        assert not caps.filesystem
        assert not caps.network


def test_sandbox_rejects_an_unavailable_start_method(monkeypatch):
    from nooa.runtime.sandbox import executor
    from nooa.runtime.sandbox.config import SandboxConfig
    from nooa.runtime.sandbox.errors import SandboxUnavailable

    monkeypatch.setattr(executor.mp, "get_all_start_methods", lambda: [])
    with pytest.raises(SandboxUnavailable, match="start method"):
        executor.SandboxedExecutor(
            object(),
            SandboxConfig(filesystem=False, network=True, require=False),
            cell_timeout=1.0,
        )
