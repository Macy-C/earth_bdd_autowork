$ErrorActionPreference = "Stop"

$launchers = @()

$py = Get-Command py -ErrorAction SilentlyContinue
if ($null -ne $py) {
    $launchers += @{
        FileName = $py.Source
        Arguments = "-3.11 -B -m autowork_core.utils.debug_tools.shadow_companion session-start --project-root ."
    }
}

$python = Get-Command python -ErrorAction SilentlyContinue
if ($null -ne $python) {
    $launchers += @{
        FileName = $python.Source
        Arguments = "-B -m autowork_core.utils.debug_tools.shadow_companion session-start --project-root ."
    }
}

foreach ($launcher in $launchers) {
    try {
        $startInfo = [System.Diagnostics.ProcessStartInfo]::new()
        $startInfo.FileName = $launcher.FileName
        $startInfo.Arguments = $launcher.Arguments
        $startInfo.WorkingDirectory = (Get-Location).Path
        $startInfo.UseShellExecute = $false
        $startInfo.CreateNoWindow = $true
        $startInfo.RedirectStandardOutput = $true
        $startInfo.RedirectStandardError = $true

        $process = [System.Diagnostics.Process]::new()
        $process.StartInfo = $startInfo
        [void]$process.Start()
        $stdout = $process.StandardOutput.ReadToEnd()
        [void]$process.StandardError.ReadToEnd()
        $process.WaitForExit()
        if ($process.ExitCode -eq 0 -and -not [string]::IsNullOrWhiteSpace($stdout)) {
            Write-Output $stdout.Trim()
            exit 0
        }
    }
    catch {
        continue
    }
}

Write-Output '{"continue":true,"systemMessage":"SHADOW_COMPANION_STATUS_UNAVAILABLE: Python 3.11 hook runner failed. Ignore Shadow state and use the full normal workflow."}'
exit 0