"""Widgets both sections share: the thumbnail Grid, the paged Feed, the second tab row (ImageTabs), the image
tab (Viewer, with a strip view for multi-page works) and Section, the search bar + top tab row each section is."""
import html
import subprocess
import time
import urllib.parse
import webbrowser
from collections import Counter
from pathlib import Path

from PySide6.QtCore import QEvent, QMimeData, QModelIndex, QObject, QRect, QSize, QStringListModel, Qt, QTimer, QUrl
from PySide6.QtGui import QColor, QFont, QIcon, QImage, QImageReader, QKeySequence, QPixmap, QShortcut, QStandardItem, QStandardItemModel
from PySide6.QtWidgets import (
    QApplication, QComboBox, QCompleter, QGridLayout, QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem, QMenu,
    QPushButton, QScrollArea, QSizePolicy, QStackedWidget, QStyle, QStyledItemDelegate, QTabBar, QTabWidget, QTextBrowser,
    QTreeWidget, QVBoxLayout, QWidget,
)

from . import pixiv
from .look import host_env
from .core import (
    CAT_COLOR, CJK, LABELS, NAMES, THUMB, allowed, cached, fetch_all, fetch_file, fold, http, interleave, local, log,
    referer, run, same, save_conf, to_ui, undecodable,
)

QImageReader.setAllocationLimit(1024)  # MB; Qt's default 256 refuses images above about 67 megapixels


def decode_original(path):
    """A downloaded original -> QImage. A file that won't decode leaves cache/ (so opening it again downloads it
    again) and the error says why."""
    img = QImage(str(path))
    if img.isNull():
        why = undecodable(path)
        if path.parent.name == "cache":
            path.unlink(missing_ok=True)
        raise RuntimeError(f"{path.name}: {why}")
    return img


def why_failed(err):
    """Text for an image that didn't show: the download error, or (downloaded fine, no error) a format Qt can't
    decode, which is nearly always WebP without Qt's imageformats plugin."""
    if err:
        return str(err)
    if b"webp" not in [bytes(f) for f in QImageReader.supportedImageFormats()]:
        return "this Qt can't decode WebP (Pawchive thumbnails, some booru files). Nix: rebuild with qt6.qtimageformats; see the log"
    return "the file downloaded but isn't an image Qt can read"


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

# True while a middle-click runs an item's double-click action: tabs it opens stay in the background
BACKGROUND = False


def in_background(fn, *args):
    global BACKGROUND
    BACKGROUND = True
    try:
        fn(*args)
    finally:
        BACKGROUND = False


class MiddleClick(QObject):
    """Middle-click on a list / tree item or a text link = its double-click / click action, in a background tab."""

    def __init__(self, view):
        super().__init__(view)
        self.view = view
        view.viewport().installEventFilter(self)

    def eventFilter(self, obj, e):
        if e.type() != QEvent.MouseButtonRelease or e.button() != Qt.MiddleButton:
            return False
        v, pos = self.view, e.position().toPoint()
        if isinstance(v, QTextBrowser):
            href = v.anchorAt(pos)
            if href:
                in_background(v.anchorClicked.emit, QUrl(href))
            return bool(href)
        it = v.itemAt(pos)
        if it:
            in_background(v.itemActivated.emit, *((it, 0) if isinstance(v, QTreeWidget) else (it,)))
        return bool(it)


def middle_click(view):
    MiddleClick(view)
    return view


class Marks(QStyledItemDelegate):
    """A thumbnail's ♥ (ImageSearcher favourite) and ★ (the site's own favourite / Pixiv bookmark), clickable: filled
    when on, outlined on hover. No ★ where the site has no account favourites."""

    def paint(self, painter, option, index):
        super().paint(painter, option, index)
        grid, p = self.parent(), index.data(Qt.UserRole)
        hover = bool(option.state & QStyle.State_MouseOver)
        if not p:
            return
        painter.save()
        f = QFont(painter.font())
        f.setPixelSize(15)
        painter.setFont(f)
        for rect, kind, on in grid.marks(option.rect, p):
            if on or hover:
                painter.setPen(Qt.NoPen)
                painter.setBrush(QColor(0, 0, 0, 150))
                painter.drawRoundedRect(rect, 5, 5)
                painter.setPen(QColor({"fav": "#ff5a7a", "site": "#ffc83d"}[kind] if on else "#e0e0e0"))
                painter.drawText(rect, Qt.AlignCenter, {("fav", True): "♥", ("fav", False): "♡", ("site", True): "★", ("site", False): "☆"}[kind, on])
        painter.restore()


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
        self.setItemDelegate(Marks(self))
        self.setMouseTracking(True)  # hover outlines of ♥ / ★
        self.viewport().setAttribute(Qt.WA_Hover)

    def mouseReleaseEvent(self, e):
        it = self.itemAt(e.position().toPoint()) if e.button() == Qt.MiddleButton else None
        if it:
            in_background(self.on_open, self, self.row(it), True)
        super().mouseReleaseEvent(e)

    # --- ♥ / ★ on the thumbnails: click them, or F / B on the selected one ---
    def main_window(self):
        """The main window, once this grid sits in it (None for a loose grid, as in the self-test)."""
        w = self.window()
        return w if hasattr(w, "owner") and w is not self else None

    def marks(self, rect, p):
        """[(QRect, "fav" | "site", on)] in the tile's top-right corner."""
        main = self.main_window()
        if not main:
            return []
        site = main.owner(p).site_fav_state(p)
        r = QRect(rect.right() - 25, rect.top() + 3, 22, 22)
        return [(r, "fav", main.is_fav(p))] + ([] if site is None else [(r.translated(-25, 0), "site", site)])

    def mark_at(self, pos):
        it = self.itemAt(pos)
        if it and it.data(Qt.UserRole):
            for rect, kind, _ in self.marks(self.visualItemRect(it), it.data(Qt.UserRole)):
                if rect.contains(pos):
                    return it, kind
        return None, None

    def mousePressEvent(self, e):
        it, kind = self.mark_at(e.position().toPoint()) if e.button() == Qt.LeftButton else (None, None)
        if it:
            return self.toggle(it, kind)  # not a selection, and not half of a double-click that opens the image
        super().mousePressEvent(e)

    def mouseDoubleClickEvent(self, e):
        if not self.mark_at(e.position().toPoint())[0]:
            super().mouseDoubleClickEvent(e)

    def keyPressEvent(self, e):
        it = self.currentItem()
        kind = {Qt.Key_F: "fav", Qt.Key_B: "site"}.get(e.key()) if not e.modifiers() else None
        if it and kind and self.main_window():
            return self.toggle(it, kind)
        super().keyPressEvent(e)

    def toggle(self, it, kind):
        main, p = self.main_window(), it.data(Qt.UserRole)
        if kind == "fav":
            main.toggle_fav(p)
            return self.viewport().update()

        def changed(new):
            try:
                it.setData(Qt.UserRole, new)
            except RuntimeError:  # tile gone meanwhile (new search)
                pass
        main.owner(p).toggle_site_fav(p, changed)

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
            img = QImage.fromData(http(url))
            return img if img.isNull() else img.scaled(THUMB, THUMB, Qt.KeepAspectRatio, Qt.SmoothTransformation)

        def done(img, err):
            if err or img is None or img.isNull():
                return
            icon = THUMBS.setdefault(url, QIcon(QPixmap.fromImage(img)))
            try:
                item.setIcon(icon)
            except RuntimeError:  # item deleted by a new search
                pass
        run(fetch, done, "thumb")


class WordCompleter(QCompleter):
    """Completes the last space-separated word of a line edit (a leading - is kept)."""

    def splitPath(self, path):
        return [path.split(" ")[-1].lstrip("-")]

    def pathFromIndex(self, index):
        head, _, last = self.widget().text().rpartition(" ")
        return f"{head} {'-' if last.startswith('-') else ''}{index.data()} ".lstrip()


def fav_match(tags, text, cat=""):
    """Favourites search: every word is part of some tag, no -word is, and cat (exact) is one of them."""
    low, words = [t.lower() for t in tags], text.lower().split()
    return ((not cat or cat in tags) and all(any(w in t for t in low) for w in words if w[0] != "-")
            and not any(any(w[1:] in t for t in low) for w in words if w[0] == "-" and len(w) > 1))


class Favorites(QWidget):
    """A section's ♥ favourites, sorted into categories by their own tags: pick a category, or search with tags
    (all must match; part of a tag is enough, -tag excludes)."""

    def __init__(self, section, mine):
        super().__init__()
        self.section, self.mine, self.posts, self.imgtabs = section, mine, [], None
        self.grid = Grid(section.open_image)
        self.cat = QComboBox(toolTip="Category: favourites with this tag (the most used tags first)", minimumContentsLength=24)
        self.cat.currentIndexChanged.connect(self.filter)
        self.q = QLineEdit(placeholderText="search favourites by tag, e.g. blue_eyes -sketch", clearButtonEnabled=True)
        typing = QTimer(self, singleShot=True, interval=200, timeout=self.filter)
        self.q.textChanged.connect(typing.start)
        self.q.setCompleter(WordCompleter(self.q, caseSensitivity=Qt.CaseInsensitive, filterMode=Qt.MatchContains))
        row = QHBoxLayout()
        for w, stretch in ((QLabel("category:"), 0), (self.cat, 0), (self.q, 1)):
            row.addWidget(w, stretch)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addLayout(row)
        lay.addWidget(self.grid, 1)

    def refresh(self):
        self.posts = [p for p in self.section.conf["favorites"] if self.mine(p)]
        counts = Counter(t for p in self.posts for t in p["tags"])
        top = [t for t, _ in counts.most_common()]
        was = self.cat.currentData()
        self.cat.blockSignals(True)
        self.cat.clear()
        self.cat.addItem(f"All ({len(self.posts)})", "")
        for t in top[:300]:  # ponytail: the 300 most used tags; the search box reaches the rest
            self.cat.addItem(f"{t} ({counts[t]})", t)
        self.cat.setCurrentIndex(max(self.cat.findData(was), 0))
        self.cat.blockSignals(False)
        self.q.completer().setModel(QStringListModel(top, self.q.completer()))
        self.filter()

    def filter(self):
        shown = [p for p in self.posts if fav_match(p["tags"], self.q.text(), self.cat.currentData())]
        self.grid.restart()
        self.grid.add(shown)
        tabs = self.section.tabs
        n = f"{len(shown)}/{len(self.posts)}" if len(shown) != len(self.posts) else len(self.posts)
        tabs.setTabText(tabs.indexOf(self.imgtabs), f"Favorites ({n})")


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
        log.info("feed '%s': start from %s", self.label, ", ".join(s.label for s in self.srcs) or "nothing")
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
        bl, hidden, before = set(self.main.conf["blacklist"]), 0, self.grid.count()
        for p in interleave(lists.values()):
            if allowed(p, bl) and (not self.keep or self.keep(p)):
                self.place(p)
            else:
                hidden += 1
        counts = "  ".join(f"{s.label}:{len(l)}" for s, l in lists.items())
        msg = f"'{self.label}' page {self.page}: {self.grid.count()} posts ({counts}){self.note}"
        if self.errs:
            msg += "  |  " + "  ".join(self.errs.values())
        if not self.active:
            msg += "  — end of results"
        self.status.setText(msg)
        log.info("feed '%s' page %d: got %s; %d new tiles, %d hidden by blacklist/filters, %d merged as copies%s",
                 self.label, self.page, counts or "nothing", self.grid.count() - before, hidden,
                 sum(len(l) for l in lists.values()) - hidden - (self.grid.count() - before), "; end of results" if not self.active else "")
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
            log.info("open image tab %s #%s%s", p["site"], p["id"], " (background)" if background else "")
        if not background:
            self.setCurrentWidget(v)

    def close_image(self, i):
        if i > 0:
            w = self.widget(i)
            log.info("close image tab %s #%s", w.p["site"], w.p["id"])
            w.anim.stop()
            self.removeTab(i)
            w.setParent(None)  # freed by Python once pending image downloads let go of it


SPIN, spinning = "◐◓◑◒", {}  # label -> text: labels showing a turning ◐ while their image downloads


def spin(label, text=None):
    """Show "◐ text" turning on label until it gets a picture; text None stops it."""
    if text is None:
        spinning.pop(label, None)
        return
    global SPIN_TIMER
    spinning[label] = text
    label.setText(f"{SPIN[0]}  {text}")
    if SPIN_TIMER is None:
        SPIN_TIMER = QTimer(timeout=spin_tick)
    if not SPIN_TIMER.isActive():
        SPIN_TIMER.start(150)


def spin_tick():
    frame = SPIN[int(time.monotonic() * 6) % len(SPIN)]
    for label, text in list(spinning.items()):
        try:
            label.setText(f"{frame}  {text}")
        except RuntimeError:  # label gone with its tab
            spinning.pop(label)
    if not spinning:
        SPIN_TIMER.stop()


SPIN_TIMER = None  # made on first use: a QTimer needs the QApplication


def copy_files(paths, image=None):
    """Clipboard: the files (chat apps and file managers paste these), plus the picture itself when there's one."""
    m = QMimeData()
    if image is not None:
        m.setImageData(image)
    urls = [QUrl.fromLocalFile(str(p)) for p in paths]
    m.setUrls(urls)
    m.setData("x-special/gnome-copied-files", ("copy\n" + "\n".join(u.toString() for u in urls)).encode())
    return m


class Strip(QScrollArea):
    """Every page of a multi-page work stacked top to bottom (manga reading), each shown as its original lands in cache/.
    Zooming out (Ctrl+wheel, −/+) lays the pages out side by side, up to 8 a row. Click a page to copy it,
    Shift+click copies the range from the last click, Ctrl+click adds / removes one."""
    COLS = (1, 2, 3, 4, 6, 8)

    def __init__(self, root, post, on_copy):
        super().__init__(widgetResizable=True)
        body = QWidget()
        self.lay = QGridLayout(body)
        self.labels, self.pix, self.paths, self.cols, self.sel, self.anchor, self.on_copy = [], {}, {}, 1, [], 0, on_copy
        for i in range(len(post["files"])):
            lab = QLabel(alignment=Qt.AlignCenter)
            spin(lab, f"loading page {i + 1}…")
            self.lay.addWidget(lab, i, 0)
            self.labels.append(lab)
        self.setWidget(body)

        def get(i, url):
            path = cached(root, post, i) or fetch_file(url, local(root, post, i, "cache"))
            return path, decode_original(path)
        for i, url in enumerate(post["files"]):  # queued in order, so pages arrive top to bottom
            run(lambda i=i, url=url: get(i, url), lambda res, e, i=i: self.got(i, res, e), "view")

    def got(self, i, res, err):
        try:
            spin(self.labels[i])
            if err or res[1].isNull():
                self.labels[i].setText(f"page {i + 1} failed: {why_failed(err)} (⟳ Reload / R tries again)")
            else:
                self.paths[i], self.pix[i] = res[0], QPixmap.fromImage(res[1])
                self.fit(i)
        except RuntimeError:  # strip closed meanwhile
            pass

    def mousePressEvent(self, e):
        body = self.widget()
        lab = body.childAt(body.mapFrom(self.viewport(), e.position().toPoint()))
        if e.button() != Qt.LeftButton or lab not in self.labels:
            return super().mousePressEvent(e)
        i, mods = self.labels.index(lab), e.modifiers()
        if mods & Qt.ShiftModifier:
            lo, hi = sorted((self.anchor, i))
            self.sel = list(range(lo, hi + 1))
        elif mods & Qt.ControlModifier:
            self.sel = sorted(set(self.sel) ^ {i})
            self.anchor = i
        else:
            self.sel, self.anchor = [i], i
        for j, lb in enumerate(self.labels):
            lb.setStyleSheet("border: 3px solid #3daee9;" if j in self.sel else "")
        self.copy()

    def copy(self):
        """The selected pages that are in; -> status text."""
        have = [i for i in self.sel if i in self.paths]
        if not have:
            return self.on_copy("Click a page to copy it (Shift+click: a range, Ctrl+click: add / remove)" if not self.sel else "Those pages are still loading")
        QApplication.clipboard().setMimeData(copy_files([self.paths[i] for i in have], self.pix[have[0]].toImage() if len(have) == 1 else None))
        left = len(self.sel) - len(have)
        pages = f"page {have[0] + 1}" if len(have) == 1 else f"{len(have)} pages ({', '.join(str(i + 1) for i in have[:12])}{'…' if len(have) > 12 else ''}) as files"
        self.on_copy(f"Copied {pages}" + (f"; {left} still loading, click again once in" if left else ""))

    def fit(self, i):
        w = (self.viewport().width() - 24 - self.lay.spacing() * (self.cols - 1)) // self.cols
        self.labels[i].setPixmap(self.pix[i].scaledToWidth(max(w, 60), Qt.SmoothTransformation))

    def zoom(self, step):
        """step -1 = zoom out (more pages a row), +1 = in."""
        at = self.COLS.index(self.cols)
        cols = self.COLS[min(max(at - step, 0), len(self.COLS) - 1)]
        if cols == self.cols:
            return
        top = next((i for i, lab in enumerate(self.labels) if lab.y() + lab.height() > self.verticalScrollBar().value()), 0)
        self.cols = cols
        for i, lab in enumerate(self.labels):
            self.lay.addWidget(lab, i // cols, i % cols)  # re-adding moves it
        for i in self.pix:
            self.fit(i)
        QTimer.singleShot(0, lambda: self.ensureWidgetVisible(self.labels[top], 0, 0))  # keep the page you were on in view
        self.window().statusBar().showMessage(f"strip view: {cols} page{'s' * (cols > 1)} a row")

    def wheelEvent(self, e):
        if e.modifiers() & Qt.ControlModifier:
            return self.zoom(1 if e.angleDelta().y() > 0 else -1)
        super().wheelEvent(e)

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
        middle_click(self.tags)
        self.tags.customContextMenuRequested.connect(self.tag_menu)
        self.anim = QTimer(self, singleShot=True, timeout=self.next_frame)

        def btn(text, fn):
            b = QPushButton(text, focusPolicy=Qt.NoFocus)
            b.clicked.connect(fn)
            return b
        self.btn = btn
        self.fav = btn("♥ Favorite (F)", lambda: (self.main.toggle_fav(self.p), self.refresh_info()))
        self.sfav = btn("☆ Site favourite (B)", self.toggle_site_fav)
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
                  btn("⬇ Download (D)", self.download), btn("📋 Copy image (C)", self.copy), self.fav, self.sfav, self.play, self.strip_btn,
                  btn("⟳ Reload (R)", self.reload)):
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
                        (Qt.Key_D, self.download), (Qt.Key_C, self.copy), (Qt.Key_R, self.reload), (Qt.Key_F, self.fav.click),
                        (Qt.Key_B, self.toggle_site_fav), (Qt.Key_Minus, lambda: self.zoom_strip(-1)),
                        (Qt.Key_Plus, lambda: self.zoom_strip(1)), (Qt.Key_Equal, lambda: self.zoom_strip(1))):
            QShortcut(QKeySequence(key), self, fn).setContext(Qt.WidgetWithChildrenShortcut)  # only in the visible image tab

    def reload(self):
        """Load this post again: what failed downloads again, what's in cache/ comes straight back."""
        log.info("reload image %s #%s", self.p["site"], self.p["id"])
        self.load()

    def download(self):
        self.main.download(self.p, self.grid.feed.label if self.grid.feed else "favorites")

    def copy(self):
        """The picture on screen: the original once it's in, a video's current frame; a Pixiv animation as a GIF."""
        if self.p.get("ugoira") and self.frames:
            return self.copy_gif()
        if isinstance(self.stack.currentWidget(), Strip):
            return self.stack.currentWidget().copy()
        if not self.pix:
            return self.main.statusBar().showMessage("Nothing to copy yet: the image is still loading")
        QApplication.clipboard().setPixmap(self.pix)
        self.main.statusBar().showMessage(f"Copied {self.pix.width()}x{self.pix.height()} image" + ("" if self.hd else " (the original isn't in yet)"))

    def copy_gif(self):
        """The GIF goes on the clipboard twice: as image/gif and as a file (chat apps take the file, image editors the data)."""
        frames, path, bar = [(pix.toImage(), d) for pix, d in self.frames], local(self.main.conf["download_dir"], self.p, 0, "cache").with_suffix(".gif"), self.main.statusBar()
        bar.showMessage(f"Making a GIF of {len(frames)} frames…")

        def make():
            data = pixiv.gif(frames)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
            return data

        def done(data, err):
            if err:
                log.warning("copy gif #%s failed: %s", self.p["id"], err)
                return bar.showMessage(f"Copy failed: {err}")
            m = copy_files([path])
            m.setData("image/gif", data)
            QApplication.clipboard().setMimeData(m)
            log.info("copied ugoira #%s as a %d kB GIF", self.p["id"], len(data) // 1024)
            bar.showMessage(f"Copied the animation as a GIF ({len(frames)} frames, {len(data) // 1024} kB)")
        run(make, done)

    def toggle_site_fav(self):
        def changed(new):
            it = self.grid.item(self.locate())
            if it and same(it.data(Qt.UserRole), self.p):
                it.setData(Qt.UserRole, new)
            self.p = new
            self.refresh_info()
        self.main.owner(self.p).toggle_site_fav(self.p, changed)

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
        log.info("image %s #%s%s %s (%s)", p["site"], p["id"], f" page {self.pg + 1}/{len(p['files'])}" if len(p["files"]) > 1 else "",
                 "video" if p["video"] else f"{p['w']}x{p['h']}", p["page"])
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
        for t in ((p.get("cats") or {}).get("character") or [])[:2]:
            self.extra.addWidget(self.btn(f"📖 {t}", lambda t=t: self.main.read(t)))
        self.anim.stop()
        self.frames = []
        if self.strip_on and len(p["files"]) > 1:
            spin(self.img)
            return self.show_strip()
        self.stack.setCurrentWidget(self.img)
        # the original is downloaded into cache/ (or favorites/) and shown from there; until it's in, the sample
        # stands in. Videos keep their poster.
        root, pg = self.main.conf["download_dir"], self.pg
        full = None if p["video"] else p["files"][pg]
        have = full and cached(root, p, pg)
        folder = "favorites" if self.main.is_fav(p) else "cache"
        key, self.pix, self.hd, self.fails = (pg, p["site"], p["id"]), None, False, 0
        self.img.clear()
        spin(self.img, "loading…")
        self.quality.setText("loading original…" if full else "")

        def done(img, err, original):
            if not self.current(key) or self.frames or self.hd or self.stack.currentWidget() is not self.img:
                return
            if err or img is None or img.isNull():
                self.fails += 1
                if original:
                    self.quality.setText(f"original failed: {why_failed(err)} — showing the sample")
                if not self.pix and self.fails == (2 if full and not have else 1):
                    spin(self.img)
                    self.img.setText(f"failed: {why_failed(err)}")
                    log.warning("image %s #%s didn't show: %s", self.p["site"], self.p["id"], why_failed(err))
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
            run(lambda: decode_original(have or fetch_file(full, local(root, p, pg, folder))), lambda i, e: done(i, e, True), "view")
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

    def zoom_strip(self, step):
        if isinstance(self.stack.currentWidget(), Strip):
            self.stack.currentWidget().zoom(step)

    def show_strip(self):
        old = self.stack.widget(1)
        if old:
            self.stack.removeWidget(old)
            old.deleteLater()
        self.stack.addWidget(Strip(self.main.conf["download_dir"], self.p, self.main.statusBar().showMessage))
        self.stack.setCurrentIndex(1)
        self.strip_btn.setText("◫ Single page")
        self.quality.setText(f"strip view, {len(self.p['files'])} pages (Ctrl+wheel or −/+ to zoom out)")

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
        owner = self.main.owner(p)
        site = owner.site_fav_state(p)
        self.sfav.setVisible(site is not None)
        self.sfav.setText(f"{'★ Remove from' if site else '☆ Add to'} {owner.SITE_FAV} (B)")

    def rescale(self):
        if self.pix:
            spin(self.img)
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
        log.info("play video %s #%s", p["site"], p["id"])
        url = next((u for u in p["samples"] + p["files"] if Path(urllib.parse.urlparse(u).path).suffix in (".mp4", ".webm")), p["files"][0])
        try:
            subprocess.Popen(["mpv", f"--referrer={referer(url)}" if referer(url) else "--", url], env=host_env())
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
        m.addAction("📖 Read in Reader", lambda: self.main.read(t))
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
        self.timer = QTimer(self, singleShot=True, interval=120, timeout=self.autocomplete)
        self.q.textEdited.connect(lambda: self.timer.start())
        self.completions = {}  # word -> results: going back over a word is instant
        self.refresh_saved()

    # --- tabs ---
    def refresh_favs(self):
        self.favs.refresh()

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
            log.info("open tab %s%s", key, " (background)" if BACKGROUND else "")
        if not BACKGROUND:
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
        log.info("close tab %s", w.key)
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
        if len(word) < (1 if CJK.search(word) else 2):
            return
        text = self.q.text()

        def done(res, err):
            if err or self.q.text() != text:
                return
            self.completions[word] = res
            self.model.clear()
            for value, label in res:
                it = QStandardItem(label)
                it.setData(value, Qt.UserRole)
                self.model.appendRow(it)
            if res:
                self.completer.complete()
        if word in self.completions:
            return done(self.completions[word], None)
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

    # --- the site's own favourites (★): a section that has them sets SITE_FAV and site_fav_write ---
    SITE_FAV = ""  # what to call them on the button ("Danbooru favourites", "Pixiv bookmarks")

    def site_fav_state(self, p):
        """True / False, or None when p's site has no account favourites this app can set."""
        return f"{p['site']}:{p['id']}" in self.conf["site_favs"] if self.SITE_FAV else None

    def site_fav_write(self, p, on):
        """Worker thread: add / remove p on the site; raises with a message on failure."""
        raise NotImplementedError

    def site_fav_marked(self, p, on):
        """After a write: remember it -> the post to show from now on."""
        key, favs = f"{p['site']}:{p['id']}", self.conf["site_favs"]
        if on and key not in favs:
            favs.append(key)
        elif not on and key in favs:
            favs.remove(key)
        save_conf(self.conf)
        return p

    def toggle_site_fav(self, p, changed):
        """★ pressed on p; changed(new post) runs once the site said yes."""
        on = self.site_fav_state(p)
        name = f"{NAMES.get(p['site'], p['site'])} #{p['id']}"
        if on is None:
            return self.main.statusBar().showMessage(f"{NAMES.get(p['site'], p['site'])} has no site favourites here")
        on = not on

        def done(_, err):
            if err:
                log.warning("site favourite %s %s failed: %s", "add" if on else "remove", name, err)
                return self.main.statusBar().showMessage(f"★ {name}: {err}")
            log.info("site favourite %s %s", "added" if on else "removed", name)
            self.main.statusBar().showMessage(f"{'★ Added' if on else '☆ Removed'} {name} {'to' if on else 'from'} {self.SITE_FAV}")
            changed(self.site_fav_marked(p, on))
        run(lambda: self.site_fav_write(p, on), done)

    # --- what a section provides ---
    def complete(self, word):
        return []

    def post_actions(self, viewer):
        """[(button text, fn)] for the image tab of viewer.p"""
        return []

    def tag_actions(self, tag, cat):
        """[(menu text, fn)] for a tag's right-click menu in an image tab"""
        return []


class LiveBulk(QWidget):
    """A bulk download's images, added the moment their files are saved (shown from those files: opening one
    needs no second download). Workers call landed(post, paths); cancel (an Event) adds a Cancel button."""

    def __init__(self, section, label, title, cancel=None):
        super().__init__()
        self.section, self.label, self.title, self.cancel = section, label, title, cancel
        self.grid = Grid(section.open_image)
        self.grid.feed = self  # Viewer: download label; more() is a no-op
        self.status = QLabel(f"{title}: images appear here as they are saved.")
        row = QHBoxLayout()
        row.addWidget(self.status, 1)
        if cancel is not None:
            self.cancel_btn = QPushButton("Cancel", clicked=lambda: (cancel.set(), log.info("bulk '%s' cancel pressed", label),
                                                                     self.status.setText(f"{title}: stopping after the current files…")))
            row.addWidget(self.cancel_btn)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addLayout(row)
        lay.addWidget(self.grid, 1)

    def more(self):
        pass

    def landed(self, post, paths):
        """From a worker thread: hand the saved post to the UI thread."""
        to_ui(lambda *_: self.add(post, paths))

    def add(self, post, paths):
        paths = [Path(p) for p in paths]
        if not paths or not all(p.exists() for p in paths):
            return
        uris = [p.as_uri() for p in paths]
        # the tile points at the saved files (thumbnail, viewer, mpv, favorite all read them locally);
        # videos keep the site's poster image as their still
        local = {**post, "files": uris, "samples": post["samples"] if post["video"] else uris,
                 "thumb": post["thumb"] if post["video"] else uris[0]}
        try:
            self.grid.add([local])
        except RuntimeError:  # tab closed while the run goes on
            return
        self.status.setText(f"{self.title}: {self.grid.count()} saved so far (newest last) · in {paths[0].parent}")

    def finished(self, text):
        self.status.setText(f"{self.title}: {text}")
        if self.cancel is not None:
            self.cancel_btn.hide()
