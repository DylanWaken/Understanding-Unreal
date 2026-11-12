param(
    [Parameter(Mandatory)] [string]$EngineRoot,
    [Parameter(Mandatory)] [string]$ProjectRoot,
    [Parameter(Mandatory)] [string]$OutputRoot,
    [string]$Tag = 'full'
)
$ErrorActionPreference = 'Stop'
$CaptureRoot = [System.IO.Path]::GetFullPath($OutputRoot)
$ProjectRoot = (Resolve-Path -LiteralPath $ProjectRoot).Path
$Project = Join-Path $ProjectRoot 'UnrealLab.uproject'
$Script = Join-Path $PSScriptRoot 'gpudump_capture.py'
$BuffersRoot = Join-Path $CaptureRoot 'buffers'
$LogDir = Join-Path $CaptureRoot 'logs'
New-Item -ItemType Directory -Force $LogDir, (Join-Path $BuffersRoot 'runs'), (Join-Path $BuffersRoot 'logs') | Out-Null

$stamp = (Get-Date).ToUniversalTime().ToString('yyyyMMdd-HHmmss')
$stdout = Join-Path $LogDir "$Tag-$stamp.stdout.log"
$stderr = Join-Path $LogDir "$Tag-$stamp.stderr.log"
$runPath = Join-Path $BuffersRoot "runs\run-$Tag-$stamp.json"

$exe = Join-Path $EngineRoot 'Engine\Binaries\Win64\UnrealEditor.exe'
$arguments = '"{0}" -ExecutePythonScript="{1}" -RenderOffscreen -unattended -nosplash -NoSound -NoSourceControl -dx12 -ResX=1280 -ResY=720 -ForceRes -stdout -FullStdOutLogOutput -ini:Engine:[ConsoleVariables]:r.RHI.UniformBufferContentMap.Enable=1' -f $Project, $Script

$drives = Get-PSDrive -PSProvider FileSystem | ForEach-Object {
    [ordered]@{ name = $_.Name; free_gb = [math]::Round($_.Free / 1GB, 2); used_gb = [math]::Round($_.Used / 1GB, 2) }
}
$gpu = $null
if (Get-Command nvidia-smi -ErrorAction SilentlyContinue) {
    $gpu = (& nvidia-smi --query-gpu=name,driver_version,memory.total,memory.free --format=csv,noheader) -join "`n"
}
$buildVersion = Get-Content (Join-Path $EngineRoot 'Engine\Build\Build.version') -Raw | ConvertFrom-Json
$engineSha = (& git -C $EngineRoot rev-parse HEAD)
$engineBranch = (& git -C $EngineRoot rev-parse --abbrev-ref HEAD)
$engineModules = Get-Content (Join-Path $EngineRoot 'Engine\Binaries\Win64\UnrealEditor.modules') -Raw | ConvertFrom-Json
$projManifestPath = Join-Path $ProjectRoot 'Binaries\Win64\UnrealEditor.modules'
$projManifest = Get-Content $projManifestPath -Raw | ConvertFrom-Json
$products = @(
    'Engine\Binaries\Win64\UnrealEditor-RenderCore.dll',
    'Engine\Source\Runtime\RenderCore\Private\DumpGPU.cpp',
    'Engine\Shaders\Private\Tools\DumpTexture.usf',
    'Engine\Shaders\Private\Tools\DumpBuffer.usf',
    'Engine\Binaries\Win64\UnrealEditor.exe'
)
$hashes = [ordered]@{}
foreach ($rel in $products) {
    $p = Join-Path $EngineRoot $rel
    $hashes[$rel] = [ordered]@{ sha256 = (Get-FileHash $p -Algorithm SHA256).Hash; bytes = (Get-Item $p).Length; mtime_utc = (Get-Item $p).LastWriteTimeUtc.ToString('o') }
}
$moduleDll = Join-Path $ProjectRoot 'Binaries\Win64\UnrealEditor-ArdaLabCapture.dll'
$module = [ordered]@{
    path = $moduleDll
    sha256 = (Get-FileHash $moduleDll -Algorithm SHA256).Hash
    bytes = (Get-Item $moduleDll).Length
    mtime_utc = (Get-Item $moduleDll).LastWriteTimeUtc.ToString('o')
    module_name = 'ArdaLabCapture'
    manifest_build_id = $projManifest.BuildId
    engine_build_id = $engineModules.BuildId
    manifest_matches_engine = ($projManifest.BuildId -eq $engineModules.BuildId)
    note = 'Module provenance observed at launch. Build the portable project with the selected engine before launching.'
}
$metadata = [ordered]@{
    schema = 'unreal-understanding-full-capture-run-v2'
    tag = $Tag
    pid = $null
    status = 'starting'
    started_utc = (Get-Date).ToUniversalTime().ToString('o')
    executable = $exe
    command = ('"' + $exe + '" ' + $arguments)
    arguments = $arguments
    project = $Project
    script = $Script
    engine_root = $EngineRoot
    engine_version = "$($buildVersion.MajorVersion).$($buildVersion.MinorVersion).$($buildVersion.PatchVersion)-0+$($buildVersion.BranchName)"
    engine_git_sha = $engineSha
    engine_git_branch = $engineBranch
    engine_build_id = $engineModules.BuildId
    gpu = $gpu
    resolution = @(1280, 720)
    rhi = 'Direct3D 12'
    shader_platform = 'PCD3D_SM6'
    free_space_at_start = $drives
    engine_products = $hashes
    project_module = $module
    stdout_log = $stdout
    stderr_log = $stderr
    startup_ini_cvar = 'r.RHI.UniformBufferContentMap.Enable=1 (before any uniform buffer creation)'
    dump_root = '*'
    dump_texture = 2
    dump_buffer = 2
    dump_pass_parameters = 1
    dump_console_variables = 1
    dump_native_uniform_buffer_textures = 1
    dump_buffer_skip_tracking = 1
}
$metadata | ConvertTo-Json -Depth 12 | Set-Content -LiteralPath $runPath -Encoding utf8

$env:ARDA_BUFFERS_ROOT = $BuffersRoot
$env:ARDA_PROJECT_ROOT = $ProjectRoot
$env:ARDA_DUMP_MODE = 'binary'
$env:ARDA_DUMP_ROOT = '*'
$started = (Get-Date).ToUniversalTime()
$proc = Start-Process -FilePath $exe -ArgumentList $arguments -WindowStyle Hidden -PassThru `
    -RedirectStandardOutput $stdout -RedirectStandardError $stderr
$env:ARDA_BUFFERS_ROOT = $null
$env:ARDA_PROJECT_ROOT = $null
$env:ARDA_DUMP_MODE = $null
$env:ARDA_DUMP_ROOT = $null

$metadata.pid = $proc.Id
$metadata.status = 'running'
$metadata | ConvertTo-Json -Depth 12 | Set-Content -LiteralPath $runPath -Encoding utf8
Write-Output ("Started full capture PID {0}: {1}" -f $proc.Id, $arguments)

$proc.WaitForExit(900000) | Out-Null
if (-not $proc.HasExited) {
    Write-Output ("Owned PID {0} did not exit within 900 s; killing the owned process" -f $proc.Id)
    Stop-Process -Id $proc.Id -Force
    $proc.WaitForExit()
}
$proc.Refresh()
$exitCode = $proc.ExitCode
$finished = (Get-Date).ToUniversalTime()
$metadata.finished_utc = $finished.ToString('o')
$metadata.exit_code = $exitCode
$metadata.wall_seconds = [math]::Round(($finished - $started).TotalSeconds, 1)
$metadata.free_space_at_end = Get-PSDrive -PSProvider FileSystem | ForEach-Object {
    [ordered]@{ name = $_.Name; free_gb = [math]::Round($_.Free / 1GB, 2); used_gb = [math]::Round($_.Used / 1GB, 2) }
}
$progressPath = Join-Path $BuffersRoot 'progress.json'
if (Test-Path $progressPath) {
    $progress = Get-Content $progressPath -Raw | ConvertFrom-Json
    $metadata.dump_directory = $progress.dump_directory
    $metadata.progress_state = $progress.state
}
if ($metadata.dump_directory) {
    $statusFile = Join-Path $metadata.dump_directory 'Base\Status.txt'
    if (Test-Path $statusFile) { $metadata.dump_status = (Get-Content $statusFile -Raw).Trim() }
}
$metadata.status = 'exited'
$metadata | ConvertTo-Json -Depth 12 | Set-Content -LiteralPath $runPath -Encoding utf8
Write-Output ("Unreal full capture exited with code {0} after {1} s; run={2}; dump={3}" -f $exitCode, $metadata.wall_seconds, $runPath, $metadata.dump_directory)
if ($null -ne $exitCode -and $exitCode -ne 0) { exit $exitCode }
if ($metadata.dump_status -ne 'ok') { throw "DumpGPU did not report ok. See $runPath and $stdout." }

