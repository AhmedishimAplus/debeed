"""
_exe_setup.py — Playwright + Chrome Setup Helper for Compiled .exe
====================================================================

INSTRUCTIONS:
  Add these 2 lines at the VERY TOP of run.py (before ALL other imports):

      from _exe_setup import setup
      setup()

  Replace your launch_chrome.bat call with:

      from _exe_setup import launch_chrome
      if not launch_chrome():
          # Chrome wasn't found and the user cancelled the picker
          ...

Place this file in the same folder as run.py and gui.py.

WHAT THIS DOES (Chrome path handling)
--------------------------------------
1. First tries a previously-saved Chrome path (instant, after first run).
2. If none saved, tries to auto-detect Chrome in common install folders.
3. If still not found, shows a file picker so the user can locate it.
4. Whatever is found/chosen is SAVED — the user is asked at most once.

To let users CHANGE the saved path later (e.g. add a "Settings" button
in your GUI), call: change_chrome_path()
"""

import os
import sys
import json
import socket
import platform
import tempfile
import subprocess
import time


# ─────────────────────────────────────────────────────────────────────────────
# CORE SETUP — Call this before importing playwright
# ─────────────────────────────────────────────────────────────────────────────

def setup():
    """
    Fixes playwright paths so it works correctly inside a compiled .exe.
    Call this at the very start of run.py, BEFORE importing playwright.
    Safe to call when running as a normal .py file too — does nothing then.
    """
    if not getattr(sys, 'frozen', False):
        return  # Running as .py file — nothing to fix

    browsers_path = os.path.join(
        os.path.expanduser('~'),
        'AppData', 'Local', 'ms-playwright'
    )
    os.environ.setdefault('PLAYWRIGHT_BROWSERS_PATH', browsers_path)

    if hasattr(sys, '_MEIPASS'):
        driver_dir = os.path.join(sys._MEIPASS, 'playwright', 'driver')
        if os.path.exists(driver_dir):
            os.environ['PLAYWRIGHT_DRIVER_PATH'] = driver_dir


# ─────────────────────────────────────────────────────────────────────────────
# PATH HELPER — Find bundled files inside the .exe
# ─────────────────────────────────────────────────────────────────────────────

def get_resource_path(filename: str) -> str:
    """Get the correct path to any bundled file (works in .exe and .py)."""
    if getattr(sys, 'frozen', False) and hasattr(sys, '_MEIPASS'):
        return os.path.join(sys._MEIPASS, filename)
    else:
        return os.path.join(os.path.dirname(os.path.abspath(__file__)), filename)


# ─────────────────────────────────────────────────────────────────────────────
# CONFIG STORAGE — Remember the user's Chrome path between launches
# ─────────────────────────────────────────────────────────────────────────────
# IMPORTANT: A --onefile .exe extracts to a TEMP folder that gets deleted
# when the app closes. We must NOT save config there. Instead we write to
# a small folder in the user's profile that always exists and is writable.

CONFIG_FILENAME = "autoapp_config.json"


def _config_path() -> str:
    system = platform.system()
    if system == "Windows":
        base = os.environ.get("APPDATA", os.path.expanduser("~"))
    elif system == "Darwin":  # macOS
        base = os.path.join(os.path.expanduser("~"), "Library", "Application Support")
    else:  # Linux
        base = os.path.join(os.path.expanduser("~"), ".config")

    app_dir = os.path.join(base, "AutoApp")
    os.makedirs(app_dir, exist_ok=True)
    return os.path.join(app_dir, CONFIG_FILENAME)


def _load_saved_chrome_path() -> str | None:
    path_file = _config_path()
    if not os.path.exists(path_file):
        return None
    try:
        with open(path_file, "r") as f:
            data = json.load(f)
        saved = data.get("chrome_path")
        if saved and os.path.isfile(saved):
            return saved
    except Exception:
        pass
    return None


def _save_chrome_path(chrome_path: str) -> None:
    try:
        with open(_config_path(), "w") as f:
            json.dump({"chrome_path": chrome_path}, f)
    except Exception:
        pass  # Non-fatal — worst case the user is asked again next time


# ─────────────────────────────────────────────────────────────────────────────
# CHROME AUTO-DETECTION
# ─────────────────────────────────────────────────────────────────────────────

def _find_chrome() -> str | None:
    """Search common install locations for Chrome (Edge as a fallback)."""
    system = platform.system()

    if system == "Windows":
        local_app_data = os.environ.get('LOCALAPPDATA', '')
        program_files = os.environ.get('PROGRAMFILES', r'C:\Program Files')
        program_files_x86 = os.environ.get('PROGRAMFILES(X86)', r'C:\Program Files (x86)')

        candidates = [
            os.path.join(program_files, r'Google\Chrome\Application\chrome.exe'),
            os.path.join(program_files_x86, r'Google\Chrome\Application\chrome.exe'),
            os.path.join(local_app_data, r'Google\Chrome\Application\chrome.exe'),
            os.path.join(program_files, r'Microsoft\Edge\Application\msedge.exe'),
            os.path.join(program_files_x86, r'Microsoft\Edge\Application\msedge.exe'),
        ]

    elif system == "Darwin":  # macOS — for future use, see note above
        candidates = [
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            os.path.expanduser(
                "~/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
            ),
            "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
        ]

    else:  # Linux
        candidates = [
            "/usr/bin/google-chrome",
            "/usr/bin/google-chrome-stable",
            "/usr/bin/chromium-browser",
            "/usr/bin/chromium",
        ]

    return next((p for p in candidates if os.path.isfile(p)), None)


# ─────────────────────────────────────────────────────────────────────────────
# ASK THE USER — File picker fallback
# ─────────────────────────────────────────────────────────────────────────────

def _ask_user_for_chrome() -> str | None:
    """
    Show a file picker so the user can locate Chrome manually.
    Returns the chosen path, or None if the user cancelled.
    """
    import tkinter as tk
    from tkinter import filedialog, messagebox

    # A picker needs a Tk root window. Create a hidden one if none exists yet.
    root = tk._default_root
    created_root = False
    if root is None:
        root = tk.Tk()
        root.withdraw()
        created_root = True

    messagebox.showinfo(
        "Locate Google Chrome",
        "We couldn't find Chrome automatically.\n\n"
        "Please select your Chrome application in the next window.\n\n"
        "Windows — usually:\n"
        "  C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe\n\n"
        "Mac — usually:\n"
        "  /Applications/Google Chrome.app"
    )

    if platform.system() == "Darwin":
        chosen = filedialog.askopenfilename(
            title="Select Google Chrome",
            initialdir="/Applications",
        )
        # On Mac, Chrome is a .app *folder* — point inside it to the real binary
        if chosen and chosen.endswith(".app"):
            inner = os.path.join(chosen, "Contents", "MacOS", "Google Chrome")
            if os.path.isfile(inner):
                chosen = inner
    else:
        chosen = filedialog.askopenfilename(
            title="Select chrome.exe",
            initialdir=r"C:\Program Files\Google\Chrome\Application",
            filetypes=[("chrome.exe", "chrome.exe"), ("All files", "*.*")],
        )

    if created_root:
        root.destroy()

    if chosen and os.path.isfile(chosen):
        return chosen
    return None


def get_chrome_path(force_prompt: bool = False) -> str | None:
    """
    Return a valid path to Chrome, in this order:
      1. Previously saved path        (skipped if force_prompt=True)
      2. Auto-detected install location
      3. Ask the user via a file picker

    The result is saved for next time. Returns None only if the user
    cancels the file picker.
    """
    if not force_prompt:
        saved = _load_saved_chrome_path()
        if saved:
            return saved

        found = _find_chrome()
        if found:
            _save_chrome_path(found)
            return found

    chosen = _ask_user_for_chrome()
    if chosen:
        _save_chrome_path(chosen)
    return chosen


def change_chrome_path() -> str | None:
    """
    Force the "locate Chrome" picker to appear again, overwriting the
    saved path. Hook this up to a "Change Chrome location" button or
    menu item in your GUI if users ever need to fix a wrong path.
    """
    return get_chrome_path(force_prompt=True)


# ─────────────────────────────────────────────────────────────────────────────
# LAUNCH CHROME — Replaces launch_chrome.bat
# ─────────────────────────────────────────────────────────────────────────────

def launch_chrome(port: int = 9222, headless: bool = False) -> bool:
    """
    Launch Chrome with remote debugging enabled, using a separate
    automation profile (won't touch the user's normal Chrome
    profile/tabs/cookies) — same as your original .bat did.

    Returns
    -------
    True  — Chrome is running and ready for playwright to connect
    False — No Chrome path available (user cancelled the picker)
    """

    # Already running with debugging on? Reuse it.
    if _is_port_open('127.0.0.1', port):
        return True

    chrome_path = get_chrome_path()
    if not chrome_path:
        return False

    user_data_dir = os.path.join(tempfile.gettempdir(), "chrome-automation")

    try:
        args = [
            chrome_path,
            f'--remote-debugging-port={port}',
            f'--user-data-dir={user_data_dir}',
            '--no-first-run',
            '--no-default-browser-check',
        ]
        if headless:
            args.append('--headless=new')

        subprocess.Popen(args)
        time.sleep(2.5)  # Give Chrome time to start up

        return _is_port_open('127.0.0.1', port)

    except Exception:
        return False


# ─────────────────────────────────────────────────────────────────────────────
# INTERNAL HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def _is_port_open(host: str, port: int, timeout: float = 1.0) -> bool:
    """Return True if something is already listening on host:port."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False
