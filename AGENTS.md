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
