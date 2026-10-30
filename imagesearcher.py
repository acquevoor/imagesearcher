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

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # the imagesearcher_app/ folder next to this file
from imagesearcher_app.app import main  # noqa: E402

if __name__ == "__main__":
    main()
