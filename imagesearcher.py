#!/usr/bin/env python3
"""imagesearcher: a Boorus section (Danbooru, Gelbooru, Rule34, Moebooru, e621, Philomena — any site of those engines can be
added) and a Pixiv section. This launcher installs PySide6 on a plain Python (Windows double-click) and starts the app,
which lives in the imagesearcher_app/ folder next to this file."""
import importlib.util
import io
import os
import re
import shutil
import subprocess
import sys
import threading
import urllib.request
import zipfile

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
os.environ["IMAGESEARCHER_HOME"] = HERE  # history stays here even when the code runs from CODE
REPO = "acquevoor/imagesearcher"
CODE = os.path.join(os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config"), "imagesearcher", "code")


def fetch(url, accept="*/*", timeout=30):
    req = urllib.request.Request(url, headers={"User-Agent": "imagesearcher", "Accept": accept})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def current(code):
    """The folder of the downloaded code to run, or None."""
    try:
        with open(os.path.join(code, "current")) as f:
            folder = os.path.join(code, f.read().strip())
    except OSError:
        return None
    return folder if os.path.exists(os.path.join(folder, "imagesearcher_app", "app.py")) else None


def install(data, sha, code):
    """Put a GitHub ZIP's imagesearcher_app/ in CODE/<sha>; nothing changes unless every file compiles."""
    part = os.path.join(code, f"{sha}.{os.getpid()}.part")
    shutil.rmtree(part, ignore_errors=True)
    os.makedirs(os.path.join(part, "imagesearcher_app"))
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        for name in z.namelist():
            top = name.split("/")  # <repo>-<sha>/imagesearcher_app/x.py; anything else stays out
            if len(top) == 3 and top[1] == "imagesearcher_app" and top[2].endswith(".py"):
                body = z.read(name)
                compile(body, name, "exec")
                with open(os.path.join(part, "imagesearcher_app", top[2]), "wb") as f:
                    f.write(body)
    if not os.path.exists(os.path.join(part, "imagesearcher_app", "app.py")):
        raise ValueError("no app in the download")
    keep = {sha, os.path.basename(current(code) or "")}  # the older one: a window still open may import from it
    if os.path.exists(os.path.join(code, sha)):
        shutil.rmtree(part)
    else:
        os.rename(part, os.path.join(code, sha))
    with open(os.path.join(code, "current.tmp"), "w") as f:
        f.write(sha)
    os.replace(os.path.join(code, "current.tmp"), os.path.join(code, "current"))
    for old in set(os.listdir(code)) - keep - {"current"}:
        shutil.rmtree(os.path.join(code, old), ignore_errors=True)


def update(folder=HERE, code=CODE):
    """Never stops the app: offline, no git, local edits in the way or a bad download just means it starts as it is.
    A clone pulls. A ZIP, the AppImage and the Nix package have no .git (and two of them are read-only), so the
    latest imagesearcher_app/ goes in CODE, which is run instead of the copy next to this file."""
    try:
        if os.path.exists(os.path.join(folder, ".git")):
            r = subprocess.run(["git", "-C", folder, "pull", "--ff-only", "--quiet"], capture_output=True, text=True, timeout=15,
                               env={**os.environ, "GIT_TERMINAL_PROMPT": "0"})
            print("Not updated:", r.stderr.strip()) if r.returncode else print("Up to date")
            return
        # git's own ref list, as `git ls-remote` reads it: the REST API allows 60 calls an hour per IP, and a shared IP runs out
        refs = fetch(f"https://github.com/{REPO}.git/info/refs?service=git-upload-pack", timeout=8)
        m = re.search(rb"([0-9a-f]{40}) refs/heads/master\b", refs)
        if not m:
            raise ValueError("unexpected answer from GitHub")
        sha = m.group(1).decode()
        if os.path.basename(current(code) or "") == sha:
            return print("Up to date")
        os.makedirs(code, exist_ok=True)
        install(fetch(f"https://github.com/{REPO}/archive/{sha}.zip"), sha, code)
        print("Updated to", sha[:7])
    except Exception as e:
        print("Not updated:", e)


def load():
    """The app's main(). If the downloaded code won't load, the copy next to this file still does."""
    folder = None if os.path.exists(os.path.join(HERE, ".git")) else current(CODE)
    if folder and sys.argv[1:] in ([], ["--update"]):
        sys.path.insert(0, folder)
        try:
            from imagesearcher_app.app import main
            return main
        except Exception as e:
            print("The update won't load, using the built-in version:", e)
            sys.path.remove(folder)
            for m in [m for m in sys.modules if m.split(".")[0] == "imagesearcher_app"]:
                del sys.modules[m]
    from imagesearcher_app.app import main
    return main


if __name__ == "__main__":
    if sys.argv[1:] in ([], ["--update"]):  # not for --selftest and the like
        # a thread: a dead network can hang past any timeout. If it's still going, the app starts and it finishes for next time
        t = threading.Thread(target=update, daemon=True)
        t.start()
        t.join(90 if sys.argv[1:] else 20)
    main = load()
    if sys.argv[1:] == ["--update"]:  # the AppImage's build checks this: the download worked and loads in here
        print("code:", sys.modules[main.__module__].__file__)
    else:
        main()
