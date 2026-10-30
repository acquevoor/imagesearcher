"""The window: a Boorus | Pixiv switch over the two sections, plus what they share (favorites, downloads, settings)."""
import logging
import sys
import threading
from concurrent.futures import ThreadPoolExecutor

from PySide6.QtCore import QUrl
from PySide6.QtGui import QDesktopServices, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QApplication, QButtonGroup, QCheckBox, QDialog, QDialogButtonBox, QFileDialog, QFormLayout, QHBoxLayout, QInputDialog,
    QLabel, QLineEdit, QMainWindow, QPlainTextEdit, QPushButton, QSpinBox, QStackedWidget, QVBoxLayout, QWidget,
)

from . import core, pixiv
from .booru_ui import BooruSection
from .core import (
    HISTORY_DIR, NAMES, allowed, clear_cache, download_post, fetch_all, interleave, load_conf, log, move_favorite, run,
    same, save_conf, set_history, to_ui,
)
from .pixiv_ui import PixivSection


class Settings(QDialog):
    """App-wide settings; booru sites and their keys live in Boorus → Sites…"""

    def __init__(self, main):
        super().__init__(main)
        self.setWindowTitle("Settings")
        self.resize(560, 0)
        c = main.conf
        form = QFormLayout(self)
        self.token = QLineEdit(c["pixiv"]["refresh_token"], echoMode=QLineEdit.PasswordEchoOnEdit)
        self.login_status = QLabel(wordWrap=True)
        row = QHBoxLayout()
        row.addWidget(self.token, 1)
        row.addWidget(QPushButton("Log in to Pixiv…", clicked=self.pixiv_login))
        form.addRow("Pixiv refresh token", row)
        form.addRow(self.login_status)
        self.bl = QPlainTextEdit(" ".join(c["blacklist"]), placeholderText="tags, space/newline separated; rating:e / rating:q also work")
        form.addRow("Blacklist (both sections)", self.bl)
        self.dir = QLineEdit(c["download_dir"])
        browse = QPushButton("…", clicked=lambda: self.dir.setText(QFileDialog.getExistingDirectory(self, "Download dir", self.dir.text()) or self.dir.text()))
        row = QHBoxLayout()
        row.addWidget(self.dir)
        row.addWidget(browse)
        form.addRow("Download dir", row)
        self.limit = QSpinBox(minimum=10, maximum=100, value=c["limit"])
        form.addRow("Booru posts per page", self.limit)
        form.addRow(QLabel("Booru sites, their logins and API keys: Boorus → Sites…   Pixiv token: see README.", wordWrap=True))
        form.addRow(QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel, accepted=self.accept, rejected=self.reject))

    def pixiv_login(self):
        """Fill the token by logging in through the browser: the user copies one URL back, nothing else."""
        verifier, url = pixiv.login_start()
        QDesktopServices.openUrl(QUrl(url))
        text, ok = QInputDialog.getText(self, "Log in to Pixiv", (
            "A Pixiv login page opened in your browser.\n\n"
            "1. Press F12 there and pick the Network tab.\n"
            "2. Click Continue (or log in). The page then tries to open a pixiv:// link; that's expected.\n"
            "3. In Network, find the request starting with 'callback?state=' → right-click → Copy → Copy URL.\n"
            "4. Paste it here within a minute (the bare code works too)."))
        if not ok or not text.strip():
            return
        self.login_status.setText("logging in…")

        def done(token, err):
            if err:
                return self.login_status.setText(f"<i>{err}</i>")
            self.token.setText(token)
            self.login_status.setText("Logged in. Save to keep the token.")
        run(lambda: pixiv.login_finish(verifier, pixiv.login_code(text)), done)

    def apply(self, conf):
        if self.token.text().strip() != conf["pixiv"]["refresh_token"]:
            conf["pixiv"].update(refresh_token=self.token.text().strip(), user_id="", premium=False)
            pixiv.logout()
        conf["blacklist"] = sorted(set(self.bl.toPlainText().lower().split()))
        conf["download_dir"], conf["limit"] = self.dir.text().strip(), self.limit.value()


class Main(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("imagesearcher")
        self.resize(1400, 900)
        self.conf = load_conf()
        self.cancel = None
        self.switch = QButtonGroup(self)
        top = QHBoxLayout()
        self.stack = QStackedWidget()
        self.boorus, self.pixiv = BooruSection(self), PixivSection(self)
        for i, (sec, name, key) in enumerate(((self.boorus, "Boorus", "Ctrl+1"), (self.pixiv, "Pixiv", "Ctrl+2"))):
            b = QPushButton(f"{name}", checkable=True, toolTip=key, minimumWidth=110)
            self.switch.addButton(b, i)
            top.addWidget(b)
            self.stack.addWidget(sec)
            QShortcut(QKeySequence(key), self, lambda s=sec: self.show_section(s))
        self.switch.idClicked.connect(lambda i: self.show_section(self.stack.widget(i)))
        top.addStretch()
        self.history_cb = QCheckBox("History", checked=self.conf["history"],
                                    toolTip=f"Also save the activity log, one text file per day, in {HISTORY_DIR}")
        self.history_cb.toggled.connect(self.toggle_history)
        set_history(self.conf["history"])
        for w in (self.history_cb, QPushButton("📂", toolTip=f"Open history folder: {HISTORY_DIR}", clicked=self.open_history),
                  QPushButton("⚙", toolTip="Settings", clicked=self.settings)):
            top.addWidget(w)
        central = QWidget()
        lay = QVBoxLayout(central)
        lay.addLayout(top)
        lay.addWidget(self.stack, 1)
        self.setCentralWidget(central)
        self.cancel_btn = QPushButton("Cancel download", clicked=lambda: self.cancel and self.cancel.set())
        self.cancel_btn.hide()
        self.statusBar().addPermanentWidget(self.cancel_btn)
        self.show_section(self.pixiv if self.conf["section"] == "pixiv" else self.boorus)

    def show_section(self, sec):
        self.stack.setCurrentWidget(sec)
        self.switch.button(self.stack.indexOf(sec)).setChecked(True)
        self.conf["section"] = "pixiv" if sec is self.pixiv else "boorus"

    # --- shared by both sections ---
    def toggle_history(self, on):
        self.conf["history"] = on
        save_conf(self.conf)
        set_history(on)
        log.info("history %s (%s)", "on" if on else "off", HISTORY_DIR)

    def open_history(self):
        HISTORY_DIR.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(HISTORY_DIR)))

    def is_fav(self, p):
        return any(same(f, p) for f in self.conf["favorites"])

    def toggle_fav(self, p):
        favs = self.conf["favorites"]
        if self.is_fav(p):
            favs[:] = [f for f in favs if not same(f, p)]
            log.info("unfavorite %s #%s", p["site"], p["id"])
        else:
            favs.insert(0, p)
            log.info("favorite %s #%s", p["site"], p["id"])
        save_conf(self.conf)
        (self.pixiv if p["site"] == "pixiv" else self.boorus).refresh_favs()  # open image tabs keep their own post
        root, fav = self.conf["download_dir"], self.is_fav(p)
        run(lambda: move_favorite(root, p, fav), lambda r, e: e and self.statusBar().showMessage(f"favorites folder: {e}"), "fav")

    def blacklist_add(self, tag):
        self.conf["blacklist"] = sorted(set(self.conf["blacklist"]) | {tag.lower()})
        save_conf(self.conf)
        log.info("blacklist + %s", tag)
        self.statusBar().showMessage(f"Blacklisted '{tag}' (applies to new searches)")

    def save_post(self, p, root, label, cancel=None):
        if p.get("ugoira"):
            return pixiv.download_ugoira(self.conf, p, root, label)
        return download_post(p, root, label, cancel)

    def download(self, p, label):
        root = self.conf["download_dir"]
        self.statusBar().showMessage(f"Downloading {NAMES.get(p['site'], p['site'])} #{p['id']}…")

        def done(r, e):
            msg = f"download {p['site']} #{p['id']}: " + (f"failed: {e}" if e else f"saved {r[0]}, already had {r[1]}")
            (log.error if e else log.info)(msg)
            self.statusBar().showMessage(msg)
        run(lambda: self.save_post(p, root, label), done)

    def bulk(self, feed):
        if self.cancel:
            return self.statusBar().showMessage("A bulk download is already running")
        query, srcs, keep = (feed.label, list(feed.srcs), feed.keep) if feed else ("", [], None)
        if not srcs:
            return self.statusBar().showMessage("Nothing to download in this tab")
        limit, ok = QInputDialog.getInt(self, "Bulk download", f"Download '{query}' from {', '.join(dict.fromkeys(s.label for s in srcs))}\n"
                                        f"into {self.conf['download_dir']}\n\nMax posts (0 = all):", 0, 0, 10 ** 6)
        if not ok:
            return
        self.cancel = cancel = threading.Event()
        log.info("bulk '%s' on %s, max %s", query, [s.label for s in srcs], limit or "all")
        self.cancel_btn.show()
        bl, root = set(self.conf["blacklist"]), self.conf["download_dir"]

        def progress(st, err):
            self.statusBar().showMessage(f"Bulk '{query}': {st['new']} new, {st['skip']} skipped, {st['fail']} failed, page {st['page']}"
                                         + (f"  |  {st['err']}" if st["err"] else ""))

        def work():
            st = {"new": 0, "skip": 0, "fail": 0, "page": 0, "posts": 0, "err": ""}
            active = srcs
            while active and not cancel.is_set() and not (limit and st["posts"] >= limit):
                st["page"] += 1
                lists, errs = fetch_all(active, st["page"])
                st["err"] = "  ".join(errs.values())
                active = [s for s in active if lists[s]]
                posts = [p for p in interleave(lists.values()) if allowed(p, bl) and (not keep or keep(p))]
                if limit:
                    posts = posts[:limit - st["posts"]]
                st["posts"] += len(posts)
                # ponytail: 4 parallel downloads per page, polite enough for boorus; Pixiv may still 429 on huge runs
                with ThreadPoolExecutor(4) as ex:
                    for f in [ex.submit(self.save_post, p, root, query, cancel) for p in posts]:
                        try:
                            n, s = f.result()
                            st["new"] += n
                            st["skip"] += s
                        except Exception as e:
                            st["fail"] += 1
                            st["err"] = str(e)
                to_ui(progress, dict(st))
            return st

        def finished(st, err):
            self.cancel = None
            self.cancel_btn.hide()
            if err:
                log.error("bulk '%s' failed: %s", query, err)
                return self.statusBar().showMessage(f"Bulk failed: {err}")
            progress(st, None)
            log.info("bulk '%s' %s: %d new, %d skipped, %d failed", query, "cancelled" if cancel.is_set() else "done",
                     st["new"], st["skip"], st["fail"])
            self.statusBar().showMessage(self.statusBar().currentMessage() + ("  — cancelled" if cancel.is_set() else "  — done"))

        def thread():
            try:
                res, err = work(), None
            except Exception as e:
                res, err = None, e
            to_ui(finished, res, err)
        threading.Thread(target=thread, daemon=True).start()

    def settings(self):
        d = Settings(self)
        if d.exec():
            d.apply(self.conf)
            save_conf(self.conf)
            self.boorus.reload_sites()  # posts-per-page lives in the engines
            log.info("settings saved")
            self.statusBar().showMessage("Settings saved")


def main():
    if "--selftest" in sys.argv:
        from .selftest import selftest
        return selftest()
    if sys.stderr:  # console window / terminal; absent under pythonw
        logging.basicConfig(format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
    log.setLevel(logging.INFO)
    app = QApplication(sys.argv)
    app.setApplicationName("imagesearcher")
    app.setDesktopFileName("imagesearcher")
    core.bridge = core.Bridge()
    w = Main()
    clear_cache(w.conf["download_dir"])  # left over from a crash
    app.aboutToQuit.connect(lambda: core.shutdown(w.conf))
    log.info("started")
    w.show()
    code = app.exec()
    log.info("quit")
    sys.exit(code)
