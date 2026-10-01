# Installing KovaaKs Tracker

The application ZIP contains only the runtime, its dependency list, this guide,
and the license. Python 3.10 or newer is required; this is not a bundled executable.

## Install and run

1. Download the **kovaaks-tracker.zip** asset from an application release on the
   [Releases page](https://github.com/Naxeron/kovaaks-tracker/releases). The
   `scenario-data` prerelease contains datasets, not the application. GitHub's
   automatic **Source code** downloads include development files and tests.
2. Extract the entire ZIP to a writable folder. Keep `kovaaks_web.py`, `kovaaks/`,
   `requirements/`, and `docs/` together.
3. Open a terminal in the extracted `kovaaks-tracker` folder. Create and activate
   a virtual environment (recommended):

   ```bash
   python -m venv .venv
   ```

   Windows PowerShell: `.venv\Scripts\Activate.ps1`

   Linux/macOS: `source .venv/bin/activate`

   On Linux, if using the distribution's GTK/PyGObject packages listed below,
   create it with `python -m venv --system-site-packages .venv` instead so the
   virtual environment can see those packages.
4. Install dependencies and start the app:

   ```bash
   python -m pip install -r requirements/runtime.txt
   python kovaaks_web.py
   ```

   Use `python3` instead of `python` if that is your system's Python command.
5. Enter your KovaaKs credentials in Settings and select your stats directory.

## Linux desktop dependencies

`pywebview` requires a system GUI/WebKit backend. The application defaults to GTK
on Linux. On Arch Linux:

```bash
sudo pacman -S python-gobject webkit2gtk-4.1
```

On Debian/Ubuntu:

```bash
sudo apt install python3-gi python3-gi-cairo gir1.2-gtk-3.0 gir1.2-webkit2-4.1
```

## Settings, scores, and upgrades

The app creates `data/` beside the launcher for `config.json`, `kovaaks.log`, and
`scores_cache.json.gz`. Keep that folder when upgrading, or copy it to your new
installation before launching. Close the app before replacing application files.
Shared scenario datasets download automatically; they are not bundled in the ZIP.

Older installations stored `config.json` and `kovaaks.log` beside the launcher.
The app migrates those files into `data/` when possible and gives an existing file
in `data/` precedence. If a file cannot be moved, it remains in its original
location and the app continues using that path. Invalid settings are left intact.
When moving an old installation to a new folder, copy its `data/` folder and any
root-level `config.json` and `kovaaks.log` before the first launch.

Passwords are saved through Python `keyring` in Windows Credential Manager,
macOS Keychain, or a supported Linux Secret Service / KWallet store. On Linux,
the credential service must be installed and available in your desktop D-Bus
session; you may be prompted to unlock it. If secure storage is unavailable,
the app explains that the password is usable only for the current session.
It never falls back to a plaintext password file.

In Settings, leave the password field blank to keep the current password, enter
a new one to replace it, or use **Forget saved password** to remove it. Changing
the username selects that account's credentials. Saved passwords are never
sent back to the web interface. Any password left in an older config is removed
only after a verified save to the OS store or an explicit forget operation.

