param(
    [Parameter(Mandatory = $true)]
    [ValidateSet('metadata', 'binary', 'vsm', 'pt1024')]
    [string]$Mode,
    [Parameter(Mandatory = $true)]
    [string]$EngineRoot,
    [string]$BuffersRoot = (Join-Path (Split-Path -Parent $PSScriptRoot) 'output\buffers'),
    [string]$ProjectRoot = (Join-Path (Split-Path -Parent $PSScriptRoot) 'UnrealLab'),
    [string]$ReferenceCaptures = (Join-Path (Split-Path -Parent $PSScriptRoot) 'output\reference\captures'),
    [string]$ExtraArgs = '',
    [switch]$UniformBufferMap
)
$ErrorActionPreference = 'Stop'
if (-not (Test-Path -LiteralPath $EngineRoot)) { throw "EngineRoot does not exist: $EngineRoot" }
if (-not (Test-Path -LiteralPath $ProjectRoot)) { throw "ProjectRoot does not exist: $ProjectRoot" }
if ($UniformBufferMap) {
    $ExtraArgs = ($ExtraArgs + ' -ini:Engine:[ConsoleVariables]:r.RHI.UniformBufferContentMap.Enable=1').Trim()
}
New-Item -ItemType Directory -Force $BuffersRoot, $ReferenceCaptures | Out-Null
$Project = Join-Path $ProjectRoot 'UnrealLab.uproject'
$Script = Join-Path $PSScriptRoot 'gpudump_capture.py'
$LogDir = Join-Path $BuffersRoot 'logs'
New-Item -ItemType Directory -Force $LogDir, (Join-Path $BuffersRoot 'runs') | Out-Null

$stamp = (Get-Date).ToUniversalTime().ToString('yyyyMMdd-HHmmss')
$stdout = Join-Path $LogDir "$Mode-$stamp.stdout.log"
$stderr = Join-Path $LogDir "$Mode-$stamp.stderr.log"
$metadataPath = Join-Path $BuffersRoot "runs\run-$Mode-$stamp.json"

$exe = Join-Path $EngineRoot 'Engine\Binaries\Win64\UnrealEditor.exe'
$arguments = '"{0}" -ExecutePythonScript="{1}" -RenderOffscreen -unattended -nosplash -NoSound -NoSourceControl -dx12 -ResX=1280 -ResY=720 -ForceRes -stdout -FullStdOutLogOutput {2}' -f $Project, $Script, $ExtraArgs

$drives = Get-PSDrive -PSProvider FileSystem | Where-Object { $null -ne $_.Used } | ForEach-Object {
    [ordered]@{ name = $_.Name; free_gb = [math]::Round($_.Free / 1GB, 2); used_gb = [math]::Round($_.Used / 1GB, 2) }
}
$gpu = $null
try { $gpu = (& nvidia-smi --query-gpu=name,driver_version,memory.total,memory.free --format=csv,noheader) -join "`n" } catch { }
$buildVersion = $null
$versionPath = Join-Path $EngineRoot 'Engine\Build\Build.version'
if (Test-Path -LiteralPath $versionPath) { $buildVersion = Get-Content -LiteralPath $versionPath -Raw | ConvertFrom-Json }
$engineSha = $null
$engineBranch = $null
try { $engineSha = (& git -C $EngineRoot rev-parse HEAD) } catch { }
try { $engineBranch = (& git -C $EngineRoot rev-parse --abbrev-ref HEAD) } catch { }
$engineVersion = if ($buildVersion) {
    "$($buildVersion.MajorVersion).$($buildVersion.MinorVersion).$($buildVersion.PatchVersion)-0+$($buildVersion.BranchName)"
} else { $null }

$metadata = [ordered]@{
    schema = 'unreal-active-frame-dump-run-v1'
    mode = $Mode
    pid = $null
    started_utc = (Get-Date).ToUniversalTime().ToString('o')
    executable = $exe
    command = ('"' + $exe + '" ' + $arguments)
    arguments = $arguments
    project = $Project
    script = $Script
    engine_root = $EngineRoot
    engine_version = $engineVersion
    engine_git_sha = $engineSha
    engine_git_branch = $engineBranch
    gpu = $gpu
    resolution = @(1280, 720)
    rhi = 'Direct3D 12'
    shader_platform = 'PCD3D_SM6'
    free_space_at_start = $drives
    stdout_log = $stdout
    stderr_log = $stderr
}
$metadata | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $metadataPath -Encoding utf8

$env:ARDA_BUFFERS_ROOT = $BuffersRoot
$env:ARDA_PROJECT_ROOT = $ProjectRoot
$env:ARDA_DUMP_MODE = $Mode
$env:ARDA_REFERENCE_CAPTURES = $ReferenceCaptures
$started = (Get-Date).ToUniversalTime()
$proc = Start-Process -FilePath $exe -ArgumentList $arguments -WindowStyle Hidden -PassThru `
    -RedirectStandardOutput $stdout -RedirectStandardError $stderr
$env:ARDA_BUFFERS_ROOT = $null
$env:ARDA_PROJECT_ROOT = $null
$env:ARDA_DUMP_MODE = $null
$env:ARDA_REFERENCE_CAPTURES = $null

$metadata.pid = $proc.Id
$metadata | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $metadataPath -Encoding utf8
Write-Output ("Started mode={0} PID {1}: {2} {3}" -f $Mode, $proc.Id, $exe, $arguments)

$proc.WaitForExit()
$proc.Refresh()
$exitCode = $proc.ExitCode
$finished = (Get-Date).ToUniversalTime()
$metadata.finished_utc = $finished.ToString('o')
$metadata.exit_code = $exitCode
$metadata.wall_seconds = [math]::Round(($finished - $started).TotalSeconds, 1)
$metadata.free_space_at_end = Get-PSDrive -PSProvider FileSystem | Where-Object { $null -ne $_.Used } | ForEach-Object {
    [ordered]@{ name = $_.Name; free_gb = [math]::Round($_.Free / 1GB, 2); used_gb = [math]::Round($_.Used / 1GB, 2) }
}
$metadata | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $metadataPath -Encoding utf8
Write-Output ("Unreal mode={0} exited with code {1} after {2} s" -f $Mode, $exitCode, $metadata.wall_seconds)
if ($null -ne $exitCode -and $exitCode -ne 0) { exit $exitCode }
