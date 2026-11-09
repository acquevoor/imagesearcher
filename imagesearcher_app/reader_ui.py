"""The Reader section: search a tag or character, pick a hit on the left (Pixiv summary, booru wikis, Fandom articles),
read it on the right. Links inside an article stay in the tab, with Back / Forward."""
import html
import urllib.parse
import webbrowser

from PySide6.QtCore import Qt
from PySide6.QtGui import QPixmap, QTextDocument
from PySide6.QtWidgets import (
    QHBoxLayout, QLabel, QListWidget, QListWidgetItem, QPushButton, QSplitter, QTextBrowser, QVBoxLayout, QWidget,
)

from . import reader as rd
from .core import log, run
from .widgets import Section, middle_click

START = ("<h2>Reader</h2><p>Type a tag or a character in the box above, in any form: <code>hakurei_reimu</code>, "
         "<code>Reimu Hakurei</code> or <code>初音ミク</code>. A tab opens with what the wikis have: the Pixiv encyclopedia's "
         "summary, booru wikis and Fandom articles.</p><p>From any image, right-click a tag → <b>📖 Read in Reader</b>.</p>")


RANK = {"pixiv": 0, "booru": 1, "fandom": 2}


class ReaderSection(Section):
    SAVED = "rd_saved"

    def __init__(self, main):
        super().__init__(main, "tag or character…   Enter = read   (hakurei_reimu, Reimu Hakurei, 初音ミク)")
        self.bar.addWidget(self.q, 1)
        for w in (QPushButton("Search", clicked=lambda: self.search()), self.saved, self.star):
            self.bar.addWidget(w)
        start = QTextBrowser()
        start.setHtml(START)
        self.add_fixed(start, "Start", grid=False)

    def search(self, q=None):
        q = (self.q.text() if q is None else q).strip()
        if not q:
            return
        self.q.setText(q)
        self.main.show_section(self)
        log.info("reader: read '%s'", q)
        view = self.open_tab(f"read:{q.lower()}", f"📖 {q}", lambda: ReadView(self, q))
        view.imgtabs.setTabText(0, "Article")
        view.query = q

    def open_tag(self, tag, cat=""):
        self.search(tag)


class Article(QTextBrowser):
    """Shows an entry's HTML; images come from the dict filled by reader.load_images (Qt won't fetch them itself)."""

    def __init__(self):
        super().__init__(openLinks=False)
        self.images = {}
        self.document().setDefaultStyleSheet("img {margin: 6px} table {margin: 6px 0}")

    def loadResource(self, kind, url):
        if kind == QTextDocument.ImageResource and url.toString() in self.images:
            return QPixmap.fromImage(self.images[url.toString()])
        return super().loadResource(kind, url)

    def show_html(self, body, images=None, keep_scroll=False):
        pos = self.verticalScrollBar().value()
        self.images = images or {}
        self.setHtml(body)
        self.verticalScrollBar().setValue(pos if keep_scroll else 0)


class ReadView(QWidget):
    def __init__(self, section, q):
        super().__init__()
        self.section, self.q, self.hist, self.pos, self.gen, self.pending, self.found = section, q, [], -1, 0, 0, 0
        self.fails, self.nothing, self.image_note, self.auto = [], [], "", True  # auto: you haven't picked anything yet
        self.hits = QListWidget(wordWrap=True)
        self.hits.itemClicked.connect(lambda it: self.pick(it.data(Qt.UserRole)))
        self.status = QLabel("Looking…", wordWrap=True)
        self.view = Article()
        self.view.anchorClicked.connect(self.link)
        middle_click(self.view)
        self.back, self.fwd = QPushButton("◀", enabled=False, clicked=lambda: self.go(-1)), QPushButton("▶", enabled=False, clicked=lambda: self.go(1))
        self.site = QPushButton("Open on site", enabled=False, clicked=lambda: webbrowser.open(self.current()["url"]))
        self.full = QPushButton("Full article", visible=False, toolTip="The whole encyclopedia page, in your browser",
                                clicked=lambda: webbrowser.open(self.current()["url"]))
        left = QWidget()
        ll = QVBoxLayout(left)
        ll.setContentsMargins(0, 0, 0, 0)
        ll.addWidget(self.hits, 1)
        ll.addWidget(self.status)
        row = QHBoxLayout()
        for w in (self.back, self.fwd, self.site, self.full):
            row.addWidget(w)
        row.addStretch()
        right = QWidget()
        rl = QVBoxLayout(right)
        rl.setContentsMargins(0, 0, 0, 0)
        rl.addLayout(row)
        rl.addWidget(self.view, 1)
        split = QSplitter()
        split.addWidget(left)
        split.addWidget(right)
        split.setSizes([320, 1000])
        QVBoxLayout(self).addWidget(split)
        conf, engines = section.conf, section.main.boorus.engines
        run(lambda: rd.names(conf, engines, q), self.got_names)

    # --- hits: each source answers on its own, a slow or down one holds nothing up ---
    def got_names(self, n, err):
        if err:
            return self.status.setText(str(err))
        log.info("reader: '%s' -> tag '%s', words '%s', Pixiv '%s'", self.q, n["tag"], n["text"], n["pixiv"])
        boorus = [e for e in self.section.main.boorus.engines if e.has("wiki") and e.site["on"]] if n["tag"] else []
        if n["pixiv"]:
            self.ask("Pixiv", lambda: [rd.pixiv_entry(n["pixiv"])])
        for e in boorus:
            self.ask(e.name, lambda e=e: [rd.booru_entry(e, n["tag"])])
        self.ask("Fandom", lambda: rd.fandom_search(n["text"]))

    def ask(self, name, fn):
        self.pending += 1
        run(fn, lambda res, err: self.answered(name, res, err))

    def answered(self, name, res, err):
        self.pending -= 1
        if err:
            log.warning("reader: %s: %s", name, err)
            self.fails.append(name)
        if not err and not any(res or []) and name != "Fandom":
            self.nothing.append(name)
        for e in filter(None, res or []):
            label = {"pixiv": lambda: f"Pixiv: {e['title']}", "booru": lambda: f"{e['site']} wiki"}.get(
                e["source"], lambda: f"{e['title']}\n{e['wiki']}")()
            it = QListWidgetItem(label)
            it.setData(Qt.UserRole, e)
            it.setToolTip(e.get("snippet") or "")
            row = sum(RANK[self.hits.item(i).data(Qt.UserRole)["source"]] <= RANK[e["source"]] for i in range(self.hits.count()))
            self.hits.insertItem(row, it)  # Pixiv, then booru wikis, then Fandom, whichever answers first
            self.found += 1
            if self.auto and row == 0:  # until you pick something, the best hit so far is the one showing
                self.hist, self.pos = [], -1
                self.show_entry(e)
        self.show_status()

    def show_status(self):
        self.status.setText((f"{self.found} found" if self.found else "Nothing found" if not self.pending else "Looking…")
                            + (" · still looking…" if self.pending and self.found else "")
                            + (f" · no answer from {', '.join(self.fails)}" if self.fails else "")
                            + (f" · nothing from {', '.join(self.nothing)} (no page, or the site is down)" if self.nothing else "")
                            + (f" · images: {self.image_note}" if self.image_note else ""))

    # --- showing an entry ---
    def pick(self, e):
        self.auto = False
        self.show_entry(e)

    def current(self):
        return self.hist[self.pos]

    def show_entry(self, e, push=True):
        if push:
            del self.hist[self.pos + 1:]
            self.hist.append(e)
            self.pos = len(self.hist) - 1
        self.back.setEnabled(self.pos > 0)
        self.fwd.setEnabled(self.pos < len(self.hist) - 1)
        self.site.setEnabled(bool(e.get("url")))
        self.full.setVisible(e["source"] == "pixiv")
        self.site.setVisible(e["source"] != "pixiv")
        self.gen += 1
        gen = self.gen
        log.info("reader: show %s '%s'", e["source"], e["title"])
        if e["source"] == "fandom":
            self.view.show_html(f"<h2>{html.escape(e['title'])}</h2><p><i>loading…</i></p>")
            run(lambda: rd.fandom_article(e["host"], e["page"]), lambda a, err: self.got_article(gen, e, a, err))
        elif e["source"] == "pixiv":
            self.render(gen, rd.pixiv_html(e), [e["image"]] if e["image"] else [])
        else:
            self.render(gen, rd.booru_html(e), [])

    def got_article(self, gen, e, a, err):
        if gen != self.gen:  # moved on to another article
            return
        if err or not a:
            return self.view.show_html(f"<h2>{html.escape(e['title'])}</h2><p>{html.escape(str(err) if err else 'This page is not on that wiki.')}</p>")
        e["url"] = a["url"]
        self.site.setEnabled(True)
        self.render(gen, f"<h2>{html.escape(a['title'])}</h2><p><small>{html.escape(e.get('wiki') or e['host'])}</small></p>{a['html']}", a["images"])

    def render(self, gen, body, images):
        """The text at once; the images follow when they're in (a late answer for another article is dropped)."""
        self.view.show_html(rd.drop_images(body, ()))
        self.image_note = ""
        if not images:
            return self.show_status()

        def done(res, err):
            if gen != self.gen or err:
                return
            imgs, why = res
            self.image_note = why
            self.show_status()
            if imgs:
                self.view.show_html(rd.drop_images(body, imgs), imgs, keep_scroll=True)
        run(lambda: rd.load_images(images), done, "view")

    def go(self, d):
        self.auto = False
        self.pos += d
        self.show_entry(self.current(), push=False)

    def link(self, url):
        u = url.toString()
        self.auto = False
        if url.scheme() == "wiki":
            page = urllib.parse.unquote(url.path(url.ComponentFormattingOption.FullyEncoded)[1:])
            self.show_entry({"source": "fandom", "title": page.replace("_", " "), "host": url.host(), "page": page, "wiki": url.host(),
                             "url": f"https://{url.host()}/wiki/{urllib.parse.quote(page)}"})
        elif url.scheme() == "read":
            self.section.search(urllib.parse.unquote(url.path(url.ComponentFormattingOption.FullyEncoded)[1:]))
        elif u.startswith("#") or url.hasFragment() and not url.host():
            self.view.scrollToAnchor(url.fragment())
        else:
            webbrowser.open(u)
