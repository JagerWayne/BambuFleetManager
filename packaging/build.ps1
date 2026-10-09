#requires -Version 5.1
<#
.SYNOPSIS
    Build the Bambu Fleet Manager tray app and Windows installer.

.DESCRIPTION
    Produces dist\BambuFleetManager\BambuFleetManager.exe (PyInstaller bundle)
    and, unless -NoInstaller is given, dist\BambuFleetManagerSetup.exe (Inno
    Setup installer). With -Release it also creates the git tag and uploads the
    installer to a GitHub Release so the in-app updater can find it.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File packaging\build.ps1
    powershell -ExecutionPolicy Bypass -File packaging\build.ps1 -Release
#>
param(
    [switch]$NoInstaller,
    [switch]$Release,
    [switch]$SkipPyInstaller,
    [string]$InnoPath = "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe"
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

function Info($msg) { Write-Host "[build] $msg" -ForegroundColor Cyan }
function Fail($msg) { Write-Host "[build] ERROR: $msg" -ForegroundColor Red; exit 1 }

# Run a native command without PowerShell 5.1 turning its stderr into a
# terminating error; return the process exit code instead.
function Invoke-Native {
    param([string]$File, [string[]]$Arguments)
    $eap = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    & $File @Arguments 2>&1 | ForEach-Object { Write-Host $_ }
    $code = $LASTEXITCODE
    $ErrorActionPreference = $eap
    return $code
}

$version = (Get-Content (Join-Path $root 'VERSION') -Raw).Trim()
if (-not $version) { Fail 'VERSION file is empty.' }
Info "Version $version"

# --- toolchain ------------------------------------------------------------
$py = Join-Path $root '.venv\Scripts\python.exe'
if (-not (Test-Path $py)) {
    Info 'Creating virtual environment ...'
    if ((Invoke-Native 'python' @('-m', 'venv', '.venv')) -ne 0) { Fail 'venv creation failed.' }
    $py = Join-Path $root '.venv\Scripts\python.exe'
}
Info 'Ensuring build dependencies ...'
# Prefer the pinned lockfile so the installer matches what CI tested;
# fall back to the floor manifest when the lock has not been generated.
$runtimeReqs = 'requirements.txt'
if (Test-Path (Join-Path $root 'requirements.lock')) { $runtimeReqs = 'requirements.lock' }
if ((Invoke-Native $py @('-m', 'pip', 'install', '--disable-pip-version-check', '--quiet', '-r', $runtimeReqs, '-r', 'requirements-dev.txt')) -ne 0) {
    Fail 'dependency installation failed.'
}

# refresh the icon (kept in sync with the installer)
if ((Invoke-Native $py @('packaging\make_icon.py')) -ne 0) { Fail 'icon generation failed.' }

# --- PyInstaller bundle ---------------------------------------------------
if (-not $SkipPyInstaller) {
    Info 'Building tray app with PyInstaller ...'
    if ((Invoke-Native $py @('-m', 'PyInstaller', 'packaging\bambu-fleet-manager.spec',
            '--noconfirm', '--clean', '--distpath', 'dist', '--workpath', 'build')) -ne 0) {
        Fail 'PyInstaller build failed.'
    }
} else {
    Info 'Skipping PyInstaller (-SkipPyInstaller).'
}
$bundle = Join-Path $root 'dist\BambuFleetManager'
if (-not (Test-Path $bundle)) { Fail "Bundle not found: $bundle" }

# smoke test the frozen exe on a scratch port
Info 'Smoke-testing the frozen app ...'
$scratch = Join-Path $env:TEMP ("bfm_selftest_" + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Force -Path (Join-Path $scratch 'config') | Out-Null
'{"host":"127.0.0.1","port":8137}' | Set-Content (Join-Path $scratch 'config\settings.json')
$env:BFM_DATA_DIR = $scratch
$proc = Start-Process -FilePath (Join-Path $bundle 'BambuFleetManager.exe') `
    -ArgumentList '--selftest' -Wait -PassThru
$result = Get-Content (Join-Path $scratch 'selftest.txt') -ErrorAction SilentlyContinue
Remove-Item -Recurse -Force $scratch -ErrorAction SilentlyContinue
Remove-Item Env:\BFM_DATA_DIR -ErrorAction SilentlyContinue
if ($proc.ExitCode -ne 0 -or ($result -notmatch '^OK')) {
    Fail "Frozen app smoke test failed (exit $($proc.ExitCode), result '$result')."
}
Info "Smoke test: $result"

# --- Inno Setup installer -------------------------------------------------
$setup = Join-Path $root 'dist\BambuFleetManagerSetup.exe'
if (-not $NoInstaller) {
    if (-not (Test-Path $InnoPath)) {
        Fail "Inno Setup compiler not found at '$InnoPath'. Install it (winget install JRSoftware.InnoSetup) or pass -InnoPath."
    }
    Info 'Compiling installer with Inno Setup ...'
    if ((Invoke-Native $InnoPath @("/DMyAppVersion=$version", 'packaging\installer.iss')) -ne 0) {
        Fail 'Inno Setup compilation failed.'
    }
    Info "Installer: $setup"
} else {
    Info 'Skipping installer (-NoInstaller).'
}

# --- GitHub release -------------------------------------------------------
if ($Release) {
    if ($NoInstaller) { Fail '-Release requires the installer (drop -NoInstaller).' }
    $tag = "v$version"
    Info "Publishing GitHub release $tag ..."
    if ((Invoke-Native 'git' @('tag', '-f', $tag)) -ne 0) { Fail 'git tag failed.' }
    if ((Invoke-Native 'git' @('push', '--force', 'origin', $tag)) -ne 0) { Fail 'git push failed.' }
    $hasRelease = (Invoke-Native 'gh' @('release', 'view', $tag)) -eq 0
    if ($hasRelease) {
        if ((Invoke-Native 'gh' @('release', 'upload', $tag, $setup, '--clobber')) -ne 0) { Fail 'release upload failed.' }
    } else {
        if ((Invoke-Native 'gh' @('release', 'create', $tag, $setup, '--title', "Bambu Fleet Manager $version", '--generate-notes')) -ne 0) {
            Fail 'GitHub release failed.'
        }
    }
    Info "Release $tag published."
}

Info 'Done.'
