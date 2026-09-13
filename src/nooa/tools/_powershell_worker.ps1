# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
param(
    [Parameter(Mandatory = $true)][int]$Port,
    [Parameter(Mandatory = $true)][string]$Token,
    [Parameter(Mandatory = $true)][string]$WorkingDirectory
)

$ErrorActionPreference = 'Stop'
$utf8 = New-Object System.Text.UTF8Encoding($false)
[Console]::OutputEncoding = $utf8
[Console]::InputEncoding = $utf8
$client = New-Object System.Net.Sockets.TcpClient
$client.Connect('127.0.0.1', $Port)
$network = $client.GetStream()
$reader = New-Object System.IO.StreamReader($network, $utf8)
$writer = New-Object System.IO.StreamWriter($network, $utf8)
$writer.AutoFlush = $true

function Send-Frame($Frame) {
    $writer.WriteLine(($Frame | ConvertTo-Json -Compress -Depth 8))
}

function Send-Text([string]$Kind, [string]$Text, [string]$Id) {
    for ($offset = 0; $offset -lt $Text.Length;) {
        $length = [Math]::Min(4096, $Text.Length - $offset)
        if (($offset + $length) -lt $Text.Length -and
            [char]::IsHighSurrogate($Text[$offset + $length - 1])) {
            $length--
        }
        Send-Frame @{
            kind = $Kind
            id = $Id
            text = $Text.Substring($offset, $length)
        }
        $offset += $length
    }
}

function Drain-Streams($State) {
    foreach ($item in $State.output.ReadAll()) {
        Send-Text 'stdout' ([string]$item + "`n") $State.id
    }
    foreach ($item in $State.pipeline.Streams.Error.ReadAll()) {
        if ($item.FullyQualifiedErrorId -notlike 'NativeCommandError*') {
            $State.failed = $true
        }
        Send-Text 'stderr' ([string]$item + "`n") $State.id
    }
    foreach ($item in $State.pipeline.Streams.Warning.ReadAll()) {
        Send-Text 'stderr' ("WARNING: " + [string]$item + "`n") $State.id
    }
    foreach ($item in $State.pipeline.Streams.Information.ReadAll()) {
        Send-Text 'stdout' ([string]$item.MessageData + "`n") $State.id
    }
    foreach ($item in $State.pipeline.Streams.Verbose.ReadAll()) {
        Send-Text 'stdout' ([string]$item + "`n") $State.id
    }
    foreach ($item in $State.pipeline.Streams.Debug.ReadAll()) {
        Send-Text 'stdout' ([string]$item + "`n") $State.id
    }
    $State.pipeline.Streams.Progress.Clear()
}

# Capture `exit N` without exiting the protocol owner. Interactive prompts
# are unavailable; normal output is collected from the PowerShell streams.
Add-Type -TypeDefinition @'
using System;
using System.Collections.Generic;
using System.Collections.ObjectModel;
using System.Globalization;
using System.Management.Automation;
using System.Management.Automation.Host;
using System.Security;

public sealed class NooaShellHost : PSHost {
    private readonly Guid id = Guid.NewGuid();
    private readonly PSHostUserInterface ui = new NooaShellUI();
    public int? ExitCode { get; set; }
    public override Guid InstanceId { get { return id; } }
    public override string Name { get { return "NOOA"; } }
    public override Version Version { get { return new Version(1, 0); } }
    public override CultureInfo CurrentCulture { get { return CultureInfo.CurrentCulture; } }
    public override CultureInfo CurrentUICulture { get { return CultureInfo.CurrentUICulture; } }
    public override PSHostUserInterface UI { get { return ui; } }
    public override void SetShouldExit(int code) { ExitCode = code; }
    public override void EnterNestedPrompt() { throw new NotSupportedException("Interactive prompts are unavailable."); }
    public override void ExitNestedPrompt() { }
    public override void NotifyBeginApplication() { }
    public override void NotifyEndApplication() { }
}
public sealed class NooaShellUI : PSHostUserInterface {
    public override PSHostRawUserInterface RawUI { get { return null; } }
    public override string ReadLine() { throw new NotSupportedException("Use stdin= instead of an interactive prompt."); }
    public override SecureString ReadLineAsSecureString() { throw new NotSupportedException("Interactive prompts are unavailable."); }
    public override void Write(string value) { }
    public override void Write(ConsoleColor foreground, ConsoleColor background, string value) { }
    public override void WriteLine(string value) { }
    public override void WriteErrorLine(string value) { }
    public override void WriteDebugLine(string value) { }
    public override void WriteProgress(long source, ProgressRecord value) { }
    public override void WriteVerboseLine(string value) { }
    public override void WriteWarningLine(string value) { }
    public override Dictionary<string, PSObject> Prompt(string caption, string message, Collection<FieldDescription> fields) {
        throw new NotSupportedException("Interactive prompts are unavailable.");
    }
    public override PSCredential PromptForCredential(string caption, string message, string user, string target) {
        throw new NotSupportedException("Interactive prompts are unavailable.");
    }
    public override PSCredential PromptForCredential(string caption, string message, string user, string target, PSCredentialTypes types, PSCredentialUIOptions options) {
        throw new NotSupportedException("Interactive prompts are unavailable.");
    }
    public override int PromptForChoice(string caption, string message, Collection<ChoiceDescription> choices, int defaultChoice) {
        throw new NotSupportedException("Interactive prompts are unavailable.");
    }
}
'@
$shellHost = New-Object NooaShellHost
$runspace = [System.Management.Automation.Runspaces.RunspaceFactory]::CreateRunspace($shellHost)
$runspace.Open()
$null = $runspace.SessionStateProxy.Path.SetLocation($WorkingDirectory)
$setup = [PowerShell]::Create()
$setup.Runspace = $runspace
$null = $setup.AddScript(@'
$global:OutputEncoding = New-Object System.Text.UTF8Encoding($false)
[Console]::OutputEncoding = $global:OutputEncoding
$global:ProgressPreference = 'SilentlyContinue'
$global:ErrorActionPreference = 'Continue'
if ($PSVersionTable.PSVersion.Major -ge 7) {
    $global:PSNativeCommandArgumentPassing = 'Standard'
    $global:PSNativeCommandUseErrorActionPreference = $false
}
'@, $false)
$null = $setup.Invoke()
$setup.Dispose()

# Reflection keeps the same asynchronous streaming API usable on PowerShell 5.1.
$beginMethod = [PowerShell].GetMethods() | Where-Object {
    $_.Name -eq 'BeginInvoke' -and $_.IsGenericMethodDefinition -and
    $_.GetGenericArguments().Length -eq 2 -and $_.GetParameters().Length -eq 2
} | Select-Object -First 1
$begin = $beginMethod.MakeGenericMethod([psobject], [psobject])
$active = $null

try {
    Send-Frame @{ kind = 'ready'; token = $Token; version = $PSVersionTable.PSVersion.ToString() }
    $readTask = $reader.ReadLineAsync()
    while ($true) {
        if ($readTask.IsCompleted) {
            $line = $readTask.GetAwaiter().GetResult()
            if ($null -eq $line) { break }
            $request = $line | ConvertFrom-Json
            if ($request.kind -eq 'shutdown') { break }
            if ($null -ne $active) { throw 'A PowerShell command is already running' }
            $pipeline = [PowerShell]::Create()
            $pipeline.Runspace = $runspace
            $shellHost.ExitCode = $null
            $runspace.SessionStateProxy.SetVariable('__nooa_code', [string]$request.command)
            $runspace.SessionStateProxy.SetVariable('LASTEXITCODE', 0)
            $runspace.SessionStateProxy.SetVariable('__nooa_success', $false)
            $null = $pipeline.AddScript(
                '. ([scriptblock]::Create($global:__nooa_code)); $global:__nooa_success = $?',
                $false
            ).AddCommand('Out-String').AddParameter('Stream').AddParameter('Width', 4096)
            $inputData = New-Object 'System.Management.Automation.PSDataCollection[psobject]'
            $outputData = New-Object 'System.Management.Automation.PSDataCollection[psobject]'
            $inputData.Complete()
            $arguments = New-Object object[] 2
            $arguments[0] = $inputData.PSObject.BaseObject
            $arguments[1] = $outputData.PSObject.BaseObject
            $invocation = $begin.Invoke($pipeline, $arguments)
            $active = @{
                id = [string]$request.id
                pipeline = $pipeline
                input = $inputData
                output = $outputData
                invocation = $invocation
                failed = $false
            }
            $readTask = $reader.ReadLineAsync()
        }

        if ($null -ne $active) {
            Drain-Streams $active
            if ($active.invocation.IsCompleted) {
                try { $null = $active.pipeline.EndInvoke($active.invocation) }
                catch {
                    $active.failed = $true
                    Send-Text 'stderr' ($_.Exception.Message + "`n") $active.id
                }
                Drain-Streams $active
                $exitCode = 0
                $lastExit = $runspace.SessionStateProxy.GetVariable('LASTEXITCODE')
                if ($null -ne $lastExit) { $exitCode = [int]$lastExit }
                if ($null -ne $shellHost.ExitCode) {
                    $exitCode = $shellHost.ExitCode
                } elseif ($active.failed -and $exitCode -eq 0) {
                    $exitCode = 1
                }
                $environment = @{}
                foreach ($entry in [Environment]::GetEnvironmentVariables().GetEnumerator()) {
                    $environment[[string]$entry.Key] = [string]$entry.Value
                }
                Send-Frame @{
                    kind = 'done'
                    id = $active.id
                    exit_code = $exitCode
                    cwd = $runspace.SessionStateProxy.Path.CurrentFileSystemLocation.ProviderPath
                    environment = $environment
                }
                $active.pipeline.Dispose()
                $active.input.Dispose()
                $active.output.Dispose()
                $active = $null
            }
        }
        Start-Sleep -Milliseconds 10
    }
}
catch {
    $requestId = if ($null -ne $active) { $active.id } else { [string]$request.id }
    Send-Frame @{ kind = 'broken'; id = $requestId; text = $_.Exception.ToString() }
}
finally {
    if ($null -ne $active) {
        $active.pipeline.Stop()
        $active.pipeline.Dispose()
    }
    $runspace.Dispose()
    $writer.Dispose()
    $reader.Dispose()
    $client.Dispose()
}
