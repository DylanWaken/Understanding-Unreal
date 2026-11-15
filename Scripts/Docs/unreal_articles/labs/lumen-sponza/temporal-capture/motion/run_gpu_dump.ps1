param(
    [string]$Workspace = 'D:\UnrealUnderstanding-Sponza',
    [string]$EngineRoot = 'D:\UnrealEngine',
    [string]$Tag = 'gpu-dump'
)
$ErrorActionPreference = 'Stop'

$Project = Join-Path $Workspace 'project\UnrealLab\UnrealLab.uproject'
$Script = Join-Path $Workspace 'scripts\capture_gpu_dump.py'
$GpuDir = Join-Path $Workspace 'output\gpu-dump'
$LogDir = Join-Path $GpuDir 'logs'
New-Item -ItemType Directory -Force $LogDir | Out-Null

if (-not (Test-Path -LiteralPath $Project)) { throw "Project missing: $Project" }
if (-not (Test-Path -LiteralPath $Script)) { throw "Capture script missing: $Script" }
$existing = Get-Process UnrealEditor, UnrealEditor-Cmd, ShaderCompileWorker -ErrorAction SilentlyContinue
if ($existing) { throw ("Refusing to launch: Unreal process already running: " + (($existing | ForEach-Object { $_.Id }) -join ',')) }

$exe = Join-Path $EngineRoot 'Engine\Binaries\Win64\UnrealEditor.exe'
if (-not (Test-Path -LiteralPath $exe)) { throw "Editor missing: $exe" }

$stamp = (Get-Date).ToUniversalTime().ToString('yyyyMMdd-HHmmss')
$stdout = Join-Path $LogDir "$Tag-$stamp.stdout.log"
$stderr = Join-Path $LogDir "$Tag-$stamp.stderr.log"
$runPath = Join-Path $LogDir "$Tag-$stamp-launcher.json"

# The startup ini override is required before any uniform buffer is created so
# r.RHI.UniformBufferContentMap records the CPU copy consumed by native
# uniform-buffer texture export.  Runtime console assignment alone is too late.
$arguments = '"{0}" -ExecutePythonScript="{1}" -RenderOffscreen -unattended -nosplash -NoSound -NoSourceControl -dx12 -ResX=1280 -ResY=720 -ForceRes -stdout -FullStdOutLogOutput -ini:Engine:[ConsoleVariables]:r.RHI.UniformBufferContentMap.Enable=1' -f $Project, $Script

$buildVersionPath = Join-Path $EngineRoot 'Engine\Build\Build.version'
$engineVersionPath = Join-Path $EngineRoot 'Engine\Binaries\Win64\UnrealEditor.version'
$engineModulesPath = Join-Path $EngineRoot 'Engine\Binaries\Win64\UnrealEditor.modules'
$projectModulesPath = Join-Path $Workspace 'project\UnrealLab\Binaries\Win64\UnrealEditor.modules'
$bridgeDll = Join-Path $Workspace 'project\UnrealLab\Binaries\Win64\UnrealEditor-ArdaLabCapture.dll'

$engineVersion = Get-Content -LiteralPath $engineVersionPath -Raw | ConvertFrom-Json
$engineModules = Get-Content -LiteralPath $engineModulesPath -Raw | ConvertFrom-Json
$projectModules = Get-Content -LiteralPath $projectModulesPath -Raw | ConvertFrom-Json

$hashFiles = @(
    $exe,
    $buildVersionPath,
    $engineVersionPath,
    $engineModulesPath,
    $projectModulesPath,
    $bridgeDll,
    (Join-Path $EngineRoot 'Engine\Binaries\Win64\UnrealEditor-RenderCore.dll'),
    (Join-Path $EngineRoot 'Engine\Binaries\Win64\UnrealEditor-Renderer.dll'),
    (Join-Path $EngineRoot 'Engine\Binaries\Win64\UnrealEditor-RHI.dll'),
    (Join-Path $EngineRoot 'Engine\Binaries\Win64\UnrealEditor-D3D12RHI.dll'),
    (Join-Path $EngineRoot 'Engine\Binaries\Win64\UnrealEditor-Engine.dll'),
    (Join-Path $EngineRoot 'Engine\Binaries\Win64\UnrealEditor-UnrealEd.dll')
)
$hashes = [ordered]@{}
foreach ($p in $hashFiles) {
    if (Test-Path -LiteralPath $p) {
        $item = Get-Item -LiteralPath $p
        $hashes[$p] = [ordered]@{
            sha256 = (Get-FileHash -LiteralPath $p -Algorithm SHA256).Hash
            bytes = $item.Length
            mtime_utc = $item.LastWriteTimeUtc.ToString('o')
        }
    } else {
        $hashes[$p] = [ordered]@{ exists = $false }
    }
}

$driveBefore = Get-PSDrive D | Select-Object @{n='free_gb';e={[math]::Round($_.Free/1GB,2)}}
$record = [ordered]@{
    schema = 'arda-sponza-gpu-dump-launcher-v1'
    tag = $Tag
    status = 'starting'
    started_utc = (Get-Date).ToUniversalTime().ToString('o')
    executable = $exe
    arguments = $arguments
    project = $Project
    script = $Script
    script_sha256 = (Get-FileHash -LiteralPath $Script -Algorithm SHA256).Hash
    launcher_script = $PSCommandPath
    launcher_script_sha256 = (Get-FileHash -LiteralPath $PSCommandPath -Algorithm SHA256).Hash
    engine_root = $EngineRoot
    engine_build_version = $engineVersion
    engine_build_id = $engineModules.BuildId
    project_module_build_id = $projectModules.BuildId
    engine_build_id_matches_project = ($engineModules.BuildId -eq $projectModules.BuildId)
    declared_source_base = '71fe36aac5a8df5ccd66c763ffc902b29b6a9c43'
    declared_bridge_build_id = 'b4effda9-1b7d-4e62-b9a6-cb99e41a70c9'
    bridge_build_id_matches_declared = ($projectModules.BuildId -eq 'b4effda9-1b7d-4e62-b9a6-cb99e41a70c9')
    startup_ini_cvar = 'r.RHI.UniformBufferContentMap.Enable=1 (before any uniform buffer creation)'
    resolution = @(1280, 720)
    engine_file_hashes = $hashes
    free_space_before = $driveBefore
    stdout_log = $stdout
    stderr_log = $stderr
}
$record | ConvertTo-Json -Depth 12 | Set-Content -LiteralPath $runPath -Encoding utf8

$started = (Get-Date).ToUniversalTime()
$process = Start-Process -FilePath $exe -ArgumentList $arguments -WorkingDirectory $Workspace `
    -WindowStyle Hidden -RedirectStandardOutput $stdout -RedirectStandardError $stderr -PassThru
$record.pid = $process.Id
$record.status = 'running'
$record | ConvertTo-Json -Depth 12 | Set-Content -LiteralPath $runPath -Encoding utf8
Write-Output ("Started GPU dump PID {0} at {1}" -f $process.Id, $started.ToString('o'))

# Foreground wait.  Only this owned PID is ever terminated; no process-name kill.
$timeoutMs = 4 * 60 * 60 * 1000
$exited = $process.WaitForExit($timeoutMs)
if (-not $exited) {
    Write-Output ("Owned PID {0} exceeded 4 h; stopping only that PID" -f $process.Id)
    Stop-Process -Id $process.Id -Force
    $process.WaitForExit()
} else {
    # A no-arg WaitForExit is needed by some Windows PowerShell hosts before
    # ExitCode is populated even after the timed wait returns true.
    $process.WaitForExit()
}
$process.Refresh()
$exitCode = $process.ExitCode
$finished = (Get-Date).ToUniversalTime()
$stdoutText = if (Test-Path -LiteralPath $stdout) { Get-Content -LiteralPath $stdout -Raw } else { '' }
$stderrText = if (Test-Path -LiteralPath $stderr) { Get-Content -LiteralPath $stderr -Raw } else { '' }
$pythonError = ($stdoutText -match 'Python script executed with errors') -or
               ($stderrText -match 'Python script executed with errors') -or
               ($stdoutText -match 'ARDA_GPU_DUMP_ERROR') -or
               ($stderrText -match 'ARDA_GPU_DUMP_ERROR')

$latestPath = Join-Path $GpuDir 'latest-run.json'
$latest = $null
if (Test-Path -LiteralPath $latestPath) { $latest = Get-Content -LiteralPath $latestPath -Raw | ConvertFrom-Json }
$dumpStatus = $null
$dumpDirectory = $null
$dumpFiles = $null
$dumpBytes = $null
$dumpRunId = $null
if ($latest) {
    $dumpStatus = $latest.dump_status
    $dumpDirectory = $latest.raw_directory
    $dumpFiles = $latest.dump_file_count
    $dumpBytes = $latest.dump_total_bytes
    $dumpRunId = $latest.run_id
    if ($latest.session -and (Test-Path -LiteralPath $latest.session)) {
        $session = Get-Content -LiteralPath $latest.session -Raw | ConvertFrom-Json
        if ($session.dump_status) { $dumpStatus = $session.dump_status }
        if ($session.dump_directory) { $dumpDirectory = $session.dump_directory }
        if ($session.dump_file_count) { $dumpFiles = $session.dump_file_count }
        if ($session.dump_total_bytes) { $dumpBytes = $session.dump_total_bytes }
        $dumpRunId = $session.run_id
    }
}

$driveAfter = Get-PSDrive D | Select-Object @{n='free_gb';e={[math]::Round($_.Free/1GB,2)}}
$record.finished_utc = $finished.ToString('o')
$record.wall_seconds = [math]::Round(($finished - $started).TotalSeconds, 1)
$record.exit_code = $exitCode
$record.python_error_detected = [bool]$pythonError
$record.dump_run_id = $dumpRunId
$record.dump_status = $dumpStatus
$record.dump_directory = $dumpDirectory
$record.dump_file_count = $dumpFiles
$record.dump_total_bytes = $dumpBytes
$record.free_space_after = $driveAfter
$record.stdout_bytes = $stdoutText.Length
$record.stderr_bytes = $stderrText.Length
$record.status = 'exited'
$record | ConvertTo-Json -Depth 12 | Set-Content -LiteralPath $runPath -Encoding utf8

Write-Output ("GPU dump exited with code {0} after {1} s; launcher={2}; dump={3}; status={4}; files={5}; bytes={6}" -f `
    $exitCode, $record.wall_seconds, $runPath, $dumpDirectory, $dumpStatus, $dumpFiles, $dumpBytes)

if ($null -ne $exitCode -and $exitCode -ne 0) { exit $exitCode }
if ($pythonError) { exit 3 }
if ($dumpStatus -ne 'ok') { throw "DumpGPU did not report ok. See $runPath, $stdout and the session record." }
if (-not $dumpDirectory -or -not (Test-Path -LiteralPath $dumpDirectory)) {
    throw "DumpGPU did not record an existing raw directory. See $runPath."
}
