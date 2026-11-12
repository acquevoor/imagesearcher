"""The Comics section: E-Hentai and Hitomi side by side (tick which), Latest / Popular / Favorites, searches and
tag tabs. A gallery opens like any multi-page post: PgUp/PgDn or ▤ strip view to read it."""
from PySide6.QtWidgets import QCheckBox, QPushButton

from . import comics
from .core import NAMES, log, save_conf
from .paw_ui import LazyFeed
from .widgets import Favorites, Section


class ComicsSection(Section):
    SAVED = "comics_saved"

    def __init__(self, main):
        super().__init__(main, "search comics…  female:big_breasts artist:name language:english -male:yaoi  (_ = space, -term excludes)")
        self.bar.addWidget(self.q, 1)
        self.ticks = {}
        for sid in ("eh", "hitomi"):
            cb = QCheckBox(NAMES[sid], checked=sid in self.conf["comics"]["sources"], toolTip=f"Search {NAMES[sid]}")
            cb.toggled.connect(self.ticked)
            self.ticks[sid] = cb
            self.bar.addWidget(cb)
        self.no_ai = QCheckBox("Hide AI", checked=self.conf["comics"]["no_ai"], toolTip=f"Leaves out galleries tagged AI generated "
                               f"(adds -{comics.AI} to every search). Hitomi rarely tags them, so there it only drops the tagged ones")
        self.no_ai.toggled.connect(self.ticked)
        for w in (self.no_ai, QPushButton("Search", clicked=lambda: self.search()), self.saved, self.star):
            self.bar.addWidget(w)
        self.latest = LazyFeed(self, "Latest", lambda: comics.sources(self.conf))
        self.add_fixed(self.latest, "Latest")
        self.popular = LazyFeed(self, "Popular", lambda: comics.sources(self.conf, popular=True))
        self.add_fixed(self.popular, "Popular")
        self.favs = Favorites(self, lambda p: p["site"] in ("eh", "hitomi"))
        self.add_fixed(self.favs, "Favorites")
        self.refresh_favs()

    def ticked(self):
        self.conf["comics"].update(sources=[sid for sid, cb in self.ticks.items() if cb.isChecked()], no_ai=self.no_ai.isChecked())
        save_conf(self.conf)
        log.info("comics: searching %s%s", ", ".join(self.conf["comics"]["sources"]) or "nothing", ", AI hidden" * self.no_ai.isChecked())
        for f in (self.latest, self.popular):  # already loaded: again with the new choice
            if f.started:
                f.start(f.make())

    def complete(self, word):
        return comics.complete(word)

    def search(self, q=None):
        q = (self.q.text() if q is None else q).strip()
        self.main.show_section(self)
        srcs = comics.sources(self.conf, q)
        if not srcs:
            return self.main.statusBar().showMessage("Comics: tick E-Hentai and/or Hitomi")
        log.info("comics search '%s' on %s", q, ", ".join(s.label for s in srcs))
        f = self.open_feed(f"search:{q}|{len(srcs)}", f"🔎 {q}" if q else "Latest", srcs)
        f.query = q

    def open_tag(self, tag, cat=""):
        self.search(tag.replace(" ", "_"))

    def post_actions(self, v):
        """Quick searches for the gallery's artists / groups / series."""
        p = v.p
        if p["site"] not in ("eh", "hitomi"):
            return []
        keys = [t for t in p["tags"] if t.split(":", 1)[0] in ("artist", "group", "series")][:4]
        return [(f"🔎 {t}", lambda t=t: self.open_tag(t)) for t in keys]
