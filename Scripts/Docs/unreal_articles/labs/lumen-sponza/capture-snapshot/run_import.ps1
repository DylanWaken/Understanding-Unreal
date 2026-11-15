param(
    [string]$EngineRoot = 'D:\UnrealEngine',
    [string]$Workspace = 'D:\UnrealUnderstanding-Sponza'
)
$ErrorActionPreference = 'Stop'
$Project = Join-Path $Workspace 'project\UnrealLab\UnrealLab.uproject'
$Source = Join-Path $Workspace 'source\Sponza\glTF\SponzaSplit.gltf'
$Logs = Join-Path $Workspace 'output\logs'
New-Item -ItemType Directory -Force $Logs | Out-Null
if (-not (Test-Path -LiteralPath $Project)) { throw "Project missing: $Project" }
if (-not (Test-Path -LiteralPath $Source)) { throw "Split glTF missing: $Source" }

$existing = Get-Process UnrealEditor, UnrealEditor-Cmd, ShaderCompileWorker -ErrorAction SilentlyContinue
if ($existing) { throw ("Refusing to launch: Unreal process already running: " + (($existing | ForEach-Object { $_.Id }) -join ',')) }

$exe = Join-Path $EngineRoot 'Engine\Binaries\Win64\UnrealEditor-Cmd.exe'
if (-not (Test-Path -LiteralPath $exe)) { throw "Commandlet host missing: $exe" }
$arguments = @(
    "`"$Project`"",
    '-run=ImportAssets',
    "-source=`"$Source`"",
    '-dest=/Game/Sponza',
    '-nosourcecontrol',
    '-replaceexisting',
    '-unattended',
    '-nosplash',
    '-NoSound',
    '-NoSourceControl',
    '-dx12',
    '-stdout',
    '-FullStdOutLogOutput'
)
$stdout = Join-Path $Logs 'import.stdout.log'
$stderr = Join-Path $Logs 'import.stderr.log'
if (Test-Path -LiteralPath $stdout) { Remove-Item -LiteralPath $stdout -Force }
if (Test-Path -LiteralPath $stderr) { Remove-Item -LiteralPath $stderr -Force }
$started = (Get-Date).ToUniversalTime()
$psi = New-Object System.Diagnostics.ProcessStartInfo
$psi.FileName = $exe
$psi.Arguments = ($arguments -join ' ')
$psi.UseShellExecute = $false
$psi.CreateNoWindow = $true
$psi.RedirectStandardOutput = $true
$psi.RedirectStandardError = $true
$psi.WorkingDirectory = $Workspace
$process = New-Object System.Diagnostics.Process
$process.StartInfo = $psi
[void]$process.Start()
$outTask = $process.StandardOutput.ReadToEndAsync()
$errTask = $process.StandardError.ReadToEndAsync()
Write-Output ("Import PID $($process.Id): $exe " + ($arguments -join ' '))
$process.WaitForExit()
$stdoutText = $outTask.Result
$stderrText = $errTask.Result
[System.IO.File]::WriteAllText($stdout, $stdoutText)
[System.IO.File]::WriteAllText($stderr, $stderrText)
$exitCode = $process.ExitCode
$commandletSuccess = $stdoutText -match 'Success - 0 error\(s\)'
if (-not $commandletSuccess -and $exitCode -eq 0) { $exitCode = 4 }
$finished = (Get-Date).ToUniversalTime()
$record = [ordered]@{
    schema = 'arda-sponza-import-run-v1'
    pid = $process.Id
    started_utc = $started.ToString('o')
    finished_utc = $finished.ToString('o')
    wall_seconds = ($finished - $started).TotalSeconds
    exit_code = $exitCode
    commandlet_success_marker = $commandletSuccess
    executable = $exe
    executable_sha256 = (Get-FileHash -LiteralPath $exe -Algorithm SHA256).Hash
    project = $Project
    source = $Source
    source_sha256 = (Get-FileHash -LiteralPath $Source -Algorithm SHA256).Hash
    destination = '/Game/Sponza'
    arguments = $arguments
    stdout_log = $stdout
    stderr_log = $stderr
    stdout_bytes = $stdoutText.Length
    stderr_bytes = $stderrText.Length
}
$record | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath (Join-Path $Logs 'import-run.json') -Encoding utf8
Write-Output ("Unreal import exited with code $exitCode after $([math]::Round(($finished-$started).TotalSeconds,1)) s")
if ($exitCode -ne 0) { throw "Import commandlet failed with exit code $exitCode" }
