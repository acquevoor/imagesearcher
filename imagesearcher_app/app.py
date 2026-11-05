"""The window: a Boorus | Pixiv (| X, opt-in) switch over the sections, plus what they share (favorites, downloads, settings)."""
import json
import logging
import os
import platform
import sys
import threading
import traceback
from concurrent.futures import ThreadPoolExecutor

from PySide6.QtCore import Qt, QTimer, QUrl
from PySide6.QtGui import QDesktopServices, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QApplication, QButtonGroup, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFileDialog, QFormLayout, QHBoxLayout, QInputDialog,
    QLabel, QLineEdit, QMainWindow, QPlainTextEdit, QPushButton, QSpinBox, QStackedWidget, QVBoxLayout, QWidget,
)

from . import core, imageboard, pawchive, pixiv, x
from .booru_ui import BooruSection
from .keys import KeysDialog, missing
from .core import (
    HISTORY_DIR, NAMES, allowed, clear_cache, download_post, fetch_all, interleave, load_conf, log, move_favorite, run,
    same, save_conf, set_history, to_ui,
)
from .pixiv_ui import PixivSection
from .ib_ui import IBSection, pass_cloudflare
from .paw_ui import PawSection
from .reader_ui import ReaderSection
from .x_ui import XSection


SECRETS = {"refresh_token", "auth_token", "ct0", "key", "session", "cookie"}


def log_changes(before, after, path=""):
    """One log line per changed setting; secrets only as set / changed / cleared."""
    for k in sorted(set(before) | set(after)):
        a, b, name = before.get(k), after.get(k), f"{path}{k}"
        if isinstance(a, dict) and isinstance(b, dict):
            log_changes(a, b, name + ".")
        elif a != b:
            if k in SECRETS:
                log.info("setting %s %s", name, "cleared" if not b else "set" if not a else "changed")
            elif isinstance(b, list) or isinstance(a, list):  # lists may hold site keys: sizes only, except the blacklist
                if k == "blacklist":
                    log.info("setting blacklist: +%s -%s", sorted(set(b) - set(a)) or "nothing", sorted(set(a) - set(b)) or "nothing")
                else:
                    log.info("setting %s: %d -> %d entries", name, len(a or []), len(b or []))
            else:
                log.info("setting %s: %r -> %r", name, a, b)


def x_missing(conf):
    c = conf["x"]
    return [n for n, v in (("auth_token", c["auth_token"]), ("ct0", c["ct0"]), ("main account (Timeline/Discover)", c["seed_handle"])) if not v]


class Settings(QDialog):
    """App-wide settings; booru sites and their keys live in Boorus → Sites…"""

    def __init__(self, main):
        super().__init__(main)
        self.setWindowTitle("Settings")
        self.resize(560, 0)
        c = main.conf
        form = QFormLayout(self)
        self.token = QLineEdit(c["pixiv"]["refresh_token"], echoMode=QLineEdit.PasswordEchoOnEdit)
        row = QHBoxLayout()
        row.addWidget(self.token, 1)
        row.addWidget(QPushButton("Fetch keys…", toolTip="Log in to Gelbooru / Rule34 / Pixiv and save their keys", clicked=self.fetch_keys))
        form.addRow("Pixiv refresh token", row)
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
        form.addRow(QLabel("Gelbooru / Rule34 / Pixiv keys: Fetch keys…   Other sites' logins and keys: Boorus → Sites…", wordWrap=True))
        # X: opt-in, off by default
        xc = c["x"]
        self.x_on = QCheckBox("Enable the X section (restart to show/hide it)", checked=xc["enabled"])
        form.addRow(QLabel("<b>X</b> — read-only. Use an <b>alt</b> account: reading X this way is against X's terms and can get "
                           "that account locked. Log in like JDownloader: on x.com (logged in as the alt) copy the cookies with the "
                           "<b>Flag Cookies</b> browser extension, then press 📋 Paste cookies.", wordWrap=True))
        form.addRow(self.x_on)
        self.x_alt = xc["alt_id"]
        self.x_paste = QPushButton("📋 Paste cookies", toolTip="Reads auth_token and ct0 from the clipboard (Flag Cookies / "
                                   "EditThisCookie / Cookie-Editor JSON, cookies.txt or a Cookie header)", clicked=self.paste_x)
        self.x_test = QPushButton("Test login", toolTip="One request to X with these cookies", clicked=self.test_x)
        self.x_note = QLabel(wordWrap=True)
        row = QHBoxLayout()
        for w in (self.x_paste, self.x_test):
            row.addWidget(w)
        row.addWidget(self.x_note, 1)
        form.addRow("X login", row)
        self.x_token = QLineEdit(xc["auth_token"], echoMode=QLineEdit.PasswordEchoOnEdit, placeholderText="filled by Paste cookies")
        self.x_ct0 = QLineEdit(xc["ct0"], echoMode=QLineEdit.PasswordEchoOnEdit, placeholderText="filled by Paste cookies")
        self.x_seed = QLineEdit(xc["seed_handle"], placeholderText="@your_main, for Timeline / Discover (its following list must be public)")
        self.x_pace = QComboBox()
        for k, label in x.PACES.items():
            self.x_pace.addItem(label, k)
        self.x_pace.setCurrentIndex(self.x_pace.findData(x.pace(c)))
        self.x_pause = QSpinBox(minimum=15, maximum=x.MAX_PAUSE_MIN, singleStep=15, suffix=" min", value=xc["pause_min"])
        self.x_rand = QCheckBox("randomize (±25%)", checked=xc["pause_random"])
        form.addRow("X auth_token", self.x_token)
        form.addRow("X ct0", self.x_ct0)
        form.addRow("Main account", self.x_seed)
        form.addRow("X pace", self.x_pace)
        row = QHBoxLayout()
        row.addWidget(self.x_pause)
        row.addWidget(self.x_rand)
        form.addRow("Careful pace: pause on a rate limit for", row)
        # Pawchive (always on): host, file server, login by pasted cookie
        pc = c["paw"]
        form.addRow(QLabel("<b>Pawchive</b> — log in like JDownloader: on pawchive.pw (logged in) copy the cookies with the "
                           "<b>Flag Cookies</b> extension, then press 📋 Paste cookies. Only needed for account favorites.", wordWrap=True))
        self.paw_host = QLineEdit(pc["host"])
        self.paw_node = QSpinBox(minimum=1, maximum=9, value=int(pc["node"]), prefix="n", toolTip="File server used when a post doesn't say")
        self.paw_session = pc["session"]
        self.paw_note = QLabel("logged in" if pc["session"] else "not logged in", wordWrap=True)
        row = QHBoxLayout()
        for w in (self.paw_host, self.paw_node, QPushButton("📋 Paste cookies", clicked=self.paste_paw),
                  QPushButton("Test login", clicked=self.test_paw), QPushButton("Log out", clicked=self.logout_paw)):
            row.addWidget(w)
        form.addRow("Pawchive", row)
        form.addRow("", self.paw_note)
        # Imageboard: opt-in
        ic = c["ib"]
        self.ib_on = QCheckBox("Enable the Imageboard section (restart to show/hide it)", checked=ic["enabled"])
        form.addRow(QLabel("<b>Imageboard</b> — 4chan boards through the archives (desuarchive, archiveofsins, archived.moe) and live "
                           "4chan. NSFW boards by default.", wordWrap=True))
        form.addRow(self.ib_on)
        self.ib_boards = QPlainTextEdit("\n".join(f"{b} {h}" for b, h in ic["boards"].items()), maximumHeight=80,
                                        placeholderText="one per line: board archive-host   e.g.  d desuarchive.org")
        self.ib_boards.setToolTip("Archives: " + ", ".join(imageboard.ARCHIVES) + ". archived.moe needs Pass Cloudflare.")
        form.addRow("Boards → archive", self.ib_boards)
        self.ib_names = QComboBox()
        self.ib_names.addItem("Original filename", "original")
        self.ib_names.addItem("4chan timestamp", "timestamp")
        self.ib_names.setCurrentIndex(max(0, self.ib_names.findData(ic.get("names", "original"))))
        cf = QPushButton("☁ Pass Cloudflare…", toolTip="archived.moe, or an archive whose search is behind Cloudflare",
                         clicked=lambda: pass_cloudflare(self, self.parent().conf))
        row = QHBoxLayout()
        row.addWidget(self.ib_names)
        row.addWidget(cf)
        form.addRow("Save files as", row)
        form.addRow(QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel, accepted=self.accept, rejected=self.reject))

    def paste_x(self):
        tmp = {"x": {}}
        try:
            summary = x.cookie_login(tmp, QApplication.clipboard().text())
        except RuntimeError as e:
            log.warning("x: paste cookies: %s", e)
            return self.x_note.setText(f"<i>{e}</i>")
        self.x_token.setText(tmp["x"]["auth_token"])
        self.x_ct0.setText(tmp["x"]["ct0"])
        self.x_alt = tmp["x"]["alt_id"] or self.x_alt
        self.x_ua = tmp["x"].get("ua", "")
        self.x_on.setChecked(True)
        self.x_note.setText(f"Got {summary}. Press Save (and Test login if you like).")

    def test_x(self):
        """One UserByScreenName request with the cookies in the fields (saved or not)."""
        tmp = json.loads(json.dumps(self.parent().conf))
        tmp["x"].update(enabled=True, auth_token=self.x_token.text().strip(), ct0=self.x_ct0.text().strip(),
                        pace=self.x_pace.currentData(), alt_id=self.x_alt)
        if getattr(self, "x_ua", ""):
            tmp["x"]["ua"] = self.x_ua
        x.fix_pasted_export(tmp)
        who = self.x_seed.text().strip().lstrip("@") or "X"
        self.x_note.setText("Testing…")
        self.x_test.setEnabled(False)
        log.info("x: test login (looking up @%s)", who)

        def done(u, e):
            self.x_test.setEnabled(True)
            msg = f"✗ {e}" if e else f"✓ logged in. @{u['handle']} follows {u['following']:,} and has {u['followers']:,} followers"
            log.info("x: test login: %s", msg)
            self.x_note.setText(msg)
        run(lambda: x.user(tmp, x.load_state(), who), done, "x")

    def paste_paw(self):
        tmp = {"paw": {**self.parent().conf["paw"], "host": self.paw_host.text().strip()}}
        try:
            pawchive.cookie_login(tmp, QApplication.clipboard().text())
        except RuntimeError as e:
            return self.paw_note.setText(f"<i>{e}</i>")
        self.paw_session = tmp["paw"]["session"]
        self.paw_note.setText("Got the session cookie. Press Save (and Test login if you like).")

    def test_paw(self):
        tmp = json.loads(json.dumps(self.parent().conf))
        tmp["paw"].update(host=self.paw_host.text().strip(), session=self.paw_session)
        self.paw_note.setText("Testing…")
        run(lambda: pawchive.check_login(tmp),
            lambda r, e: self.paw_note.setText(f"✗ {e}" if e else f"✓ logged in: {r[0]} favorite posts, {r[1]} favorite artists"))

    def logout_paw(self):
        self.paw_session = ""
        self.paw_note.setText("Logged out (press Save)")

    def fetch_keys(self):
        """The key fetcher; afterwards the token field shows what it saved, so Save can't put the old one back."""
        d = KeysDialog(self.parent())
        d.finished.connect(lambda _: self.token.setText(self.parent().conf["pixiv"]["refresh_token"]))
        d.open()

    def apply(self, conf):
        before = json.loads(json.dumps(conf))
        self._apply(conf)
        log_changes(before, conf)

    def _apply(self, conf):
        if self.token.text().strip() != conf["pixiv"]["refresh_token"]:
            conf["pixiv"].update(refresh_token=self.token.text().strip(), user_id="", premium=False)
            pixiv.logout()
        conf["blacklist"] = sorted(set(self.bl.toPlainText().lower().split()))
        conf["download_dir"], conf["limit"] = self.dir.text().strip(), self.limit.value()
        if getattr(self, "x_ua", ""):
            conf["x"]["ua"] = self.x_ua
        conf["x"].update(enabled=self.x_on.isChecked(), auth_token=self.x_token.text().strip(), ct0=self.x_ct0.text().strip(),
                         seed_handle=self.x_seed.text().strip().lstrip("@"), pause_min=self.x_pause.value(), pause_random=self.x_rand.isChecked(),
                         pace=self.x_pace.currentData(), alt_id=self.x_alt)
        x.fix_pasted_export(conf)  # a whole export pasted into the auth_token field
        conf["paw"].update(host=self.paw_host.text().strip() or "pawchive.pw", node=self.paw_node.value(), session=self.paw_session)
        pairs = [ln.split() for ln in self.ib_boards.toPlainText().splitlines() if len(ln.split()) == 2]
        conf["ib"].update(enabled=self.ib_on.isChecked(), names=self.ib_names.currentData(),
                          boards={b.strip("/").lower(): h.lower() for b, h in pairs} or conf["ib"]["boards"])


class Main(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("imagesearcher")
        self.resize(1400, 900)
        self.conf = load_conf()
        imageboard.apply_passes(self.conf)  # Cloudflare passes: the Reader's images use them too
        self.cancel = None
        self.switch = QButtonGroup(self)
        top = QHBoxLayout()
        self.stack = QStackedWidget()
        self.boorus, self.pixiv = BooruSection(self), PixivSection(self)
        self.paw = PawSection(self)
        self.reader = ReaderSection(self)
        self.x = XSection(self) if self.conf["x"]["enabled"] else None
        self.ib = IBSection(self) if self.conf["ib"]["enabled"] else None
        secs = [(self.boorus, "Boorus", "Ctrl+1"), (self.pixiv, "Pixiv", "Ctrl+2"), (self.paw, "Pawchive", "Ctrl+5"), (self.reader, "Reader", "Ctrl+6")] \
            + ([(self.x, "X", "Ctrl+3")] if self.x else []) + ([(self.ib, "Imageboard", "Ctrl+4")] if self.ib else [])
        for i, (sec, name, key) in enumerate(secs):
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
        self.last_status = ""
        self.statusBar().messageChanged.connect(self.log_status)  # everything shown down there is in the log too
        self.show_section({"pixiv": self.pixiv, "x": self.x, "ib": self.ib, "paw": self.paw, "rd": self.reader}.get(self.conf["section"]) or self.boorus)

    def show_section(self, sec):
        if self.stack.currentWidget() is not sec:
            log.info("section -> %s", self.switch.button(self.stack.indexOf(sec)).text())
        self.stack.setCurrentWidget(sec)
        self.switch.button(self.stack.indexOf(sec)).setChecked(True)
        self.conf["section"] = {id(self.pixiv): "pixiv", id(self.x): "x", id(self.ib): "ib", id(self.paw): "paw", id(self.reader): "rd"}.get(id(sec), "boorus")

    def read(self, tag):
        """📖 Read in Reader, from any section's tags."""
        self.reader.search(tag)

    def log_status(self, msg):
        if msg and msg != self.last_status and not msg.startswith("Loading page"):
            log.info("status: %s", msg)
        self.last_status = msg or self.last_status

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
        sec = {"pixiv": self.pixiv, "x": self.x, "ib": self.ib, "paw": self.paw}.get(p["site"], self.boorus)
        if sec:  # open image tabs keep their own post
            sec.refresh_favs()
        root, fav = self.conf["download_dir"], self.is_fav(p)
        run(lambda: move_favorite(root, p, fav), lambda r, e: e and self.statusBar().showMessage(f"favorites folder: {e}"), "fav")

    def blacklist_add(self, tag):
        self.conf["blacklist"] = sorted(set(self.conf["blacklist"]) | {tag.lower()})
        save_conf(self.conf)
        log.info("blacklist + %s", tag)
        self.statusBar().showMessage(f"Blacklisted '{tag}' (applies to new searches)")

    def save_post(self, p, root, label, cancel=None):
        if p["site"] == "ib":  # original filenames, imageboard/<board>/<label>/
            return imageboard.save(self.conf, p, root, label, cancel)
        if p["site"] == "paw":  # original filenames, pawchive/<creator> (<service>)/<post> - <title>/
            return pawchive.save(self.conf, p, root, cancel)
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
            restart = self.conf["x"]["enabled"] != bool(self.x) or self.conf["ib"]["enabled"] != bool(self.ib)
            if self.conf["x"]["enabled"] and (miss := x_missing(self.conf)):
                log.warning("x: enabled but not set up, missing: %s", ", ".join(miss))
            self.statusBar().showMessage("Settings saved" + ("  — restart the app to show/hide the X / Imageboard section" if restart else ""))


def main():
    if "--selftest" in sys.argv:
        from .selftest import selftest
        return selftest()
    if sys.stderr:  # console window / terminal; absent under pythonw
        logging.basicConfig(format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
    # IMAGESEARCHER_DEBUG=1 also logs every image / thumbnail fetch and tracebacks of handled errors
    log.setLevel(logging.DEBUG if os.environ.get("IMAGESEARCHER_DEBUG") else logging.INFO)

    def crash(kind, exc, tb):
        log.critical("unhandled error: %s", "".join(traceback.format_exception(kind, exc, tb)).rstrip())
    sys.excepthook = crash
    threading.excepthook = lambda a: crash(a.exc_type, a.exc_value, a.exc_traceback)
    QApplication.setAttribute(Qt.AA_ShareOpenGLContexts)  # the key fetcher's web view needs it, set before the app exists
    app = QApplication(sys.argv)
    app.setApplicationName("imagesearcher")
    app.setDesktopFileName("imagesearcher")
    core.bridge = core.Bridge()
    import PySide6
    log.info("started: Python %s, PySide6 %s, %s %s", platform.python_version(), PySide6.__version__, platform.system(), platform.release())
    w = Main()
    clear_cache(w.conf["download_dir"])  # left over from a crash
    app.aboutToQuit.connect(lambda: (x.STOP.set(), core.shutdown(w.conf)))  # STOP wakes X requests waiting their turn
    c = w.conf
    log.info("config %s, downloads %s, history %s", core.CONF_PATH, c["download_dir"], f"on ({HISTORY_DIR})" if c["history"] else "off")
    log.info("boorus on: %s; pixiv: %s; x: %s", ", ".join(b["name"] for b in c["boorus"] if b["on"]) or "none",
             "token set" if c["pixiv"]["refresh_token"] else "no token",
             "off" if not c["x"]["enabled"] else f"on, missing {', '.join(x_missing(c))}" if x_missing(c) else "on, set up")
    from PySide6.QtGui import QImageReader
    if b"webp" not in [bytes(f) for f in QImageReader.supportedImageFormats()]:
        log.warning("this Qt can't decode WebP images (Pawchive thumbnails, some booru files show blank): "
                    "install Qt's imageformats plugin (Nix: qt6.qtimageformats on QT_PLUGIN_PATH)")
    log.info("pawchive: %s, file server n%s, %s", c["paw"]["host"], c["paw"]["node"], "logged in" if c["paw"]["session"] else "not logged in")
    log.info("imageboard: %s", "off" if not c["ib"]["enabled"] else
             "on, " + ", ".join(f"/{b}/ via {h}" for b, h in c["ib"]["boards"].items()) + (f"; Cloudflare passed for {', '.join(c['ib']['cf'])}" if c["ib"]["cf"] else ""))
    w.show()
    if w.conf["keys_prompt"] and missing(w.conf):  # offered on every launch while a key is empty, unless switched off
        QTimer.singleShot(0, lambda: KeysDialog(w).show())
    code = app.exec()
    log.info("quit")
    sys.exit(code)
