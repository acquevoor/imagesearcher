"""The Boorus section: every configured booru site (any engine in boorus.py) searched together."""
import html
import webbrowser

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFormLayout, QHBoxLayout, QLabel, QLineEdit,
    QListWidget, QListWidgetItem, QMessageBox, QPushButton, QScrollArea, QSplitter, QTreeWidget, QTreeWidgetItem,
    QVBoxLayout, QWidget,
)

from . import boorus, bridge
from .boorus import BooruSource, PixivMatchSource
from .core import CAT_COLOR, CJK, PRESETS, SITE_FIELDS, fetch_all, log, rating_ok, run, save_conf
from .widgets import Feed, Grid, Section, link, rich_label, unlink


class BooruSection(Section):
    def __init__(self, main):
        super().__init__(main, "tags…   Enter = search   (-tag excludes; each site's own syntax works)")
        self.sitebar = QHBoxLayout()
        self.bar.addLayout(self.sitebar)
        self.bar.addWidget(self.q, 1)
        for w in (QPushButton("Search", clicked=lambda: self.search()), self.saved, self.star,
                  QPushButton("⬇ Bulk", toolTip="Download every result of the current tab", clicked=lambda: main.bulk(self.current_feed())),
                  QPushButton("Sites…", toolTip="Add, edit, order booru sites", clicked=self.edit_sites)):
            self.bar.addWidget(w)
        self.reload_sites()
        self.tags_tab = self.add_fixed(TagsTab(self), "Tags", grid=False)
        self.new = NewFeed(self)
        self.add_fixed(self.new, "New")
        self.favs = Grid(self.open_image)
        self.add_fixed(self.favs, "Favorites")
        self.refresh_favs()

    # --- sites ---
    def reload_sites(self):
        self.engines = boorus.engines(self.conf)
        while self.sitebar.count():
            self.sitebar.takeAt(0).widget().setParent(None)  # gone now, not at the next event loop turn
        for e in self.engines:
            cb = QCheckBox(e.short, checked=e.site["on"], toolTip=f"{e.name} — {e.url} ({boorus.ENGINE_NAMES[e.site['engine']]})")
            cb.toggled.connect(lambda on, s=e.site: (s.update(on=on), save_conf(self.conf)))
            self.sitebar.addWidget(cb)

    def ticked(self):
        return [e for e in self.engines if e.site["on"]] or self.engines[:1]

    def first(self, feature, engines=None):
        """First site (ticked ones first) that has feature, else None."""
        return next((e for e in (engines or self.ticked()) + self.engines if e.has(feature)), None)

    def edit_sites(self):
        if SitesDialog(self).exec():
            save_conf(self.conf)
            self.reload_sites()
            self.tags_tab.reload_sites()
            log.info("sites: %s", ", ".join(s["id"] for s in self.conf["boorus"]))

    # --- opening tabs ---
    def search(self, q=None):
        q = (self.q.text() if q is None else q).strip()
        self.q.setText(q)
        log.info("booru search '%s'", q)
        self.open_tag(q)  # every search gets its own top tab, its images open as tabs under it

    def open_tag(self, tag, cat=""):
        if cat == "artist":
            return self.open_artist(tag)
        if CJK.search(tag):  # a Pixiv-style tag: open the booru tag whose wiki lists it
            engines = self.engines
            return run(lambda: bridge.booru_tag(engines, tag), lambda t, e: self.open_tag(t) if t else
                       self.main.statusBar().showMessage(f"No booru wiki lists '{tag}'" + (f": {e}" if e else "")))
        self.open_tab(f"tag:{tag}", tag or "all posts", lambda: TagView(self, tag))

    def open_artist(self, tag):
        self.open_tab(f"artist:{tag}", f"🎨 {tag}", lambda: ArtistView(self, tag))

    def open_pool(self, engine, pid, name):
        self.open_tab(f"pool:{engine.id}:{pid}", f"▤ {name}", lambda: TagView(self, engine.pool_query(pid), [engine], name.replace("_", " ")))

    def find_pixiv(self, pid):
        """Bridge: the boorus' copies of a Pixiv work."""
        engines = [e for e in self.ticked() if e.has("find_pixiv")] or [e for e in self.engines if e.has("find_pixiv")]
        self.main.show_section(self)
        self.open_feed(f"pixiv:{pid}", f"PX {pid} on boorus", [PixivMatchSource(e, pid) for e in engines])

    def link(self, href, tag=""):
        kind, cat, text = unlink(href)
        if kind == "px":  # a wiki's Japanese name: search it on Pixiv
            self.main.pixiv.search(text)
        elif kind == "tag":
            self.open_tag(text, cat)

    # --- search box / image tabs ---
    def complete(self, word):
        e = self.first("complete")
        return e.complete(word) if e else []

    def post_actions(self, v):
        p, out = v.p, []
        if p.get("pixiv_id"):
            out.append(("Open on Pixiv (web)", lambda: webbrowser.open(f"https://www.pixiv.net/artworks/{p['pixiv_id']}")))
            if self.conf["pixiv"]["refresh_token"]:
                out.append(("Pixiv: more like this", lambda: self.main.pixiv.open_related(p["pixiv_id"])))
        return out

    def tag_actions(self, tag, cat):
        q = self.q.text().strip()
        out = [("Search", lambda: self.search(tag)), ("Add to query", lambda: self.search(f"{q} {tag}".strip())),
               ("Exclude from query", lambda: self.search(f"{q} -{tag}".strip())),
               ("Follow", lambda: self.toggle_follow(("artist:" if cat == "artist" else "tag:") + tag))]
        if self.conf["pixiv"]["refresh_token"]:
            out.append(("Search on Pixiv", lambda: self.pixiv_search(tag)))
        return out

    def pixiv_search(self, q):
        """Bridge: a booru query -> its Pixiv words (wiki other names) -> a Pixiv search tab."""
        conf, e = self.conf, bridge.wiki_engine(self.engines)
        run(lambda: bridge.pixiv_word(conf, e, q), lambda w, err: w and self.main.pixiv.search(w))

    # --- favorites / follows ---
    def refresh_favs(self):
        favs = [p for p in self.conf["favorites"] if p["site"] != "pixiv"]
        self.favs.restart()
        self.favs.add(favs)
        self.tabs.setTabText(self.tabs.indexOf(self.favs.imgtabs), f"Favorites ({len(favs)})")

    def toggle_follow(self, key):
        """Follow / unfollow "tag:x" or "artist:x" on the ticked sites -> now following? Starts at the newest post."""
        f = self.conf["follows"]
        if key in f:
            del f[key]
            save_conf(self.conf)
            log.info("unfollow %s", key)
            self.new.reload()
            return False
        tag = key.split(":", 1)[1]
        srcs = [BooruSource(e, tag) for e in self.ticked()]
        f[key] = {}
        log.info("follow %s", key)

        def seed():
            lists, errs = fetch_all(srcs, 1)
            return {s.engine.id: max((int(p["id"]) for p in l), default=0) for s, l in lists.items() if s not in errs}

        def done(r, e):
            if key in f:
                f[key].update(r or {})
                save_conf(self.conf)
                self.new.reload()
            if e or not r:
                self.main.statusBar().showMessage(f"Following {key}, but no site answered: {e or 'check the site keys'}")
        run(seed, done)
        return True


class TagView(Feed):
    """One search / tag across the ticked sites: wiki header + related tags from the metadata site, sort, ratings."""

    def __init__(self, section, tag, engines=None, title=None):
        super().__init__(section, title or tag or "all posts")
        self.base = self.tag = tag
        self.fixed_engines, self.info, self.count = engines, None, None
        self.query = tag if not engines else None  # the search box shows it (not for pools)
        self.head = rich_label()
        self.head.linkActivated.connect(self.link)
        self.sort = QComboBox()
        self.sort.addItems(["Newest", "Score", "Random"])
        self.rate = {r: QCheckBox(r, checked=True, toolTip="rating filter") for r in "gsqe"}
        self.follow = QPushButton(clicked=self.toggle_follow, enabled=bool(tag) and not engines)
        self.row = QHBoxLayout()
        for w in (self.sort, QLabel("rating:"), *self.rate.values()):
            self.row.addWidget(w)
        self.row.addStretch()
        self.row.addWidget(self.follow)
        if section.conf["pixiv"]["refresh_token"] and tag and not engines:
            self.row.addWidget(QPushButton("Search on Pixiv", toolTip="Translate the tags through the wiki and search Pixiv",
                                           clicked=lambda: section.pixiv_search(self.tag)))
        scroll = QScrollArea(widgetResizable=True, maximumHeight=220)
        scroll.setWidget(self.head)
        self.lay.insertWidget(0, scroll)
        self.lay.insertLayout(1, self.row)
        self.sort.currentIndexChanged.connect(self.reload)
        for c in self.rate.values():
            c.toggled.connect(self.reload)
        self.render()
        self.reload()
        meta, counter = section.first("wiki"), section.first("count", engines)
        if tag and not engines and (meta or counter):
            run(lambda: (meta and meta.wiki(tag), meta and meta.has("related") and meta.related(tag), counter and counter.count(tag),
                         counter and counter.name), self.got_head)

    def engines(self):
        return self.fixed_engines or self.section.ticked()

    def reload(self):
        sort = self.sort.currentText()
        engines = self.engines()
        unsorted = [e.short for e in engines if sort != "Newest" and sort not in e.SORT]
        self.note = f"  ({', '.join(unsorted)} can't sort by {sort.lower()})" if unsorted else ""
        ratings = {r for r, c in self.rate.items() if c.isChecked()}
        self.keep = None if len(ratings) == 4 else lambda p: rating_ok(p, ratings)
        self.follow.setText("★ Following" if f"tag:{self.base}" in self.section.conf["follows"] else "☆ Follow")
        self.start([BooruSource(e, self.tag, sort if sort in e.SORT else "") for e in engines])

    def got_head(self, res, err):
        if not err:
            wiki, related, self.count, self.count_site = res
            self.info = {"body": (wiki or {}).get("body", ""), "names": [n for n in (wiki or {}).get("names", []) if CJK.search(n)][:8],
                         "related": related or []}
            self.render()

    def render(self):
        out = f"<h3>{html.escape(self.label)}</h3>"
        extra = self.tag[len(self.base):].strip()
        if extra:
            out += f"<p>narrowed by <b>{html.escape(extra)}</b> · {link('reset', '', label='⟲ reset')}</p>"
        if self.count is not None:
            out += f"<p>{self.count:,} posts on {html.escape(self.count_site)}</p>"
        out += tag_html(self.info) if self.info else ""
        self.head.setText(out)

    def link(self, href):
        kind, cat, text = unlink(href)
        ctrl = QApplication.keyboardModifiers() & Qt.ControlModifier
        if kind == "reset":
            self.tag = self.base
        elif kind == "tag" and not ctrl and self.base and not self.fixed_engines:  # narrow; Ctrl+click opens its own tab
            self.tag = f"{self.tag} {text}"
        else:
            return self.section.link(href, self.base)
        self.render()
        self.reload()

    def toggle_follow(self):
        on = self.section.toggle_follow(f"tag:{self.base}")
        self.follow.setText("★ Following" if on else "☆ Follow")


def tag_html(info):
    out = ""
    if info.get("names"):
        out += "<p><b>Other names</b> (click = search on Pixiv): " + " · ".join(link("px", n) for n in info["names"]) + "</p>"
    if info.get("body"):
        out += f"<p>{html.escape(info['body'])}</p>"
    if info.get("related"):
        out += "<p><b>Related:</b> " + " ".join(link("tag", t, c) for t, c in info["related"]) + "</p>"
    return out


class ArtistView(TagView):
    """An artist tag: their posts on the ticked sites + the artist card (other names, links) from a site that has one."""

    def __init__(self, section, tag):
        self.card = None
        super().__init__(section, tag)
        self.label = tag
        self.follow.setText("★ Following" if f"artist:{tag}" in section.conf["follows"] else "☆ Follow")
        self.px_btn = QPushButton("Pixiv account", visible=False)
        self.row.addWidget(self.px_btn)
        e = section.first("artist")
        if e:
            run(lambda: e.artist(tag), self.got_card)

    def got_card(self, a, err):
        self.card = a or {"names": [], "urls": [], "none": err or "no artist entry"}
        uid = bridge.pixiv_uid(self.card["urls"])
        if uid and self.section.conf["pixiv"]["refresh_token"]:  # bridge to the Pixiv section
            self.px_btn.clicked.connect(lambda: self.section.main.pixiv.open_user(uid))
            self.px_btn.setVisible(True)
        self.render()

    def render(self):
        super().render()
        a = getattr(self, "card", None)
        if a:
            names = "<p>aka " + ", ".join(map(html.escape, a["names"])) + "</p>" if a["names"] else ""
            urls = " · ".join(f"<a href='{html.escape(u)}'>{html.escape(u.split('/')[2] if '//' in u else u)}</a>" for u in a["urls"])
            self.head.setText(self.head.text().replace("</h3>", "</h3>" + names + (f"<p>{urls}</p>" if urls else ""), 1))

    def link(self, href):
        if href.startswith("http"):
            return webbrowser.open(href)
        super().link(href)

    def toggle_follow(self):
        on = self.section.toggle_follow(f"artist:{self.base}")
        self.follow.setText("★ Following" if on else "☆ Follow")


def follow_srcs(follows, engines):
    """{key: {site id: last id}} -> {BooruSource: (key, site id)} for posts newer than the last one seen."""
    by_id, out = {e.id: e for e in engines}, {}
    for key, f in follows.items():
        tag = key.split(":", 1)[1]
        for sid, last in f.items():
            if sid in by_id:
                e = by_id[sid]
                out[BooruSource(e, e.join(tag, e.id_after(last)))] = key, sid
    return out


class NewFeed(Feed):
    """Posts newer than the last one seen, for every followed tag and artist, on the sites they were followed on."""

    def __init__(self, section):
        super().__init__(section, "New")
        self.keys, self.newest = {}, {}
        self.head = rich_label()
        self.head.linkActivated.connect(self.link)
        row = QHBoxLayout()
        row.addWidget(self.head, 1)
        for w in (QPushButton("⟳ Refresh", clicked=self.reload), QPushButton("✓ Mark all seen", clicked=self.mark_seen)):
            row.addWidget(w)
        self.lay.insertLayout(0, row)
        # ponytail: checks only while the app runs; no background service
        QTimer(self, interval=15 * 60 * 1000, timeout=lambda: self.isVisible() or self.reload()).start()
        QTimer.singleShot(0, self.reload)  # once the section is built

    def reload(self):
        f = self.section.conf["follows"]
        self.keys, self.newest = follow_srcs(f, self.section.engines), {}
        self.head.setText("Following: " + "  ".join(f"{link('open', k, label=k.split(':', 1)[1])} {link('unf', k, label='×')}" for k in f)
                          if f else "Follow tags or artists (☆ in their tabs) to collect their new posts here.")
        self.start(list(self.keys))
        self.count("")

    def count(self, n):
        tabs = self.section.tabs
        tabs.setTabText(tabs.indexOf(self.imgtabs), f"New ({n})" if n else "New")

    def loaded(self, lists):
        for src, posts in lists.items():
            if posts:
                self.newest[src] = max(self.newest.get(src, 0), *(int(p["id"]) for p in posts))
        n = self.grid.count()
        self.count(f"{n}{'+' if self.active else ''}" if n else "")

    def mark_seen(self):
        f = self.section.conf["follows"]
        for src, top in self.newest.items():
            key, sid = self.keys[src]
            if key in f:
                f[key][sid] = max(f[key].get(sid, 0), top)
        save_conf(self.section.conf)
        log.info("new: marked seen")
        self.reload()

    def link(self, href):
        kind, _, key = unlink(href)
        if kind == "unf":
            self.section.toggle_follow(key)
        else:
            what, name = key.split(":", 1)
            self.section.open_artist(name) if what == "artist" else self.section.open_tag(name)


class TagsTab(QWidget):
    """Browse one site's tags (by category, count, name, date) or pools; double-click opens a tag tab."""

    def __init__(self, section):
        super().__init__()
        self.section, self.cur, self.page, self.gen, self.loading, self.done = section, None, 0, 0, False, False
        self.q = QLineEdit(placeholderText="filter tags…   (* = wildcard; empty = most used)")
        self.mode, self.order, self.site = QComboBox(), QComboBox(), QComboBox()
        self.order.addItems(boorus.ORDERS)
        self.timer = QTimer(self, singleShot=True, interval=300, timeout=self.reload)
        self.q.textEdited.connect(lambda: self.timer.start())
        top = QHBoxLayout()
        for w in (self.q, self.site, self.mode, self.order):
            top.addWidget(w, int(w is self.q))

        self.list = QTreeWidget(rootIsDecorated=False, uniformRowHeights=True)
        self.list.setHeaderLabels(["Tag", "Posts", "Pixiv"])
        self.list.setColumnWidth(0, 300)
        self.list.currentItemChanged.connect(lambda it, _: self.detail(it))
        self.list.itemActivated.connect(lambda it, _: self.open(it))
        sb = self.list.verticalScrollBar()
        sb.valueChanged.connect(lambda v: v >= sb.maximum() - 50 and self.more())

        self.info = rich_label()
        self.info.linkActivated.connect(lambda h: section.link(h, self.cur and self.cur["name"]))
        scroll = QScrollArea(widgetResizable=True)
        scroll.setWidget(self.info)
        self.follow = QPushButton("☆ Follow", clicked=self.toggle_follow)
        btns = QHBoxLayout()
        for w in (QPushButton("Open", clicked=lambda: self.open()), self.follow,
                  QPushButton("Blacklist", clicked=lambda: self.cur and section.main.blacklist_add(self.cur["name"])),
                  QPushButton("Copy", clicked=lambda: self.cur and QApplication.clipboard().setText(self.cur["name"]))):
            btns.addWidget(w)
        right = QWidget()
        rl = QVBoxLayout(right)
        rl.addWidget(scroll, 1)
        rl.addLayout(btns)
        split = QSplitter()
        split.addWidget(self.list)
        split.addWidget(right)
        split.setSizes([600, 500])
        lay = QVBoxLayout(self)
        lay.addLayout(top)
        lay.addWidget(split, 1)
        self.site.currentIndexChanged.connect(self.site_changed)
        for w in (self.mode, self.order):
            w.currentIndexChanged.connect(self.reload)
        self.reload_sites()

    def engine(self):
        return next((e for e in self.section.engines if e.id == self.site.currentData()), None)

    def reload_sites(self):
        self.site.blockSignals(True)
        self.site.clear()
        for e in self.section.engines:
            if e.has("tags"):
                self.site.addItem(e.name, e.id)
        self.site.blockSignals(False)
        self.site_changed()

    def site_changed(self):
        e = self.engine()
        self.mode.blockSignals(True)
        self.mode.clear()
        self.mode.addItems(["All"] + [c.capitalize() for c in dict.fromkeys(e.CATS.values())] if e else ["All"])
        if e and e.has("pools"):
            self.mode.addItem("Pools")
        self.mode.blockSignals(False)
        self.reload()

    def reload(self):
        self.gen += 1
        self.page, self.done, self.loading = 0, False, False
        self.list.clear()
        self.more()

    def more(self):
        e = self.engine()
        if self.loading or self.done or not e:
            return
        self.loading = True
        self.page += 1
        word, mode, order, page, gen = self.q.text().strip(), self.mode.currentText(), self.order.currentText(), self.page, self.gen
        pat = word if "*" in word else f"*{word}*" if word else ""
        if mode == "Pools":
            fn = lambda: [{**r, "engine": e} for r in e.pools(pat, order, page)]  # noqa: E731
        else:
            fn = lambda: e.tags(pat, None if mode == "All" else mode.lower(), order, page)  # noqa: E731
        run(fn, lambda r, err: self.got(gen, r, err))

    def got(self, gen, rows, err):
        if gen != self.gen:
            return
        self.loading = False
        if err:
            self.done = True
            return self.info.setText(f"<i>{html.escape(str(err))}</i>")
        self.done = not rows
        items = []
        for r in rows:
            it = QTreeWidgetItem([r["name"], f"{r['count']:,}", ""])
            it.setData(0, Qt.UserRole, r)
            if r["cat"] in CAT_COLOR:
                it.setForeground(0, QColor(CAT_COLOR[r["cat"]]))
            it.setTextAlignment(1, Qt.AlignRight | Qt.AlignVCenter)
            items.append(it)
        self.list.addTopLevelItems(items)
        names, conf, we = [r["name"] for r in rows if r["cat"] != "pool"], self.section.conf, bridge.wiki_engine(self.section.engines)
        if names and we:  # the Pixiv column, from the wikis
            run(lambda: bridge.pixiv_names(conf, we, names),
                lambda m, e: gen == self.gen and [it.setText(2, conf["pixiv_map"].get(it.text(0), "")) for it in items])
        if not self.done and self.list.verticalScrollBar().maximum() == 0:
            QTimer.singleShot(0, self.more)

    def detail(self, it):
        r = self.cur = it and it.data(0, Qt.UserRole)
        if not r:
            return
        head = f"<h2 style='color:{CAT_COLOR.get(r['cat'], '')}'>{html.escape(r['name'])}</h2>{r['count']:,} posts · {r['cat']}"
        self.follow.setEnabled(r["cat"] != "pool")
        self.follow.setText("★ Following" if self.key(r) in self.section.conf["follows"] else "☆ Follow")
        if r["cat"] == "pool":
            return self.info.setText(f"{head}<p>{html.escape(r['desc'])}</p>")
        e = self.engine() if self.engine().has("wiki") else self.section.first("wiki")
        if not e:
            return self.info.setText(head)
        self.info.setText(head + "<p><i>loading wiki…</i></p>")
        name = r["name"]
        run(lambda: {**(e.wiki(name) or {}), "related": e.related(name) if e.has("related") else []},
            lambda res, err: self.cur is r and self.info.setText(head + (f"<p><i>{html.escape(str(err))}</i></p>" if err else tag_html(
                {**res, "names": [n for n in res.get("names", []) if CJK.search(n)][:8]}))))

    def open(self, it=None):
        r = it.data(0, Qt.UserRole) if it else self.cur
        if r and r["cat"] == "pool":
            self.section.open_pool(r["engine"], r["id"], r["name"])
        elif r:
            self.section.open_tag(r["name"], r["cat"])

    @staticmethod
    def key(r):
        return ("artist:" if r["cat"] == "artist" else "tag:") + r["name"]

    def toggle_follow(self):
        if self.cur and self.cur["cat"] != "pool":
            on = self.section.toggle_follow(self.key(self.cur))
            self.follow.setText("★ Following" if on else "☆ Follow")


class SitesDialog(QDialog):
    """Add (from a preset or any URL, engine auto-detected), edit, remove and order the booru sites."""

    def __init__(self, section):
        super().__init__(section)
        self.setWindowTitle("Booru sites")
        self.resize(560, 420)
        self.conf = section.conf
        self.sites = [dict(s) for s in self.conf["boorus"]]
        self.list = QListWidget()
        self.list.itemDoubleClicked.connect(lambda _: self.edit())
        btns = QVBoxLayout()
        for text, fn in (("Add…", self.add), ("Edit…", self.edit), ("Remove", self.remove), ("↑ Up", lambda: self.move(-1)),
                         ("↓ Down", lambda: self.move(1))):
            btns.addWidget(QPushButton(text, clicked=fn))
        btns.addStretch()
        row = QHBoxLayout()
        row.addWidget(self.list, 1)
        row.addLayout(btns)
        lay = QVBoxLayout(self)
        lay.addWidget(QLabel("Ticked sites are searched together, in this order. The first one with a wiki names tags for Pixiv."))
        lay.addLayout(row)
        lay.addWidget(QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel, accepted=self.save, rejected=self.reject))
        self.fill()

    def fill(self, select=None):
        self.list.clear()
        for s in self.sites:
            it = QListWidgetItem(f"{s['name']}  [{s['short']}]  —  {s['url']}  ({boorus.ENGINE_NAMES[s['engine']]})")
            it.setFlags(it.flags() | Qt.ItemIsUserCheckable)
            it.setCheckState(Qt.Checked if s["on"] else Qt.Unchecked)
            self.list.addItem(it)
        if select is not None:
            self.list.setCurrentRow(select)

    def sync_checks(self):
        for i, s in enumerate(self.sites):
            s["on"] = self.list.item(i).checkState() == Qt.Checked

    def add(self):
        self.sync_checks()
        d = SiteDialog(self, None, self.conf, [s["id"] for s in self.sites])
        if d.exec():
            self.sites.append(d.site)
            self.fill(len(self.sites) - 1)

    def edit(self):
        i = self.list.currentRow()
        if i < 0:
            return
        self.sync_checks()
        d = SiteDialog(self, self.sites[i], self.conf, [])
        if d.exec():
            self.sites[i] = d.site
            self.fill(i)

    def remove(self):
        i = self.list.currentRow()
        if i >= 0 and len(self.sites) > 1:
            self.sync_checks()
            del self.sites[i]
            self.fill(min(i, len(self.sites) - 1))

    def move(self, d):
        i = self.list.currentRow()
        if 0 <= i + d < len(self.sites) and i >= 0:
            self.sync_checks()
            self.sites[i], self.sites[i + d] = self.sites[i + d], self.sites[i]
            self.fill(i + d)

    def save(self):
        self.sync_checks()
        self.conf["boorus"] = self.sites
        self.accept()


class SiteDialog(QDialog):
    """One site: pick a preset or paste any URL and press Detect; keys are optional on most sites."""

    def __init__(self, parent, site, conf, taken):
        super().__init__(parent)
        self.setWindowTitle("Edit site" if site else "Add site")
        self.resize(520, 0)
        self.conf, self.site, self.taken = conf, dict(site or SITE_FIELDS), taken
        form = QFormLayout(self)
        if not site:
            self.preset = QComboBox()
            self.preset.addItem("Custom URL…", None)
            for p in PRESETS:
                if p["id"] not in taken:
                    self.preset.addItem(f"{p['name']} ({p['url'].split('//')[1]})", p)
            self.preset.currentIndexChanged.connect(self.use_preset)
            form.addRow("Preset", self.preset)
        self.url = QLineEdit(self.site["url"], placeholderText="https://example-booru.net")
        detect = QPushButton("Detect", clicked=self.detect)
        r = QHBoxLayout()
        r.addWidget(self.url, 1)
        r.addWidget(detect)
        form.addRow("URL", r)
        self.engine = QComboBox()
        for k, v in boorus.ENGINE_NAMES.items():
            self.engine.addItem(v, k)
        self.engine.setCurrentIndex(max(self.engine.findData(self.site["engine"]), 0))
        form.addRow("Engine", self.engine)
        self.f = {}
        for key, label, tip in (("name", "Name", ""), ("short", "Tile label", "2–4 letters shown on thumbnails"),
                                ("api_url", "API URL", "only if the API lives elsewhere (rule34: https://api.rule34.xxx)"),
                                ("login", "Login / user id", "Gelbooru-family: your numeric user id"),
                                ("key", "API key", "Moebooru: password hash; Philomena: your key"),
                                ("filter_id", "Filter id", "Philomena only: the filter to search with (empty = site default)")):
            self.f[key] = QLineEdit(self.site[key], toolTip=tip, placeholderText=tip)
            if key == "key":
                self.f[key].setEchoMode(QLineEdit.PasswordEchoOnEdit)
            form.addRow(label, self.f[key])
        self.status = QLabel(wordWrap=True)
        form.addRow(self.status)
        form.addRow(QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel, accepted=self.ok, rejected=self.reject))

    def use_preset(self):
        p = self.preset.currentData()
        if p:
            self.site = {**SITE_FIELDS, **p, "on": True}
            self.url.setText(p["url"])
            self.engine.setCurrentIndex(self.engine.findData(p["engine"]))
            for k, e in self.f.items():
                e.setText(self.site.get(k, ""))

    def detect(self):
        url = self.url.text().strip()
        self.status.setText("probing…")

        def done(res, err):
            if err:
                return self.status.setText(f"<i>{html.escape(str(err))}</i>")
            eng, base, api = res
            self.url.setText(base)
            self.engine.setCurrentIndex(self.engine.findData(eng))
            self.f["api_url"].setText(api)
            if not self.f["name"].text():
                host = base.split("//")[1]
                self.f["name"].setText(host.removeprefix("www.").split(".")[0].capitalize())
            self.status.setText(f"found a {boorus.ENGINE_NAMES[eng]} API" + (f" at {api}" if api else ""))
        run(lambda: boorus.detect(url), done)

    def ok(self):
        url, name = self.url.text().strip().rstrip("/"), self.f["name"].text().strip()
        if not url.startswith("http") or not name:
            return QMessageBox.warning(self, "Site", "Needs a name and an http(s) URL (Detect fills both).")
        fields = {k: e.text().strip() for k, e in self.f.items()}
        if not self.site.get("id"):  # new: unique id + label
            fresh = boorus.new_site({"boorus": [{"id": i} for i in self.taken]}, name, self.engine.currentData(), url, fields["api_url"])
            self.site = {**fresh, **{k: v for k, v in fields.items() if v}, "id": self.site.get("id") or fresh["id"]}
            if self.site["id"] in self.taken:
                self.site["id"] = fresh["id"]
        else:
            self.site.update(fields)
        self.site.update(url=url, engine=self.engine.currentData(), short=self.site["short"] or name[:3].upper())
        self.accept()
