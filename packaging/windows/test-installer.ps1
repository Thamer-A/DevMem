# Tests the DevMem Windows installer for real: silent per-user install, the installed `devmem` and app, silent uninstall.
# Meant for a throwaway machine (the GitHub Actions runner, a VM): it installs into %LOCALAPPDATA%\Programs\DevMem and
# changes the user's PATH (restored at the end). Windows PowerShell 5.1 or PowerShell 7.
#
#   powershell -NoProfile -ExecutionPolicy Bypass -File packaging\windows\test-installer.ps1 -Installer dist\DevMem-Setup-0.4.0.exe
#   ... -AppDir build\windows\stage      only the checks of an assembled or installed folder; installs nothing, PATH untouched
#
# Every devmem command runs with DEVMEM_USER_DATA in a temporary folder and in a temporary project.
[CmdletBinding()]
param(
    [string]$Installer = "",
    [string]$AppDir = "",
    [string]$WorkDir = ""
)

$ErrorActionPreference = "Continue"   # native commands write to stderr on purpose (exit status checks); asserts throw
Set-StrictMode -Version 2.0

$Here = Split-Path -Parent $MyInvocation.MyCommand.Path
$Repo = (Resolve-Path (Join-Path $Here "..\..")).Path
$InitPy = Get-Content -Raw -Encoding UTF8 (Join-Path $Repo "plugin\devmem\__init__.py")
$Version = [regex]::Match($InitPy, '(?m)^__version__\s*=\s*["'']([^"'']+)["'']').Groups[1].Value
if (-not $Version) { throw "no __version__ in plugin\devmem\__init__.py" }

if (-not $WorkDir) { $WorkDir = Join-Path ([IO.Path]::GetTempPath()) ("devmem-installer-test-" + [guid]::NewGuid().ToString("N").Substring(0, 8)) }
New-Item -ItemType Directory -Force -Path $WorkDir | Out-Null
$WorkDir = (Resolve-Path -LiteralPath $WorkDir).Path
$env:DEVMEM_USER_DATA = Join-Path $WorkDir "userdata"
foreach ($k in @("PYTHONPATH", "PYTHONHOME", "PYTHONSTARTUP", "PYTHONUSERBASE")) { Remove-Item -Path "Env:$k" -ErrorAction SilentlyContinue }

$script:Failures = 0
# any terminating error (a missing file read with -ErrorAction Stop, a property that is not there) counts as a failure
trap { Write-Host "  FAIL  error: $($_.Exception.Message)" -ForegroundColor Red; $script:Failures++; continue }
function Check([bool]$Ok, [string]$What) {
    if ($Ok) { Write-Host "  ok    $What" }
    else { Write-Host "  FAIL  $What" -ForegroundColor Red; $script:Failures++ }
}
function Step([string]$Title) { Write-Host ""; Write-Host "== $Title" -ForegroundColor Cyan }

function Get-UserPathRaw {
    # the registry value as stored (REG_EXPAND_SZ entries unexpanded), or $null when there is none
    $key = Get-Item -LiteralPath "HKCU:\Environment"
    if ($key.GetValueNames() -notcontains "Path") { return $null }
    return $key.GetValue("Path", $null, [Microsoft.Win32.RegistryValueOptions]::DoNotExpandEnvironmentNames)
}
function Set-UserPathRaw($Value) {
    if ($null -eq $Value) { Remove-ItemProperty -LiteralPath "HKCU:\Environment" -Name "Path" -ErrorAction SilentlyContinue }
    else { New-ItemProperty -LiteralPath "HKCU:\Environment" -Name "Path" -PropertyType ExpandString -Value $Value -Force | Out-Null }
}
function Count-Entry($PathValue, [string]$Dir) {
    if ($null -eq $PathValue) { return 0 }
    return @($PathValue.Split(";") | Where-Object { $_.Trim() -ieq $Dir }).Count
}

function Invoke-Hook([string]$Launcher, $Handler, [hashtable]$Payload, [string]$Tmp) {
    # Runs one hook handler exactly as written in settings.json (exec form: command + args, no shell) with the payload
    # on stdin as UTF-8 without a BOM, as Claude Code sends it. The launcher is a Python subprocess call: PowerShell's
    # own pipe and .NET's Process.StandardInput both prefix a BOM when the console input encoding is UTF-8.
    $hf = Join-Path $Tmp "handler.json"
    $pf = Join-Path $Tmp "payload.json"
    $utf8 = New-Object System.Text.UTF8Encoding($false)
    [IO.File]::WriteAllText($hf, ($Handler | ConvertTo-Json -Compress -Depth 5), $utf8)
    [IO.File]::WriteAllText($pf, ($Payload | ConvertTo-Json -Compress), $utf8)
    $prog = "import json,subprocess,sys;h=json.load(open(sys.argv[1],encoding='utf-8'));d=open(sys.argv[2],'rb').read();" +
            "sys.exit(subprocess.run([h['command']]+h['args'],input=d,stdout=subprocess.DEVNULL).returncode)"
    & $Launcher -I -c $prog $hf $pf
    return $LASTEXITCODE
}

# ---------------------------------------------------------------- checks of a DevMem folder
function Test-AppFolder([string]$App) {
    Step "files in $App"
    foreach ($rel in @("python\python.exe", "python\pythonw.exe", "app\devmem\__main__.py", "app\devmem\ui\index.html",
                       "bin\devmem.cmd", "devmem.ico")) {
        Check (Test-Path -LiteralPath (Join-Path $App $rel)) $rel
    }
    $pth = @(Get-ChildItem -LiteralPath (Join-Path $App "python") -Filter "python3*._pth")
    Check ($pth.Count -eq 1) "one python3*._pth"
    if ($pth.Count -eq 1) {
        $lines = @(Get-Content -LiteralPath $pth[0].FullName | Where-Object { $_ -and -not $_.StartsWith("#") })
        Check (($lines -join "|") -eq ("{0}.zip|.|..\app" -f $pth[0].BaseName)) "._pth lists the stdlib zip, the python folder and ..\app only ($($lines -join ', '))"
    }
    Check (-not (Get-ChildItem -LiteralPath (Join-Path $App "app") -Recurse -Directory -Filter "tests" -ErrorAction SilentlyContinue)) "no tests folder shipped"

    $cmd = Join-Path $App "bin\devmem.cmd"
    Step "devmem command"
    $v = (& $cmd --version) -join ""
    Check ($LASTEXITCODE -eq 0 -and $v.Trim() -eq "devmem $Version") "devmem --version -> '$v' (exit $LASTEXITCODE)"
    & $cmd no-such-command 2>$null | Out-Null
    Check ($LASTEXITCODE -eq 2) "an unknown command exits 2 through the shim (got $LASTEXITCODE)"
    $exe = (& $cmd doctor --project $WorkDir 2>$null | Select-String -SimpleMatch "[ ok ] python" | Select-Object -First 1)
    Check ($null -ne $exe -and $exe.Line -like "*$App\python\python.exe*") "doctor reports the bundled interpreter ($exe)"

    Step "a project: init, status, doctor, one recorded session"
    $proj = Join-Path $WorkDir "proj with space"
    if (Test-Path -LiteralPath $proj) { Remove-Item -LiteralPath $proj -Recurse -Force }
    New-Item -ItemType Directory -Force -Path (Join-Path $proj "src") | Out-Null
    Set-Content -LiteralPath (Join-Path $proj "README.md") -Value "# demo`nA tiny project for the installer test." -Encoding UTF8
    Set-Content -LiteralPath (Join-Path $proj "src\main.py") -Value "def greet(name):`n    return 'hello ' + name" -Encoding UTF8
    Push-Location -LiteralPath $proj
    try {
        & $cmd init | Out-Host
        Check ($LASTEXITCODE -eq 0) "devmem init (exit $LASTEXITCODE)"
        # `devmem init` writes the personal, git-ignored settings file by default
        $settings = Get-Content -Raw -Encoding UTF8 -ErrorAction Stop (Join-Path $proj ".claude\settings.local.json") | ConvertFrom-Json
        $h = $settings.hooks.UserPromptSubmit[0].hooks[0]
        $bundled = (Resolve-Path -LiteralPath (Join-Path $App "python\python.exe")).Path
        Check ($h.command -ieq $bundled) "hook handlers run the bundled python.exe ($($h.command))"
        Check ($h.args[0] -eq "-c" -and $h.args[2] -ieq (Resolve-Path -LiteralPath (Join-Path $App "app")).Path) "hook handlers load devmem from the app folder ($($h.args[2]))"
        & $cmd status | Out-Host
        Check ($LASTEXITCODE -eq 0) "devmem status (exit $LASTEXITCODE)"
        & $cmd doctor | Out-Host
        Check ($LASTEXITCODE -eq 0) "devmem doctor (exit $LASTEXITCODE)"

        $sid = [guid]::NewGuid().ToString()
        $base = @{ session_id = $sid; transcript_path = (Join-Path $proj "no-transcript.jsonl"); cwd = $proj }
        $prompt = "installer smoke test: rename greet to welcome"
        $events = @(
            ($base + @{ hook_event_name = "SessionStart"; source = "startup" }),
            ($base + @{ hook_event_name = "UserPromptSubmit"; prompt = $prompt; permission_mode = "default" }),
            # (the turn ends before the session does: a session that ends with a turn open is left in
            # .devmemory\live and finished by the next SessionStart, so it would have no record yet)
            ($base + @{ hook_event_name = "Stop"; stop_hook_active = $false }),
            ($base + @{ hook_event_name = "SessionEnd"; reason = "other" })
        )
        foreach ($e in $events) {
            $handler = $settings.hooks.($e.hook_event_name)[0].hooks[0]
            $rc = Invoke-Hook $bundled $handler $e $WorkDir
            Check ($rc -eq 0) "hook $($e.hook_event_name) through the bundled interpreter (exit $rc)"
        }
        $records = @(Get-ChildItem -LiteralPath (Join-Path $proj ".devmemory\sessions") -File -ErrorAction SilentlyContinue |
                     Where-Object { (Get-Content -Raw -Encoding UTF8 -LiteralPath $_.FullName) -like "*$prompt*" })
        Check ($records.Count -ge 1) "the session was recorded in .devmemory\sessions ($($records.Count) file(s) hold the prompt)"
        $tempLog = Join-Path ([IO.Path]::GetTempPath()) "devmem_hook_errors.log"
        if (Test-Path -LiteralPath $tempLog) { Write-Host "  note: $tempLog exists:"; Get-Content -LiteralPath $tempLog -Tail 3 | Out-Host }
        $s = (& $cmd sessions) -join "`n"
        Check ($LASTEXITCODE -eq 0 -and $s -like "*rename greet*") "devmem sessions lists it"
    } catch {
        Check $false "error in the project checks: $($_.Exception.Message)"
    } finally {
        Pop-Location
    }

    Step "app"
    $dry = (& $cmd app --dry-run) -join "`n"
    Check ($LASTEXITCODE -eq 0 -and $dry -match "http://127\.0\.0\.1:\d+/\?launch=") "devmem app --dry-run (exit $LASTEXITCODE)"
    $p = Start-Process -FilePath (Join-Path $App "python\pythonw.exe") -ArgumentList @("-I", "-m", "devmem", "app", "--dry-run") -Wait -PassThru -WorkingDirectory $App
    Check ($p.ExitCode -eq 0) "pythonw.exe -m devmem app --dry-run, as the shortcut runs it but without opening a window (exit $($p.ExitCode))"
    return $proj
}

# ---------------------------------------------------------------- folder only
if ($AppDir) {
    $null = Test-AppFolder (Resolve-Path -LiteralPath $AppDir).Path
    Write-Host ""
    if ($script:Failures) { Write-Host "$($script:Failures) check(s) failed" -ForegroundColor Red; exit 1 }
    Write-Host "all checks passed"; exit 0
}
if (-not $Installer) { throw "pass -Installer <DevMem-Setup-x.y.z.exe> or -AppDir <folder>" }

# ---------------------------------------------------------------- install, test, uninstall
$Installer = (Resolve-Path -LiteralPath $Installer).Path
$App = Join-Path $env:LOCALAPPDATA "Programs\DevMem"
$Bin = Join-Path $App "bin"
$Shortcut = Join-Path $env:APPDATA "Microsoft\Windows\Start Menu\Programs\DevMem.lnk"
$Desktop = Join-Path ([Environment]::GetFolderPath("Desktop")) "DevMem.lnk"
if (Test-Path -LiteralPath $App) { throw "$App already exists: run this on a machine without DevMem installed" }

function Get-ExpectedPath($Start) {
    # what the installer must make of a user PATH: the bin folder appended as one entry, every byte before it kept
    if ($null -eq $Start -or $Start -eq "") { return $Bin }
    if ($Start.EndsWith(";")) { return $Start + $Bin + ";" }
    return $Start + ";" + $Bin
}
function Invoke-SilentInstall([string[]]$Extra = @()) {
    $p = Start-Process -FilePath $Installer -ArgumentList (@("/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/CURRENTUSER") + $Extra) -Wait -PassThru
    return $p.ExitCode
}
function Invoke-SilentUninstall {
    $p = Start-Process -FilePath (Join-Path $App "unins000.exe") -ArgumentList @("/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART") -Wait -PassThru
    # the uninstaller runs a copy of itself from the temp folder; wait until it has finished removing files
    $deadline = (Get-Date).AddSeconds(90)
    while ((Test-Path -LiteralPath (Join-Path $App "unins000.exe")) -and (Get-Date) -lt $deadline) { Start-Sleep -Milliseconds 500 }
    return $p.ExitCode
}
function Show-Path($Value) { if ($null -eq $Value) { return "(no Path value)" } return "'$Value'" }

$OriginalPath = Get-UserPathRaw
# a PATH with other entries and a trailing ';': the installer must append once, and the uninstaller restore it exactly
$Before = $(if ($OriginalPath) { $OriginalPath.TrimEnd(";") + ";" } else { "" }) + "C:\DevMem CI\other tool;"
Set-UserPathRaw $Before
try {
    Step "silent install of $Installer"
    $log = Join-Path $WorkDir "install.log"
    $rc = Invoke-SilentInstall @("/LOG=`"$log`"")
    Check ($rc -eq 0) "installer exit code 0 (got $rc)"
    Check (Test-Path -LiteralPath (Join-Path $App "unins000.exe")) "uninstaller in $App"
    Check (Test-Path -LiteralPath (Join-Path $App "LICENSE.txt")) "LICENSE.txt"
    Check (Test-Path -LiteralPath (Join-Path $App "devmem-installed.marker")) "the installer's marker file (required before it deletes python and app)"
    Check (Test-Path -LiteralPath $Shortcut) "Start Menu shortcut $Shortcut"
    Check (-not (Test-Path -LiteralPath $Desktop)) "no desktop shortcut (that task is off by default)"
    if (Test-Path -LiteralPath $Shortcut) {
        $lnk = (New-Object -ComObject WScript.Shell).CreateShortcut($Shortcut)
        Check ($lnk.TargetPath -ieq (Join-Path $App "python\pythonw.exe")) "shortcut runs pythonw.exe ($($lnk.TargetPath))"
        Check ($lnk.Arguments -eq "-I -m devmem app") "shortcut arguments ($($lnk.Arguments))"
    }
    $After = Get-UserPathRaw
    Check ($After -ceq (Get-ExpectedPath $Before)) "user PATH: the bin folder appended once, the rest unchanged ('$After')"
    Check ((Count-Entry $After $Bin) -eq 1) "user PATH holds the bin folder exactly once"
    $kind = (Get-Item -LiteralPath "HKCU:\Environment").GetValueKind("Path")
    Check ($kind -eq [Microsoft.Win32.RegistryValueKind]::ExpandString) "user PATH is still REG_EXPAND_SZ ($kind)"

    $proj = Test-AppFolder $App

    Step "devmem through PATH in a new environment"
    $saved = $env:Path
    try {
        $machine = [Environment]::GetEnvironmentVariable("Path", "Machine")
        $user = [Environment]::GetEnvironmentVariable("Path", "User")
        $env:Path = "$machine;$user"
        $found = Get-Command devmem -ErrorAction SilentlyContinue | Select-Object -First 1
        Check ($null -ne $found -and $found.Source -ieq (Join-Path $Bin "devmem.cmd")) "devmem resolves to $Bin\devmem.cmd ($(if ($found) { $found.Source }))"
        $v = (& devmem --version) -join ""
        Check ($LASTEXITCODE -eq 0 -and $v.Trim() -eq "devmem $Version") "devmem --version via PATH -> '$v'"
        # "devmem" in quotes from a folder whose parent holds python\python.exe: cmd used to point %~dp0 at the current
        # folder, and the shim ran that program (W1). hostname.exe stands in for the planted one.
        $hostile = Join-Path $WorkDir "hostile repo"
        New-Item -ItemType Directory -Force -Path (Join-Path $hostile "sub"), (Join-Path $hostile "python") | Out-Null
        Copy-Item (Join-Path $env:SystemRoot "System32\hostname.exe") (Join-Path $hostile "python\python.exe") -Force
        $runner = Join-Path $WorkDir "quoted-devmem.bat"
        Set-Content -LiteralPath $runner -Value "@echo off`r`ncall `"devmem`" --version" -Encoding ascii
        Push-Location (Join-Path $hostile "sub")
        try { $v = (& cmd /d /c $runner) -join "" } finally { Pop-Location }
        Check ($LASTEXITCODE -eq 0 -and $v.Trim() -eq "devmem $Version") "`"devmem`" --version from a hostile folder -> '$v'"
    } finally {
        $env:Path = $saved
    }

    Step "a copy in a folder whose path has spaces"
    $copy = Join-Path $WorkDir "copy with spaces\DevMem"
    New-Item -ItemType Directory -Force -Path $copy | Out-Null
    Copy-Item -Path (Join-Path $App "*") -Destination $copy -Recurse -Force
    $v = (& (Join-Path $copy "bin\devmem.cmd") --version) -join ""
    Check ($LASTEXITCODE -eq 0 -and $v.Trim() -eq "devmem $Version") "devmem.cmd from '$copy' -> '$v'"

    Step "installing again (an upgrade over the same version) keeps one PATH entry"
    $rc = Invoke-SilentInstall
    Check ($rc -eq 0) "second install exit code 0 (got $rc)"
    Check ((Get-UserPathRaw) -ceq (Get-ExpectedPath $Before)) "user PATH unchanged by the second install"
    Check ((Test-Path -LiteralPath (Join-Path $proj ".devmemory"))) "the project's .devmemory is untouched"

    Step "silent uninstall"
    $rc = Invoke-SilentUninstall
    Check ($rc -eq 0) "uninstaller exit code 0 (got $rc)"
    foreach ($rel in @("python", "app", "bin", "unins000.exe", "devmem.ico")) {
        Check (-not (Test-Path -LiteralPath (Join-Path $App $rel))) "$rel removed"
    }
    Check (-not (Test-Path -LiteralPath $App)) "$App removed"
    Check (-not (Test-Path -LiteralPath $Shortcut)) "Start Menu shortcut removed"
    $AfterUninstall = Get-UserPathRaw
    Check ($AfterUninstall -ceq $Before) "user PATH back to exactly what it was ('$AfterUninstall')"
    Check ((Test-Path -LiteralPath (Join-Path $proj ".devmemory\sessions"))) "the project's .devmemory (with its sessions) is still there"
    Check ((Test-Path -LiteralPath $env:DEVMEM_USER_DATA)) "devmem's per-user data folder is still there"

    # the same round trip for the other shapes a user PATH can have
    $noTrailing = $Before.TrimEnd(";")
    foreach ($start in @($noTrailing, $null)) {
        Step "install and uninstall with the user PATH $(Show-Path $start)"
        Set-UserPathRaw $start
        $rc = Invoke-SilentInstall
        Check ($rc -eq 0) "install exit code 0 (got $rc)"
        $got = Get-UserPathRaw
        Check ($got -ceq (Get-ExpectedPath $start)) "after install: $(Show-Path $got)"
        $rc = Invoke-SilentInstall
        Check ($rc -eq 0 -and (Get-UserPathRaw) -ceq (Get-ExpectedPath $start)) "a second install changes nothing (exit $rc)"
        $rc = Invoke-SilentUninstall
        Check ($rc -eq 0) "uninstall exit code 0 (got $rc)"
        $got = Get-UserPathRaw
        Check ($got -ceq $start) "after uninstall, exactly as before: $(Show-Path $got)"
        Check (-not (Test-Path -LiteralPath $App)) "$App removed"
    }

    # C3: an app folder that is a junction is never installed into or deleted through, and a python/app folder the
    # installer did not make (no marker) is left alone; both refuse with nothing changed
    Step "a junction or a foreign folder in the program folder is left alone"
    Set-UserPathRaw $Before
    $outside = Join-Path $WorkDir "outside-target"
    New-Item -ItemType Directory -Force -Path $outside | Out-Null
    Set-Content -LiteralPath (Join-Path $outside "keep.txt") -Value "keep" -Encoding ascii
    New-Item -ItemType Directory -Force -Path $App | Out-Null
    New-Item -ItemType Junction -Path (Join-Path $App "app") -Target $outside | Out-Null
    $rc = Invoke-SilentInstall
    Check ($rc -ne 0) "install refused with a junction at $App\app (exit $rc)"
    Check (Test-Path -LiteralPath (Join-Path $outside "keep.txt")) "the junction's target is untouched"
    Check (-not (Test-Path -LiteralPath (Join-Path $App "python"))) "nothing installed"
    [System.IO.Directory]::Delete((Join-Path $App "app"))          # the junction itself, not its target
    New-Item -ItemType Directory -Force -Path (Join-Path $App "app") | Out-Null
    Set-Content -LiteralPath (Join-Path $App "app\mine.txt") -Value "mine" -Encoding ascii
    $rc = Invoke-SilentInstall
    Check ($rc -ne 0) "install refused into an app folder it did not make (exit $rc)"
    Check (Test-Path -LiteralPath (Join-Path $App "app\mine.txt")) "that folder is untouched"
    Remove-Item -LiteralPath $App -Recurse -Force
    Check ((Get-UserPathRaw) -ceq $Before) "user PATH unchanged by the refused installs"
} finally {
    Set-UserPathRaw $OriginalPath
}

Write-Host ""
if ($script:Failures) { Write-Host "$($script:Failures) check(s) failed" -ForegroundColor Red; exit 1 }
Write-Host "all checks passed"
exit 0
