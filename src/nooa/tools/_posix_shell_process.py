# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Keep a shell's PID reserved until its original process group is cleaned up."""

from __future__ import annotations

import asyncio
import contextlib
import os
import selectors
import signal
import subprocess
import sys
import time
import weakref
from pathlib import Path

_READY_PREFIX = b"__NOOA_SHELL_PID__:"
_controls: weakref.WeakSet[_ControlPipe] = weakref.WeakSet()


class _ControlPipe:
    def __init__(self, fd: int) -> None:
        self.fd: int | None = fd
        os.set_blocking(fd, False)
        _controls.add(self)

    def send(self, signum: int) -> None:
        if self.fd is not None:
            try:
                os.write(self.fd, bytes([signum]))
            except OSError:
                self.close()

    def close(self) -> None:
        fd, self.fd = self.fd, None
        if fd is not None:
            with contextlib.suppress(OSError):
                os.close(fd)


def _close_inherited_controls() -> None:
    for control in list(_controls):
        control.close()


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_close_inherited_controls)


class PosixShellProcess:
    """Subprocess-style shell handle; signals travel through an owned pipe."""

    def __init__(self, supervisor: asyncio.subprocess.Process, control: _ControlPipe) -> None:
        self._supervisor = supervisor
        self._control = control
        self._shell_pid: int | None = None

    @classmethod
    async def start(cls, cwd: Path, env: dict[str, str], control_fd: int) -> PosixShellProcess:
        if os.name != "posix":
            raise OSError("POSIX shell processes require a POSIX platform")
        read_fd, write_fd = os.pipe()
        control = _ControlPipe(write_fd)
        startup = asyncio.create_task(
            asyncio.create_subprocess_exec(
                sys.executable,
                "-I",
                "-S",
                str(Path(__file__).resolve()),
                str(read_fd),
                str(control_fd),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(cwd),
                env=env,
                start_new_session=True,
                pass_fds=(read_fd, control_fd),
            )
        )
        try:
            try:
                supervisor = await asyncio.shield(startup)
            except BaseException:
                control.close()
                with contextlib.suppress(BaseException):
                    supervisor = await startup
                    await asyncio.wait_for(supervisor.wait(), timeout=5)
                raise
            return cls(supervisor, control)
        finally:
            os.close(read_fd)

    async def ready(self, reader: asyncio.StreamReader) -> None:
        line = await asyncio.wait_for(reader.readline(), timeout=10)
        if not line.startswith(_READY_PREFIX):
            raise RuntimeError("POSIX shell supervisor exited before the shell became ready")
        self._shell_pid = int(line[len(_READY_PREFIX) :])

    @property
    def pid(self) -> int:
        return self._shell_pid if self._shell_pid is not None else self._supervisor.pid

    @property
    def returncode(self) -> int | None:
        return self._supervisor.returncode

    @property
    def stdin(self) -> asyncio.StreamWriter | None:
        return self._supervisor.stdin

    @property
    def stdout(self) -> asyncio.StreamReader | None:
        return self._supervisor.stdout

    @property
    def stderr(self) -> asyncio.StreamReader | None:
        return self._supervisor.stderr

    async def wait(self) -> int:
        return await self._supervisor.wait()

    def send_signal(self, signum: int) -> None:
        self._control.send(signum)

    def terminate(self) -> None:
        self.send_signal(signal.SIGTERM)

    def kill(self) -> None:
        self.send_signal(signal.SIGKILL)

    def close_control(self) -> None:
        self._control.close()

    def __del__(self) -> None:
        with contextlib.suppress(Exception):
            self._control.close()


def _supervise(lifeline_fd: int, control_fd: int) -> int:
    wake_read, wake_write = os.pipe()
    os.set_blocking(wake_read, False)
    os.set_blocking(wake_write, False)
    previous_wakeup = signal.set_wakeup_fd(wake_write)
    child_changed = False
    pending_signals: list[int] = []

    def notify(signum: int, _frame: object) -> None:
        nonlocal child_changed
        if signum == signal.SIGCHLD:
            child_changed = True
        else:
            pending_signals.append(signum)

    for signum in (signal.SIGCHLD, signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(signum, notify)

    child: subprocess.Popen[bytes] | None = None
    reaped = False
    selector = selectors.DefaultSelector()
    try:
        selector.register(lifeline_fd, selectors.EVENT_READ)
        selector.register(wake_read, selectors.EVENT_READ)
        child = subprocess.Popen(
            ["/bin/bash", "--norc", "--noprofile"],
            stdin=sys.stdin.buffer,
            stdout=sys.stdout.buffer,
            stderr=sys.stderr.buffer,
            start_new_session=True,
            pass_fds=(control_fd,),
        )
        os.write(control_fd, _READY_PREFIX + str(child.pid).encode("ascii") + b"\n")
        os.close(control_fd)
        control_fd = -1
        deadline: float | None = None
        stop = False
        while not stop and not child_changed:
            signals_to_send, pending_signals = pending_signals, []
            for signum in signals_to_send:
                if signum == signal.SIGKILL:
                    stop = True
                    break
                if signum in (signal.SIGTERM, signal.SIGHUP):
                    with contextlib.suppress(ProcessLookupError):
                        os.killpg(child.pid, signal.SIGTERM)
                    if deadline is None:
                        deadline = time.monotonic() + 3
                else:
                    with contextlib.suppress(ProcessLookupError):
                        os.kill(child.pid, signum)
            if stop or child_changed:
                break
            remaining = None if deadline is None else max(0, deadline - time.monotonic())
            if remaining == 0:
                break
            readable = {key.fd for key, _ in selector.select(remaining)}
            if wake_read in readable:
                with contextlib.suppress(BlockingIOError):
                    os.read(wake_read, 4096)
            if lifeline_fd in readable:
                commands = os.read(lifeline_fd, 4096)
                if not commands:
                    break
                pending_signals.extend(commands)

        # Do not poll()/wait() before this signal: even an exited leader must
        # stay unreaped so its numeric PID cannot identify an unrelated group.
        with contextlib.suppress(ProcessLookupError):
            os.killpg(child.pid, signal.SIGKILL)
        result = child.wait()
        reaped = True
        return result
    finally:
        if child is not None and not reaped:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(child.pid, signal.SIGKILL)
            child.wait()
        selector.close()
        signal.set_wakeup_fd(previous_wakeup)
        for fd in (lifeline_fd, control_fd, wake_read, wake_write):
            if fd >= 0:
                with contextlib.suppress(OSError):
                    os.close(fd)


if __name__ == "__main__":
    exit_code = _supervise(int(sys.argv[1]), int(sys.argv[2]))
    if exit_code < 0:
        import resource

        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
        signum = -exit_code
        if signum not in (signal.SIGKILL, signal.SIGSTOP):
            signal.signal(signum, signal.SIG_DFL)
        os.kill(os.getpid(), signum)
    raise SystemExit(exit_code)
