"""The X section (opt-in): Timeline = media from the accounts your main follows, Discover = media from the accounts
many of them follow, artist tabs, Favorites, and bulk downloads of one @handle (optionally with its following /
followers). Read-only; every request waits its turn in x.budget, and a banner shows what X traffic is doing."""
import html
import threading
import time
import webbrowser

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QDialog, QDialogButtonBox, QFormLayout, QHBoxLayout, QLabel, QLineEdit, QListWidgetItem,
    QPushButton, QSpinBox, QVBoxLayout, QWidget,
)

from . import x
from .core import allowed, dest, log, run, save_conf, to_ui
from .widgets import Feed, Grid, LiveBulk, Section, rich_label


class XSection(Section):
    SAVED = "x_saved"

    def __init__(self, main):
        super().__init__(main, "@handle   Enter = open that account's media tab")
        if x.fix_pasted_export(self.conf):
            save_conf(self.conf)
        self.state, self.graph_note, self.last_state, self.bulk_note = x.load_state(), "", "", ""
        self.jobs, self.cancels = {}, {}  # kind ("check1", "check2", "graph", "bulk") -> running job name / its Stop event
        st = self.state
        log.info("x: state loaded: %d follows, ring 2 = %d accounts%s, %d cached posts, %d accounts checked before",
                 len(st["follows"]), len(st["graph"]["ring2"]), f" (graph paused at {st['graph']['next']})" if st["graph"]["next"] else "",
                 len(st["posts"]), len(st["checked"]))
        self.bar.addWidget(self.q, 1)
        for w in (QPushButton("Open", clicked=lambda: self.search()), self.saved, self.star,
                  QPushButton("⬇ Bulk @handle…", toolTip="Download all media of one account (optionally its following / followers too)",
                              clicked=lambda: self.bulk_dialog())):
            self.bar.addWidget(w)
        self.banner = QLabel(wordWrap=True)
        self.resume_btn = QPushButton("Resume X", clicked=self.resume)
        self.paste_btn = QPushButton("📋 Paste cookies", toolTip="Log in: the alt's x.com cookies, copied with the Flag Cookies "
                                     "browser extension (like JDownloader)", clicked=self.paste_cookies)
        self.continue_btn = QPushButton("", clicked=self.continue_bulk)
        self.cancel_btn = QPushButton("Cancel bulk", clicked=self.cancel_bulk)
        row = QHBoxLayout()
        row.addWidget(self.banner, 1)
        for w in (self.paste_btn, self.continue_btn, self.cancel_btn, self.resume_btn):
            row.addWidget(w)
        self.layout().insertLayout(1, row)
        self.timeline, self.discover = Ring(self, 1), Ring(self, 2)
        self.add_fixed(self.timeline, "Timeline")
        self.add_fixed(self.discover, "Discover")
        self.favs = Grid(self.open_image)
        self.add_fixed(self.favs, "Favorites")
        self.refresh_favs()
        QTimer(self, interval=5000, timeout=self.update_banner).start()
        self.update_banner()

    # --- banner / jobs: one job per kind; kinds run side by side (the budget still spaces every request) ---
    def update_banner(self):
        why = x.paused(self.conf)
        if not x.ready(self.conf):
            text = x.NOT_SET
        elif why:
            text = why
        else:
            text = f"X: {x.budget.status(x.pace(self.conf))} · " + (" · ".join(self.jobs.values()) or "idle")
        for note in (self.graph_note, self.bulk_note):
            if note:
                text += f" · {note}"
        b = self.state.get("bulk")
        self.paste_btn.setVisible(not x.ready(self.conf))
        self.cancel_btn.setVisible("bulk" in self.jobs)
        resumable = bool(b and not b["finished"] and "bulk" not in self.jobs)
        self.continue_btn.setVisible(resumable)
        if resumable:
            self.continue_btn.setText(f"Continue bulk @{b['handle']} ({b['i']}/{len(b['accounts'])} accounts)")
        kind = "not set up" if not x.ready(self.conf) else why or (", ".join(self.jobs.values()) or "idle")
        if kind != self.last_state:  # the banner's meaning changed (not just the request counter)
            log.info("x: status: %s", text)
            self.last_state = kind
        self.banner.setText(text)
        self.resume_btn.setVisible(bool(why))

    POOL = {"graph": "xgraph", "bulk": "xbulk"}  # checks use "x"

    def running(self, kind):
        return kind in self.jobs

    def stop(self, kind):
        if kind in self.cancels:
            log.info("x: stop pressed for %s", self.jobs.get(kind, kind))
            self.cancels[kind].set()
            self.main.statusBar().showMessage(f"X: stopping {self.jobs.get(kind, kind)} after the current request…")

    def start_job(self, kind, name, fn, done):
        """Run fn(cancel) on this kind's X worker; refuse while a job of the same kind runs. -> True if started."""
        if kind in self.jobs:
            log.info("x: not starting '%s': '%s' is still running", name, self.jobs[kind])
            self.main.statusBar().showMessage(f"X: {self.jobs[kind]} is still running")
            return False
        try:
            x.check(self.conf)
        except RuntimeError as e:
            log.warning("x: not starting '%s': %s", name, e)
            self.main.statusBar().showMessage(str(e))
            self.update_banner()
            return False
        log.info("x: job started: %s", name)
        t0, cancel = time.time(), threading.Event()
        self.jobs[kind], self.cancels[kind] = name, cancel
        self.update_banner()

        def finished(res, err):
            self.jobs.pop(kind, None)
            self.cancels.pop(kind, None)
            if cancel.is_set() and not err:
                log.info("x: job stopped: %s", name)
            log.info("x: job %s: %s (%.0f s)", "failed" if err else "done", name, time.time() - t0)
            if err:
                log.warning("x: %s: %s", name, err)
                self.main.statusBar().showMessage(str(err))
            done(res, err)
            self.update_banner()
        run(lambda: fn(cancel), finished, self.POOL.get(kind, "x"))
        return True

    def resume(self):
        x.resume(self.conf)
        self.update_banner()

    def paste_cookies(self):
        """JDownloader-style login: the cookies from the clipboard straight into the settings."""
        try:
            summary = x.cookie_login(self.conf, QApplication.clipboard().text())
        except RuntimeError as e:
            log.warning("x: paste cookies: %s", e)
            return self.main.statusBar().showMessage(f"X: {e}")
        save_conf(self.conf)
        self.main.statusBar().showMessage(f"X: logged in with the pasted cookies ({summary})")
        self.update_banner()

    # --- bulk: one @handle (+ optionally its following / followers) ---
    def bulk_dialog(self, handle=None):
        f = self.current_feed()
        d = BulkDialog(self, handle if handle is not None else (getattr(f, "query", "") or "").lstrip("@"))
        if d.exec():
            h, fol, fwr, mx = d.values()
            if h:
                self.start_bulk(h, fol, fwr, mx)

    def start_bulk(self, handle, following_max, followers_max, max_posts):
        conf, state, root = self.conf, self.state, self.conf["download_dir"]
        view = self.bulk_view(handle)

        def work(cancel):
            x.bulk_plan(conf, state, handle, following_max, followers_max, max_posts)
            return x.bulk_run(conf, state, root, cancel, self.bulk_progress, on_file=view.on_file)
        if not self.start_job("bulk", f"bulk @{handle}", work, self.bulk_done):
            view.status.setText("Not started, see the banner.")

    def continue_bulk(self):
        b, conf, state, root = self.state["bulk"], self.conf, self.state, self.conf["download_dir"]
        log.info("x: continuing bulk @%s at account %d/%d", b["handle"], b["i"] + 1, len(b["accounts"]))
        view = self.bulk_view(b["handle"])
        self.start_job("bulk", f"bulk @{b['handle']} (continued)",
                       lambda cancel: x.bulk_run(conf, state, root, cancel, self.bulk_progress, on_file=view.on_file), self.bulk_done)

    def bulk_view(self, handle):
        """The live tab of a bulk run: each image shows up the moment its file is saved."""
        view = self.open_tab(f"bulk:{handle.lower()}", f"⬇ @{handle}", lambda: LiveBulk(self, f"x bulk {handle}", f"Bulk @{handle}"))
        root = self.conf["download_dir"]
        view.on_file = lambda p, acct: view.landed(p, [dest(root, p, i, acct) for i in range(len(p["files"]))])
        return view

    def cancel_bulk(self):
        self.stop("bulk")
        self.bulk_note = "bulk: stopping after the current files…"
        self.update_banner()

    def bulk_progress(self, b):
        def show(*_):
            st, a = b["stats"], b["acct"]
            cur = b["accounts"][b["i"]]["handle"] if b["i"] < len(b["accounts"]) else "done"
            self.bulk_note = (f"bulk @{b['handle']}: account {min(b['i'] + 1, len(b['accounts']))}/{len(b['accounts'])} (@{cur}), "
                              f"{st['new'] + a['new']} files new, {st['skip'] + a['skip']} already had, {st['fail'] + a['fail']} failed")
            self.main.statusBar().showMessage(self.bulk_note)
            self.update_banner()
        to_ui(show)

    def bulk_done(self, b, err):
        self.bulk_note = ""
        if b:
            st = b["stats"]
            self.main.statusBar().showMessage(f"X bulk @{b['handle']} {'finished' if b['finished'] else 'stopped'}: "
                                              f"{st['new']} files new, {st['skip']} already had, {st['fail']} failed"
                                              + ("" if b["finished"] else " — Continue picks it up"))
        self.update_banner()

    # --- tabs ---
    def search(self, q=None):
        q = (self.q.text() if q is None else q).strip().lstrip("@")
        if q:
            self.open_artist(handle=q)

    def open_artist(self, handle="", uid=""):
        """Media tab of an account; a bare @handle is looked up first (one request)."""
        self.main.show_section(self)
        key = f"user:{(uid or handle).lower()}"
        log.info("x: open account tab @%s%s", handle, f" (id {uid})" if uid else "")
        known = uid or next((a["id"] for a in self.state["follows"] + self.state["graph"]["ring2"]
                             if a["handle"].lower() == handle.lower()), "")

        def make():
            f = Feed(self, f"@{handle}" if handle else uid)
            f.query = f"@{handle}" if handle else ""
            if known:
                f.start([x.MediaSource(self.conf, known, handle)])
            else:
                f.status.setText(f"Looking up @{handle}…")
                conf, state = self.conf, self.state
                run(lambda: x.user(conf, state, handle), lambda u, e: f.status.setText(str(e)) if e else f.start([x.MediaSource(conf, u["id"], u["handle"])]))
            return f
        self.open_tab(key, f"👤 @{handle}" if handle else f"👤 {uid}", make)

    def open_tag(self, tag, cat=""):
        if tag.startswith("user:"):
            self.open_artist(handle=tag[5:])
        else:  # hashtags: X's own page in the browser (no search from the app)
            webbrowser.open(f"https://x.com/hashtag/{tag}")

    def post_actions(self, v):
        p = v.p
        if p["site"] != "x":
            return []
        out = [(f"👤 @{p['handle']}", lambda: self.open_artist(handle=p["handle"], uid=p["user"]))] if p.get("handle") else []
        if p.get("rt_by"):
            out.append((f"🔁 via @{p['rt_by']}", lambda: self.open_artist(handle=p["rt_by"])))
        if p.get("handle"):
            out.append(("⬇ Bulk this account…", lambda: self.bulk_dialog(p["handle"])))
        return out + [("Open on X", lambda: webbrowser.open(p["page"]))]

    def tag_actions(self, tag, cat):
        return [("Open account", lambda: self.open_artist(handle=tag[5:]))] if tag.startswith("user:") else []

    def refresh_favs(self):
        favs = [p for p in self.conf["favorites"] if p["site"] == "x"]
        self.favs.restart()
        self.favs.add(favs)
        self.tabs.setTabText(self.tabs.indexOf(self.favs.imgtabs), f"Favorites ({len(favs)})")


class Ring(QWidget):
    """Timeline (ring 1) or Discover (ring 2): the cached posts at once, then on first show one batch of
    account checks; "Check next" runs another. Discover also builds the follow graph (hours, resumable)."""

    def __init__(self, section, ring):
        super().__init__()
        self.section, self.ring, self.started = section, ring, False
        self.label = "x timeline" if ring == 1 else "x discover"
        self.grid = Grid(section.open_image)
        self.grid.feed = self  # Viewer: download label; more() is a no-op (no requests from arrow keys)
        self.check_btn = QPushButton(f"⟳ Check next {x.BATCH} accounts", clicked=self.check)
        self.status = QLabel()
        row = QHBoxLayout()
        row.addWidget(self.status, 1)
        if ring == 2:
            self.graph_btn = QPushButton("Build graph", clicked=self.build)
            row.addWidget(self.graph_btn)
        row.addWidget(self.check_btn)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addLayout(row)
        if ring == 2:
            self.who = rich_label()
            self.who.setMaximumHeight(90)
            self.who.linkActivated.connect(lambda h: section.open_artist(handle=h))
            lay.addWidget(self.who)
        lay.addWidget(self.grid, 1)

    def more(self):
        pass

    def accounts(self):
        st = self.section.state
        return st["follows"] if self.ring == 1 else st["graph"]["ring2"]

    def showEvent(self, e):
        super().showEvent(e)
        if not self.started:
            self.started = True
            self.show_cached()
            if self.ring == 1 and not self.section.conf["x"]["seed_handle"]:
                log.info("x: %s: no main @handle set, nothing checked", self.label)
                self.status.setText(x.NO_SEED)
            elif self.accounts() or self.ring == 1:
                log.info("x: %s opened for the first time this session: one automatic check", self.label)
                self.check()
            else:
                log.info("x: %s: no accounts yet (graph not built), nothing checked", self.label)

    def show_cached(self):
        bl = set(self.section.conf["blacklist"])
        self.grid.restart()
        mine = [p for p in self.section.state["posts"] if p.get("ring") == self.ring]
        self.grid.add([p for p in mine if allowed(p, bl)])
        log.info("x: %s: showing %d cached posts (%d hidden by blacklist)", self.label, self.grid.count(), len(mine) - self.grid.count())
        self.refresh()

    def refresh(self):
        accts, st = self.accounts(), self.section.state
        checked = sum(1 for a in accts if a["id"] in st["checked"])
        self.status.setText(f"{self.grid.count()} posts · {checked}/{len(accts)} accounts checked" if accts else
                            ("Build the graph first: it reads who your follows follow (slow, resumable)." if self.ring == 2
                             else "Nothing checked yet."))
        if self.ring == 2:
            g = st["graph"]
            if g["next"]:
                self.graph_btn.setText(f"Continue graph ({g['next']}/{len(st['follows'])})")
            elif g["built"]:
                days = (time.time() - g["built"]) / 86400
                self.graph_btn.setText(f"Rebuild graph (built {days:.0f} d ago)")
            self.graph_btn.setEnabled(x.graph_due(st) or self.section.running("graph"))
            if self.section.running("graph"):
                self.graph_btn.setText("■ Stop graph (progress is kept)")
            self.graph_btn.setToolTip(f"Rebuilt at most every {x.REBUILD_DAYS} days. Reads the first {x.RING2_DEPTH} follows of "
                                      f"each of your follows: about {2 * len(st['follows']) or '…'} requests, hours at the safe pace.")
            self.who.setText(" · ".join(f"<a href='{html.escape(a['handle'])}'>@{html.escape(a['handle'])}</a> ({a['n']})"
                                        for a in g["ring2"][:60]))

    def add_new(self, posts):
        """Newest first at the top (ponytail: arrival order across accounts is only roughly by date)."""
        bl = set(self.section.conf["blacklist"])
        for p in sorted(posts, key=lambda p: int(p["id"])):
            if allowed(p, bl):
                it = QListWidgetItem(QIcon(self.grid.blank), "")
                self.grid.insertItem(0, it)
                self.grid.set_post(it, p)
        self.refresh()

    def check(self):
        """⟳ Check next N accounts, or ■ Stop while that check runs. Posts show up account by account."""
        sec, kind = self.section, f"check{self.ring}"
        if sec.running(kind):
            return sec.stop(kind)
        conf, state, ring = sec.conf, sec.state, self.ring

        def work(cancel):
            accts = x.seed(conf, state) if ring == 1 else state["graph"]["ring2"]
            return x.check_batch(conf, state, accts, ring, got=lambda posts: to_ui(lambda p, e: self.add_new(p), posts), cancel=cancel)

        def done(n, err):
            self.check_btn.setText(f"⟳ Check next {x.BATCH} accounts")
            self.refresh()
            if not err:
                sec.main.statusBar().showMessage(f"X {self.label}: {n} new posts")
        if sec.start_job(kind, f"checking {self.label}", work, done):
            self.check_btn.setText("■ Stop checking")

    def build(self):
        """Build / continue the follow graph, or ■ Stop it (its progress is kept)."""
        sec = self.section
        if sec.running("graph"):
            return sec.stop("graph")
        conf, state = sec.conf, sec.state

        def progress(i, total):
            def show(*_):
                sec.graph_note = f"graph {i}/{total} follows read"
                sec.update_banner()
            to_ui(show)

        def done(ring2, err):
            sec.graph_note = ""
            self.refresh()
            if err:
                return
            sec.main.statusBar().showMessage(f"X: graph built, {len(ring2)} accounts to discover" if ring2 is not None
                                             else "X: graph stopped; Continue graph picks it up")
        if sec.start_job("graph", "building the follow graph", lambda cancel: x.build_ring2(conf, state, progress, cancel), done):
            self.graph_btn.setText("■ Stop graph (progress is kept)")
            self.graph_btn.setEnabled(True)


class BulkDialog(QDialog):
    """Bulk one @handle: its media tab, optionally the accounts it follows and/or its followers (each capped)."""

    def __init__(self, section, handle=""):
        super().__init__(section)
        self.setWindowTitle("X bulk download")
        self.section = section
        form = QFormLayout(self)
        self.handle = QLineEdit(handle, placeholderText="@handle")
        self.fol = QCheckBox("include the accounts it follows, at most")
        self.fol_n = QSpinBox(minimum=1, maximum=100_000, value=200)
        self.fwr = QCheckBox("include its followers, at most")
        self.fwr_n = QSpinBox(minimum=1, maximum=100_000, value=200)
        self.max = QSpinBox(minimum=0, maximum=1_000_000, singleStep=100, specialValueText="all", value=0)
        self.where = QLabel(wordWrap=True)
        form.addRow("Account", self.handle)
        for cb, n in ((self.fol, self.fol_n), (self.fwr, self.fwr_n)):
            row = QHBoxLayout()
            row.addWidget(cb)
            row.addWidget(n)
            row.addStretch()
            form.addRow(row)
        form.addRow("Max posts per account", self.max)
        form.addRow(self.where)
        form.addRow(QLabel(f"Pace: {x.PACES[x.pace(section.conf)]} (⚙). Own media only (the media tab). Resumable: "
                           "a quit, lock or Cancel can be continued later; files already on disk are skipped.", wordWrap=True))
        form.addRow(QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel, accepted=self.accept, rejected=self.reject))
        self.handle.textChanged.connect(self.preview)
        self.preview()

    def preview(self):
        h = self.handle.text().strip().lstrip("@") or "handle"
        sample = {"site": "x", "id": "1234567890", "files": ["https://pbs.twimg.com/media/A.jpg?name=orig"]}
        self.where.setText(f"Saved as {dest(self.section.conf['download_dir'], sample, 0, h)} (one folder per account)")

    def values(self):
        return (self.handle.text().strip().lstrip("@"), self.fol_n.value() if self.fol.isChecked() else 0,
                self.fwr_n.value() if self.fwr.isChecked() else 0, self.max.value())

