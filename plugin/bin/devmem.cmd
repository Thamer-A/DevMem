@echo off
rem devmem launcher of the Claude Code plugin for Windows cmd: used when hooks run under PowerShell (Windows
rem without Git Bash) and from PowerShell or cmd. Same behaviour as the sh launcher next to it (see there):
rem   devmem COMMAND [ARGS]          the devmem command line
rem   devmem --plugin-hook EVENT     one hook event; always exit status 0, prints only devmem's output
rem The two -c programs are devmem.plugin.HOOK_BOOTSTRAP and CLI_BOOTSTRAP, copied verbatim.
rem Python is searched as python.exe, py.exe, python3.exe in the PATH folders only (never a .bat or .cmd shim, never
rem the current folder: the search runs from this folder; quotes in PATH entries are dropped first), Microsoft Store
rem aliases (\WindowsApps\) are skipped, and a candidate counts only when it exits 0 and prints the sentinel
rem `DEVMEM-PY ` and the path of an existing .exe file. The cache holds that same line; a cache line without the
rem sentinel, with a double quote in it, or naming a missing or non-.exe file is ignored. If the cached interpreter does not
rem finish the hook bootstrap (which always exits 0), the cache is deleted and one line logged. cmd cannot limit
rem how long a probe runs, so a search that never finished (a python.exe that hangs) stops further searches until
rem the next day. Every use of this file's folder is quoted: a plugin folder whose path holds & works; one holding
rem % or ^ is refused by the hook command before cmd sees it (devmem.plugin.hook_command).
setlocal EnableExtensions DisableDelayedExpansion
rem cmd looks for a program in the current folder (for a hook: the user's project) before PATH unless this is set;
rem the one system tool used below is also called by its full path
set "NoDefaultCurrentDirectoryInExePath=1"
set "DM_SYS=%SystemRoot%\System32"
if not defined SystemRoot set "DM_SYS=%windir%\System32"
set "DM_HOOK="
if "%~1"=="--plugin-hook" set "DM_HOOK=1"
if defined DM_HOOK if defined CLAUDE_PROJECT_DIR if not exist "%CLAUDE_PROJECT_DIR%\.devmemory\" exit /b 0
set "DM_ROOT=%~dp0.."
set "DM_PY="
set "DM_CACHE="
if defined DM_HOOK if defined CLAUDE_PLUGIN_DATA set "DM_CACHE=%CLAUDE_PLUGIN_DATA%\python-path-cmd"
if defined DM_CACHE if exist "%DM_CACHE%" call :readcache
if not defined DM_PY call :find
if not defined DM_PY goto nopython
if not defined DM_HOOK goto cli
"%DM_PY%" -I -S -c "import sys,os;R=os.path.abspath(sys.argv.pop(1));P=os.path.join(R,'devmem','plugin.py');L='devmem_hook_errors'+('_'+str(os.getuid()) if hasattr(os,'getuid') else '')+'.log';N=getattr(os,'O_NOFOLLOW',0)+getattr(os,'O_NONBLOCK',0);G=hasattr(os,'getuid');exec(chr(10).join(['try:', ' if not os.path.isfile(P):', '  raise ImportError(P)', ' sys.path.insert(0,R)', ' from devmem.plugin import hook_main', ' hook_main(sys.argv[1:])', 'except BaseException as e:', ' try:', '  import tempfile,time,stat', '  p=os.path.join(tempfile.gettempdir(),L)', '  d=os.open(p,os.O_WRONLY+os.O_APPEND+os.O_CREAT+N,384)', '  s=os.fstat(d)', '  if not stat.S_ISREG(s.st_mode) or (G and s.st_uid-os.getuid()):', '   os.close(d)', '   raise OSError(p)', '  f=os.fdopen(d,chr(97))', '  f.write(repr((time.ctime(),R,sys.argv[1:2],e))+chr(10))', '  f.close()', ' except BaseException:', '  pass']));os._exit(0)" "%DM_ROOT%" %2
if "%ERRORLEVEL%"=="0" exit /b 0
rem the bootstrap always exits 0: anything else means the cached program is not (or no longer) a working Python
if defined DM_CACHE del /q "%DM_CACHE%" >nul 2>&1
>>"%TEMP%\devmem_hook_errors.log" echo %DATE% %TIME% devmem plugin hook %2: the cached interpreter did not run the devmem bootstrap; removed from the cache, Python is searched again next time
exit /b 0

:cli
"%DM_PY%" -I -c "import sys,os;R=os.path.abspath(sys.argv.pop(1));os.path.isfile(os.path.join(R,'devmem','cli.py')) or sys.exit('devmem: no devmem package in '+R);sys.path.insert(0,R);os.environ['DEVMEM_PLUGIN_ROOT']=R;from devmem.cli import main;sys.exit(main())" "%DM_ROOT%" %*
exit /b %ERRORLEVEL%

:nopython
if defined DM_HOOK goto nopython_hook
echo devmem: no Python 3.10 or newer found (tried python.exe, py.exe -3, python3.exe on PATH). Install Python 3.10+ and try again. 1>&2
exit /b 127
:nopython_hook
>>"%TEMP%\devmem_hook_errors.log" echo %DATE% %TIME% devmem plugin hook %2: no Python 3.10 or newer found (tried python.exe, py.exe -3, python3.exe on PATH)%DM_WHY%; nothing recorded
exit /b 0

:readcache
rem one line `DEVMEM-PY <path>`, no double quote, an existing file, not a Store alias; else Python is searched again
call :oneline "%DM_CACHE%"
call :sentinel
set "DM_PY=%DM_P%"
exit /b 0

:find
rem from this file's folder, not the project: the search and the stalled-search check run no program found there
pushd "%~dp0"
set "DM_WHY="
if defined DM_CACHE if exist "%DM_CACHE%.probing" call :stalled
if defined DM_WHY (popd & exit /b 0)
if defined DM_CACHE if not exist "%CLAUDE_PLUGIN_DATA%\" mkdir "%CLAUDE_PLUGIN_DATA%" >nul 2>&1
if defined DM_CACHE 2>nul type nul >"%DM_CACHE%.probing"
rem PATH entries may be written in quotes ("C:\Program Files (x86)\x"): drop every quote, then split at ;
set "DM_PATH=%PATH:"=%"
rem every python.exe, then py.exe, then python3.exe in the PATH folders, in PATH order (not only the first, which
rem may be a shim); the current folder and relative entries are not searched
for %%N in (python.exe py.exe python3.exe) do if not defined DM_PY for %%D in ("%DM_PATH:;=" "%") do if not defined DM_PY if not "%%~D"=="" if not "%%~D"=="." if exist "%%~D\%%N" if not exist "%%~D\%%N\" call :probe "%%~D\%%N" %%N
popd
if defined DM_CACHE del /q "%DM_CACHE%.probing" >nul 2>&1
if defined DM_PY if defined DM_CACHE 2>nul >"%DM_CACHE%" (<nul set /p "=DEVMEM-PY %DM_PY%")
exit /b 0

:stalled
rem the marker of a search that never finished: still today's (forfiles /d -1 lists it only from the next day)
set "DM_WHY=; an earlier search for Python did not finish today (does a python on PATH hang?)"
for %%M in ("%DM_CACHE%.probing") do "%DM_SYS%\forfiles.exe" /p "%%~dpM." /m "%%~nxM" /d -1 >nul 2>&1 && set "DM_WHY="
exit /b 0

:probe
rem %1: the candidate's full path (quoted), %2: its name
set "DM_C=%~1"
set "DM_3="
if /i "%~2"=="py.exe" set "DM_3=-3"
call :storealias "%DM_C%"
if not defined DM_C exit /b 0
set "DM_OUT=%TEMP%\devmem_probe_%RANDOM%%RANDOM%.txt"
"%DM_C%" %DM_3% -I -c "import sys;sys.version_info>=(3,10) and sys.stdout.write('DEVMEM-PY '+sys.executable)" >"%DM_OUT%" 2>nul <nul
if not "%ERRORLEVEL%"=="0" goto probe_done
call :oneline "%DM_OUT%"
call :sentinel
if defined DM_P set "DM_PY=%DM_P%"
:probe_done
del /q "%DM_OUT%" >nul 2>&1
exit /b 0

:oneline
rem DM_L = the file's only line, or empty when it has none or several
set "DM_L="
set "DM_N=0"
for /f "usebackq delims=" %%L in ("%~1") do (
  set "DM_L=%%L"
  set /a DM_N+=1
)
if not "%DM_N%"=="1" set "DM_L="
exit /b 0

:sentinel
rem DM_P = the path after `DEVMEM-PY ` in DM_L when DM_L holds no double quote and names an existing .exe file
rem outside \WindowsApps\; else empty. The quote test runs with delayed expansion, where a quote cannot break the line.
set "DM_P="
if not defined DM_L exit /b 0
setlocal EnableDelayedExpansion
set "DM_V=!DM_L:"=!"
if not "!DM_V!"=="!DM_L!" (endlocal & exit /b 0)
endlocal
if not "%DM_L:~0,10%"=="DEVMEM-PY " exit /b 0
set "DM_P=%DM_L:~10%"
if not defined DM_P exit /b 0
if /i not "%DM_P:~-4%"==".exe" set "DM_P="
if defined DM_P if not exist "%DM_P%" set "DM_P="
if defined DM_P if exist "%DM_P%\" set "DM_P="
if defined DM_P call :storealias "%DM_P%"
exit /b 0

:storealias
rem clears DM_C / DM_P when %1 is under a \WindowsApps\ folder (Microsoft Store app execution aliases)
set "DM_A=%~1"
set "DM_B=%DM_A:\WindowsApps\=%"
if /i not "%DM_A%"=="%DM_B%" (
  if "%DM_A%"=="%DM_C%" set "DM_C="
  if "%DM_A%"=="%DM_P%" set "DM_P="
)
exit /b 0
