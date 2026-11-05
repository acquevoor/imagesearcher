"""The Imageboard section (opt-in): one tab per board (live 4chan catalog or the archive's index), thread tabs
(every image of a thread), archive search, a thread watcher, thread bulk downloads, and the Cloudflare pass
for archived.moe."""
import html
import re
import threading
import time
import webbrowser
from concurrent.futures import ThreadPoolExecutor, as_completed

from PySide6.QtCore import QDate, Qt, QUrl
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDateEdit, QDialog, QHBoxLayout, QInputDialog, QLabel, QLineEdit, QPushButton, QVBoxLayout, QWidget,
)

from . import imageboard as ib
from .core import allowed, log, run, save_conf, to_ui
from .keys import web
from .widgets import Feed, Grid, LiveBulk, Section, rich_label


class IBSection(Section):
    SAVED = "ib_saved"

    def __init__(self, main):
        super().__init__(main, "search the archives…   Enter = search   (Filters ▾: boards, subject, filename, tripcode)")
        self.filters_btn = QPushButton("Filters ▾", checkable=True)
        self.bar.addWidget(self.q, 1)
        for w in (QPushButton("Search", clicked=lambda: self.search()), self.filters_btn, self.saved, self.star,
                  QPushButton("☁ Pass Cloudflare…", toolTip="For an archive that answers with Cloudflare's browser check "
                              "(archived.moe; archiveofsins' search)", clicked=lambda: pass_cloudflare(self, self.conf))):
            self.bar.addWidget(w)
        self.filters = QWidget(visible=False)
        self.filters_btn.toggled.connect(self.filters.setVisible)
        self.board_cb = {b: QCheckBox(f"/{b}/", checked=True) for b, _ in ib.boards(self.conf)}
        self.subject, self.filename, self.trip = (QLineEdit(placeholderText=t) for t in ("subject", "filename", "tripcode"))
        self.images_only = QCheckBox("images only", checked=True)
        self.dated = QCheckBox("from")
        self.start_d, self.end_d = QDateEdit(QDate.currentDate().addMonths(-1), calendarPopup=True), QDateEdit(QDate.currentDate(), calendarPopup=True)
        fl = QHBoxLayout(self.filters)
        fl.setContentsMargins(0, 0, 0, 0)
        for w in (*self.board_cb.values(), self.subject, self.filename, self.trip, self.images_only, self.dated, self.start_d,
                  QLabel("to"), self.end_d):
            fl.addWidget(w)
        self.layout().insertWidget(1, self.filters)

        self.board_tabs = {}
        for b, host in ib.boards(self.conf):
            t = BoardTab(self, b)
            self.board_tabs[b] = t
            self.add_fixed(t, f"/{b}/")
        self.watched = WatchTab(self)
        self.add_fixed(self.watched, "Watched")
        self.favs = Grid(self.open_image)
        self.add_fixed(self.favs, "Favorites")
        self.refresh_favs()

    # --- search ---
    def query(self):
        q = {"text": self.q.text().strip(), "subject": self.subject.text().strip(), "filename": self.filename.text().strip(),
             "tripcode": self.trip.text().strip(), "images_only": self.images_only.isChecked()}
        if self.dated.isChecked():
            q.update(start=self.start_d.date().toString("yyyy-MM-dd"), end=self.end_d.date().toString("yyyy-MM-dd"))
        return q

    def search(self, q=None, **extra):
        if q is not None:
            self.q.setText(q)
        query = {**self.query(), **extra}
        boards = [b for b, cb in self.board_cb.items() if cb.isChecked()]
        if not any(query[k] for k in ib.SEARCH_FIELDS) or not boards:
            return self.main.statusBar().showMessage("Type something to search for and tick at least one board")
        self.main.show_section(self)
        srcs = ib.search_sources(self.conf, boards, **query)
        title = " ".join(f"{k}:{v}" if k != "text" else v for k, v in query.items() if k in ib.SEARCH_FIELDS and v) + f"  {'/'.join(boards)}"
        log.info("ib search %s on %s", {k: v for k, v in query.items() if v}, boards)
        feed = self.open_feed(f"search:{sorted(query.items())}|{boards}", title, srcs)
        feed.query = query["text"]

    # --- threads ---
    def open_thread(self, board, num, title=""):
        self.main.show_section(self)
        return self.open_tab(f"thread:{board}/{num}", f"🧵 /{board}/ {title or num}"[:40], lambda: ThreadView(self, board, num))

    def open_thread_of(self, p):
        return self.open_thread(p["board"], p["thread"], p.get("subject") or "")

    def open_tag(self, tag, cat=""):
        if tag.startswith("thread:"):  # thread:<board>/<no>
            return self.open_thread(*tag[7:].split("/", 1))
        if tag.startswith("/") and tag.endswith("/") and tag.strip("/") in self.board_tabs:
            return self.tabs.setCurrentWidget(self.board_tabs[tag.strip("/")].imgtabs)
        if tag.startswith("file:"):
            return self.search(q="", filename=tag[5:])
        self.search(tag)

    def post_actions(self, v):
        p = v.p
        if p["site"] != "ib":
            return []
        key = ib.watch_key(p["board"], p["thread"])
        watching = key in self.conf["ib"]["watch"]
        return [(f"🧵 Open thread /{p['board']}/{p['thread']}", lambda: self.open_thread_of(p)),
                ("★ Unwatch thread" if watching else "☆ Watch thread",
                 lambda: (ib.watch(self.conf, p["board"], p["thread"], p.get("subject") or p["title"], not watching), v.load())),
                ("Open post on " + ("4chan" if p["source"] == "4chan" else p["source"]), lambda: webbrowser.open(p["page"]))]

    def tag_actions(self, tag, cat):
        if tag.startswith("file:"):
            return [("Search this filename", lambda: self.search(q="", filename=tag[5:]))]
        return []

    def refresh_favs(self):
        favs = [p for p in self.conf["favorites"] if p["site"] == "ib"]
        self.favs.restart()
        self.favs.add(favs)
        self.tabs.setTabText(self.tabs.indexOf(self.favs.imgtabs), f"Favorites ({len(favs)})")

    # --- thread bulk ---
    def bulk_thread(self, board, num, posts, info):
        folder = ib.thread_folder(info, num)
        cancel = threading.Event()
        title = f"/{board}/{num}"
        view = self.open_tab(f"bulk:{board}/{num}", f"⬇ {title}", lambda: LiveBulk(self, folder, f"Bulk {title}", cancel))
        conf, root = self.conf, self.conf["download_dir"]
        log.info("ib: bulk /%s/%s: %d images into imageboard/%s/%s", board, num, len(posts), board, folder)

        def work():
            st = {"new": 0, "skip": 0, "fail": 0}
            with ThreadPoolExecutor(3) as ex:
                futs = {ex.submit(ib.save, conf, p, root, folder, cancel): p for p in posts}
                for f in as_completed(futs):
                    p = futs[f]
                    try:
                        n, s = f.result()
                        st["new"] += n
                        st["skip"] += s
                        if n + s:
                            view.landed(p, [ib.target(conf, p, root, folder)])
                    except Exception as e:
                        st["fail"] += 1
                        log.warning("ib: bulk %s: %s failed: %s", title, p["filename"], e)
            return st

        def done(st, err):
            msg = f"failed: {err}" if err else (f"{'cancelled' if cancel.is_set() else 'done'}: {st['new']} new, "
                                                f"{st['skip']} already had, {st['fail']} failed")
            log.info("ib: bulk %s %s", title, msg)
            view.finished(msg)
            self.main.statusBar().showMessage(f"Bulk {title} {msg}")
        run(work, done)


class BoardTab(Feed):
    """A board: live 4chan catalog (every thread now) or the archive's index (paged, newest bumped first).
    Tiles are thread OPs; opening one opens the thread."""
    started = False

    def __init__(self, section, board):
        super().__init__(section, f"/{board}/")
        self.board = board
        self.mode = QComboBox()
        self.mode.addItems(["Live (4chan)", f"Archive ({ib.host_of(section.conf, board) or 'none'})"])
        self.mode.currentIndexChanged.connect(lambda: self.reload())
        self.grid.on_open = lambda grid, row, bg=False: section.open_thread_of(grid.item(row).data(Qt.UserRole))
        self.mark = lambda it, p: self.grid.set_post(it, p, ib.tile_text(p))
        row = QHBoxLayout()
        for w in (QLabel(f"/{board}/ threads:"), self.mode, QPushButton("⟳ Refresh", clicked=self.reload)):
            row.addWidget(w)
        row.addStretch()
        self.lay.insertLayout(0, row)

    def showEvent(self, e):
        super().showEvent(e)
        if not self.started:
            self.started = True
            self.reload()

    def reload(self):
        conf = self.section.conf
        live = self.mode.currentIndex() == 0
        log.info("ib: /%s/ %s", self.board, "live catalog" if live else "archive index")
        self.start([ib.CatalogSource(conf, self.board) if live else ib.IndexSource(conf, self.board)])


class ThreadView(Feed):
    """Every image of one thread (live, or the archive copy once it died), with watch / bulk / open buttons."""

    def __init__(self, section, board, num):
        super().__init__(section, f"/{board}/{num}")
        self.board, self.num, self.src = board, str(num), ib.ThreadSource(section.conf, board, num)
        self.head = rich_label()
        self.watch_btn = QPushButton(clicked=self.toggle_watch)
        self.bulk_btn = QPushButton("⬇ Bulk thread", enabled=False, clicked=self.bulk)
        self.open_btn = QPushButton("Open on site", clicked=self.open_site)
        row = QHBoxLayout()
        row.addWidget(self.head, 1)
        for w in (self.watch_btn, self.bulk_btn, self.open_btn, QPushButton("⟳ Refresh", clicked=self.reload)):
            row.addWidget(w)
        self.lay.insertLayout(0, row)
        self.update_watch()
        self.reload()

    def reload(self):
        self.head.setText(f"<b>/{self.board}/{self.num}</b> loading…")
        self.start([self.src])

    def loaded(self, lists):
        i = self.src.info
        if not i:
            return
        self.bulk_btn.setEnabled(self.grid.count() > 0)
        state = "alive on 4chan" if i["alive"] else f"archived ({i['source']})"
        self.head.setText(f"<b>/{self.board}/{self.num}</b> {html.escape(i['subject'])} — {state}, {i['replies']} replies, "
                          f"{self.grid.count()} images<br><small>{html.escape(i['comment'][:300])}</small>")
        tabs = self.section.tabs
        if self.imgtabs and tabs.indexOf(self.imgtabs) >= 0:
            tabs.setTabText(tabs.indexOf(self.imgtabs), f"🧵 /{self.board}/ {i['subject'] or self.num}"[:40])

    def posts(self):
        return [self.grid.item(r).data(Qt.UserRole) for r in range(self.grid.count())]

    def update_watch(self):
        on = ib.watch_key(self.board, self.num) in self.section.conf["ib"]["watch"]
        self.watch_btn.setText("★ Watching" if on else "☆ Watch")

    def toggle_watch(self):
        on = ib.watch_key(self.board, self.num) not in self.section.conf["ib"]["watch"]
        ib.watch(self.section.conf, self.board, self.num, (self.src.info or {}).get("subject") or "", on)
        if on:  # what's here now counts as seen
            ib.mark_seen(self.section.conf, ib.watch_key(self.board, self.num), self.posts())
        self.update_watch()
        self.section.watched.refresh_title()

    def bulk(self):
        self.section.bulk_thread(self.board, self.num, self.posts(), self.src.info or {})

    def open_site(self):
        i = self.src.info or {}
        host = ib.host_of(self.section.conf, self.board)
        url = (f"{ib.LIVE_BOARDS}/{self.board}/thread/{self.num}" if i.get("alive") or not host
               else f"https://{host}/{self.board}/thread/{self.num}/")
        webbrowser.open(url)


class WatchTab(QWidget):
    """New images in the watched threads since they were marked seen; refreshed when shown and on ⟳."""

    def __init__(self, section):
        super().__init__()
        self.section, self.label, self.new, self.started = section, "watched", {}, False
        self.grid = Grid(section.open_image)
        self.grid.feed = self
        self.status = QLabel()
        row = QHBoxLayout()
        row.addWidget(self.status, 1)
        for w in (QPushButton("⟳ Refresh", clicked=self.refresh), QPushButton("✓ Mark all seen", clicked=self.mark_seen)):
            row.addWidget(w)
        self.who = rich_label()
        self.who.setMaximumHeight(80)
        self.who.linkActivated.connect(lambda key: section.open_thread(*key.split("/", 1)))
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addLayout(row)
        lay.addWidget(self.who)
        lay.addWidget(self.grid, 1)

    def more(self):
        pass

    def showEvent(self, e):
        super().showEvent(e)
        if not self.started:
            self.started = True
            self.refresh()

    def refresh(self):
        conf = self.section.conf
        if not conf["ib"]["watch"]:
            self.status.setText("No watched threads. Open a thread and press ☆ Watch.")
            return self.list_threads()
        self.status.setText(f"Checking {len(conf['ib']['watch'])} threads…")
        run(lambda: ib.check_watched(conf), self.got)

    def got(self, res, err):
        if err:
            return self.status.setText(str(err))
        self.new = dict(res)
        bl = set(self.section.conf["blacklist"])
        self.grid.restart()
        self.grid.add([p for _, posts in res for p in posts if allowed(p, bl)])
        self.status.setText(f"{self.grid.count()} new images in {sum(1 for _, ps in res if ps)} of {len(res)} watched threads")
        self.list_threads()
        self.refresh_title()

    def list_threads(self):
        w = self.section.conf["ib"]["watch"]
        self.who.setText(" · ".join(f"<a href='{k}'>/{k.split('/')[0]}/ {html.escape(v['title'][:30] or k.split('/')[1])}</a>"
                                    f" ({v.get('new', 0)} new{'' if v.get('alive', True) else ', archived'})" for k, v in w.items()))

    def refresh_title(self):
        n = sum(v.get("new", 0) for v in self.section.conf["ib"]["watch"].values())
        tabs = self.section.tabs
        tabs.setTabText(tabs.indexOf(self.imgtabs), f"Watched ({n})" if n else "Watched")

    def mark_seen(self):
        for key, posts in self.new.items():
            if key in self.section.conf["ib"]["watch"]:
                ib.mark_seen(self.section.conf, key, posts)
        log.info("ib: watched threads marked seen")
        self.new = {}
        self.grid.restart()
        self.status.setText("All seen.")
        self.list_threads()
        self.refresh_title()


def pass_cloudflare(parent, conf, host=None):
    """Pick an archive (unless given) and pass its Cloudflare check in a built-in browser window."""
    if host is None:
        hosts = ib.cf_hosts(conf)
        host, ok = QInputDialog.getItem(parent, "Pass Cloudflare", "Archive:", hosts, 0, False)
        if not ok:
            return
    CloudflarePass(parent, conf, host).exec()


class CloudflarePass(QDialog):
    """archived.moe sits behind Cloudflare's browser check: pass it once in a built-in browser window, keep the
    cf_clearance cookie and that window's exact user agent (Cloudflare ties the cookie to both). Like JDownloader."""

    def __init__(self, parent, conf, host="archived.moe"):
        super().__init__(parent)
        self.conf, self.host = conf, host
        self.setWindowTitle(f"Pass Cloudflare: {host}")
        self.resize(900, 700)
        lay = QVBoxLayout(self)
        self.status = QLabel(f"Loading {host}… If a check box appears, tick it. The window closes by itself once {host} lets you in.",
                             wordWrap=True)
        lay.addWidget(self.status)
        parts = web()
        if not parts:
            self.status.setText("This PySide6 has no built-in browser (QtWebEngine), so archived.moe can't be used here.")
            return
        Page, Profile, View = parts
        self.profile = Profile(self)  # off the record: nothing reaches the disk
        self.profile.setHttpUserAgent(re.sub(r" QtWebEngine/\S+", "", self.profile.httpUserAgent()))
        self.profile.cookieStore().cookieAdded.connect(self.cookie)
        self.view = View(self)
        self.view.setPage(Page(self.profile, self.view))
        lay.addWidget(self.view, 1)
        log.info("ib: Cloudflare pass for %s started", host)
        self.view.load(QUrl(f"https://{host}/"))

    def cookie(self, c):
        text = lambda v: v.data().decode() if hasattr(v, "data") else v.decode() if isinstance(v, bytes) else str(v)  # noqa: E731
        if text(c.name()) == "cf_clearance" and self.host in text(c.domain()):
            self.conf["ib"]["cf"][self.host] = {"cookie": text(c.value()), "ua": self.profile.httpUserAgent(),
                                                "ts": time.time()}
            save_conf(self.conf)
            ib.apply_passes(self.conf)
            log.info("ib: Cloudflare passed for %s (cookie kept, user agent %s)", self.host, self.profile.httpUserAgent()[:60])
            self.status.setText("Passed. You can close this window.")
            self.accept()
