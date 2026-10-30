"""Widgets both sections share: the thumbnail Grid, the paged Feed, the second tab row (ImageTabs), the image
tab (Viewer, with a strip view for multi-page works) and Section, the search bar + top tab row each section is."""
import html
import subprocess
import urllib.parse
import webbrowser
from pathlib import Path

from PySide6.QtCore import QModelIndex, QSize, Qt, QTimer
from PySide6.QtGui import QColor, QIcon, QImage, QKeySequence, QPixmap, QShortcut, QStandardItem, QStandardItemModel
from PySide6.QtWidgets import (
    QApplication, QComboBox, QCompleter, QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem, QMenu,
    QPushButton, QScrollArea, QSizePolicy, QStackedWidget, QTabBar, QTabWidget, QVBoxLayout, QWidget,
)

from . import pixiv
from .core import (
    CAT_COLOR, LABELS, NAMES, PIXIV_REFERER, THUMB, allowed, cached, fetch_all, fetch_file, fold, http, interleave, local, log,
    run, same, save_conf,
)


def rich_label():
    lab = QLabel(wordWrap=True, textFormat=Qt.RichText, textInteractionFlags=Qt.TextBrowserInteraction, alignment=Qt.AlignTop)
    lab.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
    return lab


def link(kind, text, cat="", label=None):
    """<a> whose href "kind:cat:text" goes to the owning widget's link handler."""
    style = f" style='color:{CAT_COLOR[cat]}'" if cat in CAT_COLOR else ""
    return f"<a href='{kind}:{cat}:{urllib.parse.quote(text)}'{style}>{html.escape(label or text)}</a>"


def unlink(href):
    kind, cat, text = href.split(":", 2)
    return kind, cat, urllib.parse.unquote(text)


def no_close(tabs, i):
    for side in (QTabBar.LeftSide, QTabBar.RightSide):
        tabs.tabBar().setTabButton(i, side, None)


THUMBS = {}  # ponytail: unbounded in-memory cache, fine for a browsing session; LRU if RAM ever matters


class Grid(QListWidget):
    def __init__(self, on_open):
        super().__init__()
        self.gen, self.feed, self.on_open = 0, None, on_open
        self.setViewMode(QListWidget.IconMode)
        self.setIconSize(QSize(THUMB, THUMB))
        self.setGridSize(QSize(THUMB + 12, THUMB + 28))
        self.setResizeMode(QListWidget.Adjust)
        self.setMovement(QListWidget.Static)
        self.setUniformItemSizes(True)
        self.blank = QPixmap(THUMB, THUMB)
        self.blank.fill(Qt.transparent)
        self.itemActivated.connect(lambda it: on_open(self, self.row(it)))

    def mouseReleaseEvent(self, e):
        it = self.itemAt(e.position().toPoint()) if e.button() == Qt.MiddleButton else None
        if it:
            self.on_open(self, self.row(it), True)
        super().mouseReleaseEvent(e)

    def restart(self):
        self.gen += 1
        self.clear()

    def add(self, posts):
        items = []
        for p in posts:
            it = QListWidgetItem(QIcon(self.blank), "")
            self.addItem(it)
            self.set_post(it, p)
            items.append(it)
        return items

    def set_post(self, it, p, text=None):
        pages = f" [{len(p['files'])}]" if len(p["files"]) > 1 else ""
        alts = "".join("+" + LABELS.get(a["site"], "?") for a in p.get("alts", []))
        anim = " ▶" if p["video"] or p.get("ugoira") else ""
        it.setText(text or f"{LABELS.get(p['site'], p['site'])}{alts} {p['id']}{pages}{anim}")
        it.setData(Qt.UserRole, p)
        it.setToolTip(p.get("title") or " ".join(p["tags"][:40]))
        self.load_thumb(p["thumb"], it)

    def load_thumb(self, url, item):
        if url in THUMBS:
            item.setIcon(THUMBS[url])
            return
        gen = self.gen

        def fetch():
            if gen != self.gen:  # stale search, skip the download
                return None
            return QImage.fromData(http(url)).scaled(THUMB, THUMB, Qt.KeepAspectRatio, Qt.SmoothTransformation)

        def done(img, err):
            if err or img is None or img.isNull():
                return
            icon = THUMBS.setdefault(url, QIcon(QPixmap.fromImage(img)))
            try:
                item.setIcon(icon)
            except RuntimeError:  # item deleted by a new search
                pass
        run(fetch, done, "thumb")


class Feed(QWidget):
    """A paged grid over Sources (core.Source), interleaved and cross-site deduped: every search / tag / list tab."""

    def __init__(self, section, label=""):
        super().__init__()
        self.section, self.main, self.label, self.srcs, self.note = section, section.main, label, [], ""
        self.keep = self.mark = None  # optional post filter / tile decorator
        self.gen = self.page = 0
        self.loading, self.active, self.errs, self.seen = False, [], {}, {}
        self.grid = Grid(section.open_image)
        self.grid.feed = self
        self.status = QLabel(wordWrap=True)
        self.lay = QVBoxLayout(self)
        self.lay.setContentsMargins(0, 0, 0, 0)
        self.lay.addWidget(self.grid, 1)
        self.lay.addWidget(self.status)
        sb = self.grid.verticalScrollBar()
        sb.valueChanged.connect(lambda v: v >= sb.maximum() - THUMB and self.more())

    def start(self, srcs, label=None):
        self.srcs, self.label = list(srcs), self.label if label is None else label
        self.gen += 1
        self.page, self.loading, self.errs, self.seen, self.active = 0, False, {}, {}, list(self.srcs)
        self.grid.restart()
        self.status.setText("Nothing to show." + self.note if not self.srcs else "")
        self.more()

    def stop(self):
        self.gen += 1
        self.active = []

    def more(self):
        if self.loading or not self.active:
            return
        self.loading = True
        self.page += 1
        gen, srcs, page = self.gen, list(self.active), self.page
        self.status.setText(f"Loading page {page}…")
        run(lambda: fetch_all(srcs, page), lambda r, e: self.got(gen, r, e))

    def got(self, gen, res, err):
        if gen != self.gen:
            return
        self.loading = False
        if err:
            log.error("'%s' failed: %s", self.label, err)
            self.status.setText(str(err))
            return
        lists, errs = res
        for s, e in errs.items():
            if s not in self.errs:
                log.warning(e)
        self.errs.update(errs)  # sticky: a source that failed on page 1 stays explained
        self.active = [s for s in self.active if lists[s]]  # drop exhausted/broken sources from paging
        bl = set(self.main.conf["blacklist"])
        for p in interleave(lists.values()):
            if allowed(p, bl) and (not self.keep or self.keep(p)):
                self.place(p)
        counts = "  ".join(f"{s.label}:{len(l)}" for s, l in lists.items())
        msg = f"'{self.label}' page {self.page}: {self.grid.count()} posts ({counts}){self.note}"
        if self.errs:
            msg += "  |  " + "  ".join(self.errs.values())
        if not self.active:
            msg += "  — end of results"
        self.status.setText(msg)
        log.info("'%s' page %d: %s", self.label, self.page, counts)
        self.loaded(lists)
        self.fill()

    def fill(self):
        """Fetch the next page while the grid doesn't fill the window, but only when visible: a hidden tab's
        scrollbar always reads empty, and paging on in the background could crawl a whole site."""
        if self.active and self.isVisible() and self.grid.verticalScrollBar().maximum() == 0:
            QTimer.singleShot(0, self.more)

    def showEvent(self, e):
        super().showEvent(e)
        self.fill()

    def place(self, p):
        r = fold(self.seen, p)
        if not r:
            return
        slot, new = r
        if new:
            slot.append(self.grid.add([slot[0]])[0])
            if self.mark:
                self.mark(slot[1], slot[0])
        else:
            self.grid.set_post(slot[1], slot[0])

    def loaded(self, lists):
        pass


class ImageTabs(QTabWidget):
    """The second tab row: a top tab's grid (fixed, first) plus one closable tab per opened image, browser style.
    Double-click opens and shows an image; Ctrl+double-click or middle-click opens it in the background."""

    def __init__(self, section, home, title="▦ Grid"):
        super().__init__(tabsClosable=True, documentMode=True)
        self.section, self.home = section, home
        home.imgtabs = self
        self.addTab(home, title)
        no_close(self, 0)
        self.tabCloseRequested.connect(self.close_image)
        self.currentChanged.connect(lambda i: self.widget(i) and self.widget(i).setFocus())
        for key, fn in (("Ctrl+W", lambda: self.close_image(self.currentIndex())), ("Escape", lambda: self.setCurrentIndex(0)),
                        ("Ctrl+Tab", lambda: self.setCurrentIndex((self.currentIndex() + 1) % self.count())),
                        ("Ctrl+Shift+Tab", lambda: self.setCurrentIndex((self.currentIndex() - 1) % self.count()))):
            QShortcut(QKeySequence(key), self, fn).setContext(Qt.WidgetWithChildrenShortcut)

    def open_image(self, grid, row, background=False):
        p = grid.item(row).data(Qt.UserRole)
        v = next((self.widget(i) for i in range(1, self.count()) if same(self.widget(i).p, p)), None)
        if v is None:
            v = Viewer(self.section, self, grid, row)
            self.addTab(v, "")
            v.load()
            log.info("view %s #%s", p["site"], p["id"])
        if not background:
            self.setCurrentWidget(v)

    def close_image(self, i):
        if i > 0:
            w = self.widget(i)
            w.anim.stop()
            self.removeTab(i)
            w.setParent(None)  # freed by Python once pending image downloads let go of it


class Strip(QScrollArea):
    """Every page of a multi-page work stacked top to bottom (manga reading), each shown as its original lands in cache/."""

    def __init__(self, root, post):
        super().__init__(widgetResizable=True)
        body = QWidget()
        lay = QVBoxLayout(body)
        self.labels, self.pix = [], {}
        for i in range(len(post["files"])):
            lab = QLabel(f"loading page {i + 1}…", alignment=Qt.AlignCenter)
            lay.addWidget(lab)
            self.labels.append(lab)
        self.setWidget(body)
        for i, url in enumerate(post["files"]):  # queued in order, so pages arrive top to bottom
            run(lambda i=i, url=url: QImage(str(cached(root, post, i) or fetch_file(url, local(root, post, i, "cache")))),
                lambda img, e, i=i: self.got(i, img, e), "view")

    def got(self, i, img, err):
        try:
            if err or img.isNull():
                self.labels[i].setText(f"page {i + 1} failed: {err}")
            else:
                self.pix[i] = QPixmap.fromImage(img)
                self.fit(i)
        except RuntimeError:  # strip closed meanwhile
            pass

    def fit(self, i):
        self.labels[i].setPixmap(self.pix[i].scaledToWidth(max(self.viewport().width() - 24, 100), Qt.SmoothTransformation))

    def resizeEvent(self, e):
        super().resizeEvent(e)
        for i in self.pix:
            self.fit(i)


class Viewer(QWidget):
    """One image tab: the picture + side panel. ←/→ walk the grid it was opened from, PgUp/PgDn flip pages.
    The section adds its own buttons (post_actions) and tag menu entries (tag_actions)."""

    def __init__(self, section, tabs, grid, row):
        super().__init__(focusPolicy=Qt.StrongFocus)
        self.section, self.main, self.tabs, self.grid, self.row = section, section.main, tabs, grid, row
        self.p, self.pg, self.pix, self.frames, self.fi, self.strip_on = grid.item(row).data(Qt.UserRole), 0, None, [], 0, False
        self.img = QLabel(alignment=Qt.AlignCenter)
        self.img.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Ignored)
        self.stack = QStackedWidget()
        self.stack.addWidget(self.img)
        self.info = QLabel(wordWrap=True, textInteractionFlags=Qt.TextSelectableByMouse)
        self.tags = QListWidget(focusPolicy=Qt.NoFocus, contextMenuPolicy=Qt.CustomContextMenu)
        self.tags.itemActivated.connect(lambda it: self.section.open_tag(it.text(), it.data(Qt.UserRole) or ""))
        self.tags.customContextMenuRequested.connect(self.tag_menu)
        self.anim = QTimer(self, singleShot=True, timeout=self.next_frame)

        def btn(text, fn):
            b = QPushButton(text, focusPolicy=Qt.NoFocus)
            b.clicked.connect(fn)
            return b
        self.btn = btn
        self.fav = btn("♥ Favorite (F)", lambda: (self.main.toggle_fav(self.p), self.refresh_info()))
        self.play = btn("▶ Play in mpv", self.play_video)
        self.strip_btn = btn("▤ Strip view", self.toggle_strip)
        self.pages, self.quality = QLabel(), QLabel(wordWrap=True)
        self.extra = QVBoxLayout()  # the section's buttons for this post
        nav = QHBoxLayout()
        for w in (btn("◀", lambda: self.step(-1)), btn("▶", lambda: self.step(1))):
            nav.addWidget(w)
        side = QVBoxLayout()
        side.addLayout(nav)
        for w in (self.pages, self.info, self.quality, QLabel("Tags (double-click = open tab, right-click = more)"), self.tags,
                  btn("⬇ Download (D)", self.download), self.fav, self.play, self.strip_btn):
            side.addWidget(w)
        side.addLayout(self.extra)
        side.addWidget(btn("Open page", lambda: webbrowser.open(self.p["page"])))
        sidew = QWidget()
        sidew.setLayout(side)
        sidew.setFixedWidth(300)
        lay = QHBoxLayout(self)
        lay.addWidget(self.stack, 1)
        lay.addWidget(sidew)

        for key, fn in ((Qt.Key_Left, lambda: self.step(-1)), (Qt.Key_Right, lambda: self.step(1)),
                        (Qt.Key_PageUp, lambda: self.flip(-1)), (Qt.Key_PageDown, lambda: self.flip(1)),
                        (Qt.Key_D, self.download), (Qt.Key_F, self.fav.click)):
            QShortcut(QKeySequence(key), self, fn).setContext(Qt.WidgetWithChildrenShortcut)  # only in the visible image tab

    def download(self):
        self.main.download(self.p, self.grid.feed.label if self.grid.feed else "favorites")

    def locate(self):
        """This tab's row in its grid; rows move when the grid reloads, the tab keeps its post either way."""
        it = self.grid.item(self.row)
        if it and same(it.data(Qt.UserRole), self.p):
            return self.row
        return next((i for i in range(self.grid.count()) if same(self.grid.item(i).data(Qt.UserRole), self.p)), self.row)

    def step(self, d):
        row = self.locate() + d
        if 0 <= row < self.grid.count():
            self.row, self.p, self.pg = row, self.grid.item(row).data(Qt.UserRole), 0
            self.grid.setCurrentRow(row)
            self.load()
        elif row >= self.grid.count() and self.grid.feed:
            self.grid.feed.more()  # walked off the end: fetch next page, press → again

    def flip(self, d):
        if 0 <= self.pg + d < len(self.p["files"]):
            self.pg += d
            self.load()

    def current(self, key):
        return key == (self.pg, self.p["site"], self.p["id"])  # still showing what was requested

    def set_post(self, p):
        """Swap in an updated copy of this tab's post (bookmark state, a booru copy, …) and its grid tile."""
        it = self.grid.item(self.locate())
        if it and same(it.data(Qt.UserRole), self.p):
            self.grid.set_post(it, p)
        self.p = p
        self.load()

    def load(self):
        p = self.p
        i = self.tabs.indexOf(self)
        self.tabs.setTabText(i, f"{LABELS.get(p['site'], p['site'])} {p['id']}")
        self.tabs.setTabToolTip(i, p.get("title") or " ".join(p["tags"][:30]))
        self.tags.clear()
        for cat, tags in [(c, p["cats"][c]) for c in CAT_COLOR if p["cats"].get(c)] if p.get("cats") else [("", p["tags"])]:
            for t in tags:
                it = QListWidgetItem(t)
                it.setData(Qt.UserRole, cat)
                if cat:
                    it.setForeground(QColor(CAT_COLOR[cat]))
                self.tags.addItem(it)
        self.refresh_info()
        self.play.setVisible(p["video"])
        self.strip_btn.setVisible(len(p["files"]) > 1)
        while self.extra.count():  # the section's buttons for this post
            self.extra.takeAt(0).widget().setParent(None)
        for text, fn in self.section.post_actions(self):
            self.extra.addWidget(self.btn(text, fn))
        self.anim.stop()
        self.frames = []
        if self.strip_on and len(p["files"]) > 1:
            return self.show_strip()
        self.stack.setCurrentWidget(self.img)
        # the original is downloaded into cache/ (or favorites/) and shown from there; until it's in, the sample
        # stands in. Videos keep their poster.
        root, pg = self.main.conf["download_dir"], self.pg
        full = None if p["video"] else p["files"][pg]
        have = full and cached(root, p, pg)
        folder = "favorites" if self.main.is_fav(p) else "cache"
        key, self.pix, self.hd, self.fails = (pg, p["site"], p["id"]), None, False, 0
        self.img.setText("loading…")
        self.quality.setText("loading original…" if full else "")

        def done(img, err, original):
            if not self.current(key) or self.frames or self.hd:
                return
            if err or img is None or img.isNull():
                self.fails += 1
                if original:
                    self.quality.setText(f"original failed{': ' + str(err) if err else ''} — showing the sample")
                if not self.pix and self.fails == (2 if full and not have else 1):
                    self.img.setText(f"failed: {err}")
                return
            self.pix, self.hd = QPixmap.fromImage(img), original or not full
            if original:
                self.quality.setText(f"original {img.width()}×{img.height()}, in {folder if not have else have.parent.name}/")
            self.rescale()
        if not have:
            sample = p["thumb"] if p["video"] else p["samples"][pg]
            run(lambda: QImage.fromData(http(sample)), lambda i, e: done(i, e, False), "view")
        if full:
            # ponytail: Qt refuses to decode images over 256 MB (~8000×8000), the sample stays up then
            run(lambda: QImage(str(have or fetch_file(full, local(root, p, pg, folder)))), lambda i, e: done(i, e, True), "view")
        self.prefetch(root)
        if p.get("ugoira"):
            conf = self.main.conf

            def frames(res, err):
                if self.current(key) and res:
                    self.frames, self.fi = [(QPixmap.fromImage(i), d) for i, d in res], 0
                    self.next_frame()
            run(lambda: pixiv.ugoira_frames(conf, p["id"]), frames, "view")

    def toggle_strip(self):
        self.strip_on = not self.strip_on
        self.load()

    def show_strip(self):
        old = self.stack.widget(1)
        if old:
            self.stack.removeWidget(old)
            old.deleteLater()
        self.stack.addWidget(Strip(self.main.conf["download_dir"], self.p))
        self.stack.setCurrentIndex(1)
        self.strip_btn.setText("◫ Single page")
        self.quality.setText(f"strip view, {len(self.p['files'])} pages")

    def prefetch(self, root):
        """Pull the next post's original into cache/ so → opens it straight from disk."""
        it = self.grid.item(self.locate() + 1)
        n = it and it.data(Qt.UserRole)
        if n and not n["video"] and not cached(root, n, 0):
            run(lambda: fetch_file(n["files"][0], local(root, n, 0, "cache")), lambda r, e: None, "view")

    def next_frame(self):
        if self.frames and self.isVisible():
            self.pix, delay = self.frames[self.fi]
            self.rescale()
            self.fi = (self.fi + 1) % len(self.frames)
            self.anim.start(max(delay, 16))

    def refresh_info(self):
        p = self.p
        n = len(p["files"])
        self.strip_btn.setText("◫ Single page" if self.strip_on else "▤ Strip view")
        self.pages.setText(f"page {self.pg + 1}/{n} (PgUp/PgDn)" if n > 1 else "")
        self.pages.setVisible(n > 1 and not self.strip_on)
        head = f"<b>{html.escape(p.get('title') or NAMES.get(p['site'], p['site']) + ' #' + p['id'])}</b>"
        by = f"<br>by {html.escape(p['user_name'])}" if p.get("user_name") else ""
        stats = f"  ♥{p['bookmarks']:,}" if "bookmarks" in p else ""
        alts = "".join(f"<br>also on {NAMES.get(a['site'], a['site'])} #{a['id']}" for a in p.get("alts", []))
        self.info.setText(f"{head}{by}<br>{p['w']}×{p['h']}  rating:{p['rating']}{stats}{' · AI' if p.get('ai') else ''}{alts}"
                          f"<br>{self.locate() + 1}/{self.grid.count()} (←/→, Esc = grid, Ctrl+W = close)")
        self.fav.setText(("♥ Unfavorite" if self.main.is_fav(p) else "♡ Favorite") + " (F)")

    def rescale(self):
        if self.pix:
            self.img.setPixmap(self.pix.scaled(self.img.size(), Qt.KeepAspectRatio, Qt.SmoothTransformation))

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self.rescale()

    def showEvent(self, e):
        super().showEvent(e)
        self.next_frame()  # resume an animation paused while the tab was in the background

    def hideEvent(self, e):
        super().hideEvent(e)
        self.anim.stop()

    def play_video(self):
        p = self.p
        url = next((u for u in p["samples"] + p["files"] if Path(urllib.parse.urlparse(u).path).suffix in (".mp4", ".webm")), p["files"][0])
        try:
            subprocess.Popen(["mpv", f"--referrer={PIXIV_REFERER}" if p["site"] == "pixiv" else "--", url])
        except FileNotFoundError:  # no mpv (typical on Windows): let the browser play it
            webbrowser.open(p["page"])

    def tag_menu(self, pos):
        it = self.tags.itemAt(pos)
        if not it:
            return
        t, cat, m = it.text(), it.data(Qt.UserRole) or "", QMenu(self)
        m.addAction("Open tab", lambda: self.section.open_tag(t, cat))
        for text, fn in self.section.tag_actions(t, cat):
            m.addAction(text, fn)
        m.addAction("Blacklist", lambda: self.main.blacklist_add(t))
        m.addAction("Copy", lambda: QApplication.clipboard().setText(t))
        m.exec(self.tags.mapToGlobal(pos))


class Section(QWidget):
    """One half of the app: a search bar (with autocomplete and saved searches) over the top tab row.
    Fixed tabs come first; every search / tag / user opens a closable tab, each holding its own ImageTabs row."""
    SAVED = "saved"  # conf key of this section's saved searches

    def __init__(self, main, placeholder):
        super().__init__()
        self.main, self.conf, self.dyn, self.fixed = main, main.conf, {}, 0
        self.q = QLineEdit(placeholderText=placeholder)
        self.q.returnPressed.connect(lambda: self.search())
        self.saved = QComboBox(minimumContentsLength=16)
        self.saved.activated.connect(lambda i: i and self.search(self.saved.itemText(i)))
        self.star = QPushButton("☆", toolTip="Save / unsave this search", clicked=self.toggle_saved)
        self.bar = QHBoxLayout()
        self.tabs = QTabWidget(tabsClosable=True)
        self.tabs.tabCloseRequested.connect(self.close_tab)
        self.tabs.currentChanged.connect(self.sync_query)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addLayout(self.bar)
        lay.addWidget(self.tabs, 1)
        # autocomplete: completes the last token only, debounced
        self.model = QStandardItemModel()
        self.completer = QCompleter(self.model, self)
        self.completer.setWidget(self.q)
        self.completer.setCompletionMode(QCompleter.UnfilteredPopupCompletion)
        self.completer.activated[QModelIndex].connect(self.insert_completion)
        self.timer = QTimer(self, singleShot=True, interval=250, timeout=self.autocomplete)
        self.q.textEdited.connect(lambda: self.timer.start())
        self.refresh_saved()

    # --- tabs ---
    def add_fixed(self, widget, title, grid=True):
        w = ImageTabs(self, widget) if grid else widget
        self.tabs.addTab(w, title)
        no_close(self.tabs, self.tabs.count() - 1)
        self.fixed = self.tabs.count()
        return w

    def open_tab(self, key, title, make):
        """Focus the tab for key, creating it with make() the first time."""
        w = self.dyn.get(key)
        if w is None:
            w = self.dyn[key] = ImageTabs(self, make())
            w.key = key
            self.tabs.setTabToolTip(self.tabs.addTab(w, title[:40]), title)
            log.info("open tab %s", key)
        self.tabs.setCurrentWidget(w)
        return w.home

    def open_feed(self, key, title, srcs, note=""):
        def make():
            f = Feed(self, title)
            f.note = note
            f.start(srcs)
            return f
        return self.open_tab(key, title, make)

    def close_tab(self, i):
        w = self.tabs.widget(i)
        if not hasattr(w, "key"):
            return
        for f in [w.home, *getattr(w.home, "feeds", [])]:
            if isinstance(f, Feed):
                f.stop()  # late replies are dropped instead of paging on in the background
        self.tabs.removeTab(i)
        del self.dyn[w.key]
        w.setParent(None)  # freed by Python once pending callbacks let go of it

    def open_image(self, grid, row, background=None):
        """Open grid[row] as an image tab in the second row of the grid's own top tab."""
        if background is None:
            background = bool(QApplication.keyboardModifiers() & Qt.ControlModifier)
        w = grid
        while not isinstance(w, ImageTabs):
            w = w.parentWidget()
        w.open_image(grid, row, background)

    def current_feed(self):
        w = getattr(self.tabs.currentWidget(), "home", None)
        return w if isinstance(w, Feed) else next(iter(getattr(w, "feeds", [])), None)

    def sync_query(self, i):
        """The search box follows the top tab (its query attribute), so ☆ works on what you're looking at."""
        q = getattr(getattr(self.tabs.widget(i), "home", None), "query", None)
        if q is not None:
            self.q.setText(q)
            self.star.setText("★" if q in self.conf[self.SAVED] else "☆")

    # --- search box ---
    def autocomplete(self):
        word = self.q.text().split(" ")[-1].lstrip("-")
        if len(word) < 2:
            return
        text = self.q.text()

        def done(res, err):
            if err or self.q.text() != text:
                return
            self.model.clear()
            for value, label in res:
                it = QStandardItem(label)
                it.setData(value, Qt.UserRole)
                self.model.appendRow(it)
            if res:
                self.completer.complete()
        run(lambda: self.complete(word), done)

    def insert_completion(self, index):
        head, _, last = self.q.text().rpartition(" ")
        neg = "-" if last.startswith("-") else ""
        self.q.setText(f"{head} {neg}{index.data(Qt.UserRole)} ".lstrip())

    def refresh_saved(self):
        self.saved.clear()
        self.saved.addItems(["Saved searches…"] + self.conf[self.SAVED])

    def toggle_saved(self):
        q = self.q.text().strip()
        if not q:
            return
        s = self.conf[self.SAVED]
        s.remove(q) if q in s else s.append(q)
        log.info("saved search %s '%s'", "added" if q in s else "removed", q)
        save_conf(self.conf)
        self.refresh_saved()
        self.star.setText("★" if q in s else "☆")

    # --- what a section provides ---
    def complete(self, word):
        return []

    def post_actions(self, viewer):
        """[(button text, fn)] for the image tab of viewer.p"""
        return []

    def tag_actions(self, tag, cat):
        """[(menu text, fn)] for a tag's right-click menu in an image tab"""
        return []
