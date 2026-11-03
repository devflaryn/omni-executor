<#
.SYNOPSIS
    Build omni-exec.exe (the Windows Omni Executor) from this checkout.

.DESCRIPTION
    Windows counterpart of build-macos.sh. Three stages, in this order:

      1. npm     -- build the React frontend into frontend\dist
      2. PyInstaller -- freeze main.py into the headless backend,
                    omni-exec-py.exe (no engine inside: native omnidroid is
                    a separate bundle the app downloads)
      3. cargo   -- build the Tauri shell, omni-exec.exe, which EMBEDS
                    frontend\dist and spawns the backend over stdio

    and then assembles all three into one folder, dist\omni-exec\, which is
    what gets zipped. The shell must be built after the frontend: Tauri bakes
    the built assets into the binary at compile time.

    ONE-DIR, not one-file -- see the header of OmniExecutor-win.spec for why.

    Neither PyInstaller nor cargo cross-builds here: this must run ON Windows,
    and it needs Node, Python with PyInstaller, AND a Rust toolchain.

.PARAMETER SkipFrontend
    Reuse the existing frontend\dist instead of running npm.

.PARAMETER Zip
    Also produce <DistPath>\omni-exec-win64.zip.

.PARAMETER Installer
    Also build OmniExecutorSetup.exe into <DistPath> with Inno Setup (ISCC).
    It CARRIES the build -- nothing is downloaded at install time -- because
    the old PyInstaller download stub was reported as a trojan downloader by
    antivirus engines (see packaging\OmniExecutor.iss). An installer also
    writes files without the Mark-of-the-Web a downloaded .zip puts on them.
    It is rebuilt for every release, since it contains that release.

.PARAMETER DistPath
    Output directory (default: dist). PyInstaller DELETES and recreates this
    folder, so the build fails with "WinError 32 / access denied" if anything
    holds it open -- an Explorer window showing it is enough, and so is a
    shell whose working directory is inside it. Build somewhere else rather
    than hunting the lock: -DistPath dist-new

.EXAMPLE
    .\build-windows.ps1 -Zip

.EXAMPLE
    .\build-windows.ps1 -SkipFrontend -DistPath dist-new
#>
[CmdletBinding()]
param(
    [switch]$SkipFrontend,
    [switch]$Zip,
    [switch]$Installer,
    [string]$DistPath = "dist"
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

Assert-Command -Name "cargo" -Fix "Install Rust (https://rustup.rs) -- the window is a Tauri app now."

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

# --- freeze the backend --------------------------------------------------
# omni-exec-py.exe is the headless half: the Api the window calls over stdio.
# It is NOT what the user launches; see the assembly step below.
Write-Host "==> freezing backend (PyInstaller, one-dir) -> $DistPath"
Invoke-Native -What "PyInstaller" -Command {
    & $python -m PyInstaller --noconfirm --distpath $DistPath OmniExecutor-win.spec
}

$backendDir = Join-Path $DistPath "omni-exec-py"
$backend = Join-Path $backendDir "omni-exec-py.exe"
if (-not (Test-Path $backend)) { throw "expected $backend to exist after the build" }

# --- build the shell -----------------------------------------------------
# The window. Cargo, not `tauri build`: the installer (Inno Setup, below) has
# to carry the backend's whole PyInstaller tree beside the shell, which the
# NSIS/MSI bundles the Tauri CLI produces do not lay out -- only the exe.
# Compiled code records where it was built, home folder and all. Strip that,
# and keep only the file name of the debug-symbols path.
$env:RUSTFLAGS = "--remap-path-prefix=$env:USERPROFILE=C:\build -C link-arg=/PDBALTPATH:%_PDB%"
Write-Host "==> building shell (cargo, release) -> src-tauri\target\release"
Invoke-Native -What "cargo build" -Command {
    cargo build --release --features custom-protocol `
        --manifest-path (Join-Path $PSScriptRoot "src-tauri\Cargo.toml")
}
$shell = Join-Path $PSScriptRoot "src-tauri\target\release\omni-exec.exe"
if (-not (Test-Path $shell)) { throw "expected $shell to exist after the build" }

# DID THE FRONTEND ACTUALLY GO IN? Without `--features custom-protocol` the
# build succeeds, the exe runs, and it loads build.devUrl instead of its own
# assets -- so it works on a developer's machine with Vite up and shows
# "localhost refused to connect" everywhere else. That shipped once. The proof
# is cheap: the hashed asset name from frontend/dist must appear in the binary.
$asset = (Get-ChildItem (Join-Path $PSScriptRoot "frontend\dist\assets") -Filter "main-*.js" |
          Select-Object -First 1).Name
if (-not $asset) { throw "no main-*.js in frontend\dist\assets -- was the frontend built?" }
# Latin-1 maps every byte to one char, so a binary read back this way can be
# searched for an ASCII needle without false negatives from encoding.
$binary = [System.Text.Encoding]::GetEncoding(28591).GetString(
              [System.IO.File]::ReadAllBytes($shell))
if (-not $binary.Contains($asset)) {
    throw "the shell does not embed frontend\dist ($asset is not in the binary). " +
          "It would open a 'localhost refused to connect' page. This means the " +
          "custom-protocol feature did not take -- see src-tauri\Cargo.toml."
}
Write-Host "    frontend embedded OK ($asset)"

# --- assemble ------------------------------------------------------------
# One folder, two executables, side by side. The layout is load-bearing:
# updates.app_dir() is the backend's own directory, so the backend must sit at
# the ROOT of the install for the file-by-file updater to replace the right
# tree, and backend.rs looks for omni-exec-py.exe beside the shell.
$appDir = Join-Path $DistPath "omni-exec"
Write-Host "==> assembling -> $appDir"
if (Test-Path $appDir) { Remove-Item $appDir -Recurse -Force }
New-Item -ItemType Directory -Path $appDir | Out-Null
Copy-Item -Path (Join-Path $backendDir "*") -Destination $appDir -Recurse -Force
Copy-Item -Path $shell -Destination $appDir -Force
Remove-Item $backendDir -Recurse -Force

$exe = Join-Path $appDir "omni-exec.exe"              # what the user launches
$backendExe = Join-Path $appDir "omni-exec-py.exe"   # what it spawns
if (-not (Test-Path $exe)) { throw "expected $exe to exist after assembly" }
if (-not (Test-Path $backendExe)) { throw "expected $backendExe to exist after assembly" }

# --- nothing that ships may name this machine's home folder ---------------
foreach ($file in Get-ChildItem $appDir -Recurse -File -Include *.exe,*.dll,*.pyd) {
    $text = [System.Text.Encoding]::GetEncoding(28591).GetString([System.IO.File]::ReadAllBytes($file.FullName))
    if ($text.IndexOf($env:USERPROFILE, [StringComparison]::OrdinalIgnoreCase) -ge 0) {
        throw "$($file.FullName) contains $env:USERPROFILE"
    }
}

# --- smoke test ----------------------------------------------------------
# The frozen backend starts, imports everything it ships, and reports the
# version this build was stamped with.
Write-Host "==> smoke test: omni-exec-py.exe --doctor"
# Start-Process with separate stdout/stderr files, NOT `& $exe ... 2>&1`:
# Windows PowerShell 5.1 wraps native stderr in ErrorRecords, which made this
# script exit non-zero on a build that had actually succeeded.
$so = Join-Path $env:TEMP "omni-exec-smoke.out"
$se = Join-Path $env:TEMP "omni-exec-smoke.err"
$p = Start-Process -FilePath $backendExe -ArgumentList '--doctor' `
                   -NoNewWindow -Wait -PassThru `
                   -RedirectStandardOutput $so -RedirectStandardError $se
$out = (Get-Content $so -Raw -ErrorAction SilentlyContinue)
$err = (Get-Content $se -Raw -ErrorAction SilentlyContinue)
Remove-Item $so, $se -Force -ErrorAction SilentlyContinue
if ($err) { Write-Host "    (stderr) $($err.Trim())" }
if ($p.ExitCode -ne 0) {
    throw "backend smoke test FAILED -- exit code $($p.ExitCode). $err"
}
if ($out -notmatch '"app_version"') {
    throw "backend smoke test FAILED -- expected JSON with app_version, got: $out"
}
Write-Host "    backend OK"

# --- package -------------------------------------------------------------
if ($Zip) {
    # $zipPath, not $zip: PowerShell variable names are case-INSENSITIVE, so
    # `$zip = "..."` assigns a String to the [switch]$Zip parameter and the
    # script dies with a SwitchParameter cast error.
    $zipPath = Join-Path $DistPath "omni-exec-win64.zip"
    if (Test-Path $zipPath) { Remove-Item $zipPath -Force }
    Write-Host "==> zipping -> $zipPath"
    Compress-Archive -Path (Join-Path $DistPath "omni-exec\*") -DestinationPath $zipPath
    Write-Host ("    {0:N1} MB" -f ((Get-Item $zipPath).Length / 1MB))
}

# --- installer ------------------------------------------------------------
if ($Installer) {
    Write-Host "==> building OmniExecutorSetup.exe (Inno Setup)"
    $iscc = (Get-Command ISCC.exe -ErrorAction SilentlyContinue).Source
    if (-not $iscc) {
        foreach ($dir in @("$env:LOCALAPPDATA\Programs\Inno Setup 6",
                           "${env:ProgramFiles(x86)}\Inno Setup 6",
                           "$env:ProgramFiles\Inno Setup 6")) {
            if (Test-Path (Join-Path $dir "ISCC.exe")) { $iscc = Join-Path $dir "ISCC.exe"; break }
        }
    }
    if (-not $iscc) {
        throw "Inno Setup 6 is not installed. Run: winget install JRSoftware.InnoSetup --scope user"
    }
    $version = (& $python -c "import updates; print(updates.APP_VERSION)").Trim()
    $source = (Resolve-Path (Join-Path $DistPath "omni-exec")).Path
    $outDir = (Resolve-Path $DistPath).Path
    # Up to three tries. ISCC writes the setup exe's resources AFTER creating
    # it, and real-time antivirus scanning that brand-new exe holds it open:
    # "EndUpdateResource failed, try excluding the Output folder from your
    # antivirus software (110)". It is transient -- the next try passes.
    $iss = Join-Path $PSScriptRoot "packaging\OmniExecutor.iss"
    foreach ($try in 1..3) {
        try {
            Invoke-Native -What "ISCC" -Command {
                & $iscc /Q "/DAppVersion=$version" "/DSourceDir=$source" "/DOutputDir=$outDir" $iss
            }
            break
        } catch {
            if ($try -eq 3) { throw }
            Write-Host "    ISCC failed (try $try): $_ -- retrying"
            Start-Sleep -Seconds 5
        }
    }
    $setup = Join-Path $DistPath "OmniExecutorSetup.exe"
    if (-not (Test-Path $setup)) { throw "expected $setup to exist after the build" }
    Write-Host ("    {0:N1} MB" -f ((Get-Item $setup).Length / 1MB))
}

Write-Host ""
Write-Host "==> done: $exe"
Write-Host "    NOTE: unsigned. SmartScreen will warn on first run until the"
Write-Host "    binary is Authenticode-signed (out of scope for v1)."
