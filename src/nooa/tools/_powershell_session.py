# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Persistent native PowerShell with a separate control channel and job ownership."""

from __future__ import annotations

import asyncio
import base64
import codecs
import contextlib
import json
import os
import secrets
import shutil
import subprocess
import sys
import tempfile
from collections.abc import AsyncGenerator
from pathlib import Path
from typing import Any

from nooa.tools._bash_session import MAX_OUTPUT_CHARS


def quote_powershell(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def powershell_command_from_args(args: list[str]) -> str:
    """Invoke an external program without PowerShell 5.1's lossy argv conversion."""
    payload = base64.b64encode(json.dumps(args).encode("utf-8")).decode("ascii")
    bootstrap = (
        "import base64,json,subprocess,sys;"
        "sys.exit(subprocess.call(json.loads(base64.b64decode(sys.argv[1])),"
        "stdin=sys.stdin,stdout=sys.stdout,stderr=sys.stderr,"
        "creationflags=subprocess.CREATE_NO_WINDOW))"
    )
    return "& " + " ".join(
        quote_powershell(arg) for arg in [sys.executable, "-c", bootstrap, payload]
    )


class PowerShellSession:
    """One serialized PowerShell runspace; cancellation resets its process tree."""

    shell_name = "powershell"

    def __init__(
        self,
        cwd: str | Path = ".",
        init_command: str | None = None,
        *,
        executable: str | None = None,
    ) -> None:
        self._cwd = Path(cwd).resolve()
        self._init_command = init_command
        self._executable = executable
        self._environment = dict(os.environ)
        self._process: asyncio.subprocess.Process | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._server: asyncio.AbstractServer | None = None
        self._tasks: list[asyncio.Task[Any]] = []
        self._job: Any = None
        self._started = False
        self._started_on_loop: asyncio.AbstractEventLoop | None = None
        self._lock: asyncio.Lock | None = None
        self._active: asyncio.Queue[dict[str, Any]] | None = None
        self._active_id: str | None = None
        self._startup_stderr = ""
        self._notice = ""
        self._start_count = 0
        self.version = ""

    @property
    def cwd(self) -> Path:
        return self._cwd

    def command(self, args: list[str]) -> str:
        return powershell_command_from_args(args)

    def _program(self) -> str:
        program = self._executable or shutil.which("pwsh") or shutil.which("powershell")
        if not program:
            raise FileNotFoundError(
                "Native Windows commands require PowerShell (pwsh or powershell.exe)."
            )
        return program

    async def start(self) -> None:
        loop = asyncio.get_running_loop()
        if (
            self._started
            and self._started_on_loop is loop
            and self._process is not None
            and self._process.returncode is None
        ):
            return
        if self._started_on_loop is not None:
            reason = "event loop change" if self._started_on_loop is not loop else "worker exit"
            await self._abort(reason)
        if os.name != "nt":
            raise OSError("PowerShellSession requires native Windows")
        from nooa.tools._windows_process import WindowsJob

        token = secrets.token_hex(32)
        ready: asyncio.Future[tuple[asyncio.StreamReader, asyncio.StreamWriter]] = (
            loop.create_future()
        )

        async def accept(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            accepted = False
            try:
                raw = await asyncio.wait_for(reader.readline(), timeout=5)
                frame = json.loads(raw)
                if (
                    frame.get("kind") == "ready"
                    and secrets.compare_digest(str(frame.get("token", "")), token)
                    and not ready.done()
                ):
                    self.version = str(frame.get("version", ""))
                    ready.set_result((reader, writer))
                    accepted = True
            except (OSError, ValueError, TimeoutError):
                pass
            finally:
                if not accepted:
                    writer.close()

        server = await asyncio.start_server(accept, "127.0.0.1", 0, limit=2**20)
        self._server = server
        self._started_on_loop = loop
        self._startup_stderr = ""
        try:
            self._job = WindowsJob()
            port = server.sockets[0].getsockname()[1]
            self._process = await asyncio.create_subprocess_exec(
                self._program(),
                "-NoLogo",
                "-NoProfile",
                "-NonInteractive",
                "-File",
                str(Path(__file__).with_name("_powershell_worker.ps1")),
                "-Port",
                str(port),
                "-Token",
                token,
                "-WorkingDirectory",
                str(self._cwd),
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(self._cwd),
                env=self._environment,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            self._job.assign(self._process.pid)
            for kind in ("stdout", "stderr"):
                stream = getattr(self._process, kind)
                self._tasks.append(asyncio.create_task(self._read_raw(stream, kind, self._process)))
            exited = asyncio.create_task(self._process.wait())
            try:
                done, _ = await asyncio.wait(
                    [ready, exited], timeout=30, return_when=asyncio.FIRST_COMPLETED
                )
                if ready not in done:
                    await asyncio.sleep(0)
                    raise RuntimeError(
                        "PowerShell worker did not become ready. " + self._startup_stderr[-4000:]
                    )
                reader, self._writer = ready.result()
            finally:
                exited.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await exited
            self._tasks.append(asyncio.create_task(self._read_control(reader, self._process)))
            self._started = True
            self._start_count += 1
            if self._init_command:
                notice = self._notice
                result = await self._collect(self._init_command, 60)
                if result[2] != 0:
                    raise RuntimeError("PowerShell init_command failed: " + result[1])
                self._notice = notice
        except BaseException:
            if ready.done() and not ready.cancelled():
                _, connection = ready.result()
                connection.close()
            else:
                ready.cancel()
            await self.close()
            raise
        finally:
            server.close()
            # wait_closed also waits for accepted clients on current Python
            # releases. The session owns that connection until close().

    async def _read_raw(
        self, stream: asyncio.StreamReader, kind: str, process: asyncio.subprocess.Process
    ) -> None:
        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        while raw := await stream.read(4096):
            if self._process is not process:
                return
            text = decoder.decode(raw)
            if self._active is not None and self._process is process:
                await self._active.put({"kind": kind, "text": text})
            else:
                self._startup_stderr = (self._startup_stderr + text)[-8000:]

    async def _read_control(
        self, reader: asyncio.StreamReader, process: asyncio.subprocess.Process
    ) -> None:
        try:
            while raw := await reader.readline():
                frame = json.loads(raw)
                if (
                    self._active is not None
                    and self._process is process
                    and frame.get("id") == self._active_id
                ):
                    await self._active.put(frame)
        except (OSError, ValueError) as exc:
            if self._active is not None and self._process is process:
                await self._active.put({"kind": "broken", "text": str(exc)})
        finally:
            if self._process is process:
                self._started = False
                if self._active is not None:
                    await self._active.put({"kind": "broken", "text": "PowerShell worker exited"})

    def _current_lock(self) -> asyncio.Lock:
        loop = asyncio.get_running_loop()
        if self._lock is None or getattr(self._lock, "_loop", None) not in (None, loop):
            self._lock = asyncio.Lock()
        return self._lock

    async def _execute(
        self, command: str, timeout: float | None
    ) -> AsyncGenerator[tuple[str, str], None]:
        if timeout is not None and timeout <= 0:
            raise ValueError("timeout must be positive or None")
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=128)
        self._active = queue
        self._active_id = secrets.token_hex(12)
        completed = False
        try:
            if self._notice:
                notice, self._notice = self._notice, ""
                yield "stderr", notice
            if self._writer is None:
                raise RuntimeError("PowerShell session is not started")
            self._writer.write(
                (
                    json.dumps({"kind": "execute", "id": self._active_id, "command": command})
                    + "\n"
                ).encode("utf-8")
            )
            await self._writer.drain()
            loop = asyncio.get_running_loop()
            deadline = None if timeout is None else loop.time() + timeout
            while True:
                if not self._started:
                    raise RuntimeError("PowerShell session closed")
                remaining = None if deadline is None else max(0, deadline - loop.time())
                frame = await asyncio.wait_for(queue.get(), remaining)
                kind = frame.get("kind")
                if kind in ("stdout", "stderr"):
                    yield kind, str(frame.get("text", ""))
                elif kind == "done":
                    # Raw Console writes use OS pipes rather than PowerShell streams.
                    # Let those readers deliver bytes written before the control frame.
                    drain_until = loop.time() + 0.05
                    while (remaining := drain_until - loop.time()) > 0:
                        try:
                            extra = await asyncio.wait_for(queue.get(), remaining)
                        except TimeoutError:
                            break
                        if extra.get("kind") in ("stdout", "stderr"):
                            yield extra["kind"], str(extra.get("text", ""))
                    self._cwd = Path(frame["cwd"])
                    self._environment = dict(frame["environment"])
                    completed = True
                    yield "__done__", f"{int(frame['exit_code'])},0"
                    break
                elif kind == "broken":
                    raise RuntimeError(str(frame.get("text", "PowerShell worker exited")))
        except TimeoutError:
            await self._abort("timeout")
            completed = True
            yield "stderr", f"Command timed out after {timeout} seconds.\n{self._notice}"
            self._notice = ""
            yield "__done__", "124,1"
        finally:
            self._active = None
            self._active_id = None
            if not completed:
                await self._abort("interruption")

    async def _collect(self, command: str, timeout: float | None) -> tuple[str, str, int, bool]:
        chunks: dict[str, list[str]] = {"stdout": [], "stderr": []}
        lengths = {"stdout": 0, "stderr": 0}
        truncated: set[str] = set()
        code, timed_out = -1, False
        async for kind, text in self._execute(command, timeout):
            if kind == "__done__":
                raw_code, raw_timeout = text.split(",")
                code, timed_out = int(raw_code), bool(int(raw_timeout))
            else:
                available = max(0, MAX_OUTPUT_CHARS - lengths[kind])
                if available:
                    chunks[kind].append(text[:available])
                lengths[kind] += min(len(text), available)
                if len(text) > available:
                    truncated.add(kind)
        for kind in truncated:
            chunks[kind].append(f"\n... ({kind} truncated)")
        return "".join(chunks["stdout"]).strip(), "".join(chunks["stderr"]).strip(), code, timed_out

    async def run(self, command: str, timeout: float | None = 30) -> tuple[str, str, int]:
        stdout, stderr, code, _ = await self.run_with_timeout_flag(command, timeout)
        return stdout, stderr, code

    async def run_with_timeout_flag(
        self, command: str, timeout: float | None = 30, *, stdin: str | None = None
    ) -> tuple[str, str, int, bool]:
        async with self._current_lock():
            await self.start()
            if stdin is None:
                return await self._collect(command, timeout)
            # A separate process receives exact UTF-8 stdin bytes. PowerShell's
            # object pipeline would append newlines and re-encode the payload.
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", suffix=".json", delete=False
            ) as payload:
                json.dump(
                    {"command": command, "stdin": stdin, "executable": self._program()}, payload
                )
                path = Path(payload.name)
            try:
                return await self._collect(
                    self.command(
                        [
                            sys.executable,
                            str(Path(__file__).with_name("_powershell_stdin.py")),
                            str(path),
                        ]
                    ),
                    timeout,
                )
            finally:
                path.unlink(missing_ok=True)

    async def run_stream(
        self, command: str, timeout: float | None = 30
    ) -> AsyncGenerator[tuple[str, str], None]:
        async with self._current_lock():
            await self.start()
            stream = self._execute(command, timeout)
            try:
                async for event in stream:
                    yield event
            finally:
                await stream.aclose()

    async def _abort(self, reason: str) -> None:
        self._active = None
        await self.close()
        self._notice = (
            f"PowerShell session reset after {reason}; last completed working directory and "
            "environment are retained. PowerShell variables, functions, and background jobs "
            "must be recreated.\n"
        )

    async def reset(self) -> None:
        await self.close()
        await self.start()

    async def close(self) -> None:
        active, self._active = self._active, None
        if active is not None:
            with contextlib.suppress(asyncio.QueueFull):
                active.put_nowait({"kind": "broken", "text": "PowerShell session closed"})
        if self._job is not None:
            self._job.close()
            self._job = None
        writer, self._writer = self._writer, None
        server, self._server = self._server, None
        process, self._process = self._process, None
        same_loop = self._started_on_loop is asyncio.get_running_loop()
        self._started = False
        self._started_on_loop = None
        tasks, self._tasks = self._tasks, []
        for task in tasks:
            task.cancel()
        if writer is not None:
            with contextlib.suppress(Exception):
                writer.close()
        if server is not None:
            server.close()
        if process is not None:
            if process.returncode is None:
                with contextlib.suppress(ProcessLookupError):
                    process.kill()
            if same_loop:
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(process.wait(), timeout=5)
        if same_loop:
            if writer is not None:
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(writer.wait_closed(), timeout=5)
            if server is not None:
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(server.wait_closed(), timeout=5)
            await asyncio.gather(*tasks, return_exceptions=True)

    def __del__(self) -> None:
        if self._job is not None:
            self._job.close()
