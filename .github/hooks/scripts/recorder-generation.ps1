param(
    [Parameter(Mandatory = $true)]
    [ValidateSet("UserPromptSubmit", "PreToolUse", "PostToolUse", "Stop")]
    [string]$Event,

    [Parameter(ValueFromPipeline = $true)]
    [string]$InputObject
)

$ErrorActionPreference = "Stop"
$payload = if ([string]::IsNullOrEmpty($InputObject)) {
    [Console]::In.ReadToEnd()
}
else {
    $InputObject
}
$isRecorderPrompt = $Event -eq "UserPromptSubmit"
$bound = $false
try {
    $eventData = $payload | ConvertFrom-Json
    $sessionId = [string]$eventData.session_id
    if ([string]::IsNullOrWhiteSpace($sessionId)) {
        $sessionId = [string]$eventData.sessionId
    }
    if (-not [string]::IsNullOrWhiteSpace($sessionId)) {
        $sha = [System.Security.Cryptography.SHA256]::Create()
        try {
            $bytes = [System.Text.Encoding]::UTF8.GetBytes($sessionId)
            $digest = [Convert]::ToHexString($sha.ComputeHash($bytes)).ToLower()
        }
        finally {
            $sha.Dispose()
        }
        $binding = Join-Path (Get-Location) (
            ".copilot/recorder-routing/session-{0}.json" -f $digest.Substring(0, 24)
        )
        $bound = Test-Path -LiteralPath $binding -PathType Leaf
    }
}
catch {
}

$launchers = [System.Collections.Generic.List[object]]::new()
$launcherKeys = [System.Collections.Generic.HashSet[string]]::new(
    [System.StringComparer]::OrdinalIgnoreCase
)

function Add-RecorderLauncher {
    param(
        [string]$FileName,
        [string[]]$Prefix = @()
    )

    if ([string]::IsNullOrWhiteSpace($FileName) -or -not (
            Test-Path -LiteralPath $FileName -PathType Leaf
        )) {
        return
    }
    $resolved = (Resolve-Path -LiteralPath $FileName).Path
    $key = $resolved + "`0" + ($Prefix -join "`0")
    if ($launcherKeys.Add($key)) {
        [void]$launchers.Add([PSCustomObject]@{
            FileName = $resolved
            Prefix = @($Prefix)
        })
    }
}

if ($env:BDD_AUTOWORK_HOOK_PYTHON) {
    Add-RecorderLauncher -FileName $env:BDD_AUTOWORK_HOOK_PYTHON
}

$runtimeHintPath = Join-Path (
    Get-Location
) ".copilot/recorder-runtime/python.json"
if (Test-Path -LiteralPath $runtimeHintPath -PathType Leaf) {
    try {
        $runtimeHint = Get-Content -LiteralPath $runtimeHintPath -Raw |
            ConvertFrom-Json
        $hintProjectRoot = (Resolve-Path -LiteralPath (
            [string]$runtimeHint.project_root
        )).Path
        if ($hintProjectRoot -eq (Get-Location).Path) {
            Add-RecorderLauncher -FileName (
                [string]$runtimeHint.python_executable
            )
        }
    }
    catch {
    }
}

foreach ($environmentRoot in @($env:VIRTUAL_ENV, $env:CONDA_PREFIX)) {
    if (-not [string]::IsNullOrWhiteSpace($environmentRoot)) {
        Add-RecorderLauncher -FileName (
            Join-Path $environmentRoot "Scripts\python.exe"
        )
    }
}

foreach ($environmentName in @(".venv", "venv")) {
    Add-RecorderLauncher -FileName (
        Join-Path (Get-Location) "$environmentName\Scripts\python.exe"
    )
}

$py = Get-Command py -CommandType Application -ErrorAction SilentlyContinue
if ($null -ne $py) {
    Add-RecorderLauncher -FileName $py.Source -Prefix @("-3.11")
}

foreach ($commandName in @("python", "python3")) {
    $python = Get-Command $commandName -CommandType Application -ErrorAction SilentlyContinue
    if ($null -eq $python -or $python.Source -like "*\WindowsApps\*") {
        continue
    }
    Add-RecorderLauncher -FileName $python.Source
}

$routerArguments = @(
    "-B", "-m",
    "autowork_core.utils.debug_tools.recorder.copilot_hook_router",
    "--event", $Event, "--project-root", "."
)
$OutputEncoding = [System.Text.UTF8Encoding]::new($false)

foreach ($launcher in $launchers) {
    try {
        if ($isRecorderPrompt) {
            $probeArguments = @($launcher.Prefix + @(
                "-B",
                "-c",
                "import autowork_core.utils.debug_tools.recorder.generation_workflow"
            ))
            $null = & $launcher.FileName @probeArguments 2>$null
            if ($LASTEXITCODE -ne 0) {
                continue
            }
        }
        $arguments = @($launcher.Prefix + $routerArguments)
        $output = $payload | & $launcher.FileName @arguments 2>$null
        if ($LASTEXITCODE -eq 0 -and -not [string]::IsNullOrWhiteSpace($output)) {
            Write-Output ([string]::Join([Environment]::NewLine, $output).Trim())
            exit 0
        }
    }
    catch {
        continue
    }
}

if ($isRecorderPrompt) {
    Write-Output '{"continue":false,"stopReason":"Recorder Generation cannot find a Python that can load the complete generation workflow. Reopen the task from Workbench or set BDD_AUTOWORK_HOOK_PYTHON to a valid python.exe path."}'
}
elseif ($bound -and $Event -eq "PreToolUse") {
    Write-Output '{"continue":true,"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"deny","permissionDecisionReason":"Recorder Generation control is unavailable; bound edits fail closed."}}'
}
elseif ($bound -and $Event -eq "Stop") {
    Write-Output '{"continue":true,"hookSpecificOutput":{"hookEventName":"Stop","decision":"block","reason":"Recorder Generation control is unavailable while the bound Job may be nonterminal."}}'
}
else {
    Write-Output '{"continue":true,"systemMessage":"RECORDER_GENERATION_HOOK_UNAVAILABLE: supported Python could not load the Recorder control router."}'
}
exit 0