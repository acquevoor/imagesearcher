#!/usr/bin/env python3
"""imagesearcher: Danbooru / Gelbooru / Rule34.xxx / Pixiv browser and downloader."""
import hashlib
import importlib.util
import itertools
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

try:
    import PySide6.QtWidgets  # noqa: F401
except ImportError as e:  # first double-click on a plain Python install (e.g. Windows): fetch PySide6, then restart
    # only install when PySide6 is truly absent; installed-but-broken gets the error, not a reinstall.
    # one attempt only: if the fresh install still can't import, a restart loop would fork-bomb the machine
    if importlib.util.find_spec("PySide6") or os.environ.get("IMAGESEARCHER_PIP_TRIED"):
        input(f"PySide6 is installed but can't load: {e}\nPress Enter to close.")
        sys.exit(1)
    print("First run: installing PySide6, this takes a minute or two. Keep this window open...")
    if subprocess.call([sys.executable, "-m", "pip", "install", "PySide6"]):
        input("Installing PySide6 failed (see above). Press Enter to close.")
        sys.exit(1)
    os.environ["IMAGESEARCHER_PIP_TRIED"] = "1"
    sys.exit(subprocess.call([sys.executable] + sys.argv))

from PySide6.QtCore import QModelIndex, QObject, QSize, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QDesktopServices, QIcon, QImage, QKeySequence, QPixmap, QShortcut, QStandardItem, QStandardItemModel
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QCompleter, QDialog, QDialogButtonBox, QFileDialog, QFormLayout,
    QHBoxLayout, QInputDialog, QLabel, QLineEdit, QListWidget, QListWidgetItem, QMainWindow, QMenu,
    QPlainTextEdit, QPushButton, QSizePolicy, QSpinBox, QTabWidget, QVBoxLayout, QWidget,
)

SITES = {"danbooru": "Danbooru", "gelbooru": "Gelbooru", "rule34": "Rule34", "pixiv": "Pixiv"}
SHORT = {"danbooru": "DB", "gelbooru": "GB", "rule34": "R34", "pixiv": "PX"}
VIDEO_EXT = {".mp4", ".webm", ".zip"}  # .zip = Danbooru ugoira, its sample is a .webm
THUMB = 180
UA = "imagesearcher/1.0"
PIXIV_UA = "PixivAndroidApp/5.0.234 (Android 11; Pixel 5)"
PIXIV_REFERER = "https://app-api.pixiv.net/"
PIXIV_CLIENT = ("MOBrBDS8blbauoSck0ZfDbtuzpyT", "lsACyCD94FhDUtGTXi3QzcFE2uU1hqtDaKeqrdwj")
PIXIV_HASH = "28c1fdd170a5204386cb1313c7077b34f83e4aaf4aa829ce78c231e05b0bae2c"

CONF_PATH = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "imagesearcher" / "config.json"
DEFAULTS = {
    "danbooru": {"login": "", "api_key": ""},
    "gelbooru": {"user_id": "", "api_key": ""},
    "rule34": {"user_id": "", "api_key": ""},
    "pixiv": {"refresh_token": ""},
    "blacklist": [], "favorites": [], "saved": [], "sites": ["danbooru"], "limit": 40,
    "download_dir": str(Path.home() / "Pictures" / "imagesearcher"), "history": False,
}


def load_conf():
    conf = json.loads(json.dumps(DEFAULTS))
    try:
        for k, v in json.loads(CONF_PATH.read_text()).items():
            conf[k] = {**conf[k], **v} if isinstance(conf.get(k), dict) else v
    except FileNotFoundError:
        pass
    return conf


def save_conf(conf):
    # 0600: holds API keys and the Pixiv refresh token
    CONF_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = CONF_PATH.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(conf, f, indent=1)
    os.replace(tmp, CONF_PATH)


# ---------- logging ----------
# Only user/app actions are logged (searches, views, downloads, favorites, settings, errors), never keys or tokens.
log = logging.getLogger("imagesearcher")
APP_DIR = Path(__file__).resolve().parent
# next to imagesearcher.py, so it's in the same, findable place for everyone (unzipped or git-cloned);
# a read-only install (Nix store) falls back to the config dir
HISTORY_DIR = APP_DIR / "history" if os.access(APP_DIR, os.W_OK) else CONF_PATH.parent / "history"


class DailyHistory(logging.Handler):
    """Appends to <dir>/YYYY-MM-DD.txt; the date is taken per line, so a session past midnight starts a new file."""

    def __init__(self, folder):
        super().__init__()
        self.folder = Path(folder)
        self.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%H:%M:%S"))

    def emit(self, record):
        try:
            self.folder.mkdir(parents=True, exist_ok=True)
            with open(self.folder / f"{time.strftime('%Y-%m-%d')}.txt", "a", encoding="utf-8") as f:
                f.write(self.format(record) + "\n")
        except Exception:
            self.handleError(record)


history = DailyHistory(HISTORY_DIR)


def set_history(on):
    (log.addHandler if on else log.removeHandler)(history)


# ---------- network ----------

def open_url(url, data=None, headers=None, timeout=30):
    h = {"User-Agent": UA, **(headers or {})}
    if "pximg.net" in url:
        h["Referer"] = PIXIV_REFERER
    return urllib.request.urlopen(urllib.request.Request(url, data=data, headers=h), timeout=timeout)


def http(url, **kw):
    with open_url(url, **kw) as r:
        return r.read()


def get_json(url, site, **kw):
    hint = " (set user id + API key in Settings)" if site in GEL else ""
    try:
        body = http(url, **kw).strip()
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"{SITES[site]}: HTTP {e.code}{hint if e.code in (401, 403) else ''}") from None
    try:
        data = json.loads(body) if body else None
    except ValueError:
        data = body[:150].decode(errors="replace")
    if isinstance(data, str):  # R34 answers auth failures with a bare string
        raise RuntimeError(f"{SITES[site]}: {data}{hint}")
    return data


def qs(**params):
    return urllib.parse.urlencode({k: v for k, v in params.items() if v not in ("", None)})


def make_post(site, pid, thumb, files, samples, tags, rating, page, w=0, h=0):
    ext = Path(urllib.parse.urlparse(files[0]).path).suffix.lower()
    return {"site": site, "id": str(pid), "thumb": thumb or (samples or files)[0], "files": files,
            "samples": samples or files, "tags": tags, "rating": rating, "page": page, "w": w, "h": h,
            "video": ext in VIDEO_EXT}


def norm_rating(r):
    r = str(r or "").lower()
    return "g" if r in ("safe", "general") else r[:1]


# Danbooru

def parse_danbooru(items):
    out = []
    for p in items or []:
        variants = {v["type"]: v["url"] for v in (p.get("media_asset") or {}).get("variants") or []}
        f = p.get("file_url") or variants.get("original")
        if not f:  # deleted / gold-only
            continue
        out.append(make_post("danbooru", p["id"], variants.get("360x360") or p.get("preview_file_url"), [f],
                             [p.get("large_file_url") or f], p.get("tag_string", "").split(), p.get("rating", ""),
                             f"https://danbooru.donmai.us/posts/{p['id']}", p.get("image_width", 0), p.get("image_height", 0)))
    return out


def search_danbooru(conf, tags, page):
    c = conf["danbooru"]
    url = "https://danbooru.donmai.us/posts.json?" + qs(tags=tags, page=page, limit=conf["limit"], login=c["login"], api_key=c["api_key"])
    return parse_danbooru(get_json(url, "danbooru"))


def complete_danbooru(conf, word):
    url = "https://danbooru.donmai.us/autocomplete.json?" + urllib.parse.urlencode(
        {"search[query]": word, "search[type]": "tag_query", "limit": 15})
    return [(t["value"], f"{t['value']}  ({t.get('post_count', 0)})") for t in get_json(url, "danbooru") or []]


# Gelbooru-family (Gelbooru + Rule34.xxx share the dapi)

GEL = {
    "gelbooru": ("https://gelbooru.com/index.php", "https://gelbooru.com/index.php?page=post&s=view&id="),
    "rule34": ("https://api.rule34.xxx/index.php", "https://rule34.xxx/index.php?page=post&s=view&id="),
}


def parse_gel(site, data):
    items = data.get("post", []) if isinstance(data, dict) else data or []
    out = []
    for p in items:
        if not p.get("file_url"):
            continue
        sample = p.get("sample_url") if p.get("sample") and p.get("sample_url") else p["file_url"]
        out.append(make_post(site, p["id"], p.get("preview_url"), [p["file_url"]], [sample], p.get("tags", "").split(),
                             norm_rating(p.get("rating")), GEL[site][1] + str(p["id"]), p.get("width", 0), p.get("height", 0)))
    return out


def search_gel(site, conf, tags, page):
    c = conf[site]
    url = GEL[site][0] + "?" + qs(page="dapi", s="post", q="index", json=1, tags=tags, pid=page - 1,
                                  limit=conf["limit"], api_key=c["api_key"], user_id=c["user_id"])
    return parse_gel(site, get_json(url, site))


def complete_gel(site, conf, word):
    c = conf[site]
    if site == "rule34":
        url = "https://api.rule34.xxx/autocomplete.php?" + qs(q=word)
    else:
        url = "https://gelbooru.com/index.php?" + qs(page="autocomplete2", term=word, type="tag_query", limit=15,
                                                    api_key=c["api_key"], user_id=c["user_id"])
    res = get_json(url, site) or []
    return [(t["value"], t.get("label") or t["value"]) for t in res if t.get("value")]


# Pixiv (app API; refresh token from Settings)

_pixiv = {"token": None, "exp": 0, "lock": threading.Lock()}


def pixiv_headers(conf):
    with _pixiv["lock"]:
        if time.time() > _pixiv["exp"]:
            rt = conf["pixiv"]["refresh_token"]
            if not rt:
                raise RuntimeError("Pixiv: paste a refresh token in Settings (see README)")
            t = time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime())
            data = qs(client_id=PIXIV_CLIENT[0], client_secret=PIXIV_CLIENT[1], grant_type="refresh_token",
                      refresh_token=rt, include_policy="true").encode()
            r = json.loads(http("https://oauth.secure.pixiv.net/auth/token", data=data, headers={
                "User-Agent": PIXIV_UA, "X-Client-Time": t,
                "X-Client-Hash": hashlib.md5((t + PIXIV_HASH).encode()).hexdigest(),
                "Content-Type": "application/x-www-form-urlencoded"}))
            _pixiv["token"], _pixiv["exp"] = r["access_token"], time.time() + r.get("expires_in", 3600) - 60
            if r.get("refresh_token") and r["refresh_token"] != rt:
                conf["pixiv"]["refresh_token"] = r["refresh_token"]
                save_conf(conf)
        return {"Authorization": "Bearer " + _pixiv["token"], "User-Agent": PIXIV_UA,
                "App-OS": "android", "App-OS-Version": "11", "Accept-Language": "en-us"}


def parse_pixiv(data):
    out = []
    for i in (data or {}).get("illusts", []):
        if not i.get("visible", True):
            continue
        if i.get("meta_pages"):
            files = [m["image_urls"]["original"] for m in i["meta_pages"]]
            samples = [m["image_urls"]["large"] for m in i["meta_pages"]]
        else:
            files = [i["meta_single_page"]["original_image_url"]]
            samples = [i["image_urls"]["large"]]
        # ponytail: ugoira = first frame only; fetch /v1/ugoira/metadata zip if animation matters
        tags = list(dict.fromkeys(n for t in i.get("tags", []) for n in (t["name"], t.get("translated_name")) if n))
        out.append(make_post("pixiv", i["id"], i["image_urls"]["square_medium"], files, samples, tags,
                             "e" if i.get("x_restrict") else "g", f"https://www.pixiv.net/artworks/{i['id']}",
                             i.get("width", 0), i.get("height", 0)))
    return out


def search_pixiv(conf, tags, page):
    url = "https://app-api.pixiv.net/v1/search/illust?" + qs(
        word=tags, search_target="partial_match_for_tags", sort="date_desc", filter="for_android", offset=(page - 1) * 30)
    return parse_pixiv(get_json(url, "pixiv", headers=pixiv_headers(conf)))


def complete_pixiv(conf, word):
    url = "https://app-api.pixiv.net/v2/search/autocomplete?" + qs(word=word, merge_plain_keyword_results="true")
    tags = (get_json(url, "pixiv", headers=pixiv_headers(conf)) or {}).get("tags", [])
    return [(t["name"], f"{t['name']}  ({t['translated_name']})" if t.get("translated_name") else t["name"]) for t in tags]


SEARCH = {"danbooru": search_danbooru, "pixiv": search_pixiv,
          "gelbooru": lambda *a: search_gel("gelbooru", *a), "rule34": lambda *a: search_gel("rule34", *a)}
COMPLETE = {"danbooru": complete_danbooru, "pixiv": complete_pixiv,
            "gelbooru": lambda *a: complete_gel("gelbooru", *a), "rule34": lambda *a: complete_gel("rule34", *a)}


def fetch_all(sites, conf, tags, page):
    """Query sites in parallel -> ({site: posts}, {site: error})."""
    lists, errs = {}, {}
    with ThreadPoolExecutor(len(sites) or 1) as ex:
        futs = {s: ex.submit(SEARCH[s], conf, tags, page) for s in sites}
        for s, f in futs.items():
            try:
                lists[s] = f.result()
            except Exception as e:
                lists[s], errs[s] = [], str(e)
    return lists, errs


def interleave(lists):
    return [p for group in itertools.zip_longest(*lists) for p in group if p is not None]


def allowed(post, blacklist):
    return not ({t.lower() for t in post["tags"]} | {"rating:" + post["rating"]}) & blacklist


def dest(root, post, i, query):
    folder = re.sub(r"[^\w\-+.() ]", "_", query).strip()[:100] or "_all"
    name = Path(urllib.parse.urlparse(post["files"][i]).path)
    page = f"_p{i}" if len(post["files"]) > 1 else ""
    return Path(root) / post["site"] / folder / f"{post['site']}_{post['id']}{page}{name.suffix}"


def download_post(post, root, query, cancel=None):
    """-> (new, skipped)"""
    new = skip = 0
    for i, url in enumerate(post["files"]):
        if cancel and cancel.is_set():
            break
        d = dest(root, post, i, query)
        if d.exists():
            skip += 1
            continue
        d.parent.mkdir(parents=True, exist_ok=True)
        tmp = d.with_name(d.name + ".part")
        with open_url(url, timeout=120) as r, open(tmp, "wb") as f:
            shutil.copyfileobj(r, f)
        tmp.rename(d)
        new += 1
    return new, skip


# ---------- Qt plumbing ----------

class Bridge(QObject):
    done = Signal(object, object, object)

    def __init__(self):
        super().__init__()
        self.done.connect(self._call)

    def _call(self, cb, res, err):
        cb(res, err)


POOLS = {"net": ThreadPoolExecutor(4), "thumb": ThreadPoolExecutor(8), "view": ThreadPoolExecutor(2)}
bridge = None


def run(fn, cb, pool="net"):
    """Run fn() in a worker, then cb(result, error) on the UI thread."""
    def job():
        try:
            res, err = fn(), None
        except Exception as e:
            res, err = None, e
        bridge.done.emit(cb, res, err)
    POOLS[pool].submit(job)


THUMBS = {}  # ponytail: unbounded in-memory cache, fine for a browsing session; LRU if RAM ever matters


class Grid(QListWidget):
    def __init__(self, on_open):
        super().__init__()
        self.gen = 0
        self.setViewMode(QListWidget.IconMode)
        self.setIconSize(QSize(THUMB, THUMB))
        self.setGridSize(QSize(THUMB + 12, THUMB + 28))
        self.setResizeMode(QListWidget.Adjust)
        self.setMovement(QListWidget.Static)
        self.setUniformItemSizes(True)
        self.blank = QPixmap(THUMB, THUMB)
        self.blank.fill(Qt.transparent)
        self.itemActivated.connect(lambda it: on_open(self, self.row(it)))

    def restart(self):
        self.gen += 1
        self.clear()

    def add(self, posts):
        for p in posts:
            pages = f" [{len(p['files'])}]" if len(p["files"]) > 1 else ""
            it = QListWidgetItem(QIcon(self.blank), f"{SHORT[p['site']]} {p['id']}{pages}{' ▶' if p['video'] else ''}")
            it.setData(Qt.UserRole, p)
            it.setToolTip(" ".join(p["tags"][:40]))
            self.addItem(it)
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


class Viewer(QDialog):
    def __init__(self, main):
        super().__init__(main)
        self.main, self.grid, self.row, self.pg, self.pix = main, None, 0, 0, None
        self.resize(1300, 900)
        self.img = QLabel(alignment=Qt.AlignCenter)
        self.img.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Ignored)
        self.info = QLabel(wordWrap=True, textInteractionFlags=Qt.TextSelectableByMouse)
        self.tags = QListWidget(focusPolicy=Qt.NoFocus, contextMenuPolicy=Qt.CustomContextMenu)
        self.tags.itemActivated.connect(lambda it: self.main.search(it.text()))
        self.tags.customContextMenuRequested.connect(self.tag_menu)

        def btn(text, fn):
            b = QPushButton(text, focusPolicy=Qt.NoFocus)
            b.clicked.connect(fn)
            return b
        self.fav = btn("♥ Favorite (F)", lambda: (self.main.toggle_fav(self.post()), self.refresh_info()))
        self.play = btn("▶ Play in mpv", self.play_video)
        self.pages = QLabel()
        nav = QHBoxLayout()
        for w in (btn("◀", lambda: self.show_row(self.row - 1)), btn("▶", lambda: self.show_row(self.row + 1))):
            nav.addWidget(w)
        side = QVBoxLayout()
        side.addLayout(nav)
        for w in (self.pages, self.info, QLabel("Tags (double-click = search, right-click = more)"), self.tags,
                  btn("⬇ Download (D)", lambda: self.main.download(self.post())), self.fav, self.play,
                  btn("Open page", lambda: webbrowser.open(self.post()["page"]))):
            side.addWidget(w)
        sidew = QWidget()
        sidew.setLayout(side)
        sidew.setFixedWidth(300)
        lay = QHBoxLayout(self)
        lay.addWidget(self.img, 1)
        lay.addWidget(sidew)

        for key, fn in ((Qt.Key_Left, lambda: self.show_row(self.row - 1)), (Qt.Key_Right, lambda: self.show_row(self.row + 1)),
                        (Qt.Key_PageUp, lambda: self.flip(-1)), (Qt.Key_PageDown, lambda: self.flip(1)),
                        (Qt.Key_D, lambda: self.main.download(self.post())), (Qt.Key_F, self.fav.click)):
            QShortcut(QKeySequence(key), self, fn)

    def post(self):
        return self.grid.item(self.row).data(Qt.UserRole)

    def open(self, grid, row):
        self.grid = grid
        p = grid.item(row).data(Qt.UserRole)
        log.info("view %s #%s", p["site"], p["id"])
        self.show_row(row)
        self.show()
        self.raise_()
        self.activateWindow()

    def show_row(self, row):
        if 0 <= row < self.grid.count():
            self.row, self.pg = row, 0
            self.grid.setCurrentRow(row)
            self.load()
        elif row >= self.grid.count() and self.grid is self.main.results:
            self.main.more()  # walked off the end: fetch next page, press → again

    def flip(self, d):
        if 0 <= self.pg + d < len(self.post()["files"]):
            self.pg += d
            self.load()

    def load(self):
        p = self.post()
        self.setWindowTitle(f"{SITES[p['site']]} #{p['id']}")
        self.tags.clear()
        self.tags.addItems(p["tags"])
        self.refresh_info()
        self.play.setVisible(p["video"])
        url = p["thumb"] if p["video"] else p["samples"][self.pg]
        key, self.pix = (self.row, self.pg, p["id"]), None
        self.img.setText("loading…")

        def done(img, err):
            it = self.grid.item(self.row)
            if not it or key != (self.row, self.pg, it.data(Qt.UserRole)["id"]):  # grid cleared or moved on
                return
            if err or img.isNull():
                self.img.setText(f"failed: {err}")
            else:
                self.pix = QPixmap.fromImage(img)
                self.rescale()
        run(lambda: QImage.fromData(http(url)), done, "view")

    def refresh_info(self):
        p = self.post()
        n = len(p["files"])
        self.pages.setText(f"page {self.pg + 1}/{n} (PgUp/PgDn)" if n > 1 else "")
        self.pages.setVisible(n > 1)
        self.info.setText(f"<b>{SITES[p['site']]} #{p['id']}</b><br>{p['w']}×{p['h']}  rating:{p['rating']}"
                          f"<br>{self.row + 1}/{self.grid.count()} (←/→)")
        self.fav.setText(("♥ Unfavorite" if self.main.is_fav(p) else "♡ Favorite") + " (F)")

    def rescale(self):
        if self.pix:
            self.img.setPixmap(self.pix.scaled(self.img.size(), Qt.KeepAspectRatio, Qt.SmoothTransformation))

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self.rescale()

    def play_video(self):
        p = self.post()
        url = next((u for u in p["samples"] + p["files"] if Path(urllib.parse.urlparse(u).path).suffix in (".mp4", ".webm")), p["files"][0])
        try:
            subprocess.Popen(["mpv", f"--referrer={PIXIV_REFERER}" if p["site"] == "pixiv" else "--", url])
        except FileNotFoundError:  # no mpv (typical on Windows): let the browser play it
            webbrowser.open(p["page"])

    def tag_menu(self, pos):
        it = self.tags.itemAt(pos)
        if not it:
            return
        t, m, q = it.text(), QMenu(self), self.main.q.text().strip()
        m.addAction("Search", lambda: self.main.search(t))
        m.addAction("Add to query", lambda: self.main.search(f"{q} {t}".strip()))
        m.addAction("Exclude from query", lambda: self.main.search(f"{q} -{t}".strip()))
        m.addAction("Blacklist", lambda: self.main.blacklist_add(t))
        m.addAction("Copy", lambda: QApplication.clipboard().setText(t))
        m.exec(self.tags.mapToGlobal(pos))


class Settings(QDialog):
    def __init__(self, main):
        super().__init__(main)
        self.setWindowTitle("Settings")
        self.resize(560, 0)
        c = main.conf
        self.f = {}
        form = QFormLayout(self)
        for site, key, label in (("danbooru", "login", "Danbooru login"), ("danbooru", "api_key", "Danbooru API key"),
                                 ("gelbooru", "user_id", "Gelbooru user id"), ("gelbooru", "api_key", "Gelbooru API key"),
                                 ("rule34", "user_id", "Rule34 user id"), ("rule34", "api_key", "Rule34 API key"),
                                 ("pixiv", "refresh_token", "Pixiv refresh token")):
            e = self.f[site, key] = QLineEdit(c[site][key])
            if key != "user_id" and key != "login":
                e.setEchoMode(QLineEdit.PasswordEchoOnEdit)
            form.addRow(label, e)
        self.bl = QPlainTextEdit(" ".join(c["blacklist"]), placeholderText="tags, space/newline separated; rating:e / rating:q also work")
        form.addRow("Blacklist", self.bl)
        self.dir = QLineEdit(c["download_dir"])
        browse = QPushButton("…", clicked=lambda: self.dir.setText(QFileDialog.getExistingDirectory(self, "Download dir", self.dir.text()) or self.dir.text()))
        row = QHBoxLayout()
        row.addWidget(self.dir)
        row.addWidget(browse)
        form.addRow("Download dir", row)
        self.limit = QSpinBox(minimum=10, maximum=100, value=c["limit"])
        form.addRow("Posts per page", self.limit)
        form.addRow(QLabel('Keys: Danbooru → My Account → API Key; Gelbooru/Rule34 → My Account → Options → API Access Credentials. '
                           'Pixiv token: see README.', wordWrap=True))
        bb = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel, accepted=self.accept, rejected=self.reject)
        form.addRow(bb)

    def apply(self, conf):
        for (site, key), e in self.f.items():
            conf[site][key] = e.text().strip()
        conf["blacklist"] = sorted(set(self.bl.toPlainText().lower().split()))
        conf["download_dir"], conf["limit"] = self.dir.text().strip(), self.limit.value()
        _pixiv["exp"] = 0  # token may have changed


class Main(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("imagesearcher")
        self.resize(1400, 900)
        self.conf = load_conf()
        self.query, self.page, self.gen, self.loading, self.active, self.errs = "", 0, 0, False, [], {}
        self.cancel = None

        self.cb = {s: QCheckBox(n, checked=s in self.conf["sites"]) for s, n in SITES.items()}
        for c in self.cb.values():
            c.toggled.connect(self.save_sites)
        self.q = QLineEdit(placeholderText="tags…   Enter = search   (booru syntax: -tag, rating:g, order:score)")
        self.q.returnPressed.connect(lambda: self.search())
        self.saved = QComboBox(minimumContentsLength=18)
        self.saved.activated.connect(lambda i: i and self.search(self.saved.itemText(i)))
        self.star = QPushButton("☆", toolTip="Save / unsave this search", clicked=self.toggle_saved)

        self.history_cb = QCheckBox("History", checked=self.conf["history"],
                                    toolTip=f"Also save the activity log, one text file per day, in {HISTORY_DIR}")
        self.history_cb.toggled.connect(self.toggle_history)
        set_history(self.conf["history"])
        top = QHBoxLayout()
        for c in self.cb.values():
            top.addWidget(c)
        top.addWidget(self.q, 1)
        for w in (QPushButton("Search", clicked=lambda: self.search()), self.saved, self.star,
                  QPushButton("⬇ Bulk", toolTip="Download every result of this search", clicked=self.bulk),
                  self.history_cb, QPushButton("📂", toolTip=f"Open history folder: {HISTORY_DIR}", clicked=self.open_history),
                  QPushButton("⚙", toolTip="Settings", clicked=self.settings)):
            top.addWidget(w)

        self.viewer = Viewer(self)
        self.viewer.finished.connect(self.refresh_favs)
        self.results, self.favs = Grid(self.viewer.open), Grid(self.viewer.open)
        self.results.verticalScrollBar().valueChanged.connect(
            lambda v: v >= self.results.verticalScrollBar().maximum() - THUMB and self.more())
        self.tabs = QTabWidget()
        self.tabs.addTab(self.results, "Results")
        self.tabs.addTab(self.favs, "Favorites")

        central = QWidget()
        lay = QVBoxLayout(central)
        lay.addLayout(top)
        lay.addWidget(self.tabs)
        self.setCentralWidget(central)
        self.cancel_btn = QPushButton("Cancel download", clicked=lambda: self.cancel and self.cancel.set())
        self.cancel_btn.hide()
        self.statusBar().addPermanentWidget(self.cancel_btn)

        # autocomplete: completes the last token only, debounced
        self.model = QStandardItemModel()
        self.completer = QCompleter(self.model, self)
        self.completer.setWidget(self.q)
        self.completer.setCompletionMode(QCompleter.UnfilteredPopupCompletion)
        self.completer.activated[QModelIndex].connect(self.insert_completion)
        self.timer = QTimer(self, singleShot=True, interval=250, timeout=self.autocomplete)
        self.q.textEdited.connect(lambda: self.timer.start())

        self.refresh_saved()
        self.refresh_favs()

    # --- search ---
    def save_sites(self):
        self.conf["sites"] = [s for s, c in self.cb.items() if c.isChecked()]
        save_conf(self.conf)

    def toggle_history(self, on):
        self.conf["history"] = on
        save_conf(self.conf)
        set_history(on)
        log.info("history %s (%s)", "on" if on else "off", HISTORY_DIR)

    def open_history(self):
        HISTORY_DIR.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(HISTORY_DIR)))

    def search(self, q=None):
        self.query = (self.q.text() if q is None else q).strip()
        self.q.setText(self.query)
        self.gen += 1
        self.page, self.loading, self.errs = 0, False, {}
        self.active = [s for s, c in self.cb.items() if c.isChecked()]
        if self.viewer.grid is self.results:
            self.viewer.hide()  # its post is about to be cleared
        self.results.restart()
        self.tabs.setCurrentWidget(self.results)
        self.star.setText("★" if self.query in self.conf["saved"] else "☆")
        if not self.active:
            self.statusBar().showMessage("Tick at least one site")
        else:
            log.info("search '%s' on %s", self.query, ", ".join(self.active))
        self.more()

    def more(self):
        if self.loading or not self.active:
            return
        self.loading = True
        self.page += 1
        gen, sites, page, query = self.gen, list(self.active), self.page, self.query
        self.statusBar().showMessage(f"Loading page {page}…")
        run(lambda: fetch_all(sites, self.conf, query, page), lambda r, e: self.got(gen, r, e))

    def got(self, gen, res, err):
        if gen != self.gen:
            return
        self.loading = False
        if err:
            log.error("search '%s' failed: %s", self.query, err)
            self.statusBar().showMessage(str(err))
            return
        lists, errs = res
        for s, e in errs.items():
            if s not in self.errs:
                log.warning(e)
        self.errs.update(errs)  # sticky: a site that failed on page 1 stays explained
        self.active = [s for s in self.active if lists[s]]  # drop exhausted/broken sites from paging
        bl = set(self.conf["blacklist"])
        posts = [p for p in interleave(lists.values()) if allowed(p, bl)]
        self.results.add(posts)
        counts = "  ".join(f"{SHORT[s]}:{len(l)}" for s, l in lists.items())
        msg = f"'{self.query}' page {self.page} — {self.results.count()} posts ({counts})"
        if self.errs:
            msg += "  |  " + "  ".join(self.errs.values())
        if not self.active:
            msg += "  — end of results"
        self.statusBar().showMessage(msg)
        log.info("'%s' page %d: %s", self.query, self.page, counts)
        if self.active and self.results.verticalScrollBar().maximum() == 0:
            QTimer.singleShot(0, self.more)  # page didn't fill the window

    # --- autocomplete ---
    def autocomplete(self):
        word = self.q.text().split(" ")[-1].lstrip("-")
        site = next((s for s, c in self.cb.items() if c.isChecked()), "danbooru")
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
        run(lambda: COMPLETE[site](self.conf, word), done)

    def insert_completion(self, index):
        head, _, last = self.q.text().rpartition(" ")
        neg = "-" if last.startswith("-") else ""
        self.q.setText(f"{head} {neg}{index.data(Qt.UserRole)} ".lstrip())

    # --- saved searches / favorites / blacklist ---
    def refresh_saved(self):
        self.saved.clear()
        self.saved.addItems(["Saved searches…"] + self.conf["saved"])

    def toggle_saved(self):
        q = self.q.text().strip()
        if not q:
            return
        s = self.conf["saved"]
        s.remove(q) if q in s else s.append(q)
        log.info("saved search %s '%s'", "added" if q in s else "removed", q)
        save_conf(self.conf)
        self.refresh_saved()
        self.star.setText("★" if q in s else "☆")

    def is_fav(self, p):
        return any(f["site"] == p["site"] and f["id"] == p["id"] for f in self.conf["favorites"])

    def toggle_fav(self, p):
        favs = self.conf["favorites"]
        if self.is_fav(p):
            favs[:] = [f for f in favs if not (f["site"] == p["site"] and f["id"] == p["id"])]
            log.info("unfavorite %s #%s", p["site"], p["id"])
        else:
            favs.insert(0, p)
            log.info("favorite %s #%s", p["site"], p["id"])
        save_conf(self.conf)
        if self.viewer.grid is not self.favs:  # don't pull rows out from under an open viewer
            self.refresh_favs()

    def refresh_favs(self):
        self.favs.restart()
        self.favs.add(self.conf["favorites"])
        self.tabs.setTabText(1, f"Favorites ({len(self.conf['favorites'])})")

    def blacklist_add(self, tag):
        self.conf["blacklist"] = sorted(set(self.conf["blacklist"]) | {tag.lower()})
        save_conf(self.conf)
        log.info("blacklist + %s", tag)
        self.statusBar().showMessage(f"Blacklisted '{tag}' (applies to new searches)")

    # --- downloads ---
    def download(self, p):
        query, root = self.query, self.conf["download_dir"]
        self.statusBar().showMessage(f"Downloading {SITES[p['site']]} #{p['id']}…")
        def done(r, e):
            msg = f"download {p['site']} #{p['id']}: " + (f"failed: {e}" if e else f"saved {r[0]}, already had {r[1]}")
            (log.error if e else log.info)(msg)
            self.statusBar().showMessage(msg)
        run(lambda: download_post(p, root, query), done)

    def bulk(self):
        if self.cancel:
            return self.statusBar().showMessage("A bulk download is already running")
        query = self.q.text().strip()
        sites = [s for s, c in self.cb.items() if c.isChecked()]
        if not sites:
            return
        limit, ok = QInputDialog.getInt(self, "Bulk download", f"Download '{query}' from {', '.join(SITES[s] for s in sites)}\n"
                                        f"into {self.conf['download_dir']}\n\nMax posts (0 = all):", 0, 0, 10 ** 6)
        if not ok:
            return
        self.cancel = cancel = threading.Event()
        log.info("bulk '%s' on %s, max %s", query, ", ".join(sites), limit or "all")
        self.cancel_btn.show()
        conf, bl, root = self.conf, set(self.conf["blacklist"]), self.conf["download_dir"]

        def progress(stats, err):
            self.statusBar().showMessage(f"Bulk '{query}': {stats['new']} new, {stats['skip']} skipped, {stats['fail']} failed, page {stats['page']}"
                                         + (f"  |  {stats['err']}" if stats["err"] else ""))

        def work():
            st = {"new": 0, "skip": 0, "fail": 0, "page": 0, "posts": 0, "err": ""}
            active = sites
            while active and not cancel.is_set() and not (limit and st["posts"] >= limit):
                st["page"] += 1
                lists, errs = fetch_all(active, conf, query, st["page"])
                st["err"] = "  ".join(errs.values())
                active = [s for s in active if lists[s]]
                posts = [p for p in interleave(lists.values()) if allowed(p, bl)]
                if limit:
                    posts = posts[:limit - st["posts"]]
                st["posts"] += len(posts)
                # ponytail: 4 parallel downloads per page, polite enough for boorus; Pixiv may still 429 on huge runs
                with ThreadPoolExecutor(4) as ex:
                    for f in [ex.submit(download_post, p, root, query, cancel) for p in posts]:
                        try:
                            n, s = f.result()
                            st["new"] += n
                            st["skip"] += s
                        except Exception as e:
                            st["fail"] += 1
                            st["err"] = str(e)
                bridge.done.emit(progress, dict(st), None)
            return st

        def finished(st, err):
            self.cancel = None
            self.cancel_btn.hide()
            if err:
                log.error("bulk '%s' failed: %s", query, err)
                return self.statusBar().showMessage(f"Bulk failed: {err}")
            progress(st, None)
            log.info("bulk '%s' %s: %d new, %d skipped, %d failed", query, "cancelled" if cancel.is_set() else "done",
                     st["new"], st["skip"], st["fail"])
            self.statusBar().showMessage(self.statusBar().currentMessage() + ("  — cancelled" if cancel.is_set() else "  — done"))

        threading.Thread(target=lambda: self._bulk_thread(work, finished), daemon=True).start()

    def _bulk_thread(self, work, finished):
        try:
            res, err = work(), None
        except Exception as e:
            res, err = None, e
        bridge.done.emit(finished, res, err)

    def settings(self):
        d = Settings(self)
        if d.exec():
            d.apply(self.conf)
            save_conf(self.conf)
            log.info("settings saved")
            self.statusBar().showMessage("Settings saved")


# ---------- self-check ----------

def selftest():
    db = parse_danbooru([
        {"id": 1, "file_url": "https://cdn.donmai.us/original/a.png", "large_file_url": "https://cdn.donmai.us/sample/a.jpg",
         "tag_string": "1girl solo", "rating": "g", "image_width": 10, "image_height": 20,
         "media_asset": {"variants": [{"type": "360x360", "url": "https://cdn.donmai.us/360/a.jpg"}]}},
        {"id": 2, "tag_string": "gold_only"},  # no file -> skipped
    ])
    assert len(db) == 1 and db[0]["thumb"].endswith("360/a.jpg") and db[0]["samples"][0].endswith("sample/a.jpg")
    assert db[0]["tags"] == ["1girl", "solo"] and not db[0]["video"]

    gb = parse_gel("gelbooru", {"@attributes": {"count": 1}, "post": [
        {"id": 5, "file_url": "https://img.gelbooru.com/x.webm", "preview_url": "p.jpg", "sample_url": "", "sample": 0,
         "tags": "a b", "rating": "sensitive"}]})
    assert gb[0]["video"] and gb[0]["rating"] == "s" and gb[0]["samples"] == gb[0]["files"]
    assert parse_gel("gelbooru", {"@attributes": {"count": 0}}) == []
    r34 = parse_gel("rule34", [{"id": 9, "file_url": "https://r34/x.jpg", "sample_url": "https://r34/s.jpg", "sample": True,
                                "preview_url": "https://r34/t.jpg", "tags": " foo  bar ", "rating": "safe"}])
    assert r34[0]["samples"] == ["https://r34/s.jpg"] and r34[0]["rating"] == "g" and r34[0]["tags"] == ["foo", "bar"]
    assert "rule34.xxx" in r34[0]["page"] and parse_gel("rule34", None) == []

    px = parse_pixiv({"illusts": [
        {"id": 7, "x_restrict": 1, "image_urls": {"square_medium": "sq", "large": "lg"}, "meta_single_page": {},
         "meta_pages": [{"image_urls": {"original": "https://i.pximg.net/o_p0.png", "large": "l0"}},
                        {"image_urls": {"original": "https://i.pximg.net/o_p1.png", "large": "l1"}}],
         "tags": [{"name": "オリジナル", "translated_name": "original"}, {"name": "女の子"}]},
        {"id": 8, "x_restrict": 0, "image_urls": {"square_medium": "sq", "large": "lg"},
         "meta_single_page": {"original_image_url": "https://i.pximg.net/o.jpg"}, "meta_pages": [], "tags": []}]})
    assert len(px[0]["files"]) == 2 and px[0]["samples"] == ["l0", "l1"] and px[0]["rating"] == "e"
    assert px[0]["tags"] == ["オリジナル", "original", "女の子"] and px[1]["files"] == ["https://i.pximg.net/o.jpg"]

    assert [p["id"] for p in interleave([db, px, r34])] == ["1", "7", "9", "8"]
    assert not allowed(px[0], {"original"}) and not allowed(px[0], {"rating:e"}) and allowed(px[1], {"rating:e"})
    assert not allowed(db[0], {"1girl"}) and allowed(db[0], {"2girls"})

    assert dest("/d", px[0], 1, "blue/hair *") == Path("/d/pixiv/blue_hair _/pixiv_7_p1.png")
    assert dest("/d", db[0], 0, "") == Path("/d/danbooru/_all/danbooru_1.png")
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        h = DailyHistory(tmp)
        log.setLevel(logging.INFO)
        log.addHandler(h)
        log.info("one")
        log.info("two")
        log.removeHandler(h)
        log.info("not saved")
        lines = (Path(tmp) / f"{time.strftime('%Y-%m-%d')}.txt").read_text().splitlines()
        assert [l.split(" ", 2)[2] for l in lines] == ["one", "two"], lines  # same day appends, toggle off stops
    print("selftest ok")


def main():
    global bridge
    if "--selftest" in sys.argv:
        return selftest()
    if sys.stderr:  # console window / terminal; absent under pythonw
        logging.basicConfig(format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
    log.setLevel(logging.INFO)
    app = QApplication(sys.argv)
    app.setApplicationName("imagesearcher")
    app.setDesktopFileName("imagesearcher")
    bridge = Bridge()
    w = Main()
    log.info("started")
    w.show()
    code = app.exec()
    log.info("quit")
    sys.exit(code)


if __name__ == "__main__":
    main()
