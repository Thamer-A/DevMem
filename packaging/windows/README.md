# Windows installer (maintainers)

`DevMem-Setup-<version>.exe` is an [Inno Setup 6](https://jrsoftware.org/isinfo.php) installer around the official
python.org **embeddable** Python and the `plugin/devmem` package. Nothing is frozen (no PyInstaller): `sys.executable`
in the installed app is a real, PSF-signed `python.exe`, so `devmem init` writes the same hook entries as a pip
install.

| file | what it is |
|---|---|
| `python-embed.json` | the pinned runtime: version, URL, SHA-256, size, where the hash came from |
| `assemble.py` | builds the folder the installer ships (stdlib only; tested by `tests/test_windows_installer.py`) |
| `build.ps1` | download + verify + assemble + `ISCC.exe` (Windows PowerShell 5.1 or 7; needs no Python) |
| `devmem.iss` | the Inno Setup script (per-user, PATH task, shortcuts, uninstall) |
| `test-installer.ps1` | installs, tests and uninstalls for real; for CI or a throwaway VM |
| `devmem.ico`, `make_icon.py` | the icon and the script that draws it (`python packaging/windows/make_icon.py`) |
| `../../.github/workflows/installer.yml` | builds and tests on `windows-latest`; on a `v*` tag, attestation + draft release |

## Build locally

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File packaging\windows\build.ps1
```

1. Downloads `python-<version>-embed-amd64.zip` into `build\windows\download\` (reused while its hash matches) and
   checks its SHA-256 against `python-embed.json`. A mismatch deletes the download and stops the build.
2. Extracts it once and runs `assemble.py` with that interpreter, which builds `build\windows\stage\`:
   `python\` (the runtime; `python313._pth` rewritten to `python313.zip`, `.`, `..\app`, no `import site`, so neither
   PYTHON* variables, site-packages nor the current folder reach `sys.path`), `app\devmem\` (no `__pycache__`, no
   tests; byte-compiled afterwards unless `-NoCompile`), `bin\devmem.cmd` and `devmem.ico`. It then checks that the
   bundled `python.exe -I -m devmem --version` prints the package version.
3. Runs `ISCC.exe` if it finds one (`-Iscc <path>`, PATH, or `Inno Setup 6` under Program Files or
   `%LOCALAPPDATA%\Programs`): `dist\DevMem-Setup-<version>.exe` and `dist\DevMem-Setup-<version>.exe.sha256`
   (`<hash>  <file>`, the `sha256sum` format). Without Inno Setup it stops after step 2 and says so (`-RequireInstaller`
   makes that an error, as CI does).

The version comes from `plugin/devmem/__init__.py` (`/DAppVersion=`). `build/` and `dist/` are git-ignored.

Checks without installing anything: `python -m pytest tests/test_windows_installer.py` (the tests that run the real
bundled interpreter need the zip from step 1), and
`powershell -File packaging\windows\test-installer.ps1 -AppDir build\windows\stage` (runs the folder's `devmem`,
`init` and one hook event in a temporary project with `DEVMEM_USER_DATA` in a temporary folder).

`test-installer.ps1 -Installer dist\DevMem-Setup-<version>.exe` really installs (per-user), changes the user PATH
(restored at the end) and uninstalls: run it only on a machine where that is fine, such as the CI runner or a VM.

## Bump the bundled Python

1. Pick a release that has Windows binaries. 3.13 is in its security-fix phase: most 3.13.x security releases are
   source-only (3.13.18 is), and only some get Windows builds (3.13.17 did). The API lists the files of each release:
   `https://www.python.org/api/v2/downloads/release/?is_published=true` (find the release id), then
   `https://www.python.org/api/v2/downloads/release_file/?release=<id>`; the entry named "Windows embeddable package
   (64-bit)" has `url` and `sha256_sum`. The release page (`https://www.python.org/downloads/release/python-3XYZ/`)
   shows the same SHA-256 in its file table; check that both agree.
2. Download the zip and hash it yourself (`Get-FileHash -Algorithm SHA256`); it must match.
3. Update `python-embed.json` (`version`, `url`, `sha256`, `size`, `source` with the date), run `build.ps1` and
   `python -m pytest tests/test_windows_installer.py`. For a new minor version (3.14) the `._pth` name changes
   (`python314._pth`); `assemble.py` derives it from the version, and the pin test currently expects 3.13.x, so update
   that test on purpose.
4. Optionally verify the Sigstore bundle python.org publishes next to the zip (`.sigstore`), as described on
   https://www.python.org/downloads/metadata/sigstore/.

## Releases

Push a tag `v<version>` that equals `devmem.__version__` (the workflow checks). The `release` job attests the
installer (`actions/attest-build-provenance`) and attaches it and its `.sha256` to a **draft** release; publish the
draft by hand. Artifact attestations need a public repository (or GitHub Enterprise Cloud for a private one).

The installer is not code-signed. Options: Azure Trusted Signing (Azure Artifact Signing), SignPath's free plan for
open-source projects, or an OV code-signing certificate on a hardware token or cloud HSM. Signing would be one step
in `build.ps1`/the workflow before the hash is computed: sign the installer (Inno Setup's `SignTool=` also signs the
uninstaller) and keep the `.sha256` and attestation on the signed file.

The `AppId` GUID in `devmem.iss` must never change: Windows and later installers use it to find an installed DevMem.
