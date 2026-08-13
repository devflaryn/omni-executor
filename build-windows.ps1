<#
.SYNOPSIS
    Build omni-exec.exe (the Windows Omni Executor) from this checkout.

.DESCRIPTION
    Windows counterpart of build-macos.sh. Builds the React frontend, then
    freezes main.py + the sibling omnidroid engine into dist\omni-exec\ via
    PyInstaller, and (optionally) zips the folder for distribution.

    ONE-DIR, not one-file -- see the header of OmniExecutor-win.spec for why
    (engine dispatch re-executes the frozen binary per call, and a one-file
    build would re-unpack to a throwaway %TEMP% each time).

    PyInstaller cannot cross-build: this must run ON Windows.

.PARAMETER SkipFrontend
    Reuse the existing frontend\dist instead of running npm.

.PARAMETER Zip
    Also produce dist\omni-exec-win64.zip.

.EXAMPLE
    .\build-windows.ps1 -Zip
#>
[CmdletBinding()]
param(
    [switch]$SkipFrontend,
    [switch]$Zip
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot

function Assert-Command {
    param([string]$Name, [string]$Fix)
    if (-not (Get-Command $Name -ErrorAction SilentlyContinue)) {
        throw "$Name not found on PATH. $Fix"
    }
}

function Invoke-Native {
    <#
        Run a native executable and fail on its EXIT CODE, not on whether it
        wrote to stderr.

        Windows PowerShell 5.1 wraps every stderr line from a native command
        in an ErrorRecord, so with $ErrorActionPreference = "Stop" a perfectly
        successful tool that logs progress to stderr -- npm and PyInstaller
        both do -- aborts the build. Exit code is the only trustworthy signal.
    #>
    param(
        [Parameter(Mandatory = $true)][scriptblock]$Command,
        [Parameter(Mandatory = $true)][string]$What
    )
    $prev = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        & $Command 2>&1 | ForEach-Object { "$_" }
    } finally {
        $ErrorActionPreference = $prev
    }
    if ($LASTEXITCODE -ne 0) {
        throw "$What failed (exit code $LASTEXITCODE)"
    }
}

# --- preflight -----------------------------------------------------------
# $env:OS, not $IsWindows: the latter only exists in PowerShell 6+, and under
# Set-StrictMode referencing it on Windows PowerShell 5.1 -- the shell that
# ships with Windows, and the one this script is most likely to run under --
# is a hard error rather than $false.
if ($env:OS -ne "Windows_NT") {
    throw "PyInstaller cannot cross-build a Windows exe. Run this on Windows."
}

$python = if ($env:OMNIEXEC_PYTHON) { $env:OMNIEXEC_PYTHON }
          elseif (Test-Path ".\.venv\Scripts\python.exe") { ".\.venv\Scripts\python.exe" }
          else { "python" }
Write-Host "==> python: $python"

$prev = $ErrorActionPreference
$ErrorActionPreference = "Continue"
& $python -c "import PyInstaller" 2>&1 | Out-Null
$hasPyInstaller = ($LASTEXITCODE -eq 0)
$ErrorActionPreference = $prev
if (-not $hasPyInstaller) {
    throw "PyInstaller is not installed for '$python'. Run: $python -m pip install pyinstaller"
}

$omnidroid = Join-Path (Split-Path -Parent $PSScriptRoot) "omnidroid"
if (-not (Test-Path (Join-Path $omnidroid "omnidroid\__init__.py"))) {
    throw @"
Sibling omnidroid checkout not found at:
  $omnidroid
The frozen app embeds the engine (there is no omnidroid.exe next to it), so
the package must be importable at build time. Lay the repos out as siblings.
"@
}
Write-Host "==> engine: $omnidroid"

# --- frontend ------------------------------------------------------------
if ($SkipFrontend) {
    if (-not (Test-Path ".\frontend\dist\index.html")) {
        throw "-SkipFrontend was passed but frontend\dist\index.html does not exist."
    }
    Write-Host "==> skipping frontend build (reusing frontend\dist)"
} else {
    Assert-Command -Name "npm" -Fix "Install Node.js (https://nodejs.org)."
    Write-Host "==> building frontend"
    Push-Location frontend
    try {
        Invoke-Native -What "npm install" -Command { npm install }
        Invoke-Native -What "npm run build" -Command { npm run build }
    } finally {
        Pop-Location
    }
}

# --- freeze --------------------------------------------------------------
Write-Host "==> freezing app (PyInstaller, one-dir)"
Invoke-Native -What "PyInstaller" -Command {
    & $python -m PyInstaller --noconfirm OmniExecutor-win.spec
}

$exe = ".\dist\omni-exec\omni-exec.exe"
if (-not (Test-Path $exe)) { throw "expected $exe to exist after the build" }

# --- smoke test ----------------------------------------------------------
# Proves the in-binary engine dispatch works: no omnidroid.exe, no source
# checkout -- the frozen binary re-executes itself and runs the engine CLI.
Write-Host "==> smoke test: omni-exec.exe --omnidroid version --json"
# Start-Process with separate stdout/stderr files, NOT `& $exe ... 2>&1`.
# The engine writes housekeeping lines ("[config] created default config: …")
# to stderr, and Windows PowerShell 5.1 wraps native stderr in ErrorRecords —
# which made this script exit non-zero on a build that had actually succeeded.
$so = Join-Path $env:TEMP "omni-exec-smoke.out"
$se = Join-Path $env:TEMP "omni-exec-smoke.err"
$p = Start-Process -FilePath $exe -ArgumentList '--omnidroid', 'version', '--json' `
                   -NoNewWindow -Wait -PassThru `
                   -RedirectStandardOutput $so -RedirectStandardError $se
$out = (Get-Content $so -Raw -ErrorAction SilentlyContinue)
$err = (Get-Content $se -Raw -ErrorAction SilentlyContinue)
Remove-Item $so, $se -Force -ErrorAction SilentlyContinue
if ($err) { Write-Host "    (stderr) $($err.Trim())" }
if ($p.ExitCode -ne 0) {
    throw "engine dispatch smoke test FAILED -- exit code $($p.ExitCode). $err"
}
if ($out -notmatch '"version"') {
    throw "engine dispatch smoke test FAILED -- expected JSON with a version field, got: $out"
}
Write-Host "    engine dispatch OK"

# --- package -------------------------------------------------------------
if ($Zip) {
    # $zipPath, not $zip: PowerShell variable names are case-INSENSITIVE, so
    # `$zip = "..."` assigns a String to the [switch]$Zip parameter and the
    # script dies with a SwitchParameter cast error.
    $zipPath = ".\dist\omni-exec-win64.zip"
    if (Test-Path $zipPath) { Remove-Item $zipPath -Force }
    Write-Host "==> zipping -> $zipPath"
    Compress-Archive -Path ".\dist\omni-exec\*" -DestinationPath $zipPath
    Write-Host ("    {0:N1} MB" -f ((Get-Item $zipPath).Length / 1MB))
}

Write-Host ""
Write-Host "==> done: dist\omni-exec\omni-exec.exe"
Write-Host "    NOTE: unsigned. SmartScreen will warn on first run until the"
Write-Host "    binary is Authenticode-signed (out of scope for v1)."
