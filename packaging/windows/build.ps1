# Builds the DevMem Windows installer. Windows PowerShell 5.1 or PowerShell 7; needs no Python on the machine.
#
#   powershell -NoProfile -ExecutionPolicy Bypass -File packaging\windows\build.ps1 [-RequireInstaller] [-NoCompile]
#
# 1. downloads the python.org embeddable package pinned in python-embed.json over HTTPS (kept in build\windows\download
#    and reused while its hash still matches) and checks its SHA-256 against the pinned value: on a mismatch the file
#    is deleted and the build stops;
# 2. extracts it once to run the assembler with that same verified interpreter, and assemble.py builds
#    build\windows\stage (python\ with its ._pth rewritten, app\devmem\, bin\devmem.cmd, the icon), byte-compiles the
#    package and checks that the bundled `python -m devmem --version` prints the package version;
# 3. runs Inno Setup's ISCC.exe when it is installed (-Iscc <path>, PATH, or the usual install folders), writing
#    dist\DevMem-Setup-<version>.exe and dist\DevMem-Setup-<version>.exe.sha256. Without ISCC.exe it stops after
#    step 2 with a message (exit 0), unless -RequireInstaller is given (exit 1).
[CmdletBinding()]
param(
    [string]$Iscc = "",
    [switch]$RequireInstaller,
    [switch]$NoCompile
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version 2.0
$ProgressPreference = "SilentlyContinue"   # Invoke-WebRequest is many times slower with the progress bar in 5.1

$Here = Split-Path -Parent $MyInvocation.MyCommand.Path
$Repo = (Resolve-Path (Join-Path $Here "..\..")).Path
$BuildDir = Join-Path $Repo "build\windows"
$DownloadDir = Join-Path $BuildDir "download"
$Stage = Join-Path $BuildDir "stage"
$Dist = Join-Path $Repo "dist"

function Fail([string]$Message) {
    Write-Host "build.ps1: $Message" -ForegroundColor Red
    exit 1
}

# ---------------------------------------------------------------- versions
$Pin = Get-Content -Raw -Encoding UTF8 (Join-Path $Here "python-embed.json") | ConvertFrom-Json
if ($Pin.version -notmatch '^3\.\d{1,2}\.\d{1,3}$') { Fail "python-embed.json: bad version '$($Pin.version)'" }
if ($Pin.sha256 -cnotmatch '^[0-9a-f]{64}$') { Fail "python-embed.json: sha256 must be 64 lower-case hex digits" }
$ExpectedUrl = "https://www.python.org/ftp/python/$($Pin.version)/python-$($Pin.version)-embed-$($Pin.arch).zip"
if ($Pin.arch -ne "amd64" -or $Pin.url -ne $ExpectedUrl) { Fail "python-embed.json: url must be $ExpectedUrl (amd64)" }

$InitPy = Get-Content -Raw -Encoding UTF8 (Join-Path $Repo "plugin\devmem\__init__.py")
$m = [regex]::Match($InitPy, '(?m)^__version__\s*=\s*["'']([^"'']+)["'']')
if (-not $m.Success) { Fail "no __version__ in plugin\devmem\__init__.py" }
$Version = $m.Groups[1].Value
if ($Version -notmatch '^\d+\.\d+\.\d+([a-z0-9.+-]*)$') { Fail "unexpected package version '$Version'" }
Write-Host "devmem $Version, bundled Python $($Pin.version) ($($Pin.arch))"

# ---------------------------------------------------------------- 1. download and verify
New-Item -ItemType Directory -Force -Path $DownloadDir | Out-Null
$Zip = Join-Path $DownloadDir ("python-{0}-embed-{1}.zip" -f $Pin.version, $Pin.arch)

function Get-Sha256([string]$Path) {
    return (Get-FileHash -Algorithm SHA256 -LiteralPath $Path).Hash.ToLowerInvariant()
}

if ((Test-Path -LiteralPath $Zip) -and ((Get-Sha256 $Zip) -ne $Pin.sha256)) {
    Write-Host "Cached $Zip does not match the pinned hash; downloading it again."
    Remove-Item -LiteralPath $Zip -Force
}
if (-not (Test-Path -LiteralPath $Zip)) {
    # TLS 1.2 explicitly: Windows PowerShell 5.1 may still default to older protocols
    [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
    $Part = "$Zip.part"
    if (Test-Path -LiteralPath $Part) { Remove-Item -LiteralPath $Part -Force }
    Write-Host "Downloading $($Pin.url)"
    Invoke-WebRequest -UseBasicParsing -Uri $Pin.url -OutFile $Part
    $Got = Get-Sha256 $Part
    if ($Got -ne $Pin.sha256) {
        Remove-Item -LiteralPath $Part -Force
        Fail "SHA-256 mismatch for $($Pin.url): expected $($Pin.sha256), got $Got. The download was deleted; nothing was built."
    }
    Move-Item -LiteralPath $Part -Destination $Zip
}
Write-Host "SHA-256 verified: $($Pin.sha256)"

# ---------------------------------------------------------------- 2. assemble with the verified interpreter
$Runtime = Join-Path $BuildDir ("runtime-" + $Pin.version)
if (Test-Path -LiteralPath $Runtime) { Remove-Item -LiteralPath $Runtime -Recurse -Force }
Expand-Archive -LiteralPath $Zip -DestinationPath $Runtime
$RuntimePy = Join-Path $Runtime "python.exe"
if (-not (Test-Path -LiteralPath $RuntimePy)) { Fail "no python.exe in $Zip" }

$AssembleArgs = @("-I", (Join-Path $Here "assemble.py"), "--zip", $Zip, "--out", $Stage, "--check")
if (-not $NoCompile) { $AssembleArgs += "--compile" }
& $RuntimePy @AssembleArgs
if ($LASTEXITCODE -ne 0) { Fail "assemble.py failed (exit $LASTEXITCODE)" }
Remove-Item -LiteralPath $Runtime -Recurse -Force
Write-Host "Assembled $Stage"

# ---------------------------------------------------------------- 3. installer
function Find-Iscc {
    if ($Iscc) {
        if (Test-Path -LiteralPath $Iscc) { return (Resolve-Path -LiteralPath $Iscc).Path }
        Fail "-Iscc $Iscc does not exist"
    }
    # the install folders first: on PATH there may be only Chocolatey's shim, which hides the real version
    $candidates = @()
    foreach ($base in @(${env:ProgramFiles(x86)}, $env:ProgramFiles, (Join-Path $env:LOCALAPPDATA "Programs"))) {
        if ($base) { $candidates += (Join-Path $base "Inno Setup 6\ISCC.exe") }
    }
    foreach ($c in $candidates) {
        if (Test-Path -LiteralPath $c) { return $c }
    }
    $cmd = Get-Command "ISCC.exe" -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    return $null
}

$IsccExe = Find-Iscc
if (-not $IsccExe) {
    $msg = "Inno Setup 6 (ISCC.exe) was not found, so no installer was built. The app folder is ready in $Stage. " +
           "Install Inno Setup 6 (https://jrsoftware.org/isinfo.php) or pass -Iscc <path to ISCC.exe>, then run this again."
    if ($RequireInstaller) { Fail $msg }
    Write-Host $msg -ForegroundColor Yellow
    exit 0
}
# ISCC.exe carries no usable version resource (it reads 0.0.0.0); it prints "Compiler engine version" itself
Write-Host "Using $IsccExe"

New-Item -ItemType Directory -Force -Path $Dist | Out-Null
$IsccArgs = @("/DAppVersion=$Version", "/DStageDir=$Stage", "/DOutputDir=$Dist")
if ($Version -match '^\d+\.\d+\.\d+$') { $IsccArgs += "/DAppNumericVersion=$Version" }
$IsccArgs += (Join-Path $Here "devmem.iss")
& $IsccExe @IsccArgs
if ($LASTEXITCODE -ne 0) { Fail "ISCC.exe failed (exit $LASTEXITCODE)" }

$Setup = Join-Path $Dist "DevMem-Setup-$Version.exe"
if (-not (Test-Path -LiteralPath $Setup)) { Fail "ISCC.exe did not write $Setup" }
$Hash = Get-Sha256 $Setup
# the format of `sha256sum`: "<hash>  <file name>", so `sha256sum -c` and Get-FileHash comparisons both work
[IO.File]::WriteAllText("$Setup.sha256", "$Hash  DevMem-Setup-$Version.exe`n", (New-Object Text.UTF8Encoding($false)))
Write-Host "Built $Setup"
Write-Host "SHA-256 $Hash"
