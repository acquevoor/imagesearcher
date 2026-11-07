#!/usr/bin/env python3
"""imagesearcher: a Boorus section (Danbooru, Gelbooru, Rule34, Moebooru, e621, Philomena — any site of those engines can be
added) and a Pixiv section. This launcher installs PySide6 on a plain Python (Windows double-click) and starts the app,
which lives in the imagesearcher_app/ folder next to this file."""
import importlib.util
import os
import subprocess
import sys

try:
    import PySide6.QtWidgets  # noqa: F401
except ImportError as e:  # first double-click on a plain Python install (e.g. Windows): fetch PySide6, then restart
    # only install when PySide6 is truly absent; installed-but-broken gets the error, not a reinstall.
    # one attempt only: if the fresh install still can't import, a restart loop would fork-bomb the machine
    if importlib.util.find_spec("PySide6") or os.environ.get("IMAGESEARCHER_PIP_TRIED"):
        input(f"PySide6 is installed but can't load: {e}\nPress Enter to close.")
        sys.exit(1)
    print("First run: installing PySide6, this takes a minute or two. Keep this window open...")
    if subprocess.call([sys.executable, "-m", "pip", "install", "PySide6"]):
        input("Installing PySide6 failed (see above). Press Enter to close.")
        sys.exit(1)
    os.environ["IMAGESEARCHER_PIP_TRIED"] = "1"
    sys.exit(subprocess.call([sys.executable] + sys.argv))

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)  # the imagesearcher_app/ folder next to this file


def update(folder=HERE):
    """A git clone updates itself on start. Offline, no git or local edits in the way: start as it is."""
    if not os.path.exists(os.path.join(folder, ".git")):  # a ZIP, the AppImage or the Nix package
        return
    try:
        r = subprocess.run(["git", "-C", folder, "pull", "--ff-only", "--quiet"], capture_output=True, text=True, timeout=15,
                           env={**os.environ, "GIT_TERMINAL_PROMPT": "0"})
        if r.returncode:
            print("Not updated:", r.stderr.strip())
    except (OSError, subprocess.TimeoutExpired) as e:
        print("Not updated:", e)


if __name__ == "__main__":
    if len(sys.argv) == 1:  # not for --selftest and the like
        update()
    from imagesearcher_app.app import main
    main()
