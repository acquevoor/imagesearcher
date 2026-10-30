"""Key fetcher: logs in to Gelbooru / Rule34 / Pixiv in a small built-in browser window and saves what the app needs
(API key + user id, or the Pixiv refresh token). Offered on launch while any is missing, unless switched off, and
always from ⚙ → Fetch keys…  The login is typed into the site's own page; the window keeps no cookies afterwards."""
import re
import webbrowser

from PySide6.QtCore import QTimer, QUrl, Signal
from PySide6.QtWidgets import QCheckBox, QDialog, QDialogButtonBox, QGridLayout, QInputDialog, QLabel, QPushButton, QVBoxLayout

from . import boorus, pixiv
from .core import log, preset, run, save_conf

TARGETS = {"gelbooru": "Gelbooru", "rule34": "Rule34", "pixiv": "Pixiv"}
KEY, UID = re.compile(r"api_key=([0-9a-fA-F]{20,})"), re.compile(r"user_id=(\d+)")


def site(conf, sid):
    return next((s for s in conf["boorus"] if s["id"] == sid), None)


def missing(conf):
    """Which of TARGETS still lack their keys (a booru that isn't added yet counts as missing)."""
    out = [sid for sid in ("gelbooru", "rule34") if not ((s := site(conf, sid)) and s["login"] and s["key"])]
    return out + ([] if conf["pixiv"]["refresh_token"] else ["pixiv"])


def parse_creds(text):
    """(user id, API key) from a Gelbooru-family account page ("&api_key=…&user_id=…"), else None."""
    k, u = KEY.search(text or ""), UID.search(text or "")
    return (u[1], k[1]) if k and u else None


def options_url(sid):
    return f"{preset(sid)['url']}/index.php?page=account&s=options"


def verify(sid, creds):
    """One small API call with the fetched key; raises when the site refuses it."""
    s = {**preset(sid), "login": creds[0], "key": creds[1]}
    boorus.ENGINES[s["engine"]](s, 1).search("", 1)
    return creds


def store(conf, sid, value):
    """Save a fetch result: (user id, key) for a booru (adding the site if it isn't there), the token for Pixiv."""
    if sid == "pixiv":
        conf["pixiv"].update(refresh_token=value, user_id="", premium=False)
        pixiv.logout()
    else:
        s = site(conf, sid)
        if s is None:
            s = {**preset(sid), "on": True}
            conf["boorus"].append(s)
        s["login"], s["key"] = value
    save_conf(conf)


def web():
    """QtWebEngine's (page, profile, view) classes, or None when this PySide6 lacks it."""
    try:
        from PySide6.QtWebEngineCore import QWebEnginePage, QWebEngineProfile
        from PySide6.QtWebEngineWidgets import QWebEngineView
    except ImportError:
        return None
    return QWebEnginePage, QWebEngineProfile, QWebEngineView


class LoginView(QDialog):
    """The site's real login page in a web view. Booru: once logged in it opens the account options page and reads the
    credentials line. Pixiv: catches the login's pixiv:// redirect (with the code the app API wants)."""
    got = Signal(object)

    def __init__(self, parent, sid):
        super().__init__(parent)
        self.sid, self.finished_, self.busy, self.callback = sid, False, False, None
        Page, Profile, View = web()
        self.setWindowTitle(f"Log in to {TARGETS[sid]}")
        self.resize(1000, 760)
        self.status = QLabel(wordWrap=True)
        self.profile = Profile(self)  # no storage name = off the record: nothing reaches the disk
        dlg = self

        class Watch(Page):
            def acceptNavigationRequest(self, url, kind, main_frame):
                u = url.toString()
                if dlg.sid == "pixiv" and "code=" in u:
                    if u.startswith("pixiv://"):
                        dlg.exchange(u)
                        return False
                    if "/auth/pixiv/callback" in u:  # same code one hop earlier; used if the pixiv:// hop never shows
                        dlg.callback = u
                        QTimer.singleShot(2000, lambda: dlg.callback and dlg.exchange(dlg.callback))
                return super().acceptNavigationRequest(url, kind, main_frame)

        self.view = View(self)
        self.view.setPage(Watch(self.profile, self.view))
        self.view.loadFinished.connect(self.loaded)
        lay = QVBoxLayout(self)
        lay.addWidget(self.status)
        lay.addWidget(self.view, 1)
        if sid == "pixiv":
            self.verifier, url = pixiv.login_start()
            self.status.setText("Log in to Pixiv (or click Continue). The token is picked up by itself.")
        else:
            url = f"{preset(sid)['url']}/index.php?page=account&s=login&code=00"
            self.status.setText(f"Log in to {TARGETS[sid]} (solve the CAPTCHA if one shows). The API key is read from your account page by itself.")
        self.view.load(QUrl(url))

    # --- booru: find the credentials line ---
    def loaded(self, ok):
        if self.sid != "pixiv" and not self.finished_:
            url = self.view.url().toString()
            self.view.page().toHtml(lambda html: self.scan(url, html))

    def scan(self, url, html):
        if self.finished_ or self.busy:
            return
        creds = parse_creds(html)
        if creds:
            self.busy = True
            self.status.setText("Found the API key, checking it…")
            run(lambda: verify(self.sid, creds), self.verified)
        elif "s=options" in url:
            self.status.setText("This account has no API key yet: tick “Generate new key” on this page and Save.")
        elif "s=login" not in url and "page=account" in url:  # logged in: go read the key
            self.view.load(QUrl(options_url(self.sid)))

    def verified(self, creds, err):
        self.busy = False
        if err:
            return self.status.setText(f"{TARGETS[self.sid]} refused the key it showed: {err}")
        self.finish(creds)

    # --- pixiv: trade the code ---
    def exchange(self, url):
        if self.busy or self.finished_:
            return
        self.busy, self.callback = True, None
        self.status.setText("Logging in…")
        verifier = self.verifier
        run(lambda: pixiv.login_finish(verifier, pixiv.login_code(url)), self.exchanged)

    def exchanged(self, token, err):
        self.busy = False
        if err:
            return self.status.setText(f"{err}. Reload the page and log in again.")
        self.finish(token)

    def finish(self, value):  # not done(): that's QDialog's own, accept() calls it
        self.finished_ = True
        self.got.emit(value)
        self.accept()


class KeysDialog(QDialog):
    """One row per target: status + Fetch…; plus the "Don't show this again" toggle for the launch prompt."""

    def __init__(self, main):
        super().__init__(main)
        self.main, self.conf = main, main.conf
        self.setWindowTitle("Fetch API keys")
        self.resize(520, 0)
        lay = QVBoxLayout(self)
        lay.addWidget(QLabel("Gelbooru and Rule34 only answer with an API key + user id, and the Pixiv section needs a login token. "
                             "Fetch… opens the site's login in a small window and saves the key; your password is typed into the "
                             "site's own page and never kept.", wordWrap=True))
        grid = QGridLayout()
        self.rows = {}
        for r, (sid, name) in enumerate(TARGETS.items()):
            state, btn = QLabel(), QPushButton(clicked=lambda _=False, s=sid: self.fetch(s))
            for c, w in enumerate((QLabel(f"<b>{name}</b>"), state, btn)):
                grid.addWidget(w, r, c)
            self.rows[sid] = state, btn
        lay.addLayout(grid)
        self.hide_box = QCheckBox("Don't show this again at startup", checked=not self.conf["keys_prompt"])
        self.hide_box.toggled.connect(self.toggle_prompt)
        lay.addWidget(self.hide_box)
        lay.addWidget(QLabel("<small>⚙ Settings → Fetch keys… opens this any time.</small>"))
        lay.addWidget(QDialogButtonBox(QDialogButtonBox.Close, rejected=self.reject))
        self.refresh()

    def toggle_prompt(self, hide):
        self.conf["keys_prompt"] = not hide
        save_conf(self.conf)

    def refresh(self):
        gone = missing(self.conf)
        for sid, (state, btn) in self.rows.items():
            state.setText("missing" if sid in gone else "✓ set")
            btn.setText("Fetch…" if sid in gone else "Fetch again…")

    def fetch(self, sid):
        if web():
            v = LoginView(self, sid)
            v.got.connect(lambda value: self.fetched(sid, value))
            v.open()
        else:
            self.paste(sid)

    def paste(self, sid):
        """No QtWebEngine in this PySide6: the system browser + one paste instead."""
        if sid == "pixiv":
            verifier, url = pixiv.login_start()
            webbrowser.open(url)
            text, ok = QInputDialog.getText(self, "Log in to Pixiv", "In the browser: F12 → Network, then Continue. Copy the URL of the "
                                            "'callback?state=…&code=…' request and paste it here within a minute:")
            if ok and text.strip():
                run(lambda: pixiv.login_finish(verifier, pixiv.login_code(text)), lambda t, e: self.result(sid, t, e))
        else:
            webbrowser.open(options_url(sid))
            text, ok = QInputDialog.getText(self, f"{TARGETS[sid]} API key", "On the page that opened (log in first), copy the "
                                            "“&api_key=…&user_id=…” line under API Access Credentials and paste it here:")
            creds = ok and parse_creds(text)
            if creds:
                run(lambda: verify(sid, creds), lambda c, e: self.result(sid, c, e))

    def result(self, sid, value, err):
        if err:
            return self.rows[sid][0].setText(f"<i>{err}</i>")
        self.fetched(sid, value)

    def fetched(self, sid, value):
        store(self.conf, sid, value)
        log.info("fetched keys for %s", sid)
        self.main.boorus.reload_sites()
        self.main.boorus.tags_tab.reload_sites()
        self.refresh()
        if not missing(self.conf):
            self.accept()
