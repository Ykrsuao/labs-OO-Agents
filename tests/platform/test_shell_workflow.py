# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Native shell contracts exercised through the public coding tools."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import sys

import pytest

from nooa.tools import ShellTools
from nooa.tools._shell_session import command_from_args


@pytest.fixture(autouse=True, params=["pwsh", "powershell"] if os.name == "nt" else ["bash"])
def native_backend(request, monkeypatch):
    if os.name == "nt":
        from nooa.tools._powershell_session import PowerShellSession

        executable = shutil.which(request.param)
        if not executable:
            pytest.skip(f"{request.param} is not installed")
        monkeypatch.setattr(PowerShellSession, "_program", lambda self: executable)


async def test_native_shell_executes_a_command(tmp_path):
    shell = ShellTools(cwd=str(tmp_path))
    command = "Write-Output 'native-ready'" if os.name == "nt" else "printf native-ready"
    try:
        result = await shell.run(command)
        assert result.success, result.stderr
        assert result.stdout.strip() == "native-ready"
        assert not result.timed_out
    finally:
        await shell.close()


async def test_native_shell_preserves_directory_and_environment(tmp_path):
    child = tmp_path / "child directory"
    child.mkdir()
    shell = ShellTools(cwd=str(tmp_path))
    command = (
        "Set-Location -LiteralPath 'child directory'; $env:NOOA_SHELL_TEST = 'saved'"
        if os.name == "nt"
        else "cd 'child directory'; export NOOA_SHELL_TEST=saved"
    )
    read_env = (
        "Write-Output $env:NOOA_SHELL_TEST" if os.name == "nt" else 'printf "$NOOA_SHELL_TEST"'
    )
    try:
        assert (await shell.run(command)).success
        assert shell.cwd == child
        assert (await shell.run(read_env)).stdout.strip() == "saved"
        await shell.write_file("result.txt", "saved")
        assert (child / "result.txt").read_text(encoding="utf-8") == "saved"
    finally:
        await shell.close()


async def test_native_shell_streams_before_the_command_finishes(tmp_path):
    shell = ShellTools(cwd=str(tmp_path))
    command = (
        "Write-Output 'first'; Start-Sleep -Seconds 3; Write-Output 'last'"
        if os.name == "nt"
        else "printf 'first\\n'; sleep 3; printf 'last\\n'"
    )
    try:
        await shell.run("Write-Output warm" if os.name == "nt" else "printf warm")
        stream = shell.run_stream(command, timeout=10)
        try:
            first = await asyncio.wait_for(anext(stream), timeout=2)
            assert first.kind == "stdout"
            assert "first" in first.text
            rest = [event async for event in stream]
            assert rest[-1].kind == "done"
            assert rest[-1].returncode == 0
            assert "last" in "".join(getattr(event, "text", "") for event in rest)
        finally:
            await stream.aclose()
    finally:
        await shell.close()


async def test_native_shell_serializes_concurrent_first_calls(tmp_path):
    shell = ShellTools(cwd=str(tmp_path))
    commands = (
        ["Write-Output one", "Write-Output two"]
        if os.name == "nt"
        else ["printf one", "printf two"]
    )
    try:
        results = await asyncio.wait_for(
            asyncio.gather(*(shell.run(command) for command in commands)), timeout=20
        )
        assert [result.stdout.strip() for result in results] == ["one", "two"]
        assert all(result.success for result in results)
        assert shell.session._start_count == 1
    finally:
        await shell.close()


async def test_native_shell_passes_exact_utf8_stdin(tmp_path):
    shell = ShellTools(cwd=str(tmp_path))
    payload = "\u4e2d\u6587 \U0001f680\nquotes ' \" and no final newline"
    command = command_from_args(
        shell.session, [sys.executable, "-c", "import sys; print(sys.stdin.buffer.read().hex())"]
    )
    try:
        result = await shell.run(command, stdin=payload)
        assert result.success, result.stderr
        assert result.stdout.strip() == payload.encode("utf-8").hex()
    finally:
        await shell.close()


async def test_native_shell_accepts_stdin_larger_than_windows_argv(tmp_path):
    shell = ShellTools(cwd=str(tmp_path))
    payload = "payload \u4e2d\u6587\n" * 10_000
    command = command_from_args(
        shell.session,
        [
            sys.executable,
            "-c",
            "import hashlib,sys; print(hashlib.sha256(sys.stdin.buffer.read()).hexdigest())",
        ],
    )
    try:
        result = await shell.run(command, stdin=payload)
        assert result.success, result.stderr
        assert result.stdout == hashlib.sha256(payload.encode("utf-8")).hexdigest()
    finally:
        await shell.close()


async def test_native_argument_vectors_preserve_quotes_and_backslashes(tmp_path):
    shell = ShellTools(cwd=str(tmp_path))
    values = [
        "",
        'space "quoted" value',
        "trailing space\\",
        "single'quote",
        "\u4e2d\u6587",
        "line\nbreak",
        "$env:PATH; Write-Output injected",
    ]
    command = command_from_args(
        shell.session,
        [sys.executable, "-c", "import json,sys; print(json.dumps(sys.argv[1:]))", *values],
    )
    try:
        result = await shell.run(command)
        assert result.success, result.stderr
        assert json.loads(result.stdout) == values
    finally:
        await shell.close()


@pytest.mark.parametrize("exit_code", [0, 7, 124])
async def test_native_shell_preserves_exit_codes_and_stderr(tmp_path, exit_code):
    shell = ShellTools(cwd=str(tmp_path))
    command = command_from_args(
        shell.session,
        [
            sys.executable,
            "-c",
            f"import sys; sys.stderr.write('diagnostic'); sys.exit({exit_code})",
        ],
    )
    try:
        result = await shell.run(command)
        assert result.returncode == exit_code, result.stderr
        assert "diagnostic" in result.stderr
        assert not result.timed_out
        assert (await shell.run("Write-Output ok" if os.name == "nt" else "printf ok")).success
    finally:
        await shell.close()


async def test_native_repo_tools_return_editable_anchors(tmp_path):
    from nooa_cli.tools.repo_tools import RepoTools

    root = tmp_path / "\u4ee3\u7801 with ' quote"
    root.mkdir()
    path = root / "example.py"
    path.write_text("def greet():\n    return 42\n\nresult = greet()\n", encoding="utf-8")
    shell = ShellTools(cwd=str(root))
    repo = RepoTools(root=root, session=shell.session)
    try:
        definitions = await repo.symbols(".", query="greet")
        assert definitions
        assert definitions[0].start == 1
        references = await repo.refs("greet")
        assert references
        assert references[0].start == 4
        await shell.replace(references[0], "result = 42")
        assert "result = 42" in path.read_text(encoding="utf-8")
    finally:
        await shell.close()


async def test_native_coding_loop_edits_tests_and_shows_diff(tmp_path):
    from nooa_cli.coding.activity import ActivityShellTools, FileEdit
    from nooa_cli.tools.repo_tools import RepoTools

    from nooa import Agent
    from nooa.runtime.event_manager import EventManager
    from nooa.unifiedllm import FakeLLMClient, LLMResponse, ToolCall

    (tmp_path / "calc.py").write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")
    (tmp_path / "test_calc.py").write_text(
        "import unittest\nfrom calc import add\n"
        "class AdditionTest(unittest.TestCase):\n"
        "    def test_add(self):\n        self.assertEqual(add(2, 3), 5)\n",
        encoding="utf-8",
    )
    base_shell = ShellTools(cwd=str(tmp_path))
    events = []
    manager = EventManager()
    manager.on("*", events.append)
    observed = ActivityShellTools(base_shell, manager)
    try:
        for args in (["git", "init", "-q"], ["git", "add", "calc.py", "test_calc.py"]):
            result = await observed.run(command_from_args(base_shell.session, args))
            assert result.success, result.stderr
        check_command = command_from_args(
            base_shell.session, [sys.executable, "-m", "unittest", "-q"]
        )
        diff_command = command_from_args(base_shell.session, ["git", "diff", "--", "calc.py"])
        code = (
            "content = await self.shell.read('calc.py')\n"
            "assert 'return a - b' in content.text\n"
            "await self.shell.replace('calc.py', 'return a - b', 'return a + b')\n"
            "definitions = await self.repo.symbols('calc.py', query='add')\n"
            "assert definitions\n"
            f"check = await self.shell.run({check_command!r})\n"
            "assert check.success, check.stderr\n"
            f"diff = await self.shell.run({diff_command!r})\n"
            "assert '+    return a + b' in diff.stdout\n"
            "return_result(True)"
        )
        llm = FakeLLMClient(
            [
                LLMResponse(
                    tool_calls=[
                        ToolCall(
                            id="native-coding",
                            name="execute_python",
                            arguments=json.dumps({"code": code}),
                        )
                    ],
                    finish_reason="tool_calls",
                )
            ]
        )

        class CodingWorkflow(Agent):
            async def implement(self) -> bool:
                """Repair the project and verify it with its tests."""
                ...

        agent = CodingWorkflow(llm=llm)
        agent.shell = observed
        agent.repo = RepoTools(root=tmp_path, session=base_shell.session)
        assert await agent.implement()
        assert any(isinstance(event, FileEdit) for event in events)
    finally:
        await observed.close()


async def test_native_monitor_streams_and_can_be_closed(tmp_path):
    from nooa.runtime.producers import monitor

    command = (
        "Write-Output first; Start-Sleep -Seconds 30"
        if os.name == "nt"
        else "printf 'first\\n'; sleep 30"
    )
    stream = monitor(command)
    try:
        first = await asyncio.wait_for(anext(stream), timeout=10)
        assert first == "first"
    finally:
        await asyncio.wait_for(stream.aclose(), timeout=10)


async def test_repo_tools_fallback_without_ripgrep(tmp_path, monkeypatch):
    from nooa_cli.tools import _tree_sitter_backend
    from nooa_cli.tools.repo_tools import RepoTools

    monkeypatch.setattr(_tree_sitter_backend, "TREE_SITTER_AVAILABLE", False)
    (tmp_path / "example.py").write_text(
        "def search_fn():\n    return 42\n\nresult = search_fn()\n", encoding="utf-8"
    )
    repo = RepoTools(root=tmp_path)
    repo._has_rg = False
    definitions = await repo.symbols(".", query="search_fn")
    assert definitions
    assert definitions[0].start == 1
    references = await repo.refs("search_fn")
    assert references
    assert references[0].start == 4


async def test_coding_agent_tells_the_model_which_shell_is_active(tmp_path):
    from nooa_cli.coding.agent import CodingAgent

    from nooa.unifiedllm import FakeLLMClient

    class EnvironmentAgent(CodingAgent):
        async def inspect_environment(self) -> str:
            """Inspect the available shell."""
            ...

    llm = FakeLLMClient.with_tool_call("return_result", {"result": "checked"})
    agent = EnvironmentAgent(llm=llm, cwd=tmp_path, libs_dir=tmp_path / "libs")
    try:
        assert await agent.inspect_environment() == "checked"
        messages = json.dumps(llm.last_messages)
        assert "shell_environment" in messages
        assert ("PowerShell" if os.name == "nt" else "Bash") in messages
    finally:
        await agent.close()
