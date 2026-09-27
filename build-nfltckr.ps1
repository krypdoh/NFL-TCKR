#Requires -Version 5.1
<#
.SYNOPSIS
    NFL-TCKR combined build + sign script.

.DESCRIPTION
    1.  Reads VERSION from NFL-TCKR.py and expands to 4-part form (e.g. 0.1.23.0).
    2.  Updates version-nfl-tckr.txt with the new version numbers.
    3.  Pauses Dropbox to prevent file-lock conflicts.
    4.  Verifies Python is available.
    5.  Verifies PyInstaller, PyQt5, and requests.
    6.  Builds both onefile EXEs:
          dist\NFL-TCKR-console.exe   (console window visible)
          dist\NFL-TCKR.exe           (console window hidden)
    7.  Creates / reuses a self-signed PFX (same publisher as MLB-TCKR).
    8.  Authenticode-signs each EXE into dist\signed.
    9.  Restarts Dropbox.

.NOTES
    Same signing approach as MLB-TCKR (self-signed PFX + DigiCert timestamp).
    Do not run rcedit on these onefile EXEs — it strips the PE overlay.
#>

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

# ── Paths ──────────────────────────────────────────────────────────────────
$ScriptDir   = Split-Path -Parent $MyInvocation.MyCommand.Definition
$DistDir     = Join-Path $ScriptDir "dist"
$SignedDir   = Join-Path $DistDir "signed"
$DropboxExe  = "C:\Program Files (x86)\Dropbox\Client\Dropbox.exe"
$Publisher   = "Paul R. Charovkine"
$PfxPassword = "charovkine"
$PfxPath     = Join-Path $DistDir "NFL_SelfSigned.pfx"
$MlbPfx      = Join-Path (Split-Path $ScriptDir -Parent) "MLB-TCKR\dist\MLB_SelfSigned.pfx"

function Resolve-Python {
    $candidates = @(
        "$env:USERPROFILE\AppData\Local\Programs\Python\Python314\python.exe",
        "$env:USERPROFILE\AppData\Local\Programs\Python\Python313\python.exe",
        "$env:USERPROFILE\AppData\Local\Programs\Python\Python312\python.exe"
    )
    foreach ($path in $candidates) {
        if (Test-Path $path) { return $path }
    }
    $fromPath = Get-Command python -ErrorAction SilentlyContinue
    if ($fromPath) { return $fromPath.Source }
    return $null
}

$PythonExe = Resolve-Python

function Write-Step {
    param ([int]$n, [int]$total, [string]$msg)
    Write-Host ""
    Write-Host "-- Step $n/$total  $msg" -ForegroundColor Cyan
}

function Fail {
    param ([string]$msg)
    Write-Host ""
    Write-Host "ERROR: $msg" -ForegroundColor Red
    if (Test-Path $DropboxExe) {
        cmd /c "start /b `"`" `"$DropboxExe`" >nul 2>&1"
        Write-Host "Dropbox restarted." -ForegroundColor Yellow
    }
    Write-Host ""
    exit 1
}

$TOTAL_STEPS = 9

Write-Host ""
Write-Host "==========================================" -ForegroundColor Cyan
Write-Host "   NFL-TCKR  Build + Sign  ($TOTAL_STEPS steps)  " -ForegroundColor Cyan
Write-Host "==========================================" -ForegroundColor Cyan

# ══════════════════════════════════════════════════════════════════════════
# STEP 1 — Extract version from NFL-TCKR.py
# ══════════════════════════════════════════════════════════════════════════
Write-Step 1 $TOTAL_STEPS "Extract version from NFL-TCKR.py"

$PyFile = Join-Path $ScriptDir "NFL-TCKR.py"
if (-not (Test-Path $PyFile)) { Fail "NFL-TCKR.py not found at $PyFile" }

$VersionHit = Select-String -Path $PyFile -Pattern '^\s*VERSION\s*=\s*"(\d+\.\d+\.\d+)"' |
              Select-Object -First 1
if (-not $VersionHit) {
    Fail "Could not parse VERSION = `"x.y.z`" from NFL-TCKR.py"
}
$AppVersion  = $VersionHit.Matches[0].Groups[1].Value
$Parts       = $AppVersion.Split('.')
$VerMajor    = [int]$Parts[0]
$VerMinor    = [int]$Parts[1]
$VerPatch    = [int]$Parts[2]
$AppVersion4 = "$VerMajor.$VerMinor.$VerPatch.0"

Write-Host "  Version  :  $AppVersion -> $AppVersion4" -ForegroundColor Green

# ══════════════════════════════════════════════════════════════════════════
# STEP 2 — Update version-nfl-tckr.txt
# ══════════════════════════════════════════════════════════════════════════
Write-Step 2 $TOTAL_STEPS "Update version-nfl-tckr.txt -> $AppVersion4"

$VersionTxt = Join-Path $ScriptDir "version-nfl-tckr.txt"
if (Test-Path $VersionTxt) {
    $content = Get-Content $VersionTxt -Raw -Encoding UTF8
    $content = $content -replace 'filevers=\([^)]*\)', "filevers=($VerMajor, $VerMinor, $VerPatch, 0)"
    $content = $content -replace 'prodvers=\([^)]*\)', "prodvers=($VerMajor, $VerMinor, $VerPatch, 0)"
    $content = $content -replace "(?<=FileVersion', u')[^']+",  $AppVersion4
    $content = $content -replace "(?<=ProductVersion', u')[^']+", $AppVersion4
    Set-Content $VersionTxt $content -Encoding UTF8 -NoNewline
    Write-Host "  Done." -ForegroundColor Green
} else {
    Write-Host "  WARNING: version-nfl-tckr.txt not found - skipping." -ForegroundColor Yellow
}

# ══════════════════════════════════════════════════════════════════════════
# STEP 3 — Pause Dropbox
# ══════════════════════════════════════════════════════════════════════════
Write-Step 3 $TOTAL_STEPS "Pause Dropbox sync"

Stop-Process -Name Dropbox -ErrorAction SilentlyContinue
Start-Sleep -Seconds 2
Write-Host "  Dropbox stopped." -ForegroundColor Green

# ══════════════════════════════════════════════════════════════════════════
# STEP 4 — Verify Python
# ══════════════════════════════════════════════════════════════════════════
Write-Step 4 $TOTAL_STEPS "Verify Python"

if (-not $PythonExe -or -not (Test-Path $PythonExe)) {
    Fail "Python not found. Install 3.12+ or edit Resolve-Python in this script."
}
$PyVer = & $PythonExe --version 2>&1
Write-Host "  $PyVer  ($PythonExe)" -ForegroundColor Green

# ══════════════════════════════════════════════════════════════════════════
# STEP 5 — Verify PyInstaller + runtime deps
# ══════════════════════════════════════════════════════════════════════════
Write-Step 5 $TOTAL_STEPS "Verify PyInstaller, PyQt5, requests"

& $PythonExe -m PyInstaller --version 2>$null
if ($LASTEXITCODE -ne 0) {
    Fail "PyInstaller not found.  Run:  `"$PythonExe`" -m pip install pyinstaller"
}
Write-Host "  PyInstaller: OK" -ForegroundColor Green

& $PythonExe -c "import PyQt5, requests, certifi; print('  PyQt5 + requests + certifi: OK')"
if ($LASTEXITCODE -ne 0) {
    Fail "Missing Python packages.  Run:  `"$PythonExe`" -m pip install PyQt5 requests certifi"
}

# ══════════════════════════════════════════════════════════════════════════
# STEP 6 — Build EXEs with PyInstaller
# ══════════════════════════════════════════════════════════════════════════
Write-Step 6 $TOTAL_STEPS "Build onefile EXEs with PyInstaller"

Push-Location $ScriptDir

Write-Host "  Building NFL-TCKR-console.exe ..." -ForegroundColor Yellow
& $PythonExe -m PyInstaller NFL-TCKR-console.spec --noconfirm --clean
if ($LASTEXITCODE -ne 0) {
    Pop-Location
    Fail "PyInstaller failed building NFL-TCKR-console.exe (exit $LASTEXITCODE)"
}
if (-not (Test-Path (Join-Path $DistDir "NFL-TCKR-console.exe"))) {
    Pop-Location
    Fail "Console EXE not produced - check PyInstaller output above."
}
Write-Host "  Console EXE: OK" -ForegroundColor Green

Write-Host "  Building NFL-TCKR.exe (no console) ..." -ForegroundColor Yellow
& $PythonExe -m PyInstaller NFL-TCKR.spec --noconfirm --clean
if ($LASTEXITCODE -ne 0) {
    Pop-Location
    Fail "PyInstaller failed building NFL-TCKR.exe (exit $LASTEXITCODE)"
}
if (-not (Test-Path (Join-Path $DistDir "NFL-TCKR.exe"))) {
    Pop-Location
    Fail "No-console EXE not produced - check PyInstaller output above."
}
Write-Host "  No-console EXE: OK" -ForegroundColor Green

Pop-Location

# ══════════════════════════════════════════════════════════════════════════
# STEP 7 — Ensure PFX code-signing certificate
# ══════════════════════════════════════════════════════════════════════════
Write-Step 7 $TOTAL_STEPS "Code-signing certificate"

if (-not (Test-Path $DistDir)) { New-Item -ItemType Directory -Path $DistDir | Out-Null }

if (-not (Test-Path $PfxPath) -and (Test-Path $MlbPfx)) {
    Copy-Item -Path $MlbPfx -Destination $PfxPath -Force
    Write-Host "  Reusing MLB-TCKR certificate: $PfxPath" -ForegroundColor Green
}

if (-not (Test-Path $PfxPath)) {
    Write-Host "  Creating self-signed code-signing certificate..." -ForegroundColor Magenta
    $NewCert    = New-SelfSignedCertificate -Type CodeSigningCert -Subject "CN=$Publisher" `
                    -CertStoreLocation Cert:\CurrentUser\My `
                    -KeyUsage DigitalSignature -FriendlyName "NFL-TCKR Signing"
    $SecurePwd  = ConvertTo-SecureString -String $PfxPassword -Force -AsPlainText
    Export-PfxCertificate -Cert $NewCert -FilePath $PfxPath -Password $SecurePwd | Out-Null
    Write-Host "  Created: $PfxPath" -ForegroundColor Green
} else {
    Write-Host "  Certificate found: $PfxPath" -ForegroundColor Green
}

$CertObj = [System.Security.Cryptography.X509Certificates.X509Certificate2]::new(
                 $PfxPath, $PfxPassword,
                 [System.Security.Cryptography.X509Certificates.X509KeyStorageFlags]::DefaultKeySet
             )

$RootStore = New-Object System.Security.Cryptography.X509Certificates.X509Store("Root", "CurrentUser")
$RootStore.Open("ReadWrite")
if (-not ($RootStore.Certificates | Where-Object { $_.Thumbprint -eq $CertObj.Thumbprint })) {
    Write-Host "  Adding certificate to Trusted Root store..." -ForegroundColor Magenta
    $RootStore.Add($CertObj)
}
$RootStore.Close()

# ══════════════════════════════════════════════════════════════════════════
# STEP 8 — Authenticode sign
# ══════════════════════════════════════════════════════════════════════════
# NOTE: rcedit is NOT used. PyInstaller onefile stores the payload in the PE
# overlay; rcedit rewrites resources and discards the overlay (corrupts the EXE).
# Version metadata is already embedded via version-nfl-tckr.txt.
Write-Step 8 $TOTAL_STEPS "Authenticode sign (v$AppVersion4)"

if (-not (Test-Path $SignedDir)) { New-Item -ItemType Directory -Path $SignedDir | Out-Null }

$Exes = Get-ChildItem -Path $DistDir -Filter "NFL-TCKR*.exe"
foreach ($Exe in $Exes) {
    $Dest = Join-Path $SignedDir $Exe.Name
    Write-Host "  Copying  : $($Exe.Name) ($([math]::Round($Exe.Length/1MB,1)) MB)" -ForegroundColor Cyan
    Copy-Item -Path $Exe.FullName -Destination $Dest -Force

    Write-Host "  Signing  : $($Exe.Name)  [$AppVersion4]" -ForegroundColor Yellow
    Set-AuthenticodeSignature -FilePath $Dest -Certificate $CertObj `
        -TimestampServer "http://timestamp.digicert.com"
}

# ══════════════════════════════════════════════════════════════════════════
# STEP 9 — Restart Dropbox
# ══════════════════════════════════════════════════════════════════════════
Write-Step 9 $TOTAL_STEPS "Restart Dropbox"

if (Test-Path $DropboxExe) {
    cmd /c "start /b `"`" `"$DropboxExe`" >nul 2>&1"
    Write-Host "  Dropbox restarted." -ForegroundColor Green
} else {
    Write-Host "  Dropbox not found at expected path - skipping." -ForegroundColor Yellow
}

Write-Host ""
Write-Host "==========================================" -ForegroundColor Green
Write-Host "   BUILD + SIGN COMPLETE  v$AppVersion4   " -ForegroundColor Green
Write-Host "==========================================" -ForegroundColor Green
Write-Host ""
Write-Host "  Signed EXEs ->  $SignedDir" -ForegroundColor Cyan
Write-Host "    NFL-TCKR-console.exe   (console window visible)"
Write-Host "    NFL-TCKR.exe           (console window hidden)"
Write-Host ""
