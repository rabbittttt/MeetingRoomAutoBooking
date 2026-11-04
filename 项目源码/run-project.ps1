$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
$projectRoot = $PSScriptRoot
$configured = [Environment]::GetEnvironmentVariable('BOOKING_PYTHON')
if ($configured) {
    $python = [Environment]::ExpandEnvironmentVariables($configured)
    if (-not [IO.Path]::IsPathRooted($python)) {
        $python = Join-Path $projectRoot $python
    }
} else {
    $directory = Get-Item -LiteralPath $projectRoot
    while ($directory -and $directory.Name -ine 'Project') { $directory = $directory.Parent }
    if (-not $directory) { throw 'Project workspace was not found. Keep this project below Project and initialize Project/.venv.' }
    $python = Join-Path $directory.FullName '.venv\Scripts\python.exe'
}
if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
    throw "Workspace Python was not found: $python. Initialize Project/.venv, or explicitly set BOOKING_PYTHON."
}
$env:PATH = (Split-Path -Parent $python) + [IO.Path]::PathSeparator + $env:PATH
if (-not $configured) { $env:VIRTUAL_ENV = Split-Path -Parent (Split-Path -Parent $python) }
$env:PYTHONUTF8 = '1'
$env:PYTHONIOENCODING = 'utf-8'
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
& $python -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)'
if ($LASTEXITCODE -ne 0) { throw "The selected Python is unavailable or older than 3.10: $python" }
Set-Location -LiteralPath $projectRoot
Write-Host "Python: $python"
& $python @args
exit $LASTEXITCODE
