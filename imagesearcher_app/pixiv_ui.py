"""The Pixiv section: search with Pixiv's own filters, rankings, trending tags, recommended, the follow feed,
bookmarks, user / series / related tabs, and bookmark / follow writes to the account."""
import html
import webbrowser

from PySide6.QtCore import QDate, Qt, QTimer
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDateEdit, QHBoxLayout, QLabel, QPushButton, QSpinBox, QWidget,
)

from . import pixiv
from .core import log, run, save_conf
from .widgets import Feed, Grid, Section, rich_label


class Lazy(Feed):
    """A fixed-tab feed that only starts when first shown (no Pixiv requests for tabs never opened)."""
    started = False

    def showEvent(self, e):
        super().showEvent(e)  # Feed.showEvent: resume filling
        if not self.started:
            self.started = True
            self.reload()

    def reload(self):
        raise NotImplementedError


class PixivSection(Section):
    SAVED = "px_saved"

    def __init__(self, main):
        super().__init__(main, "Pixiv tags or words…   Enter = search   (Japanese tags find the most; -word excludes)")
        self.filters_btn = QPushButton("Filters ▾", checkable=True)
        self.bar.addWidget(self.q, 1)
        for w in (QPushButton("Search", clicked=lambda: self.search()), self.filters_btn, self.saved, self.star,
                  QPushButton("⬇ Bulk", toolTip="Download every result of the current tab", clicked=lambda: main.bulk(self.current_feed()))):
            self.bar.addWidget(w)
        self.filters = QWidget(visible=False)
        self.filters_btn.toggled.connect(self.filters.setVisible)
        self.target, self.sort, self.kind, self.r18 = QComboBox(), QComboBox(), QComboBox(), QComboBox()
        self.target.addItems(pixiv.TARGETS)
        self.sort.addItems(pixiv.SORTS)
        self.kind.addItems(["All", "Illust", "Manga"])
        self.r18.addItems(["Show", "Hide", "Only"])
        self.ai = QCheckBox("AI works", checked=True)
        self.min_bm = QSpinBox(maximum=1_000_000, singleStep=100, specialValueText="any", toolTip="minimum bookmarks")
        self.dated = QCheckBox("from")
        self.start_d, self.end_d = QDateEdit(QDate.currentDate().addMonths(-1), calendarPopup=True), QDateEdit(QDate.currentDate(), calendarPopup=True)
        fl = QHBoxLayout(self.filters)
        fl.setContentsMargins(0, 0, 0, 0)
        for w in (QLabel("match"), self.target, QLabel("sort"), self.sort, QLabel("type"), self.kind, QLabel("R-18"), self.r18,
                  self.ai, QLabel("min ♥"), self.min_bm, self.dated, self.start_d, QLabel("to"), self.end_d):
            fl.addWidget(w)
        fl.addStretch()
        self.layout().insertWidget(1, self.filters)

        self.add_fixed(Rankings(self), "Rankings")
        self.add_fixed(Trending(self), "Trending")
        self.add_fixed(ListFeed(self, "Recommended", lambda: pixiv.recommended(self.conf)), "Recommended")
        self.following = Following(self)
        self.add_fixed(self.following, "Following")
        self.add_fixed(Bookmarks(self), "Bookmarks")
        self.favs = Grid(self.open_image)
        self.add_fixed(self.favs, "Favorites")
        self.refresh_favs()

    def ready(self, feed=None):
        if not self.conf["pixiv"]["refresh_token"]:
            self.main.statusBar().showMessage(pixiv.NO_TOKEN)
            if feed:
                feed.status.setText(pixiv.NO_TOKEN)
            return False
        return True

    # --- opening tabs ---
    def options(self):
        return {"target": self.target.currentText(), "sort": self.sort.currentText(), "kind": self.kind.currentText(),
                "r18": self.r18.currentText(), "ai": self.ai.isChecked(), "min_bm": self.min_bm.value(),
                "start": self.start_d.date().toString("yyyy-MM-dd") if self.dated.isChecked() else "",
                "end": self.end_d.date().toString("yyyy-MM-dd") if self.dated.isChecked() else ""}

    def search(self, q=None):
        q = (self.q.text() if q is None else q).strip()
        if not q:
            return
        self.q.setText(q)
        self.main.show_section(self)
        o = self.options()
        changed = {k: v for k, v in o.items() if v != {"target": "Partial tags", "sort": "Newest", "kind": "All", "r18": "Show", "ai": True,
                                                         "min_bm": 0, "start": "", "end": ""}[k]}
        tag = {"target": lambda v: v.lower(), "sort": lambda v: v.lower(), "kind": lambda v: v.lower(), "r18": lambda v: f"R-18 {v.lower()}",
               "ai": lambda v: "no AI", "min_bm": lambda v: f"♥{v}+", "start": lambda v: f"from {v}", "end": lambda v: f"to {v}"}
        title = q + (f"  [{', '.join(tag[k](v) for k, v in changed.items())}]" if changed else "")
        log.info("pixiv search '%s' %s", q, changed)
        src, note = pixiv.search(self.conf, q, **o)
        feed = self.open_feed(f"search:{q}|{sorted(changed.items())}", title, [src], note)
        feed.query = q

    def open_tag(self, tag, cat=""):
        self.search(tag)

    def open_user(self, uid):
        self.main.show_section(self)
        self.open_tab(f"user:{uid}", f"👤 {uid}", lambda: UserView(self, uid))

    def open_related(self, iid):
        self.main.show_section(self)
        self.open_feed(f"related:{iid}", f"like {iid}", [pixiv.related(self.conf, iid)])

    def open_series(self, s):
        self.open_feed(f"series:{s['id']}", f"▤ {s['title']}", [pixiv.series(self.conf, s["id"])])

    # --- search box / image tabs ---
    def complete(self, word):
        return pixiv.complete(self.conf, word) if self.conf["pixiv"]["refresh_token"] else []

    def post_actions(self, v):
        p = v.p
        if p["site"] != "pixiv":
            return []
        out = [("★ Unbookmark" if p.get("bookmarked") else "☆ Bookmark on Pixiv", lambda: self.bookmark(v))]
        if p.get("user"):
            out.append((f"👤 {p.get('user_name') or p['user']}", lambda: self.open_user(p["user"])))
        if p.get("series"):
            out.append((f"▤ Series: {p['series']['title']}", lambda: self.open_series(p["series"])))
        out += [("More like this", lambda: self.open_related(p["id"])), ("Find on boorus", lambda: self.main.boorus.find_pixiv(p["id"]))]
        return out

    def tag_actions(self, tag, cat):
        q = self.q.text().strip()
        return [("Search", lambda: self.search(tag)), ("Add to query", lambda: self.search(f"{q} {tag}".strip())),
                ("Exclude from query", lambda: self.search(f"{q} -{tag}".strip())),
                ("Open on boorus", lambda: (self.main.show_section(self.main.boorus), self.main.boorus.open_tag(tag)))]

    def bookmark(self, v):
        p, conf = v.p, self.conf
        on = not p.get("bookmarked")

        def done(r, e):
            if e:
                return self.main.statusBar().showMessage(f"bookmark failed: {e}")
            log.info("pixiv %s #%s", "bookmark" if on else "unbookmark", p["id"])
            self.main.statusBar().showMessage(f"{'Bookmarked' if on else 'Removed bookmark of'} #{p['id']} on Pixiv")
            v.set_post({**p, "bookmarked": on, "bookmarks": p.get("bookmarks", 0) + (1 if on else -1)})
        run(lambda: pixiv.bookmark(conf, p["id"], on), done)

    def refresh_favs(self):
        favs = [p for p in self.conf["favorites"] if p["site"] == "pixiv"]
        self.favs.restart()
        self.favs.add(favs)
        self.tabs.setTabText(self.tabs.indexOf(self.favs.imgtabs), f"Favorites ({len(favs)})")


class ListFeed(Lazy):
    def __init__(self, section, label, make):
        super().__init__(section, label)
        self.make = make

    def reload(self):
        if self.section.ready(self):
            self.start([self.make()])


class Rankings(Lazy):
    def __init__(self, section):
        super().__init__(section, "Rankings")
        self.mode = QComboBox()
        self.mode.addItems(pixiv.RANKINGS)
        self.date = QDateEdit(QDate.currentDate().addDays(-1), calendarPopup=True, maximumDate=QDate.currentDate().addDays(-1))
        self.latest = QCheckBox("latest", checked=True)
        row = QHBoxLayout()
        for w in (QLabel("ranking"), self.mode, self.latest, self.date):
            row.addWidget(w)
        row.addStretch()
        self.lay.insertLayout(0, row)
        for sig in (self.mode.currentIndexChanged, self.date.dateChanged, self.latest.toggled):
            sig.connect(lambda *_: self.started and self.reload())

    def reload(self):
        self.date.setEnabled(not self.latest.isChecked())
        if self.section.ready(self):
            date = "" if self.latest.isChecked() else self.date.date().toString("yyyy-MM-dd")
            self.start([pixiv.ranking(self.section.conf, pixiv.RANKINGS[self.mode.currentText()], date)], self.mode.currentText())


class Following(Lazy):
    """New works from the artists you follow on Pixiv; the tab counts the ones newer than the last 'Mark all seen'."""

    def __init__(self, section):
        super().__init__(section, "Following")
        self.restrict = QComboBox()
        self.restrict.addItems(["public", "private"])
        self.restrict.currentIndexChanged.connect(lambda: self.reload())
        row = QHBoxLayout()
        for w in (QLabel("follows:"), self.restrict, QPushButton("⟳ Refresh", clicked=self.reload),
                  QPushButton("✓ Mark all seen", clicked=self.mark_seen)):
            row.addWidget(w)
        row.addStretch()
        self.lay.insertLayout(0, row)
        self.newest = 0
        QTimer(self, interval=15 * 60 * 1000, timeout=lambda: self.started and not self.isVisible() and self.reload()).start()

    def reload(self):
        if self.section.ready(self):
            self.newest = 0
            self.start([pixiv.following(self.section.conf, self.restrict.currentText())])

    def loaded(self, lists):
        seen = self.section.conf["pixiv"]["follow_seen"]
        ids = [int(p["id"]) for posts in lists.values() for p in posts]
        self.newest = max([self.newest, *ids])
        unread = sum(1 for i in range(self.grid.count()) if int(self.grid.item(i).data(Qt.UserRole)["id"]) > seen)
        tabs = self.section.tabs
        tabs.setTabText(tabs.indexOf(self.imgtabs), f"Following ({unread})" if unread else "Following")

    def mark_seen(self):
        self.section.conf["pixiv"]["follow_seen"] = max(self.newest, self.section.conf["pixiv"]["follow_seen"])
        save_conf(self.section.conf)
        self.loaded({})


class Bookmarks(Lazy):
    def __init__(self, section):
        super().__init__(section, "Bookmarks")
        self.restrict = QComboBox()
        self.restrict.addItems(["public", "private"])
        self.restrict.currentIndexChanged.connect(lambda: self.reload())
        row = QHBoxLayout()
        for w in (QLabel("your bookmarks:"), self.restrict, QPushButton("⟳ Refresh", clicked=self.reload)):
            row.addWidget(w)
        row.addStretch()
        self.lay.insertLayout(0, row)

    def reload(self):
        if self.section.ready(self):
            self.start([pixiv.bookmarks(self.section.conf, "me", self.restrict.currentText())])


class Trending(Lazy):
    """Pixiv's trending tags as tiles (each with a sample work); opening a tile searches the tag."""

    def __init__(self, section):
        super().__init__(section, "Trending")
        self.grid.on_open = lambda grid, row, bg=False: section.search(grid.item(row).data(Qt.UserRole)["trend"])
        row = QHBoxLayout()
        row.addWidget(QLabel("Trending tags on Pixiv — open one to search it"))
        row.addStretch()
        row.addWidget(QPushButton("⟳ Refresh", clicked=self.reload))
        self.lay.insertLayout(0, row)

    def reload(self):
        if not self.section.ready(self):
            return
        self.grid.restart()
        self.status.setText("Loading…")
        conf = self.section.conf

        def done(tags, err):
            if err:
                return self.status.setText(str(err))
            for t in tags:
                it = self.grid.add([{**t["post"], "trend": t["tag"]}])[0]
                self.grid.set_post(it, it.data(Qt.UserRole), f"#{t['tag']}" + (f"\n{t['translated']}" if t["translated"] else ""))
            self.status.setText(f"{len(tags)} trending tags")
        run(lambda: pixiv.trending(conf), done)


class UserView(Feed):
    """A Pixiv artist: profile header, follow / unfollow, and their works, manga or bookmarks."""

    def __init__(self, section, uid):
        super().__init__(section, f"user {uid}")
        self.uid, self.followed = uid, False
        self.head = rich_label()
        self.head.linkActivated.connect(webbrowser.open)
        self.kind = QComboBox()
        self.kind.addItems(["Works", "Manga", "Bookmarks"])
        self.kind.currentIndexChanged.connect(lambda: self.reload())
        self.follow = QPushButton("…", enabled=False, clicked=self.toggle_follow)
        row = QHBoxLayout()
        for w in (self.head, self.kind, self.follow):
            row.addWidget(w, int(w is self.head))
        self.lay.insertLayout(0, row)
        self.reload()
        conf = section.conf
        if section.ready():
            run(lambda: pixiv.user_detail(conf, uid), self.got_user)

    def reload(self):
        conf, k = self.section.conf, self.kind.currentText()
        src = pixiv.bookmarks(conf, self.uid) if k == "Bookmarks" else pixiv.user_works(conf, self.uid, "manga" if k == "Manga" else "illust")
        self.start([src])

    def got_user(self, d, err):
        if err:
            return self.head.setText(f"<i>{html.escape(str(err))}</i>")
        u, prof = d["user"], d.get("profile") or {}
        self.label, self.followed = u["name"], bool(u.get("is_followed"))
        tabs = self.section.tabs
        tabs.setTabText(tabs.indexOf(self.imgtabs), f"👤 {u['name']}"[:32])
        self.head.setText(f"<h3>{html.escape(u['name'])} <small>@{html.escape(u.get('account', ''))}</small></h3>"
                          f"{prof.get('total_illusts', 0)} illusts · {prof.get('total_manga', 0)} manga · "
                          f"<a href='https://www.pixiv.net/users/{self.uid}'>profile</a>")
        self.follow.setEnabled(True)
        self.follow.setText("✓ Following" if self.followed else "+ Follow")

    def toggle_follow(self):
        on, conf = not self.followed, self.section.conf

        def done(r, e):
            if e:
                return self.section.main.statusBar().showMessage(f"follow failed: {e}")
            log.info("pixiv %s user %s", "follow" if on else "unfollow", self.uid)
            self.followed = on
            self.follow.setText("✓ Following" if on else "+ Follow")
        run(lambda: pixiv.follow(conf, self.uid, on), done)
