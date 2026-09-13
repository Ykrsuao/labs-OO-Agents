# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Feed exact text input to a child PowerShell without using its object pipeline."""

from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
from pathlib import Path


def main() -> int:
    path = Path(sys.argv[1])
    payload = json.loads(path.read_text(encoding="utf-8"))
    literal_path = "'" + str(path).replace("'", "''") + "'"
    source = f"""
$global:OutputEncoding = New-Object System.Text.UTF8Encoding($false)
[Console]::InputEncoding = $global:OutputEncoding
[Console]::OutputEncoding = $global:OutputEncoding
$payload = [IO.File]::ReadAllText({literal_path}, [Text.Encoding]::UTF8) | ConvertFrom-Json
$global:LASTEXITCODE = 0
try {{
    . ([scriptblock]::Create($payload.command))
    if (-not $?) {{
        if ($global:LASTEXITCODE) {{ exit $global:LASTEXITCODE }}
        exit 1
    }}
    exit $global:LASTEXITCODE
}} catch {{
    [Console]::Error.WriteLine($_.Exception.Message)
    exit 1
}}
"""
    result = subprocess.run(
        [
            payload["executable"],
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-EncodedCommand",
            base64.b64encode(source.encode("utf-16-le")).decode("ascii"),
        ],
        input=payload["stdin"].encode("utf-8"),
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
