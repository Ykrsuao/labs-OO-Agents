# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""The command-session interface shared by native shell implementations."""

from __future__ import annotations

import os
import shlex
from collections.abc import AsyncGenerator
from pathlib import Path
from typing import Protocol


class ShellSession(Protocol):
    _started: bool

    @property
    def cwd(self) -> Path: ...

    async def start(self) -> None: ...

    async def run(self, command: str, timeout: float = 30) -> tuple[str, str, int]: ...

    async def run_with_timeout_flag(
        self, command: str, timeout: float = 30
    ) -> tuple[str, str, int, bool]: ...

    def run_stream(
        self, command: str, timeout: float = 30
    ) -> AsyncGenerator[tuple[str, str], None]: ...

    async def close(self) -> None: ...


def create_shell_session(cwd: str | Path, init_command: str | None = None) -> ShellSession:
    if os.name == "nt":
        from nooa.tools._powershell_session import PowerShellSession

        return PowerShellSession(cwd, init_command)
    from nooa.tools._bash_session import BashSession

    return BashSession(cwd, init_command)


def command_from_args(session: ShellSession, args: list[str]) -> str:
    if getattr(session, "shell_name", "bash") == "powershell":
        from nooa.tools._powershell_session import powershell_command_from_args

        return powershell_command_from_args(args)
    return shlex.join(args)
