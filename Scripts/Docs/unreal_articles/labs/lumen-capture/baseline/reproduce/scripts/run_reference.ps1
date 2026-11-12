param(
    [Parameter(Mandatory = $true)]
    [string]$EngineRoot,
    [string]$OutputRoot = (Join-Path (Split-Path -Parent $PSScriptRoot) 'output\reference'),
    [string]$ProjectRoot = (Join-Path (Split-Path -Parent $PSScriptRoot) 'UnrealLab'),
    [switch]$Build
)
$ErrorActionPreference = 'Stop'
if (-not (Test-Path -LiteralPath $EngineRoot)) { throw "EngineRoot does not exist: $EngineRoot" }
if (-not (Test-Path -LiteralPath $ProjectRoot)) { throw "ProjectRoot does not exist: $ProjectRoot" }

$Project = Join-Path $ProjectRoot 'UnrealLab.uproject'
$Script = Join-Path $PSScriptRoot 'capture_reference.py'
$Captures = Join-Path $OutputRoot 'captures'
$Logs = Join-Path $OutputRoot 'logs'
New-Item -ItemType Directory -Force $Captures, $Logs | Out-Null

if ($Build) {
    $buildLog = Join-Path $Logs 'build.log'
    $buildArgs = @('UnrealLabEditor', 'Win64', 'Development', "-Project=$Project", '-WaitMutex', '-NoHotReload')
    Write-Output ('Build: ' + (Join-Path $EngineRoot 'Engine\Build\BatchFiles\Build.bat') + ' ' + ($buildArgs -join ' '))
    & (Join-Path $EngineRoot 'Engine\Build\BatchFiles\Build.bat') @buildArgs 2>&1 | Tee-Object -FilePath $buildLog
    if ($LASTEXITCODE -ne 0) { throw 'Editor bridge build failed.' }
}

$stdout = Join-Path $Logs 'capture.stdout.log'
$stderr = Join-Path $Logs 'capture.stderr.log'
if (Test-Path -LiteralPath $stdout) { Remove-Item -LiteralPath $stdout -Force }
if (Test-Path -LiteralPath $stderr) { Remove-Item -LiteralPath $stderr -Force }

$arguments = '"{0}" -ExecutePythonScript="{1}" -RenderOffscreen -unattended -nosplash -NoSound -NoSourceControl -dx12 -ResX=1280 -ResY=720 -ForceRes -stdout -FullStdOutLogOutput' -f $Project, $Script
$exe = Join-Path $EngineRoot 'Engine\Binaries\Win64\UnrealEditor.exe'
$started = (Get-Date).ToUniversalTime()

$env:ARDA_REFERENCE_ROOT = $OutputRoot
$env:ARDA_PROJECT_ROOT = $ProjectRoot
$env:ARDA_REFERENCE_OUTPUT = $Captures
$captureProcess = Start-Process -FilePath $exe -ArgumentList $arguments -WindowStyle Hidden -PassThru `
    -RedirectStandardOutput $stdout -RedirectStandardError $stderr
$pidValue = $captureProcess.Id
$env:ARDA_REFERENCE_ROOT = $null
$env:ARDA_PROJECT_ROOT = $null
$env:ARDA_REFERENCE_OUTPUT = $null

$gpu = $null
try { $gpu = (& nvidia-smi --query-gpu=name,driver_version,memory.total --format=csv,noheader) -join "`n" } catch { }
$engineSha = $null
$engineBranch = $null
try { $engineSha = (& git -C $EngineRoot rev-parse HEAD) } catch { }
try { $engineBranch = (& git -C $EngineRoot rev-parse --abbrev-ref HEAD) } catch { }
$buildVersion = $null
$versionPath = Join-Path $EngineRoot 'Engine\Build\Build.version'
if (Test-Path -LiteralPath $versionPath) { $buildVersion = Get-Content -LiteralPath $versionPath -Raw | ConvertFrom-Json }
$engineVersion = if ($buildVersion) {
    "$($buildVersion.MajorVersion).$($buildVersion.MinorVersion).$($buildVersion.PatchVersion)-0+$($buildVersion.BranchName)"
} else { $null }

$metadata = [ordered]@{
    schema = 'unreal-reference-run-metadata-v1'
    pid = $pidValue
    started_utc = $started.ToString('o')
    executable = $exe
    command = ('"' + $exe + '" ' + $arguments)
    arguments = $arguments
    project = $Project
    script = $Script
    engine_root = $EngineRoot
    engine_version = $engineVersion
    engine_git_sha = $engineSha
    engine_git_branch = $engineBranch
    engine_build_version = $buildVersion
    gpu = $gpu
    resolution = @(1280, 720)
    rhi = 'Direct3D 12'
    shader_platform = 'PCD3D_SM6'
    stdout_log = $stdout
    stderr_log = $stderr
}
$metadataPath = Join-Path $OutputRoot 'run-metadata.json'
$metadata | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $metadataPath -Encoding utf8
Write-Output ("Started PID $pidValue : $exe $arguments")

$captureProcess.WaitForExit()
$captureProcess.Refresh()
$exitCode = $captureProcess.ExitCode
if ($null -eq $exitCode) { Write-Output 'Process.ExitCode was not populated; using capture session status.' }
$finished = (Get-Date).ToUniversalTime()
$metadata['finished_utc'] = $finished.ToString('o')
$metadata['exit_code'] = $exitCode
$metadata['wall_seconds'] = ($finished - $started).TotalSeconds
$metadata | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $metadataPath -Encoding utf8
Write-Output ("Unreal exited with code $exitCode after $([math]::Round(($finished-$started).TotalSeconds,1)) s")
if ($null -ne $exitCode -and $exitCode -ne 0) { throw ('Unreal exited with code ' + $exitCode) }
