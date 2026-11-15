param(
    [Parameter(Mandatory = $true)][string]$Script,
    [Parameter(Mandatory = $true)][string]$Tag,
    [string]$EngineRoot = 'D:\UnrealEngine',
    [string]$Workspace = 'D:\UnrealUnderstanding-Sponza',
    [string]$ExtraEnvironmentJson = '{}'
)
$ErrorActionPreference = 'Stop'
$Project = Join-Path $Workspace 'project\UnrealLab\UnrealLab.uproject'
$Logs = Join-Path $Workspace 'output\logs'
New-Item -ItemType Directory -Force $Logs | Out-Null
if (-not (Test-Path -LiteralPath $Project)) { throw "Project missing: $Project" }
if (-not (Test-Path -LiteralPath $Script)) { throw "Python script missing: $Script" }
$existing = Get-Process UnrealEditor, UnrealEditor-Cmd, ShaderCompileWorker -ErrorAction SilentlyContinue
if ($existing) { throw ("Refusing to launch: Unreal process already running: " + (($existing | ForEach-Object { $_.Id }) -join ',')) }

$exe = Join-Path $EngineRoot 'Engine\Binaries\Win64\UnrealEditor.exe'
if (-not (Test-Path -LiteralPath $exe)) { throw "Editor missing: $exe" }
$quotedProject = '"' + $Project + '"'
$quotedScript = '"' + $Script + '"'
$arguments = "$quotedProject -ExecutePythonScript=$quotedScript -RenderOffscreen -unattended -nosplash -NoSound -NoSourceControl -dx12 -ResX=1280 -ResY=720 -ForceRes -stdout -FullStdOutLogOutput"
$stdout = Join-Path $Logs ($Tag + '.stdout.log')
$stderr = Join-Path $Logs ($Tag + '.stderr.log')
if (Test-Path -LiteralPath $stdout) { Remove-Item -LiteralPath $stdout -Force }
if (Test-Path -LiteralPath $stderr) { Remove-Item -LiteralPath $stderr -Force }

$env:ARDA_SPONZA_WORKSPACE = $Workspace
$env:ARDA_ENGINE_ROOT = $EngineRoot
$extra = @{}
if ($ExtraEnvironmentJson -and $ExtraEnvironmentJson.Trim() -ne '') {
    $parsed = $ExtraEnvironmentJson | ConvertFrom-Json
    foreach ($prop in $parsed.PSObject.Properties) { $extra[$prop.Name] = [string]$prop.Value }
}
foreach ($key in $extra.Keys) { Set-Item -Path ("env:" + $key) -Value ([string]$extra[$key]) }

$started = (Get-Date).ToUniversalTime()
$process = Start-Process -FilePath $exe -ArgumentList $arguments -WorkingDirectory $Workspace `
    -WindowStyle Hidden -RedirectStandardOutput $stdout -RedirectStandardError $stderr -PassThru -Wait
Write-Output ("$Tag PID $($process.Id): $exe $arguments")
$exitCode = $process.ExitCode
$finished = (Get-Date).ToUniversalTime()
$stdoutText = if (Test-Path -LiteralPath $stdout) { Get-Content -LiteralPath $stdout -Raw } else { '' }
$stderrText = if (Test-Path -LiteralPath $stderr) { Get-Content -LiteralPath $stderr -Raw } else { '' }
$pythonError = ($stdoutText -match 'Python script executed with errors') -or
               ($stderrText -match 'Python script executed with errors')
if ($pythonError -and $exitCode -eq 0) { $exitCode = 3 }
$record = [ordered]@{
    schema = 'arda-sponza-unreal-hidden-run-v1'
    tag = $Tag
    pid = $process.Id
    started_utc = $started.ToString('o')
    finished_utc = $finished.ToString('o')
    wall_seconds = ($finished - $started).TotalSeconds
    exit_code = $exitCode
    window_style = 'Hidden'
    executable = $exe
    executable_sha256 = (Get-FileHash -LiteralPath $exe -Algorithm SHA256).Hash
    project = $Project
    script = $Script
    script_sha256 = (Get-FileHash -LiteralPath $Script -Algorithm SHA256).Hash
    arguments = $arguments
    environment = [ordered]@{ ARDA_SPONZA_WORKSPACE = $Workspace; ARDA_ENGINE_ROOT = $EngineRoot } + $extra
    stdout_log = $stdout
    stderr_log = $stderr
    stdout_bytes = $stdoutText.Length
    stderr_bytes = $stderrText.Length
}
$record | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath (Join-Path $Logs ($Tag + '-run.json')) -Encoding utf8
Write-Output ("$Tag exited with code $exitCode after $([math]::Round(($finished-$started).TotalSeconds,1)) s")
if ($exitCode -ne 0) { throw "$Tag failed with exit code $exitCode" }
