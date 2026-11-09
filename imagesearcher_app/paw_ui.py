"""The Pawchive section: Recent (+ search), Popular, Artists, Tags, Favorites (app) and Account (synced
favorites), creator tabs (posts, tags, links, announcements, fancards, bulk), post details (text, other
attachments, comments, revisions, prev / next), random post / artist, find by file hash, flag."""
import html
import threading
import webbrowser

from PySide6.QtCore import QDate, Qt, QTimer
from PySide6.QtWidgets import (
    QApplication, QComboBox, QDateEdit, QFileDialog, QHBoxLayout, QInputDialog, QLabel, QLineEdit, QMessageBox,
    QPushButton, QTextBrowser, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget,
)

from . import pawchive as pw
from .core import log, run, save_conf
from .widgets import Favorites, Feed, LiveBulk, Section, middle_click, rich_label


class PawSection(Section):
    SAVED = "paw_saved"

    def __init__(self, main):
        super().__init__(main, "search Pawchive posts…   Enter = search   (@name = open that artist)")
        self.bar.addWidget(self.q, 1)
        self.paste_btn = QPushButton("📋 Paste cookies", toolTip="Log in: your pawchive.pw cookies, copied with the Flag Cookies "
                                     "browser extension (only the 'session' cookie is kept)", clicked=self.paste_cookies)
        for w in (QPushButton("Search", clicked=lambda: self.search()), self.saved, self.star,
                  QPushButton("🎲 Post", toolTip="Random post", clicked=lambda: self.random("posts")),
                  QPushButton("🎲 Artist", toolTip="Random artist", clicked=lambda: self.random("artists")),
                  QPushButton("🔍 Find by hash…", toolTip="Which Pawchive posts hold these files (SHA-256)", clicked=self.hash_files),
                  QPushButton("Importer", toolTip="Pawchive's importer page in your browser (you submit your own "
                              "Patreon/Fanbox keys there; the app never handles them)", clicked=lambda: webbrowser.open(f"{pw.site(self.conf)}/importer")),
                  self.paste_btn):
            self.bar.addWidget(w)
        self.paste_btn.setVisible(not self.conf["paw"]["session"])
        self.add_fixed(LazyFeed(self, "Recent", lambda: [pw.RecentSource(self.conf)]), "Recent")
        self.popular = Popular(self)
        self.add_fixed(self.popular, "Popular")
        self.artists = Artists(self)
        self.add_fixed(self.artists, "Artists", grid=False)
        self.tags_tab = Tags(self)
        self.add_fixed(self.tags_tab, "Tags", grid=False)
        self.favs = Favorites(self, lambda p: p["site"] == "paw")
        self.add_fixed(self.favs, "Favorites")
        self.account = Account(self)
        self.add_fixed(self.account, "Account")
        self.refresh_favs()

    # --- search / tabs ---
    def search(self, q=None):
        q = (self.q.text() if q is None else q).strip()
        self.main.show_section(self)
        if q.startswith("@"):
            return self.find_artist(q[1:])
        log.info("pawchive search '%s'", q)
        f = self.open_feed(f"search:{q}", f"🔎 {q}" if q else "Recent", [pw.RecentSource(self.conf, q)])
        f.query = q

    def find_artist(self, name):
        conf = self.conf

        def done(hits, err):
            if err:
                return self.main.statusBar().showMessage(str(err))
            if not hits:
                return self.main.statusBar().showMessage(f"Pawchive: no artist named '{name}'")
            exact = [c for c in hits if (c.get("name") or "").lower() == name.lower()]
            if len(exact) == 1 or len(hits) == 1:
                c = (exact or hits)[0]
                return self.open_creator(c["service"], str(c["id"]), c.get("name"))
            self.tabs.setCurrentWidget(self.artists.imgtabs if hasattr(self.artists, "imgtabs") else self.artists)
            self.artists.filter.setText(name)
        run(lambda: pw.find_creators(pw.creators(conf), name), done)

    def open_creator(self, service, user, name=""):
        self.main.show_section(self)
        return self.open_tab(f"creator:{service}/{user}", f"👤 {name or user} ({service})", lambda: CreatorView(self, service, user, name))

    def open_post(self, service, user, pid):
        """A single post as its own tab (prev / next, random post, a revision)."""
        conf = self.conf
        return self.open_feed(f"post:{service}/{user}/{pid}", f"📄 {pid}",
                              [pw.ListSource(conf, lambda: [pw.api(conf, f"{service}/user/{user}/post/{pid}")])])

    def open_tag(self, tag, cat=""):
        if tag.startswith("creator:"):
            service, user = tag[8:].split("/", 1)
            return self.open_creator(service, user)
        if tag.startswith("service:"):
            return
        self.open_tag_listing(tag)

    def open_tag_listing(self, tag):
        self.main.show_section(self)
        f = self.open_feed(f"tag:{tag}", f"🏷 {tag}", [pw.CardSource(self.conf, "posts", tag=tag)])
        f.query = tag

    def open_image(self, grid, row, background=None):
        """Cards from HTML pages are stubs: load the full post (one request) before opening it."""
        it = grid.item(row)
        p = it.data(Qt.UserRole)
        if not p.get("stub"):
            return super().open_image(grid, row, background)
        conf = self.conf
        self.main.statusBar().showMessage(f"Loading post {p['post_id']}…")

        def done(full, err):
            if err:
                return self.main.statusBar().showMessage(str(err))
            try:
                grid.set_post(it, full)
            except RuntimeError:  # tab closed meanwhile
                return
            super(PawSection, self).open_image(grid, grid.row(it), background)
        run(lambda: pw.load_post(conf, p["service"], p["user"], p["post_id"]), done)

    def post_actions(self, v):
        p = v.p
        if p["site"] != "paw":
            return []
        out = [(f"👤 {pw.creator_name(self.conf, p['service'], p['user'])}", lambda: self.open_creator(p["service"], p["user"])),
               ("📄 Post details", lambda: self.details(p)),
               ("🔍 Same file elsewhere", lambda: self.same_file(v)), ("⚑ Flag post…", lambda: self.flag(p)),
               ("Open on site", lambda: webbrowser.open(p["page"]))]
        if p.get("preview"):
            out.insert(0, (f"⚠ {p['preview']} page(s) preview only", lambda: None))
        return out

    def tag_actions(self, tag, cat):
        if tag.startswith(("creator:", "service:")):
            return []
        return [("Posts with this tag (site-wide)", lambda: self.open_tag_listing(tag))]

    # --- details / account / moderation ---
    def details(self, p):
        self.open_tab(f"details:{p['id']}", f"📄 {p['title'] or p['post_id']}"[:40], lambda: PostDetails(self, p))

    # --- ★ = a favourite on your Pawchive account (pasted session) ---
    SITE_FAV = "Pawchive favourites"

    def site_fav_state(self, p):
        return super().site_fav_state(p) if p["site"] == "paw" else None

    def site_fav_write(self, p, on):
        pw.fav_post(self.conf, p, on)

    def flag(self, p):
        conf = self.conf

        def got(is_flagged, err):
            if err:
                return self.main.statusBar().showMessage(str(err))
            if is_flagged:
                return QMessageBox.information(self, "Flag", "This post is already flagged for re-import.")
            if QMessageBox.question(self, "Flag post", f"Flag “{p['title']}” as outdated, so Pawchive re-imports it?") == QMessageBox.Yes:
                run(lambda: pw.flag(conf, p), lambda r, e: self.main.statusBar().showMessage(f"Pawchive: {e}" if e else "Pawchive: post flagged"))
        run(lambda: pw.flagged(conf, p), got)

    def paste_cookies(self):
        try:
            pw.cookie_login(self.conf, QApplication.clipboard().text())
        except RuntimeError as e:
            log.warning("pawchive: paste cookies: %s", e)
            return self.main.statusBar().showMessage(f"Pawchive: {e}")
        save_conf(self.conf)
        self.paste_btn.hide()
        self.main.statusBar().showMessage("Pawchive: logged in with the pasted session cookie")
        self.account.started = False

    # --- random / hash ---
    def random(self, kind):
        conf = self.conf

        def done(r, err):
            if err:
                return self.main.statusBar().showMessage(str(err))
            service, user, pid = r
            if pid:
                self.open_post(service, user, pid)
            else:
                self.open_creator(service, user)
        run(lambda: pw.random_target(conf, kind), done)

    def hash_files(self):
        paths, _ = QFileDialog.getOpenFileNames(self, "Find these files on Pawchive")
        if paths:
            conf = self.conf
            self.main.show_section(self)
            self.open_feed(f"hash:{paths}", f"🔍 {len(paths)} file(s)",
                           [pw.ListSource(conf, lambda: [p for path in paths for p in pw.lookup_hash(conf, pw.sha256_file(path))])])

    def same_file(self, v):
        sha = pw.sha_of_url(v.p["files"][v.pg])
        if not sha:
            return self.main.statusBar().showMessage("Pawchive: this page is a preview, its file hash is unknown")
        conf = self.conf
        self.open_feed(f"hash:{sha}", f"🔍 {sha[:10]}…", [pw.ListSource(conf, lambda: pw.lookup_hash(conf, sha))])

    # --- bulk creator ---
    def bulk_creator(self, service, user, name, cont=False):
        key = f"{service}/{user}"
        prev = self.conf["paw"].get("bulk", {}).get(key)
        if cont and prev:
            everything = prev.get("everything", False)
        else:
            choice, ok = QInputDialog.getItem(self, "Bulk creator", f"Download every post of {name or user} ({service}):",
                                              ["Images and videos", "Everything (also zip, psd, …)"], 0, False)
            if not ok:
                return
            everything = choice.startswith("Everything")
            self.conf["paw"].setdefault("bulk", {})[key] = {"o": 0, "new": 0, "skip": 0, "fail": 0, "posts": 0, "done": False,
                                                           "everything": everything}
        cancel = threading.Event()
        title = f"{name or user} ({service})"
        view = self.open_tab(f"bulk:{key}", f"⬇ {title}", lambda: LiveBulk(self, title, f"Bulk {title}", cancel))
        view.cancel = cancel
        conf, root = self.conf, self.conf["download_dir"]

        def done(st, err):
            msg = f"failed: {err}" if err else (f"{'finished' if st['done'] else 'stopped (Continue picks it up)'}: {st['new']} files new, "
                                                f"{st['skip']} already had, {st['fail']} failed")
            view.finished(msg)
            self.main.statusBar().showMessage(f"Bulk {title} {msg}")
        run(lambda: pw.bulk_creator(conf, service, user, root, cancel, on_post=lambda p: view.landed(p, pw.saved_paths(conf, p, root)),
                                    everything=everything), done)


class LazyFeed(Feed):
    """A fixed feed that starts on first show."""
    started = False

    def __init__(self, section, label, make):
        super().__init__(section, label)
        self.make = make

    def showEvent(self, e):
        super().showEvent(e)
        if not self.started:
            self.started = True
            self.start(self.make())


class Popular(Feed):
    started = False

    def __init__(self, section):
        super().__init__(section, "Popular")
        self.period = QComboBox()
        self.period.addItems(["day", "week", "month"])
        self.date = QDateEdit(QDate.currentDate(), calendarPopup=True, maximumDate=QDate.currentDate())
        row = QHBoxLayout()
        for w in (QLabel("popular:"), self.period, QLabel("ending"), self.date, QPushButton("⟳", clicked=self.reload)):
            row.addWidget(w)
        row.addStretch()
        self.lay.insertLayout(0, row)
        self.period.currentIndexChanged.connect(lambda: self.started and self.reload())
        self.date.dateChanged.connect(lambda: self.started and self.reload())

    def showEvent(self, e):
        super().showEvent(e)
        if not self.started:
            self.started = True
            self.reload()

    def reload(self):
        d = self.date.date()
        date = "" if d == QDate.currentDate() else d.toString("yyyy-MM-dd")
        self.start([pw.CardSource(self.section.conf, "posts/popular", paged=False, period=self.period.currentText(), date=date)],
                   f"popular {self.period.currentText()}")


class ListTab(QWidget):
    """Filter box + a sortable list (Artists, Tags)."""

    def __init__(self, section, columns):
        super().__init__()
        self.section, self.started, self.rows = section, False, []
        self.filter = QLineEdit(placeholderText="filter…")
        self.list = QTreeWidget(rootIsDecorated=False, uniformRowHeights=True)
        self.list.setHeaderLabels(columns)
        self.list.setColumnWidth(0, 320)
        self.list.itemActivated.connect(lambda it, _: self.open(it.data(0, Qt.UserRole)))
        middle_click(self.list)
        self.status = QLabel()
        self.top = QHBoxLayout()
        self.top.addWidget(self.filter, 1)
        lay = QVBoxLayout(self)
        lay.addLayout(self.top)
        lay.addWidget(self.list, 1)
        lay.addWidget(self.status)
        self.timer = QTimer(self, singleShot=True, interval=300, timeout=self.render)
        self.filter.textChanged.connect(lambda: self.timer.start())

    def showEvent(self, e):
        super().showEvent(e)
        if not self.started:
            self.started = True
            self.load()


class Artists(ListTab):
    """Every creator on Pawchive (one cached list, filtered here): name, service, last update, favorites."""

    def __init__(self, section):
        super().__init__(section, ["Artist", "Service", "Updated", "♥"])
        self.service, self.sort = QComboBox(), QComboBox()
        self.service.addItem("all services", "")
        self.sort.addItems(list(pw.SORTS))
        for w in (self.service, self.sort, QPushButton("⟳ Refresh list", clicked=lambda: self.load(force=True))):
            self.top.addWidget(w)
        for c in (self.service.currentIndexChanged, self.sort.currentIndexChanged):
            c.connect(lambda: self.render())

    def load(self, force=False):
        self.status.setText("Loading the artist list (about 16 MB the first time each day)…")
        conf = self.section.conf
        run(lambda: pw.creators(conf, force=force), self.got)

    def got(self, rows, err):
        if err:
            return self.status.setText(str(err))
        self.rows = rows
        self.service.blockSignals(True)
        for s in sorted({r.get("service") for r in rows if r.get("service")}):
            if self.service.findData(s) < 0:
                self.service.addItem(s, s)
        self.service.blockSignals(False)
        self.render()

    def render(self):
        hits = pw.find_creators(self.rows, self.filter.text(), self.service.currentData() or "", self.sort.currentText())
        self.list.clear()
        for c in hits:
            it = QTreeWidgetItem([c.get("name") or "", c.get("service") or "", pw.day(c.get("updated")), str(c.get("favorited") or 0)])
            it.setData(0, Qt.UserRole, c)
            self.list.addTopLevelItem(it)
        self.status.setText(f"{len(hits)} shown of {len(self.rows):,} artists (double-click opens one)")

    def open(self, c):
        self.section.open_creator(c["service"], str(c["id"]), c.get("name"))


class Tags(ListTab):
    """Pawchive's most used tags (from its tags page); opening one lists its posts."""

    def __init__(self, section):
        super().__init__(section, ["Tag", "Posts"])

    def load(self):
        self.status.setText("Loading tags…")
        conf = self.section.conf
        run(lambda: pw.parse_tags(pw.page(conf, "posts/tags")), self.got)

    def got(self, rows, err):
        if err:
            return self.status.setText(str(err))
        self.rows = rows
        self.render()

    def render(self):
        q = self.filter.text().strip().lower()
        self.list.clear()
        shown = [(t, n) for t, n in self.rows if q in t.lower()]
        for t, n in shown:
            it = QTreeWidgetItem([t, f"{n:,}"])
            it.setData(0, Qt.UserRole, t)
            it.setTextAlignment(1, Qt.AlignRight | Qt.AlignVCenter)
            self.list.addTopLevelItem(it)
        self.status.setText(f"{len(shown)} of {len(self.rows)} tags")

    def open(self, tag):
        self.section.open_tag_listing(tag)


class Account(Feed):
    """Your Pawchive account's favorites (needs the pasted session cookie): posts as a grid, artists as a list."""
    started = False

    def __init__(self, section):
        super().__init__(section, "account favorites")
        self.kind = QComboBox()
        self.kind.addItems(["Favorite posts", "Favorite artists"])
        self.kind.currentIndexChanged.connect(lambda: self.reload())
        self.artist_list = QTreeWidget(rootIsDecorated=False, visible=False)
        self.artist_list.setHeaderLabels(["Artist", "Service", "Updated"])
        self.artist_list.setColumnWidth(0, 320)
        self.artist_list.itemActivated.connect(lambda it, _: section.open_creator(*it.data(0, Qt.UserRole)))
        middle_click(self.artist_list)
        row = QHBoxLayout()
        for w in (QLabel("Pawchive account:"), self.kind, QPushButton("⟳ Refresh", clicked=self.reload)):
            row.addWidget(w)
        row.addStretch()
        self.lay.insertLayout(0, row)
        self.lay.insertWidget(1, self.artist_list, 1)

    def showEvent(self, e):
        super().showEvent(e)
        if not self.started:
            self.started = True
            self.reload()

    def reload(self):
        conf = self.section.conf
        artists = self.kind.currentIndex() == 1
        self.artist_list.setVisible(artists)
        self.grid.setVisible(not artists)
        if not conf["paw"]["session"]:
            return self.status.setText(pw.NOT_LOGGED_IN)
        if not artists:
            return self.start([pw.ListSource(conf, lambda: pw.account_favorites(conf, "post"))])
        self.status.setText("Loading favorite artists…")

        def done(rows, err):
            if err:
                return self.status.setText(str(err))
            self.artist_list.clear()
            for c in rows:
                it = QTreeWidgetItem([c.get("name") or "", c.get("service") or "", pw.day(c.get("updated"))])
                it.setData(0, Qt.UserRole, (c["service"], str(c["id"]), c.get("name") or ""))
                self.artist_list.addTopLevelItem(it)
            self.status.setText(f"{len(rows)} favorite artists")
        run(lambda: pw.account_favorites(conf, "artist"), done)


class CreatorView(Feed):
    """One creator: posts (search + creator tags), linked accounts, announcements, fancards, favorite, bulk."""

    def __init__(self, section, service, user, name=""):
        super().__init__(section, name or user)
        self.service, self.user, self.name = service, user, name
        self.head = rich_label()
        self.head.linkActivated.connect(self.link)
        self.q = QLineEdit(placeholderText="search this artist's posts…")
        self.q.returnPressed.connect(self.reload)
        self.tag = QComboBox(minimumContentsLength=16)
        self.tag.addItem("all tags", "")
        self.tag.currentIndexChanged.connect(lambda: self.reload())
        self.cont = QPushButton("Continue bulk", clicked=lambda: section.bulk_creator(service, user, self.name, cont=True))
        b = section.conf["paw"].get("bulk", {}).get(f"{service}/{user}")
        self.cont.setVisible(bool(b and not b.get("done")))
        row1 = QHBoxLayout()
        row1.addWidget(self.head, 1)
        row2 = QHBoxLayout()
        for w in (self.q, self.tag, QPushButton("Announcements", clicked=self.announcements),
                  QPushButton("Fancards", clicked=self.fancards, visible=service == "fanbox"),
                  QPushButton("♥ Favorite", toolTip="Favorite this artist on your Pawchive account", clicked=lambda: self.fav(True)),
                  QPushButton("⬇ Bulk creator", clicked=lambda: section.bulk_creator(service, user, self.name)), self.cont,
                  QPushButton("Open on site", clicked=lambda: webbrowser.open(f"{pw.site(section.conf)}/{service}/user/{user}"))):
            row2.addWidget(w, int(w is self.q))
        self.lay.insertLayout(0, row1)
        self.lay.insertLayout(1, row2)
        self.head.setText(f"<b>{html.escape(name or user)}</b> ({service}) loading…")
        conf = section.conf
        run(lambda: pw.creator_info(conf, service, user), self.got_info)
        self.reload()

    def reload(self):
        self.start([pw.CreatorSource(self.section.conf, self.service, self.user, self.q.text().strip(), self.tag.currentData() or "")])

    def got_info(self, info, err):
        if err:
            return self.head.setText(f"<i>{html.escape(str(err))}</i>")
        p = info["profile"]
        self.name = p.get("name") or self.name
        links = " · ".join(f"<a href='creator:{l['service']}/{l['id']}'>{html.escape(l.get('name') or l['id'])} ({l['service']})</a>"
                           for l in info["links"])
        self.head.setText(f"<b>{html.escape(self.name)}</b> ({p.get('service')}) · updated {pw.day(p.get('updated'))}"
                          + (f"<br>also: {links}" if links else ""))
        self.tag.blockSignals(True)
        for t in info["tags"]:
            self.tag.addItem(f"{t['tag']} ({t['post_count']})", t["tag"])
        self.tag.blockSignals(False)
        tabs = self.section.tabs
        if self.imgtabs and tabs.indexOf(self.imgtabs) >= 0:
            tabs.setTabText(tabs.indexOf(self.imgtabs), f"👤 {self.name} ({self.service})"[:40])

    def link(self, href):
        if href.startswith("creator:"):
            service, user = href[8:].split("/", 1)
            self.section.open_creator(service, user)

    def announcements(self):
        conf = self.section.conf

        def done(rows, err):
            body = html.escape(str(err)) if err else "<hr>".join(
                f"<p><b>{(a.get('published') or a.get('added') or '')[:10]}</b></p>{a.get('content', '')}" for a in rows) or "No announcements."
            show_text(self, f"Announcements: {self.name}", body)
        run(lambda: pw.api(conf, f"{self.service}/user/{self.user}/announcements") or [], done)

    def fancards(self):
        conf, s = self.section.conf, self.section

        def load():
            cards = pw.api(conf, f"{self.service}/user/{self.user}/fancards") or []
            return [{"id": c.get("id"), "user": self.user, "service": self.service, "title": "fancard",
                     "file": {"path": c["path"], "name": c.get("name") or c["path"].split("/")[-1]}} for c in cards if c.get("path")]
        s.open_feed(f"fancards:{self.service}/{self.user}", f"🃏 {self.name}", [pw.ListSource(conf, load)])

    def fav(self, on):
        conf, s = self.section.conf, self.section
        run(lambda: pw.fav_creator(conf, self.service, self.user, on),
            lambda r, e: s.main.statusBar().showMessage(f"Pawchive: {e}" if e else f"Pawchive: {self.name} favorited"))


class PostDetails(QWidget):
    """Everything about one post: text, other attachments, comments, revisions, previous / next post."""

    def __init__(self, section, p):
        super().__init__()
        self.section, self.p = section, p
        self.view = QTextBrowser(openLinks=False)
        self.view.anchorClicked.connect(self.link)
        middle_click(self.view)
        lay = QVBoxLayout(self)
        lay.addWidget(self.view)
        self.view.setHtml(f"<h3>{html.escape(p['title'])}</h3>loading…")
        conf = section.conf
        run(lambda: self.load(conf, p), self.render)

    @staticmethod
    def load(conf, p):
        full = pw.load_post(conf, p["service"], p["user"], p["post_id"]) if p.get("stub") or not p.get("content") else p
        return full, pw.comments(conf, full), pw.revisions(conf, full)

    def render(self, res, err):
        if err:
            return self.view.setHtml(f"<i>{html.escape(str(err))}</i>")
        p, comments, revisions = res
        self.p = p
        e = html.escape
        nav = " ".join(f"<a href='post:{x}'>{label}</a>" for x, label in ((p.get("prev"), "◀ previous post"), (p.get("next"), "next post ▶")) if x)
        other = "".join(f"<li><a href='file:{e(o['url'])}'>{e(o['name'])}</a></li>" for o in p.get("other") or [])
        pages = "".join(f"<li>{e(n)}{' (preview only)' if 'thumbnail' in u else ''}</li>" for n, u in zip(p["names"], p["files"]))
        com = "".join(f"<p><b>{e(c.get('commenter_name') or c.get('commenter') or '?')}</b> "
                      f"<small>{(c.get('published') or '')[:16]}</small><br>{e(pw.text(c.get('content')))}</p>" for c in comments)
        revs = "".join(f"<li><a href='rev:{r.get('revision_id')}'>revision {r.get('revision_id')}</a> {(r.get('added') or '')[:16]}</li>"
                       for r in revisions)
        self.view.setHtml(f"<h3>{e(p['title'])}</h3><p>{e(p['published'][:16])} · <a href='open:'>open on site</a> {nav}</p>"
                          f"<p style='white-space:pre-wrap'>{e(p['content'])}</p>"
                          f"<h4>Images / videos ({len(p['files'])})</h4><ul>{pages}</ul>"
                          + (f"<h4>Other attachments</h4><ul>{other}</ul>" if other else "")
                          + (f"<h4>Comments ({len(comments)})</h4>{com}" if comments else "<p><i>No comments.</i></p>")
                          + (f"<h4>Revisions</h4><ul>{revs}</ul>" if revs else ""))

    def link(self, url):
        u, p, s = url.toString(), self.p, self.section
        kind, _, val = u.partition(":")
        if kind == "post":
            s.open_post(p["service"], p["user"], val)
        elif kind == "file":
            webbrowser.open(val)
        elif kind == "open":
            webbrowser.open(p["page"])
        elif kind == "rev":
            conf = s.conf
            s.open_feed(f"rev:{p['id']}/{val}", f"📄 {p['post_id']} rev {val}",
                        [pw.ListSource(conf, lambda: [r for r in pw.revisions(conf, p) if str(r.get("revision_id")) == val])])


def show_text(parent, title, body):
    d = QMessageBox(parent)
    d.setWindowTitle(title)
    d.setTextFormat(Qt.RichText)
    d.setText(body[:20000])
    d.exec()
