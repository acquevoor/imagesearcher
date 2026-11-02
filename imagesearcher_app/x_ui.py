"""The X section (opt-in): Timeline = media from the accounts your main follows, Discover = media from the accounts
many of them follow, artist tabs, Favorites. Read-only; every request waits its turn in x.budget, and a banner
shows what X traffic is doing (or why it's paused)."""
import html
import time
import webbrowser

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QHBoxLayout, QLabel, QListWidgetItem, QPushButton, QVBoxLayout, QWidget

from . import x
from .core import allowed, log, run, to_ui
from .widgets import Feed, Grid, Section, rich_label


class XSection(Section):
    SAVED = "x_saved"

    def __init__(self, main):
        super().__init__(main, "@handle   Enter = open that account's media tab")
        self.state, self.job, self.graph_note = x.load_state(), "", ""
        self.bar.addWidget(self.q, 1)
        for w in (QPushButton("Open", clicked=lambda: self.search()), self.saved, self.star):
            self.bar.addWidget(w)
        self.banner = QLabel(wordWrap=True)
        self.resume_btn = QPushButton("Resume X", clicked=self.resume)
        row = QHBoxLayout()
        row.addWidget(self.banner, 1)
        row.addWidget(self.resume_btn)
        self.layout().insertLayout(1, row)
        self.timeline, self.discover = Ring(self, 1), Ring(self, 2)
        self.add_fixed(self.timeline, "Timeline")
        self.add_fixed(self.discover, "Discover")
        self.favs = Grid(self.open_image)
        self.add_fixed(self.favs, "Favorites")
        self.refresh_favs()
        QTimer(self, interval=5000, timeout=self.update_banner).start()
        self.update_banner()

    # --- banner / jobs: one X job at a time (all traffic is serialized by the budget anyway) ---
    def update_banner(self):
        why = x.paused(self.conf)
        if not x.ready(self.conf):
            text = x.NOT_SET
        elif why:
            text = why
        else:
            text = f"X: {x.budget.left()} of {x.HOURLY} requests left this hour" + (f" · {self.job}" if self.job else " · idle")
        if self.graph_note:
            text += f" · {self.graph_note}"
        self.banner.setText(text)
        self.resume_btn.setVisible(bool(why))

    def start_job(self, name, fn, done):
        """Run fn() on the single X worker; refuse while another X job runs."""
        if self.job:
            self.main.statusBar().showMessage(f"X is busy: {self.job}")
            return False
        try:
            x.check(self.conf)
        except RuntimeError as e:
            self.main.statusBar().showMessage(str(e))
            self.update_banner()
            return False
        self.job = name
        self.update_banner()

        def finished(res, err):
            self.job = ""
            if err:
                log.warning("x: %s: %s", name, err)
                self.main.statusBar().showMessage(str(err))
            done(res, err)
            self.update_banner()
        run(fn, finished, "x")
        return True

    def resume(self):
        x.resume(self.conf)
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
            if self.accounts() or self.ring == 1:
                self.check()

    def show_cached(self):
        bl = set(self.section.conf["blacklist"])
        self.grid.restart()
        self.grid.add([p for p in self.section.state["posts"] if p.get("ring") == self.ring and allowed(p, bl)])
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
            self.graph_btn.setEnabled(x.graph_due(st))
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
        sec = self.section
        conf, state, ring = sec.conf, sec.state, self.ring

        def work():
            accts = x.seed(conf, state) if ring == 1 else state["graph"]["ring2"]
            return x.check_batch(conf, state, accts, ring, got=lambda posts: to_ui(lambda p, e: self.add_new(p), posts))

        def done(n, err):
            self.check_btn.setEnabled(True)
            self.refresh()
            if not err:
                sec.main.statusBar().showMessage(f"X {self.label}: {n} new posts")
        if sec.start_job(f"checking {self.label}", work, done):
            self.check_btn.setEnabled(False)

    def build(self):
        sec = self.section
        conf, state = sec.conf, sec.state

        def progress(i, total):
            def show(*_):
                sec.graph_note = f"graph {i}/{total} follows read"
                sec.update_banner()
            to_ui(show)

        def done(ring2, err):
            sec.graph_note = ""
            self.graph_btn.setEnabled(True)
            self.refresh()
            if not err:
                sec.main.statusBar().showMessage(f"X: graph built, {len(ring2)} accounts to discover")
        if sec.start_job("building the follow graph", lambda: x.build_ring2(conf, state, progress), done):
            self.graph_btn.setEnabled(False)
