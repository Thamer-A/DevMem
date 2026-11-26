; DevMem Windows installer (Inno Setup 6). Built by packaging\windows\build.ps1, which assembles the folder this
; script packages (assemble.py) and passes the version:
;   ISCC.exe /DAppVersion=0.4.0 /DStageDir=<repo>\build\windows\stage /DOutputDir=<repo>\dist devmem.iss
; Per-user, no administrator rights: everything goes to {localappdata}\Programs\DevMem and HKCU.
; Uninstalling removes the program, its shortcuts and its PATH entry. devmem's data is never touched: the per-user
; folder (AppData\Roaming\DevMemory) and every project's .devmemory folder stay.

#ifndef AppVersion
  #error AppVersion is not defined: build with packaging\windows\build.ps1 (or pass /DAppVersion=x.y.z to ISCC)
#endif
#ifndef StageDir
  #define StageDir "..\..\build\windows\stage"
#endif
#ifndef OutputDir
  #define OutputDir "..\..\dist"
#endif

[Setup]
; Never change AppId: it is how Windows and later versions of this installer recognise an installed DevMem.
AppId={{1F930877-9CFD-433A-B449-D7A8CBC97F5A}
AppName=DevMem
AppVersion={#AppVersion}
AppVerName=DevMem {#AppVersion}
AppPublisher=DevMem
AppPublisherURL=https://github.com/Thamer-A/DevMem
AppSupportURL=https://github.com/Thamer-A/DevMem/issues
AppUpdatesURL=https://github.com/Thamer-A/DevMem/releases
DefaultDirName={localappdata}\Programs\DevMem
; no folder page: the program folder is always the per-user one. /DIR= on the command line still picks another
; folder, so nothing in it is deleted recursively unless this installer's marker file is there (see [Code])
DisableDirPage=yes
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
UsedUserAreasWarning=no
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
LicenseFile=..\..\LICENSE
SetupIconFile=devmem.ico
UninstallDisplayIcon={app}\devmem.ico
UninstallDisplayName=DevMem
OutputDir={#OutputDir}
OutputBaseFilename=DevMem-Setup-{#AppVersion}
#ifdef AppNumericVersion
VersionInfoVersion={#AppNumericVersion}
#endif
VersionInfoProductName=DevMem
VersionInfoDescription=DevMem Setup
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
; tells Explorer and other running programs to reload PATH after install (and after uninstall: the uninstall log
; keeps this flag and the uninstaller refreshes the environment once its files are removed)
ChangesEnvironment=yes
CloseApplications=yes
RestartApplications=no

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Messages]
UninstalledAll=%1 was removed from your computer.%n%nYour DevMem data was kept: the DevMemory folder in AppData\Roaming and the .devmemory folder of each project. Delete them yourself if you no longer need them.%n%nProjects set up with "devmem init" still list its hook entries in .claude\settings.local.json (or settings.json), and they point at the Python that was just removed: Claude Code reports a hook error there and records nothing. Reinstall DevMem and run "devmem uninstall" in such a project to remove them.

[Tasks]
Name: "addtopath"; Description: "Add the devmem command to PATH (for this user only)"; GroupDescription: "Command line:"
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

; An upgrade replaces the runtime and the package completely (no stale modules from an older version): {app}\python
; and {app}\app are deleted in [Code] (CurStepChanged, ssInstall), only when this installer's marker file is in {app}
; (or the folder is where an earlier DevMem was installed) and none of {app}, {app}\python, {app}\app, {app}\bin is a
; link or junction. There is no [InstallDelete] section: it would delete recursively without those checks.

[Files]
Source: "{#StageDir}\python\*"; DestDir: "{app}\python"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "{#StageDir}\app\*"; DestDir: "{app}\app"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "{#StageDir}\bin\devmem.cmd"; DestDir: "{app}\bin"; Flags: ignoreversion
Source: "devmem.ico"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\..\LICENSE"; DestDir: "{app}"; DestName: "LICENSE.txt"; Flags: ignoreversion

[Icons]
Name: "{userprograms}\DevMem"; Filename: "{app}\python\pythonw.exe"; Parameters: "-I -m devmem app"; WorkingDir: "{app}"; IconFilename: "{app}\devmem.ico"; Comment: "Open the DevMem viewer"
Name: "{userdesktop}\DevMem"; Filename: "{app}\python\pythonw.exe"; Parameters: "-I -m devmem app"; WorkingDir: "{app}"; IconFilename: "{app}\devmem.ico"; Comment: "Open the DevMem viewer"; Tasks: desktopicon

[Run]
Filename: "{app}\python\pythonw.exe"; Parameters: "-I -m devmem app"; WorkingDir: "{app}"; Description: "Open DevMem"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
; {app}\app and {app}\python (with the byte code Python wrote there at run time) are deleted in [Code]
; (CurUninstallStepChanged), with the same marker and link checks as at install; here only empty folders
Type: dirifempty; Name: "{app}\bin"
Type: dirifempty; Name: "{app}"

[Code]
const
  EnvKey = 'Environment';
  MarkerName = 'devmem-installed.marker';
  UninstallKey = 'Software\Microsoft\Windows\CurrentVersion\Uninstall\{1F930877-9CFD-433A-B449-D7A8CBC97F5A}_is1';
  ReparsePointAttr = $400;

var
  ProgramFoldersOwned: Boolean;

// Is Path a symbolic link, a junction or another reparse point? FindFirst on a name without wildcards reports the
// entry itself, not what it points at.
function IsReparsePoint(const Path: String): Boolean;
var
  Rec: TFindRec;
begin
  Result := False;
  if FindFirst(RemoveBackslashUnlessRoot(Path), Rec) then
  begin
    Result := (Rec.Attributes and ReparsePointAttr) <> 0;
    FindClose(Rec);
  end;
end;

// The first of {app}, {app}\python, {app}\app, {app}\bin that is a link or junction, or ''.
function LinkInProgramFolder(): String;
begin
  Result := '';
  if IsReparsePoint(ExpandConstant('{app}')) then
    Result := ExpandConstant('{app}')
  else if IsReparsePoint(ExpandConstant('{app}\python')) then
    Result := ExpandConstant('{app}\python')
  else if IsReparsePoint(ExpandConstant('{app}\app')) then
    Result := ExpandConstant('{app}\app')
  else if IsReparsePoint(ExpandConstant('{app}\bin')) then
    Result := ExpandConstant('{app}\bin');
end;

function MarkerFile(): String;
begin
  Result := ExpandConstant('{app}\' + MarkerName);
end;

// Did this installer put {app}\python and {app}\app there? Its marker file is in {app}, or (an upgrade from a
// version that wrote no marker) DevMem's uninstall entry names this folder.
function InstalledHereBefore(): Boolean;
var
  Location: String;
begin
  Result := FileExists(MarkerFile());
  if not Result and RegQueryStringValue(HKEY_CURRENT_USER, UninstallKey, 'Inno Setup: App Path', Location) then
    Result := CompareText(RemoveBackslashUnlessRoot(Location), RemoveBackslashUnlessRoot(ExpandConstant('{app}'))) = 0;
end;

// Before anything is written: refuse a program folder that is (or holds) a link or junction, and one whose python
// or app folder this installer did not make. Nothing is changed then.
function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  Link: String;
begin
  Result := '';
  ProgramFoldersOwned := False;
  Link := LinkInProgramFolder();
  if Link <> '' then
  begin
    Result := Link + ' is a symbolic link or junction. DevMem Setup does not install into or delete through a ' +
              'link; nothing was changed.';
    Exit;
  end;
  if not DirExists(ExpandConstant('{app}\python')) and not DirExists(ExpandConstant('{app}\app')) then
    ProgramFoldersOwned := True
  else if InstalledHereBefore() then
    ProgramFoldersOwned := True
  else
    Result := ExpandConstant('{app}') + ' already holds a python or app folder that DevMem Setup did not install ' +
              '(no ' + MarkerName + '). It was left alone and nothing was changed.';
end;

// {app}\python and {app}\app, only when this installer made them and they are not links (checked again here).
procedure DeleteProgramFolders();
begin
  if not FileExists(MarkerFile()) and not ProgramFoldersOwned then
  begin
    Log('No ' + MarkerName + ' in ' + ExpandConstant('{app}') + ': its python and app folders were left alone.');
    Exit;
  end;
  if LinkInProgramFolder() <> '' then
  begin
    Log(LinkInProgramFolder() + ' is a link or junction: nothing was deleted.');
    Exit;
  end;
  DelTree(ExpandConstant('{app}\python'), True, True, True);
  DelTree(ExpandConstant('{app}\app'), True, True, True);
end;

function DevMemBin(): String;
begin
  Result := ExpandConstant('{app}\bin');
end;

// Is Entry (one ';'-separated item of a PATH value) the folder Dir? Case-insensitive, outer spaces and one trailing
// backslash ignored.
function IsDirEntry(const Entry, Dir: String): Boolean;
var
  E: String;
begin
  E := Trim(Entry);
  if (Length(E) > 0) and (E[Length(E)] = '\') then
    E := Copy(E, 1, Length(E) - 1);
  Result := CompareText(E, Dir) = 0;
end;

// PATH value Value without its entries equal to Dir (IsDirEntry). Every other entry, empty ones included, is kept as it
// was, joined with ';' again. Together with AddToUserPath (which appends Dir + ';' to a value ending in ';' and
// ';' + Dir to any other) this restores the value from before the install byte for byte. Found tells whether Dir was
// there.
function PathWithout(const Value, Dir: String; var Found: Boolean): String;
var
  Rest, Entry: String;
  P: Integer;
  First: Boolean;
begin
  Result := '';
  Found := False;
  First := True;
  Rest := Value;
  while True do
  begin
    P := Pos(';', Rest);
    if P = 0 then
      Entry := Rest
    else
      Entry := Copy(Rest, 1, P - 1);
    if IsDirEntry(Entry, Dir) then
      Found := True
    else
    begin
      if not First then
        Result := Result + ';';
      Result := Result + Entry;
      First := False;
    end;
    if P = 0 then
      Break;
    Rest := Copy(Rest, P + 1, Length(Rest));
  end;
end;

// Appends <app>\bin to the user's PATH (HKCU\Environment\Path) unless it is already one of its entries. A missing
// or empty value becomes just that folder; a value that exists but cannot be read as a string is left alone.
procedure AddToUserPath();
var
  Value, Dir, Ignored: String;
  Found: Boolean;
begin
  Dir := DevMemBin();
  if not RegQueryStringValue(HKEY_CURRENT_USER, EnvKey, 'Path', Value) then
  begin
    if RegValueExists(HKEY_CURRENT_USER, EnvKey, 'Path') then
    begin
      Log('The user PATH is not a string value; not changing it.');
      Exit;
    end;
    Value := '';
  end;
  Ignored := PathWithout(Value, Dir, Found);
  if Found then
  begin
    Log('The user PATH already holds ' + Dir + '.');
    Exit;
  end;
  if Value = '' then
    Value := Dir
  else if Copy(Value, Length(Value), 1) = ';' then
    Value := Value + Dir + ';'
  else
    Value := Value + ';' + Dir;
  if RegWriteExpandStringValue(HKEY_CURRENT_USER, EnvKey, 'Path', Value) then
    Log('Added ' + Dir + ' to the user PATH.')
  else
    Log('Could not write the user PATH.');
end;

// Removes exactly the <app>\bin entries from the user's PATH; everything else stays. A PATH left empty is deleted
// (it held nothing but this folder).
procedure RemoveFromUserPath();
var
  Value, NewValue: String;
  Found: Boolean;
begin
  if not RegQueryStringValue(HKEY_CURRENT_USER, EnvKey, 'Path', Value) then
    Exit;
  NewValue := PathWithout(Value, DevMemBin(), Found);
  if not Found then
    Exit;
  if NewValue = '' then
  begin
    if RegDeleteValue(HKEY_CURRENT_USER, EnvKey, 'Path') then
      Log('Removed the user PATH (it held only ' + DevMemBin() + ').');
  end
  else if RegWriteExpandStringValue(HKEY_CURRENT_USER, EnvKey, 'Path', NewValue) then
    Log('Removed ' + DevMemBin() + ' from the user PATH.')
  else
    Log('Could not write the user PATH.');
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if CurStep = ssInstall then
    DeleteProgramFolders();
  if CurStep = ssPostInstall then
  begin
    // the marker the next install and the uninstaller require before deleting {app}\python and {app}\app
    if not SaveStringToFile(MarkerFile(), 'DevMem {#AppVersion}: written by DevMem Setup. Its uninstaller and later ' +
                            'versions delete the python and app folders here only while this file exists.' + #13#10, False) then
      Log('Could not write ' + MarkerFile() + '.');
    // the task decides on every install, so unticking it in an upgrade takes the entry away again
    if WizardIsTaskSelected('addtopath') then
      AddToUserPath()
    else
      RemoveFromUserPath();
  end;
end;

// A program folder that is (or holds) a link or junction is not uninstalled at all: the uninstaller would delete the
// files it lists through that link.
function InitializeUninstall(): Boolean;
var
  Link: String;
begin
  Result := True;
  Link := LinkInProgramFolder();
  if Link <> '' then
  begin
    SuppressibleMsgBox(Link + ' is a symbolic link or junction. DevMem was not uninstalled and nothing was deleted; ' +
                       'remove the link, then uninstall again.', mbError, MB_OK, IDOK);
    Result := False;
  end;
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  // usUninstall comes before the files are removed and before the uninstaller refreshes the environment
  // (ChangesEnvironment), so Explorer sees the PATH without the entry
  if CurUninstallStep = usUninstall then
  begin
    RemoveFromUserPath();
    if FileExists(MarkerFile()) then
    begin
      DeleteProgramFolders();
      DeleteFile(MarkerFile());
    end
    else
    begin
      Log('No ' + MarkerName + ' in ' + ExpandConstant('{app}') + ': only the files the installer listed are removed; ' +
          'the python and app folders and anything else in them are left alone.');
      SuppressibleMsgBox(ExpandConstant('{app}') + ' has no ' + MarkerName + ', so DevMem removes only the files its ' +
                         'installer listed and leaves anything else in its python and app folders in place.',
                         mbInformation, MB_OK, IDOK);
    end;
  end;
end;
