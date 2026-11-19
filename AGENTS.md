# Repository Guidelines for Agents

## Windows Installer & Clean Uninstall Policy

When modifying installation, packaging, user data storage, or registry persistence (`scripts/installer.iss`, `core/offline.py`, `core/i18n.py`, `gui.py`, `cli.py`, etc.):

1. **Uninstalling must automatically leave a completely clean system without prompting the user.**
   Choosing to uninstall means the user does not want to keep local history, caches, logs, or settings—do **not** add confirmation dialogs asking whether to keep user data, and do **not** leave residual files or registry keys behind.
2. **Keep `scripts/installer.iss` synchronized with all persisted locations:**
   - **Installation directory (`{app}`)**: `[UninstallDelete]` must remove runtime-generated folders (`__pycache__`, `var`, `logs`, `tmp`, plus managed `core`, `runtime`, `assets`, `models`) and `{app}` (`dirifempty`).
   - **User data directories**: `[UninstallDelete]` must remove `{localappdata}\OfflinePDF Translate`.
   - **Qt `QSettings` registry keys (`core/i18n.py`)**: `[Registry]` must declare `dontcreatekey uninsdeletekey` for `HKCU\Software\OfflinePDF` (and never delete shared `HKCU\Software\QtProject`).
   - **Orphaned temporary directories**: `CurUninstallStepChanged(usPostUninstall)` in `[Code]` must clean leftover `%TEMP%` staging prefixes (`offlinepdf_stage_*`, `offlinepdf_cli_*`).
3. **Preserve user data on upgrades:**
   `[InstallDelete]` cleans only managed program directories inside `{app}`; it must **not** delete `%LOCALAPPDATA%\OfflinePDF Translate` or `HKCU\Software\OfflinePDF`, so in-place upgrades keep user settings, queues, glossaries, and history.
4. **Update regression coverage:**
   Any newly added persistent folder, registry key, shortcut, or temp prefix must be added to `scripts/installer.iss` and verified by `InstallerTests` in `tests/test_regressions.py`.

## Output Naming, Internationalization & CI Policy

1. **Output filenames:**
   `core/delivery.py` must keep universal English output suffixes (`[name].bilingual.pdf` and `[name].translated.pdf`), synchronized across `tests/test_regressions.py` and all five README files (`README.md`, `docs/README.zh-CN.md`, `docs/README.ja.md`, `docs/README.ko.md`, `docs/README.ru.md`).
2. **UI & installer language fallback:**
   Both the desktop UI (`core/i18n.py`) and Windows installer (`scripts/installer.iss`) auto-detect OS language among `zh`, `en`, `ja`, `ko`, `ru` and must default to English (`en`) when the OS locale is unrecognized.
3. **Cross-platform CI compatibility:**
   All tests in `tests/` must pass on clean source checkouts (without `models/`, `assets/fonts/`, or a Chinese OS locale) on Windows, Ubuntu, and macOS (`QT_QPA_PLATFORM=offscreen`). Guard or mock missing local models/fonts cleanly, preserve zero text clipping across all five UI languages, and keep `requirements.txt` aligned with direct imports in `core/` and `tests/`.

## Privacy & Repository Hygiene

1. **Never commit sensitive or large local artifacts:**
   Do not commit personal emails, internal hostnames/IPs, API keys, GGUF/ONNX weights, bundled runtimes/fonts, test PDFs, logs, or `dist/` binaries.
2. **No plaintext contact emails in public history:**
   Use GitHub's noreply commit email (`<id>+<username>@users.noreply.github.com`) for commits. Direct vulnerability reports to GitHub Private Vulnerability Reporting in `SECURITY.md` without exposing plaintext emails.

## Dual-Remote Release & Distribution Workflow

1. **Remotes & network routing:**
   - Primary open-source repository: **GitHub** (`origin`, `SRT117/OfflinePDF-Translate`).
   - Domestic mirror & binary download entry: **GitCode** (`gitcode`, `SRT117/OfflinePDF-Translate`). Push/upload to GitCode over a direct connection bypassing local proxy/VPN tools so large transfers do not consume proxy subscription quota. Never push multi-gigabyte transfers directly from a residential network to a cloud relay server's public IP (which risks triggering IP blocks).
2. **Pre-release checks:**
   - Run `.\runtime\python\python.exe -m unittest discover -s tests`, `.\runtime\python\python.exe gui.py --check-runtime`, and `.\runtime\python\python.exe scripts/prepare_offline_assets.py`.
   - Push `main` and the annotated tag (`vX.Y.Z`) to both `origin` and `gitcode`, and confirm `.github/workflows/tests.yml` passes across `windows-latest`, `ubuntu-latest`, and `macos-latest`.
3. **Packaging & checksums:**
   - Build the Windows installer on Windows x64 (`.\scripts\build_installer.ps1 -Version "<X.Y.Z>"`) and generate `OfflinePDF-Translate-Setup.exe.sha256`.
   - Update `THIRD_PARTY_NOTICES.md` whenever Python dependencies, `llama.cpp`, fonts, or models change.
4. **Release publishing & cleanup:**
   - Ensure GitCode releases set an active public status (such as `release_status: "latest"`) so anonymous visitors see the release and assets rather than an empty list.
   - Upload `OfflinePDF-Translate-Setup.exe` and `.sha256` to both GitCode Releases and GitHub Releases (if the installer exceeds 2 GiB, use `.\scripts\build_installer.ps1 -Split`). Note that GitCode v5 API uploads (`releases.upload` / `get_upload_url`) can fail `obs_callback` (`203` / `400 release update failed`), leaving assets without a `size` field and returning `404 NOT_PATH` on download; if that happens, upload the assets through the GitCode web UI over a direct domestic connection instead.
   - After publishing, always verify both GitCode and GitHub asset links via unauthenticated HTTP `GET` (expecting `302` redirects to CDN/asset storage) and confirm identical byte sizes and SHA-256 digests (`OfflinePDF-Translate-Setup.exe.sha256`).
   - Remove obsolete test tags/releases, temporary files, and superseded local builds in `dist/` so storage stays clean. Keep the GitCode Releases mirror link documented in `docs/README.zh-CN.md`.
