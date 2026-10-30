#!/usr/bin/env python3
"""imagesearcher: Danbooru-first tag / artist browser and downloader, with Gelbooru, Rule34.xxx and Pixiv folded in."""
import functools
import hashlib
import html
import importlib.util
import io
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
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from xml.etree import ElementTree

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
from PySide6.QtGui import (
    QColor, QDesktopServices, QIcon, QImage, QKeySequence, QPixmap, QShortcut, QStandardItem, QStandardItemModel,
)
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QCompleter, QDialog, QDialogButtonBox, QFileDialog, QFormLayout,
    QHBoxLayout, QInputDialog, QLabel, QLineEdit, QListWidget, QListWidgetItem, QMainWindow, QMenu,
    QPlainTextEdit, QPushButton, QScrollArea, QSizePolicy, QSpinBox, QSplitter, QTabBar, QTabWidget, QTreeWidget,
    QTreeWidgetItem, QVBoxLayout, QWidget,
)

SITES = {"danbooru": "Danbooru", "gelbooru": "Gelbooru", "rule34": "Rule34", "pixiv": "Pixiv"}
SHORT = {"danbooru": "DB", "gelbooru": "GB", "rule34": "R34", "pixiv": "PX"}
BOORUS = ("danbooru", "gelbooru", "rule34")
VIDEO_EXT = {".mp4", ".webm", ".zip"}  # .zip = Danbooru ugoira, its sample is a .webm
THUMB = 180
UA = "imagesearcher/1.0"
PIXIV_UA = "PixivAndroidApp/5.0.234 (Android 11; Pixel 5)"
PIXIV_REFERER = "https://app-api.pixiv.net/"
PIXIV_CLIENT = ("MOBrBDS8blbauoSck0ZfDbtuzpyT", "lsACyCD94FhDUtGTXi3QzcFE2uU1hqtDaKeqrdwj")
PIXIV_HASH = "28c1fdd170a5204386cb1313c7077b34f83e4aaf4aa829ce78c231e05b0bae2c"

CATS = {0: "general", 1: "artist", 3: "copyright", 4: "character", 5: "meta"}  # Danbooru and Gelbooru agree
CAT_COLOR = {"artist": "#e0474c", "copyright": "#b05ce6", "character": "#2fb344", "general": "#3b8eea", "meta": "#e8a33d"}
CJK = re.compile(r"[぀-ヿ㐀-鿿가-힯]")  # kana / kanji / hangul = a Pixiv-style tag
KANA = re.compile("[぀-ヿ]")  # hiragana / katakana: a Japanese name, what Pixiv tags use
META = re.compile(r"-?(rating|order|sort|score|favcount|id|pool|ordpool|user|fav|status|width|height|md5|source|pixiv"
                  r"|date|age|limit|is|has|parent|child|ratio|filetype|mpixels|tagcount):")
PX_SRC = re.compile(r"pximg\.net/.*/(\d+)_(?:p\d|ugoira)|pixiv\.net/(?:\w\w/)?artworks/(\d+)|illust_id=(\d+)")
PX_USER = re.compile(r"pixiv\.net/(?:\w\w/)?(?:users/|member\.php\?id=)(\d+)")
SORT = {"Newest": {}, "Score": {"danbooru": "order:score", "gelbooru": "sort:score:desc", "rule34": "sort:score:desc", "pixiv": "order:score"},
        "Random": {"danbooru": "order:random", "gelbooru": "sort:random", "rule34": "sort:random"}}
PX_RATE = {"g": "gs", "e": "qe"}  # Pixiv only knows all-ages vs R-18

CONF_PATH = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "imagesearcher" / "config.json"
DEFAULTS = {
    "danbooru": {"login": "", "api_key": ""},
    "gelbooru": {"user_id": "", "api_key": ""},
    "rule34": {"user_id": "", "api_key": ""},
    "pixiv": {"refresh_token": ""},
    "blacklist": [], "favorites": [], "saved": [], "sites": ["danbooru"], "limit": 40,
    "download_dir": str(Path.home() / "Pictures" / "imagesearcher"), "history": False,
    "follows": {},  # "tag:x" / "artist:x" -> {site: last seen id, "uid": Pixiv user id}
    "pixiv_map": {},  # booru tag -> Pixiv tag from its Danbooru wiki ("" = none known)
}
CONF_LOCK = threading.RLock()  # workers add to pixiv_map / refresh the Pixiv token while the UI saves


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
    with CONF_LOCK:
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
        try:
            msg = " " + json.loads(e.read())["message"]  # Danbooru explains its 4xx, e.g. the 2-tag limit
        except Exception:
            msg = ""
        if "2 tags" in msg:
            hint = " (a Danbooru login + API key in Settings lifts this)"
        raise RuntimeError(f"{SITES[site]}: HTTP {e.code}{msg}{hint if e.code in (401, 403, 422) else ''}") from None
    try:
        data = json.loads(body) if body else None
    except ValueError:
        data = body[:150].decode(errors="replace")
    if isinstance(data, str):  # R34 answers auth failures with a bare string
        raise RuntimeError(f"{SITES[site]}: {data}{hint}")
    return data


def qs(**params):
    return urllib.parse.urlencode({k: v for k, v in params.items() if v not in ("", None)})


def dan(path, conf=None, **params):
    """GET danbooru.donmai.us/<path>.json; logged in when conf has a Danbooru key."""
    c = conf["danbooru"] if conf else {}
    return get_json(f"https://danbooru.donmai.us/{path}.json?" + qs(**params, login=c.get("login"), api_key=c.get("api_key")), "danbooru")


def make_post(site, pid, thumb, files, samples, tags, rating, page, w=0, h=0, **extra):
    ext = Path(urllib.parse.urlparse(files[0]).path).suffix.lower()
    return {"site": site, "id": str(pid), "thumb": thumb or (samples or files)[0], "files": files,
            "samples": samples or files, "tags": tags, "rating": rating, "page": page, "w": w, "h": h,
            "video": ext in VIDEO_EXT, "pixiv_id": None, "md5": "", **extra}


def norm_rating(r):
    r = str(r or "").lower()
    return "g" if r in ("safe", "general") else r[:1]


def px_id(source):
    """Pixiv illust id from a booru 'source' URL, else None."""
    m = PX_SRC.search(source or "")
    return next((g for g in m.groups() if g), None) if m else None


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
                             f"https://danbooru.donmai.us/posts/{p['id']}", p.get("image_width", 0), p.get("image_height", 0),
                             pixiv_id=str(p["pixiv_id"]) if p.get("pixiv_id") else px_id(p.get("source")), md5=p.get("md5", ""),
                             cats={c: p.get(f"tag_string_{c}", "").split() for c in CAT_COLOR}))
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
                             norm_rating(p.get("rating")), GEL[site][1] + str(p["id"]), p.get("width", 0), p.get("height", 0),
                             pixiv_id=px_id(p.get("source")), md5=p.get("md5") or p.get("hash") or ""))
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


def parse_gel_tags(site, body):
    """Gelbooru answers the tag dapi in JSON, Rule34 only in XML."""
    if body.startswith(b"<"):
        return [dict(t.attrib) for t in ElementTree.fromstring(body).iter("tag")]
    try:
        data = json.loads(body) if body else []
    except ValueError:
        raise RuntimeError(f"{SITES[site]}: {body[:150].decode(errors='replace')} (set user id + API key in Settings)") from None
    return data.get("tag", []) if isinstance(data, dict) else data


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


def pixiv_api(conf, path, **params):
    return get_json(f"https://app-api.pixiv.net/{path}?" + qs(**params), "pixiv", headers=pixiv_headers(conf))


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
        tags = list(dict.fromkeys(n for t in i.get("tags", []) for n in (t["name"], t.get("translated_name")) if n))
        out.append(make_post("pixiv", i["id"], i["image_urls"]["square_medium"], files, samples, tags,
                             "e" if i.get("x_restrict") else "g", f"https://www.pixiv.net/artworks/{i['id']}",
                             i.get("width", 0), i.get("height", 0), pixiv_id=str(i["id"]), ugoira=i.get("type") == "ugoira",
                             user=str((i.get("user") or {}).get("id", ""))))
    return out


def pixiv_names(conf, tags, lookup=True):
    """booru tags -> {tag: Pixiv tag}: the first kana/kanji name in the tag's Danbooru wiki, cached in conf.
    Unknown tags fall back to the tag itself, underscores as spaces and the _(qualifier) dropped."""
    m = conf["pixiv_map"]
    todo = [t for t in dict.fromkeys(tags) if t not in m and not CJK.search(t)]
    if todo and lookup:
        try:
            found = {w["title"]: next((n for n in sorted(w["other_names"], key=lambda n: not KANA.search(n)) if CJK.search(n)), "")
                     for w in dan("wiki_pages", **{"search[title_comma]": ",".join(todo), "only": "title,other_names", "limit": 100}) or []}
            with CONF_LOCK:
                m.update({t: found.get(t, "") for t in todo})  # "" = no Pixiv name, don't ask again
        except (RuntimeError, OSError):
            pass  # Danbooru down: fall back for now, try again next time
    return {t: t if CJK.search(t) else m.get(t) or re.sub(r"_\([^)]*\)$", "", t).replace("_", " ") for t in tags}


def pixiv_query(conf, q, lookup=True):
    """booru query -> (Pixiv search words, all-ages only, popular sort). Exclusions and metatags have no Pixiv form."""
    tags, sfw, popular = [], False, False
    for t in q.split():
        if t.startswith("rating:"):
            sfw = all(r[:1] in "gs" for r in t[7:].split(","))
        elif t in ("-rating:e", "-rating:explicit"):
            sfw = True
        elif t.startswith(("order:score", "order:favcount", "sort:score")):
            popular = True
        elif not t.startswith("-") and not META.match(t):
            tags.append(t)
    words = list(pixiv_names(conf, tags, lookup).values())
    # ponytail: generic booru tags (1girl, solo) wreck a Pixiv search, so once any tag has a real Pixiv name use only those
    cjk = [w for w in words if CJK.search(w)]
    return " ".join(cjk or words), sfw, popular


def search_pixiv(conf, tags, page):
    word, sfw, popular = pixiv_query(conf, tags)
    if not word:
        raise RuntimeError("Pixiv: nothing to search for (only metatags)")
    if popular and page > 1:  # popular-preview is a single page: Pixiv's free popularity sort
        return []
    posts = parse_pixiv(pixiv_api(conf, "v1/search/" + ("popular-preview/illust" if popular else "illust"), word=word,
                                  search_target="partial_match_for_tags", sort="date_desc", filter="for_android",
                                  offset=None if popular else (page - 1) * 30))
    return [p for p in posts if p["rating"] == "g"] if sfw else posts


def pixiv_user(conf, arg, page):
    """A Pixiv user's works; arg "uid>last" keeps only works newer than last (followed artists)."""
    uid, _, last = str(arg).partition(">")
    # ponytail: type=illust only, manga-type posts would need a second type=manga feed
    posts = parse_pixiv(pixiv_api(conf, "v1/user/illusts", user_id=uid, type="illust", filter="for_android", offset=(page - 1) * 30))
    return [p for p in posts if int(p["id"]) > int(last or 0)]


def pixiv_related(conf, iid, page):
    if page > 1:  # ponytail: first page only, follow next_url if more is ever wanted
        return []
    return parse_pixiv(pixiv_api(conf, "v2/illust/related", illust_id=iid, filter="for_android"))


def complete_pixiv(conf, word):
    tags = (pixiv_api(conf, "v2/search/autocomplete", word=word, merge_plain_keyword_results="true") or {}).get("tags", [])
    return [(t["name"], f"{t['name']}  ({t['translated_name']})" if t.get("translated_name") else t["name"]) for t in tags]


def ugoira(conf, iid):
    """-> (frames zip url, [(frame file, delay ms)])"""
    m = pixiv_api(conf, "v1/ugoira/metadata", illust_id=iid)["ugoira_metadata"]
    return m["zip_urls"]["medium"], [(f["file"], f["delay"]) for f in m["frames"]]


def ugoira_frames(conf, iid):
    url, frames = ugoira(conf, iid)
    with zipfile.ZipFile(io.BytesIO(http(url))) as z:
        return [(QImage.fromData(z.read(f)), d) for f, d in frames]


SEARCH = {"danbooru": search_danbooru, "pixiv": search_pixiv,
          "gelbooru": lambda *a: search_gel("gelbooru", *a), "rule34": lambda *a: search_gel("rule34", *a)}
FEEDS = {"user": pixiv_user, "related": pixiv_related}  # non-tag Pixiv sources
COMPLETE = {"danbooru": complete_danbooru, "pixiv": complete_pixiv,
            "gelbooru": lambda *a: complete_gel("gelbooru", *a), "rule34": lambda *a: complete_gel("rule34", *a)}


def fetch(conf, src, page):
    site, kind, arg = src
    return (SEARCH[site] if kind == "tags" else FEEDS[kind])(conf, arg, page)


def fetch_all(srcs, conf, page):
    """Query sources (site, kind, arg) in parallel -> ({src: posts}, {src: error})."""
    lists, errs = {}, {}
    with ThreadPoolExecutor(len(srcs) or 1) as ex:
        futs = {s: ex.submit(fetch, conf, s, page) for s in srcs}
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


def rating_ok(post, ratings):
    """Client-side rating filter (Danbooru's anonymous 2-tag limit counts rating: tokens). Unknown ratings pass."""
    r = post["rating"]
    return not r or bool(set(PX_RATE.get(r, r) if post["site"] == "pixiv" else r) & ratings)


def fold(seen, p):
    """Cross-site dedupe within one feed. seen maps "md5:…" / "px:…" -> slot, a list whose [0] is the shown post.
    -> (slot, new): new = add a tile, else slot[0] was updated in place; None = drop p.
    Booru copies win (better tags); the Pixiv copy rides along as post["alt"]."""
    md5, px = p.get("md5"), p.get("pixiv_id")
    if md5 and "md5:" + md5 in seen:
        return None
    twin = seen.get("px:" + px) if px else None
    if twin and p["site"] == "pixiv":
        if twin[0]["site"] == "pixiv" or twin[0].get("alt"):
            return None
        twin[0] = {**twin[0], "alt": p}
        return twin, False
    if twin and twin[0]["site"] == "pixiv":  # booru copy of a Pixiv tile: take its place
        twin[0] = {**p, "alt": twin[0]}
        slot, new = twin, False
    else:  # new, or one more booru post of a multi-page Pixiv work
        slot, new = [p], True
    if md5:
        seen["md5:" + md5] = slot
    if px:
        seen.setdefault("px:" + px, slot)
    return slot, new


def follow_srcs(follows):
    """{key: {site: last id, "uid": Pixiv user}} -> {source: (key, site)} for posts newer than last seen."""
    out = {}
    for key, f in follows.items():
        tag = key.split(":", 1)[1]
        for site, last in f.items():
            if site == "pixiv" and f.get("uid"):
                out["pixiv", "user", f"{f['uid']}>{last}"] = key, site
            elif site in BOORUS:
                out[site, "tags", f"{tag} id:>{last}"] = key, site
    return out


def list_tags(conf, site, mode, order, word, page):
    """Tag (or Danbooru pool) listing -> [{name, count, cat, …}]; mode = a category name, "All" or "Pools"."""
    pat = word if "*" in word else f"*{word}*" if word else ""
    if mode == "Pools":
        rows = dan("pools", conf, **{"search[name_matches]": pat, "search[is_deleted]": "false", "page": page, "limit": 40,
                                     "search[order]": {"Count": "post_count", "Name": "name", "Newest": "created_at"}[order],
                                     "only": "id,name,post_count,description"})
        return [{"name": p["name"], "count": p["post_count"], "cat": "pool", "id": p["id"], "desc": p["description"]} for p in rows or []]
    cat = {v: k for k, v in CATS.items()}.get(mode.lower())
    if site == "danbooru":
        rows = dan("tags", conf, **{"search[name_matches]": pat, "search[category]": cat, "search[hide_empty]": "true",
                                    "search[order]": {"Count": "count", "Name": "name", "Newest": "date"}[order],
                                    "page": page, "limit": 40, "only": "name,post_count,category"})
        return [{"name": t["name"], "count": t["post_count"], "cat": CATS.get(t["category"], "general")} for t in rows or []]
    c = conf[site]
    url = GEL[site][0] + "?" + qs(page="dapi", s="tag", q="index", json=1, name_pattern=pat.replace("*", "%"), pid=page - 1, limit=40,
                                  orderby={"Count": "count", "Name": "name", "Newest": "date"}[order], order="ASC" if order == "Name" else "DESC",
                                  api_key=c["api_key"], user_id=c["user_id"])
    try:
        body = http(url).strip()
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"{SITES[site]}: HTTP {e.code}") from None
    # ponytail: Gelbooru can't filter by category server-side, so a filtered page may come back short or empty
    return [{"name": t["name"], "count": int(t.get("count", 0)), "cat": CATS.get(int(t.get("type", 0)), "general")}
            for t in parse_gel_tags(site, body) if cat is None or int(t.get("type", 0)) == cat]


def dtext(s, n=600):
    """Danbooru wiki DText -> plain excerpt."""
    s = re.sub(r"\[\[(?:[^]|]*\|)?([^]]*)\]\]", r"\1", s or "")  # [[tag|label]] -> label
    s = re.sub(r"\[/?\w+(?:=[^]]*)?\]|^h\d\.\s*", "", s, flags=re.M).strip()  # [b], [url=…], h4.
    return s[:n] + ("…" if len(s) > n else "")


@functools.lru_cache(256)  # ponytail: never expires; a wiki edit shows after restart
def tag_info(tag):
    """Danbooru wiki excerpt, other names (Pixiv tags) and related tags of one tag."""
    try:
        wiki = dan(f"wiki_pages/{urllib.parse.quote(tag, safe='')}") or {}
    except RuntimeError:
        wiki = {}  # no wiki page
    rel = dan("related_tag", query=tag, limit=25) or {}
    return {"wiki": dtext(wiki.get("body")), "names": [n for n in wiki.get("other_names", []) if CJK.search(n)][:8],
            "related": [(t["tag"]["name"], CATS.get(t["tag"]["category"], "general")) for t in rel.get("related_tags", [])
                        if t["tag"]["name"] != tag]}


def link(kind, text, cat="", label=None):
    """<a> whose href "kind:cat:text" is handled by Main.link (or a tab's own link())."""
    style = f" style='color:{CAT_COLOR[cat]}'" if cat in CAT_COLOR else ""
    return f"<a href='{kind}:{cat}:{urllib.parse.quote(text)}'{style}>{html.escape(label or text)}</a>"


def tag_html(info):
    out = ""
    if info["names"]:
        out += "<p><b>Pixiv:</b> " + " · ".join(link("px", n) for n in info["names"]) + "</p>"
    if info["wiki"]:
        out += f"<p>{html.escape(info['wiki'])}</p>"
    if info["related"]:
        out += "<p><b>Related:</b> " + " ".join(link("tag", t, c) for t, c in info["related"]) + "</p>"
    return out


def artist_info(conf, tag):
    """Danbooru artist entry -> Pixiv user id + profile, and the Pixiv ids of works already on Danbooru."""
    a = (dan("artists", conf, **{"search[name]": tag, "only": "name,other_names,urls"}) or [{}])[0]
    urls = [u["url"] for u in a.get("urls", [])]
    uid = next((m[1] for u in urls if (m := PX_USER.search(u))), None)
    # ponytail: newest 200 posts only, page further if big artists show false "missing" marks
    ids = {str(p["pixiv_id"]) for p in dan("posts", conf, tags=tag, only="pixiv_id", limit=200) or [] if p.get("pixiv_id")}
    user = None
    if uid and conf["pixiv"]["refresh_token"]:
        try:
            user = pixiv_api(conf, "v1/user/detail", user_id=uid, filter="for_android")
        except (RuntimeError, OSError):
            pass
    return {"names": a.get("other_names", []), "urls": urls, "uid": uid, "user": user, "ids": ids}


def dest(root, post, i, query):
    folder = re.sub(r"[^\w\-+.() ]", "_", query).strip()[:100] or "_all"
    name = Path(urllib.parse.urlparse(post["files"][i]).path)
    page = f"_p{i}" if len(post["files"]) > 1 else ""
    return Path(root) / post["site"] / folder / f"{post['site']}_{post['id']}{page}{name.suffix}"


CACHE_MARK = ".imagesearcher-cache"  # only a cache/ holding this file is ever deleted
CACHE_ROOTS = set()  # download dirs used this session, their cache/ goes on exit


def local(root, post, i, folder):
    """Original i of post in <download dir>/<folder>/ ("cache" or "favorites")."""
    return Path(root) / folder / dest(root, post, i, "").name


def cached(root, post, i):
    """The original already on disk (favorites first), else None."""
    return next((f for f in (local(root, post, i, "favorites"), local(root, post, i, "cache")) if f.exists()), None)


def fetch_file(url, path):
    """Download url to path once, atomically (two tabs may fetch the same file) -> path."""
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.parent.name == "cache":
            (path.parent / CACHE_MARK).touch()
            CACHE_ROOTS.add(path.parent.parent)
        tmp = path.with_name(f"{path.name}.{threading.get_ident()}.part")
        with open_url(url, timeout=120) as r, open(tmp, "wb") as f:
            shutil.copyfileobj(r, f)
        os.replace(tmp, path)
    return path


def move_favorite(root, post, fav):
    """Favorite: cached originals move into favorites/, pages never opened get downloaded there.
    Unfavorite: they move back to cache/, which is deleted on exit."""
    src, dst = ("cache", "favorites") if fav else ("favorites", "cache")
    for i, url in enumerate(post["files"]):
        a, b = local(root, post, i, src), local(root, post, i, dst)
        if b.exists():
            continue
        if a.exists():
            b.parent.mkdir(parents=True, exist_ok=True)
            if dst == "cache":
                (b.parent / CACHE_MARK).touch()
                CACHE_ROOTS.add(b.parent.parent)
            os.replace(a, b)
        elif fav:
            fetch_file(url, b)


def clear_cache(root):
    d = Path(root) / "cache"
    if (d / CACHE_MARK).exists():
        shutil.rmtree(d, ignore_errors=True)


def download_post(post, root, query, cancel=None, conf=None):
    """-> (new, skipped)"""
    if post.get("ugoira") and conf:
        return download_ugoira(conf, post, root, query)
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
        if c := cached(root, post, i):  # already opened or favorited: copy instead of downloading again
            shutil.copyfile(c, tmp)
        else:
            with open_url(url, timeout=120) as r, open(tmp, "wb") as f:
                shutil.copyfileobj(r, f)
        tmp.rename(d)
        new += 1
    return new, skip


def download_ugoira(conf, post, root, query):
    """Pixiv animation: the frames zip (full size when Pixiv has it) + a .json of frame delays.
    ponytail: no gif/webm conversion, that needs ffmpeg"""
    d = dest(root, post, 0, query).with_suffix(".zip")
    if d.exists():
        return 0, 1
    url, frames = ugoira(conf, post["id"])
    try:
        data = http(url.replace("_ugoira600x600.zip", "_ugoira1920x1080.zip"), timeout=120)
    except urllib.error.HTTPError:
        data = http(url, timeout=120)
    d.parent.mkdir(parents=True, exist_ok=True)
    d.with_suffix(".json").write_text(json.dumps([{"file": f, "delay": ms} for f, ms in frames]))
    tmp = d.with_name(d.name + ".part")
    tmp.write_bytes(data)
    tmp.rename(d)
    return 1, 0


# ---------- Qt plumbing ----------

class Bridge(QObject):
    done = Signal(object, object, object)

    def __init__(self):
        super().__init__()
        self.done.connect(self._call)

    def _call(self, cb, res, err):
        cb(res, err)


POOLS = {"net": ThreadPoolExecutor(4), "thumb": ThreadPoolExecutor(8), "view": ThreadPoolExecutor(4),  # view: sample + original
         "fav": ThreadPoolExecutor(1)}  # favorite file moves: one at a time, in click order
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


def rich_label():
    lab = QLabel(wordWrap=True, textFormat=Qt.RichText, textInteractionFlags=Qt.TextBrowserInteraction, alignment=Qt.AlignTop)
    lab.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
    return lab


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

    def set_post(self, it, p):
        pages = f" [{len(p['files'])}]" if len(p["files"]) > 1 else ""
        alt = "+" + SHORT[p["alt"]["site"]] if p.get("alt") else ""
        anim = " ▶" if p["video"] or p.get("ugoira") else ""
        it.setText(f"{SHORT[p['site']]}{alt} {p['id']}{pages}{anim}")
        it.setData(Qt.UserRole, p)
        it.setToolTip(" ".join(p["tags"][:40]))
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
    """A paged, cross-site-deduped grid over sources [(site, kind, arg)]: search results, tag tabs, artist panes, New."""

    def __init__(self, main, label=""):
        super().__init__()
        self.main, self.label, self.srcs, self.note = main, label, [], ""
        self.keep = self.mark = None  # optional post filter / tile decorator
        self.gen = self.page = 0
        self.loading, self.active, self.errs, self.seen = False, [], {}, {}
        self.grid = Grid(main.open_image)
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
        self.status.setText("Nothing to search." + self.note if not self.srcs else "")
        self.more()

    def stop(self):
        self.gen += 1
        self.active = []

    def more(self):
        if self.loading or not self.active:
            return
        self.loading = True
        self.page += 1
        gen, srcs, page, conf = self.gen, list(self.active), self.page, self.main.conf
        self.status.setText(f"Loading page {page}…")
        run(lambda: fetch_all(srcs, conf, page), lambda r, e: self.got(gen, r, e))

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
        self.errs.update(errs)  # sticky: a site that failed on page 1 stays explained
        self.active = [s for s in self.active if lists[s]]  # drop exhausted/broken sources from paging
        bl = set(self.main.conf["blacklist"])
        for p in interleave(lists.values()):
            if allowed(p, bl) and (not self.keep or self.keep(p)):
                self.place(p)
        counts = "  ".join(f"{SHORT[s[0]]}:{len(l)}" for s, l in lists.items())
        via = "".join(f'  PX via "{pixiv_query(self.main.conf, s[2], False)[0]}"' for s in self.srcs if s[:2] == ("pixiv", "tags"))
        msg = f"'{self.label}' page {self.page}: {self.grid.count()} posts ({counts}){via}{self.note}"
        if self.errs:
            msg += "  |  " + "  ".join(self.errs.values())
        if not self.active:
            msg += "  — end of results"
        self.status.setText(msg)
        log.info("'%s' page %d: %s", self.label, self.page, counts)
        self.loaded(lists)
        if self.active and self.grid.verticalScrollBar().maximum() == 0:
            QTimer.singleShot(0, self.more)  # page didn't fill the window

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


class TagView(Feed):
    """One tag's images: the boorus plus Pixiv through the tag bridge, with wiki header and related tags."""

    def __init__(self, main, tag, pixiv=None, sites=None, title=None):
        super().__init__(main, title or tag or pixiv or "all posts")
        self.base, self.tag, self.pixiv, self.sites, self.info = tag, tag, pixiv, sites, None
        self.head = rich_label()
        self.head.linkActivated.connect(self.link)
        self.src, self.sort = QComboBox(), QComboBox()
        self.src.addItems(["Both", "Boorus", "Pixiv"])
        self.sort.addItems(list(SORT))
        self.rate = {r: QCheckBox(r, checked=True, toolTip="rating filter") for r in "gsqe"}
        self.follow = QPushButton(clicked=self.toggle_follow, enabled=bool(tag) and ":" not in tag)
        row = QHBoxLayout()
        for w in (self.src, self.sort, QLabel("rating:"), *self.rate.values()):
            row.addWidget(w)
        row.addStretch()
        for w in (self.follow, QPushButton("⬇ Bulk", clicked=lambda: main.bulk(self)),
                  QPushButton("Open on Danbooru", clicked=lambda: webbrowser.open("https://danbooru.donmai.us/posts?" + qs(tags=self.tag)))):
            row.addWidget(w)
        scroll = QScrollArea(widgetResizable=True, maximumHeight=220)
        scroll.setWidget(self.head)
        self.lay.insertWidget(0, scroll)
        self.lay.insertLayout(1, row)
        for w in (self.src, self.sort):
            w.currentIndexChanged.connect(self.reload)
        for c in self.rate.values():
            c.toggled.connect(self.reload)
        self.render()
        self.reload()
        if tag and ":" not in tag:  # pools / metatags have no wiki
            run(lambda: (tag_info(tag), dan("counts/posts", tags=tag)["counts"]["posts"]), self.got_head)

    def reload(self):
        conf, mode, sort = self.main.conf, self.src.currentText(), SORT[self.sort.currentText()]
        boorus = self.sites or [s for s in BOORUS if self.main.cb[s].isChecked()] or ["danbooru"]
        srcs = [(s, "tags", f"{self.tag} {sort.get(s, '')}".strip()) for s in boorus] if mode != "Pixiv" and (self.tag or not self.pixiv) else []
        pq = f"{self.pixiv}{self.tag[len(self.base):]}" if self.pixiv else self.tag
        pq = f"{pq} {sort.get('pixiv', '')}".strip()
        has_token = bool(conf["pixiv"]["refresh_token"])
        if mode != "Boorus" and has_token and pixiv_query(conf, pq, False)[0] and not self.sites:
            srcs.append(("pixiv", "tags", pq))
        self.note = "  (Pixiv: add a refresh token in ⚙)" if mode != "Boorus" and not has_token and not self.sites else ""
        ratings = {r for r, c in self.rate.items() if c.isChecked()}
        self.keep = None if len(ratings) == 4 else lambda p: rating_ok(p, ratings)
        self.follow.setText("★ Following" if f"tag:{self.base}" in conf["follows"] else "☆ Follow")
        self.start(srcs)

    def got_head(self, res, err):
        if not err:
            self.info, self.count = res
            self.render()

    def render(self):
        title = html.escape(self.label)
        extra = self.tag[len(self.base):].strip()
        out = f"<h3>{title}</h3>"
        if extra:
            out += f"<p>narrowed by <b>{html.escape(extra)}</b> · {link('reset', '', label='⟲ reset')}</p>"
        if self.pixiv:
            out += f"<p>Pixiv pinned to <b>{html.escape(self.pixiv)}</b></p>"
        if self.info:
            out += f"<p>{self.count:,} posts on Danbooru</p>" + tag_html(self.info)
        self.head.setText(out)

    def link(self, href):
        kind, cat, text = href.split(":", 2)
        text = urllib.parse.unquote(text)
        ctrl = QApplication.keyboardModifiers() & Qt.ControlModifier
        if kind == "reset":
            self.tag = self.base
        elif kind == "tag" and self.tag and not ctrl:  # narrow this tab; Ctrl+click opens the tag in its own tab
            self.tag = f"{self.tag} {text}"
        else:
            return self.main.link(href, self.base)
        self.render()
        self.reload()

    def toggle_follow(self):
        on = self.main.toggle_follow(f"tag:{self.base}")
        self.follow.setText("★ Following" if on else "☆ Follow")


class NewFeed(Feed):
    """Posts newer than the last one seen, for every followed tag and artist."""

    def __init__(self, main):
        super().__init__(main, "New")
        self.keys, self.newest = {}, {}
        self.head = rich_label()
        self.head.linkActivated.connect(self.link)
        row = QHBoxLayout()
        row.addWidget(self.head, 1)
        for w in (QPushButton("⟳ Refresh", clicked=self.reload), QPushButton("✓ Mark all seen", clicked=self.mark_seen)):
            row.addWidget(w)
        self.lay.insertLayout(0, row)
        # ponytail: checks only while the app runs; no background service
        QTimer(self, interval=15 * 60 * 1000, timeout=self.auto).start()
        self.reload()

    def reload(self):
        f = self.main.conf["follows"]
        self.keys, self.newest = follow_srcs(f), {}
        self.head.setText("Following: " + "  ".join(f"{link('open', k, label=k.split(':', 1)[1])} {link('unf', k, label='×')}" for k in f)
                          if f else "Follow tags or artists (☆ in their tabs) to collect their new posts here.")
        self.start(list(self.keys))
        self.main.set_new_count("")

    def auto(self):
        if not self.isVisible():  # don't reshuffle the grid under the user's eyes
            self.reload()

    def loaded(self, lists):
        for src, posts in lists.items():
            if posts:
                self.newest[src] = max(self.newest.get(src, 0), *(int(p["id"]) for p in posts))
        n = self.grid.count()
        self.main.set_new_count(f"{n}{'+' if self.active else ''}" if n else "")

    def mark_seen(self):
        f = self.main.conf["follows"]
        for src, top in self.newest.items():
            key, site = self.keys[src]
            if key in f:
                f[key][site] = max(f[key].get(site, 0), top)
        save_conf(self.main.conf)
        log.info("new: marked seen")
        self.reload()

    def link(self, href):
        kind, _, key = href.split(":", 2)
        key = urllib.parse.unquote(key)
        if kind == "unf":
            self.main.toggle_follow(key)
        else:
            what, name = key.split(":", 1)
            self.main.open_artist(name) if what == "artist" else self.main.open_tag(name)


class ArtistView(QWidget):
    """A booru artist tag next to the artist's Pixiv works (the Pixiv account comes from Danbooru's artist URLs)."""

    def __init__(self, main, tag):
        super().__init__()
        self.main, self.tag, self.uid, self.ids = main, tag, None, set()
        self.head = rich_label()
        self.head.linkActivated.connect(lambda h: webbrowser.open(h) if h.startswith("http") else main.link(h, tag))
        self.head.setText(f"<h2 style='color:{CAT_COLOR['artist']}'>{html.escape(tag)}</h2><i>looking up artist…</i>")
        self.booru, self.px = Feed(main, tag), Feed(main, f"{tag} (Pixiv)")
        self.px.mark = self.mark
        self.missing = QCheckBox("Pixiv: only works not on Danbooru")
        self.missing.toggled.connect(self.refilter)
        self.follow = QPushButton("★ Following" if f"artist:{tag}" in main.conf["follows"] else "☆ Follow", clicked=self.toggle_follow)
        row = QHBoxLayout()
        for w in (self.missing, self.follow, QPushButton("⬇ Bulk booru", clicked=lambda: main.bulk(self.booru)),
                  QPushButton("⬇ Bulk Pixiv", clicked=lambda: main.bulk(self.px))):
            row.addWidget(w)
        row.insertStretch(1)
        self.split = split = QSplitter()
        split.addWidget(self.booru)
        split.addWidget(self.px)
        lay = QVBoxLayout(self)
        lay.addWidget(self.head)
        lay.addLayout(row)
        lay.addWidget(split, 1)
        self.booru.start([(s, "tags", tag) for s in BOORUS if main.cb[s].isChecked()] or [("danbooru", "tags", tag)])
        conf = main.conf
        run(lambda: artist_info(conf, tag), self.got_info)

    def got_info(self, a, err):
        name = f"<h2 style='color:{CAT_COLOR['artist']}'>{html.escape(self.tag)}</h2>"
        if err:
            return self.head.setText(f"{name}<i>{html.escape(str(err))}</i>")
        self.uid, self.ids = a["uid"], a["ids"]
        out = name + ("<p>aka " + ", ".join(map(html.escape, a["names"])) + "</p>" if a["names"] else "")
        if a["user"]:
            u = a["user"]
            out += (f"<p>Pixiv: <a href='https://www.pixiv.net/users/{self.uid}'>{html.escape(u['user']['name'])}</a>"
                    f" · {u['profile'].get('total_illusts', 0)} illusts · {len(self.ids)} of the newest 200 Danbooru posts link to Pixiv</p>")
        out += "<p>" + " · ".join(f"<a href='{html.escape(u)}'>{html.escape(urllib.parse.urlparse(u).netloc)}</a>" for u in a["urls"]) + "</p>"
        self.head.setText(out)
        if not self.uid or not self.main.conf["pixiv"]["refresh_token"]:
            self.split.setSizes([3000, 1000])  # nothing to show on the Pixiv side, give the booru the room
        if not self.uid:
            self.px.status.setText("No Pixiv account on this artist's Danbooru entry.")
        elif not self.main.conf["pixiv"]["refresh_token"]:
            self.px.status.setText("Pixiv: paste a refresh token in Settings (see README).")
        else:
            self.px.start([("pixiv", "user", self.uid)])

    def mark(self, it, p):
        if p["pixiv_id"] in self.ids:
            it.setText(it.text() + " ✓DB")
            it.setForeground(QColor("gray"))

    def refilter(self, on):
        self.px.keep = (lambda p: p["pixiv_id"] not in self.ids) if on else None
        if self.px.srcs:
            self.px.start(self.px.srcs)

    def toggle_follow(self):
        on = self.main.toggle_follow(f"artist:{self.tag}", self.uid)
        self.follow.setText("★ Following" if on else "☆ Follow")


class TagsTab(QWidget):
    """Browse Danbooru tags (or Gelbooru / R34 ones) and Danbooru pools; double-click opens a tag tab."""

    def __init__(self, main):
        super().__init__()
        self.main, self.cur, self.page, self.gen, self.loading, self.done = main, None, 0, 0, False, False
        self.q = QLineEdit(placeholderText="filter tags…   (* = wildcard; empty = most used)")
        self.mode, self.order, self.site = QComboBox(), QComboBox(), QComboBox()
        self.mode.addItems(["All", "General", "Artist", "Copyright", "Character", "Meta", "Pools"])
        self.order.addItems(["Count", "Name", "Newest"])
        for s in BOORUS:
            self.site.addItem(SITES[s], s)
        self.timer = QTimer(self, singleShot=True, interval=300, timeout=self.reload)
        self.q.textEdited.connect(lambda: self.timer.start())
        for w in (self.mode, self.order, self.site):
            w.currentIndexChanged.connect(self.reload)
        top = QHBoxLayout()
        for w in (self.q, self.mode, self.order, self.site):
            top.addWidget(w, int(w is self.q))

        self.list = QTreeWidget(rootIsDecorated=False, uniformRowHeights=True)
        self.list.setHeaderLabels(["Tag", "Posts", "Pixiv"])
        self.list.setColumnWidth(0, 300)
        self.list.currentItemChanged.connect(lambda it, _: self.detail(it))
        self.list.itemActivated.connect(lambda it, _: self.open(it))
        sb = self.list.verticalScrollBar()
        sb.valueChanged.connect(lambda v: v >= sb.maximum() - 50 and self.more())

        self.info = rich_label()
        self.info.linkActivated.connect(lambda h: main.link(h, self.cur and self.cur["name"]))
        scroll = QScrollArea(widgetResizable=True)
        scroll.setWidget(self.info)
        self.follow = QPushButton("☆ Follow", clicked=self.toggle_follow)
        btns = QHBoxLayout()
        for w in (QPushButton("Open", clicked=lambda: self.open()), self.follow,
                  QPushButton("Blacklist", clicked=lambda: self.cur and main.blacklist_add(self.cur["name"])),
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
        self.reload()

    def reload(self):
        self.gen += 1
        self.page, self.done, self.loading = 0, False, False
        self.list.clear()
        self.more()

    def more(self):
        if self.loading or self.done:
            return
        self.loading = True
        self.page += 1
        gen, conf = self.gen, self.main.conf
        args = (self.site.currentData(), self.mode.currentText(), self.order.currentText(), self.q.text().strip(), self.page)
        run(lambda: list_tags(conf, *args), lambda r, e: self.got(gen, r, e))

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
        names, conf = [r["name"] for r in rows if r["cat"] != "pool"], self.main.conf
        if names:  # fill the Pixiv column from the wikis, one batched request per page
            run(lambda: pixiv_names(conf, names),
                lambda m, e: gen == self.gen and [it.setText(2, conf["pixiv_map"].get(it.text(0), "")) for it in items])
        if not self.done and self.list.verticalScrollBar().maximum() == 0:
            QTimer.singleShot(0, self.more)

    def detail(self, it):
        r = self.cur = it and it.data(0, Qt.UserRole)
        if not r:
            return
        color = CAT_COLOR.get(r["cat"], "")
        head = f"<h2 style='color:{color}'>{html.escape(r['name'])}</h2>{r['count']:,} posts · {r['cat']}"
        self.follow.setEnabled(r["cat"] != "pool")
        self.follow.setText("★ Following" if self.key(r) in self.main.conf["follows"] else "☆ Follow")
        if r["cat"] == "pool":
            return self.info.setText(f"{head}<p>{html.escape(dtext(r['desc']))}</p>")
        self.info.setText(head + "<p><i>loading wiki…</i></p>")
        name = r["name"]
        run(lambda: tag_info(name), lambda res, e: self.cur is r and self.info.setText(
            head + (f"<p><i>{html.escape(str(e))}</i></p>" if e else tag_html(res))))

    def open(self, it=None):
        r = it.data(0, Qt.UserRole) if it else self.cur
        if r and r["cat"] == "pool":
            self.main.open_pool(r["id"], r["name"])
        elif r:
            self.main.open_tag(r["name"], r["cat"])

    @staticmethod
    def key(r):
        return ("artist:" if r["cat"] == "artist" else "tag:") + r["name"]

    def toggle_follow(self):
        if self.cur and self.cur["cat"] != "pool":
            on = self.main.toggle_follow(self.key(self.cur))
            self.follow.setText("★ Following" if on else "☆ Follow")


def same(a, b):
    return a["site"] == b["site"] and a["id"] == b["id"]


class ImageTabs(QTabWidget):
    """The second tab row: a top tab's grid (fixed, first) plus one closable tab per opened image, browser style.
    Double-click opens and shows an image; Ctrl+double-click or middle-click opens it in the background."""

    def __init__(self, main, home, title="▦ Grid"):
        super().__init__(tabsClosable=True, documentMode=True)
        self.main, self.home = main, home
        home.imgtabs = self
        self.addTab(home, title)
        for side in (QTabBar.LeftSide, QTabBar.RightSide):
            self.tabBar().setTabButton(0, side, None)
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
            v = Viewer(self.main, self, grid, row)
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


class Viewer(QWidget):
    """One image tab: the picture + side panel. ←/→ walk the grid it was opened from, PgUp/PgDn flip pages."""

    def __init__(self, main, tabs, grid, row):
        super().__init__(focusPolicy=Qt.StrongFocus)
        self.main, self.tabs, self.grid, self.row = main, tabs, grid, row
        self.p, self.pg, self.pix, self.frames, self.fi = grid.item(row).data(Qt.UserRole), 0, None, [], 0
        self.img = QLabel(alignment=Qt.AlignCenter)
        self.img.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Ignored)
        self.info = QLabel(wordWrap=True, textInteractionFlags=Qt.TextSelectableByMouse)
        self.tags = QListWidget(focusPolicy=Qt.NoFocus, contextMenuPolicy=Qt.CustomContextMenu)
        self.tags.itemActivated.connect(lambda it: self.main.open_tag(it.text(), it.data(Qt.UserRole) or ""))
        self.tags.customContextMenuRequested.connect(self.tag_menu)
        self.anim = QTimer(self, singleShot=True, timeout=self.next_frame)

        def btn(text, fn):
            b = QPushButton(text, focusPolicy=Qt.NoFocus)
            b.clicked.connect(fn)
            return b
        self.fav = btn("♥ Favorite (F)", lambda: (self.main.toggle_fav(self.p), self.refresh_info()))
        self.play = btn("▶ Play in mpv", self.play_video)
        self.px_btn = btn("Open on Pixiv", lambda: webbrowser.open(f"https://www.pixiv.net/artworks/{self.p['pixiv_id']}"))
        self.find_btn = btn("Find on Danbooru", self.find_booru)
        self.like_btn = btn("More like this (Pixiv)", lambda: self.main.open_feed(
            f"related:{self.p['pixiv_id']}", f"like PX {self.p['pixiv_id']}", [("pixiv", "related", self.p["pixiv_id"])]))
        self.pages, self.quality = QLabel(), QLabel(wordWrap=True)
        nav = QHBoxLayout()
        for w in (btn("◀", lambda: self.step(-1)), btn("▶", lambda: self.step(1))):
            nav.addWidget(w)
        side = QVBoxLayout()
        side.addLayout(nav)
        for w in (self.pages, self.info, self.quality, QLabel("Tags (double-click = open tab, right-click = more)"), self.tags,
                  btn("⬇ Download (D)", self.download), self.fav, self.play, self.px_btn, self.find_btn, self.like_btn,
                  btn("Open page", lambda: webbrowser.open(self.p["page"]))):
            side.addWidget(w)
        sidew = QWidget()
        sidew.setLayout(side)
        sidew.setFixedWidth(300)
        lay = QHBoxLayout(self)
        lay.addWidget(self.img, 1)
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

    def load(self):
        p = self.p
        i = self.tabs.indexOf(self)
        self.tabs.setTabText(i, f"{SHORT[p['site']]} {p['id']}")
        self.tabs.setTabToolTip(i, " ".join(p["tags"][:30]))
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
        self.px_btn.setVisible(bool(p.get("pixiv_id")))
        self.like_btn.setVisible(bool(p.get("pixiv_id")))
        self.find_btn.setVisible(p["site"] == "pixiv")
        # the original is downloaded into cache/ (or favorites/) and shown from there; until it's in, the sample
        # stands in. Videos keep their poster.
        root, pg = self.main.conf["download_dir"], self.pg
        full = None if p["video"] else p["files"][pg]
        have = full and cached(root, p, pg)
        folder = "favorites" if self.main.is_fav(p) else "cache"
        key, self.pix, self.hd, self.fails = (pg, p["site"], p["id"]), None, False, 0
        self.anim.stop()
        self.frames = []
        self.img.setText("loading…")
        self.quality.setText("loading original…" if full else "")

        def done(img, err, original):
            if not self.current(key) or self.frames or self.hd:
                return
            if err or img is None or img.isNull():
                self.fails += 1
                if original:
                    self.quality.setText(f"original failed{': ' + str(err) if err else ''} — showing the sample")
                if not self.pix and self.fails == (2 if full else 1):
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
            run(lambda: ugoira_frames(conf, p["id"]), frames, "view")

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
        alt = p.get("alt")
        self.pages.setText(f"page {self.pg + 1}/{n} (PgUp/PgDn)" if n > 1 else "")
        self.pages.setVisible(n > 1)
        self.info.setText(f"<b>{SITES[p['site']]} #{p['id']}</b><br>{p['w']}×{p['h']}  rating:{p['rating']}"
                          + (f"<br>also on {SITES[alt['site']]} #{alt['id']} ({len(alt['files'])} pages)" if alt else "")
                          + f"<br>{self.locate() + 1}/{self.grid.count()} (←/→, Esc = grid, Ctrl+W = close)")
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

    def find_booru(self):
        """Swap a Pixiv post for its Danbooru copy (real tags), keeping the Pixiv post as alt, in this tab and its tile."""
        p, conf = self.p, self.main.conf

        def done(r, e):
            if e or not r:
                return self.main.statusBar().showMessage(f"Pixiv #{p['id']} is not on Danbooru" + (f": {e}" if e else ""))
            new, it = {**r[0], "alt": p}, self.grid.item(self.locate())
            if it and same(it.data(Qt.UserRole), p):
                self.grid.set_post(it, new)
            if self.p is p:
                self.p = new
                self.load()
        run(lambda: search_danbooru(conf, f"pixiv:{p['pixiv_id']}", 1), done)

    def tag_menu(self, pos):
        it = self.tags.itemAt(pos)
        if not it:
            return
        t, cat, m, q = it.text(), it.data(Qt.UserRole) or "", QMenu(self), self.main.q.text().strip()
        m.addAction("Open tab", lambda: self.main.open_tag(t, cat))
        m.addAction("Search", lambda: self.main.search(t))
        m.addAction("Add to query", lambda: self.main.search(f"{q} {t}".strip()))
        m.addAction("Exclude from query", lambda: self.main.search(f"{q} -{t}".strip()))
        m.addAction("Follow", lambda: self.main.toggle_follow(("artist:" if cat == "artist" else "tag:") + t))
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
        self.query, self.cancel, self.dyn = "", None, {}

        self.cb = {s: QCheckBox(n, checked=s in self.conf["sites"]) for s, n in SITES.items()}
        for c in self.cb.values():
            c.toggled.connect(self.save_sites)
        self.q = QLineEdit(placeholderText="tags…   Enter = search   (booru syntax: -tag, rating:g, order:score; Pixiv gets the translated tags)")
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
                  QPushButton("⬇ Bulk", toolTip="Download every result of the current tab", clicked=lambda: self.bulk()),
                  self.history_cb, QPushButton("📂", toolTip=f"Open history folder: {HISTORY_DIR}", clicked=self.open_history),
                  QPushButton("⚙", toolTip="Settings", clicked=self.settings)):
            top.addWidget(w)

        self.tabs = QTabWidget(tabsClosable=True)
        self.tags_tab, self.new, self.favs = TagsTab(self), NewFeed(self), Grid(self.open_image)
        for w, t in ((self.tags_tab, "Tags"), (ImageTabs(self, self.new), "New"),
                     (ImageTabs(self, self.favs), "Favorites")):
            self.tabs.addTab(w, t)
            for side in (QTabBar.LeftSide, QTabBar.RightSide):  # fixed tabs: no close button
                self.tabs.tabBar().setTabButton(self.tabs.count() - 1, side, None)
        self.tabs.tabCloseRequested.connect(self.close_tab)
        self.tabs.currentChanged.connect(self.sync_query)
        self.tabs.setCurrentWidget(self.tags_tab)

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

    # --- tabs ---
    def open_tab(self, key, title, make):
        """Focus the tab for key, creating it with make() the first time."""
        w = self.dyn.get(key)
        if w is None:
            w = self.dyn[key] = ImageTabs(self, make())
            w.key = key
            self.tabs.addTab(w, title[:32])
            log.info("open tab %s", key)
        self.tabs.setCurrentWidget(w)

    def close_tab(self, i):
        w = self.tabs.widget(i)
        if not hasattr(w, "key"):
            return
        h = w.home
        for f in [h, getattr(h, "booru", None), getattr(h, "px", None)]:  # a tag tab, or an artist's two panes
            if isinstance(f, Feed):
                f.stop()  # late replies are dropped instead of paging on in the background
        self.tabs.removeTab(i)
        del self.dyn[w.key]
        w.setParent(None)  # freed by Python once pending callbacks let go of it

    def open_tag(self, tag, cat="", pixiv=None):
        if cat == "artist":
            return self.open_artist(tag)
        if CJK.search(tag) and pixiv is None:  # a Pixiv tag: open the booru tag whose wiki lists it, Pixiv pinned
            return run(lambda: dan("wiki_pages", **{"search[other_names_match]": tag, "only": "title", "limit": 1}),
                       lambda r, e: self.open_tag(r[0]["title"] if r and not e else "", "", tag))
        self.open_tab(f"tag:{tag}|{pixiv or ''}", tag or pixiv or "all posts", lambda: TagView(self, tag, pixiv))

    def open_artist(self, tag):
        self.open_tab(f"artist:{tag}", f"🎨 {tag}", lambda: ArtistView(self, tag))

    def open_pool(self, pid, name):
        self.open_tab(f"pool:{pid}", f"▤ {name}", lambda: TagView(self, f"ordpool:{pid}", sites=["danbooru"], title=name.replace("_", " ")))

    def open_feed(self, key, title, srcs):
        def make():
            f = Feed(self, title)
            f.start(srcs)
            return f
        self.open_tab(key, title, make)

    def link(self, href, tag=""):
        """Rich-text links: tag:<cat>:<name> opens a tag/artist tab, px::<word> opens tag with Pixiv pinned to word."""
        kind, cat, text = href.split(":", 2)
        text = urllib.parse.unquote(text)
        if kind == "px":
            self.open_tag(tag or "", pixiv=text)
        elif kind == "tag":
            self.open_tag(text, cat)

    def open_image(self, grid, row, background=None):
        """Open grid[row] as an image tab in the second row of the grid's own top tab."""
        if background is None:
            background = bool(QApplication.keyboardModifiers() & Qt.ControlModifier)
        w = grid
        while not isinstance(w, ImageTabs):
            w = w.parentWidget()
        w.open_image(grid, row, background)

    def show_grid(self, home):
        self.tabs.setCurrentWidget(home.imgtabs)
        home.imgtabs.setCurrentIndex(0)

    def current_feed(self):
        w = getattr(self.tabs.currentWidget(), "home", None)
        return w if isinstance(w, Feed) else getattr(w, "booru", None)

    def set_new_count(self, n):
        tab = getattr(getattr(self, "new", None), "imgtabs", None)  # NewFeed reports while it is still being built
        if tab:
            self.tabs.setTabText(self.tabs.indexOf(tab), f"New ({n})" if n else "New")

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
        self.star.setText("★" if self.query in self.conf["saved"] else "☆")
        log.info("search '%s'", self.query)
        self.open_tag(self.query)  # every search gets its own top tab, its images open as tabs under it

    def sync_query(self, i):
        """The search box follows the top tab, so ☆ / Add to query / Exclude work on what you're looking at."""
        home = getattr(self.tabs.widget(i), "home", None)
        if isinstance(home, TagView) and ":" not in home.base:
            self.q.setText(home.tag)
            self.star.setText("★" if home.tag in self.conf["saved"] else "☆")

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

    # --- saved searches / favorites / follows / blacklist ---
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
        self.refresh_favs()  # open image tabs keep their own post, rows may move
        root, fav = self.conf["download_dir"], self.is_fav(p)
        run(lambda: move_favorite(root, p, fav),
            lambda r, e: e and self.statusBar().showMessage(f"favorites folder: {e}"), "fav")

    def refresh_favs(self):
        self.favs.restart()
        self.favs.add(self.conf["favorites"])
        self.tabs.setTabText(self.tabs.indexOf(self.favs.imgtabs), f"Favorites ({len(self.conf['favorites'])})")

    def toggle_follow(self, key, uid=None):
        """Follow / unfollow "tag:x" or "artist:x" -> now following? New follows start at the newest post (nothing unread)."""
        f = self.conf["follows"]
        if key in f:
            del f[key]
            save_conf(self.conf)
            log.info("unfollow %s", key)
            self.new.reload()
            return False
        tag, conf = key.split(":", 1)[1], self.conf
        srcs = [(s, "tags", tag) for s in BOORUS if self.cb[s].isChecked()] or [("danbooru", "tags", tag)]
        srcs += [("pixiv", "user", uid)] if uid else []
        f[key] = {"uid": uid} if uid else {}
        log.info("follow %s", key)

        def seed():
            lists, errs = fetch_all(srcs, conf, 1)
            return {s[0]: max((int(p["id"]) for p in l), default=0) for s, l in lists.items() if s not in errs}

        def done(r, e):
            if key in f:
                f[key].update(r or {})
                save_conf(self.conf)
                self.new.reload()
            if e or not r:
                self.statusBar().showMessage(f"Following {key}, but no site answered: {e or 'check keys in Settings'}")
        run(seed, done)
        return True

    def blacklist_add(self, tag):
        self.conf["blacklist"] = sorted(set(self.conf["blacklist"]) | {tag.lower()})
        save_conf(self.conf)
        log.info("blacklist + %s", tag)
        self.statusBar().showMessage(f"Blacklisted '{tag}' (applies to new searches)")

    # --- downloads ---
    def download(self, p, label):
        if p.get("alt") and len(p["alt"]["files"]) > len(p["files"]):  # the Pixiv original has more pages than the booru copy
            p = p["alt"]
        root, conf = self.conf["download_dir"], self.conf
        self.statusBar().showMessage(f"Downloading {SITES[p['site']]} #{p['id']}…")

        def done(r, e):
            msg = f"download {p['site']} #{p['id']}: " + (f"failed: {e}" if e else f"saved {r[0]}, already had {r[1]}")
            (log.error if e else log.info)(msg)
            self.statusBar().showMessage(msg)
        run(lambda: download_post(p, root, label, conf=conf), done)

    def bulk(self, feed=None):
        if self.cancel:
            return self.statusBar().showMessage("A bulk download is already running")
        feed = feed or self.current_feed()
        query, srcs, keep = (feed.label, list(feed.srcs), feed.keep) if feed else ("", [], None)
        if not srcs:
            return self.statusBar().showMessage("Nothing to download in this tab")
        limit, ok = QInputDialog.getInt(self, "Bulk download", f"Download '{query}' from {', '.join(dict.fromkeys(SITES[s[0]] for s in srcs))}\n"
                                        f"into {self.conf['download_dir']}\n\nMax posts (0 = all):", 0, 0, 10 ** 6)
        if not ok:
            return
        self.cancel = cancel = threading.Event()
        log.info("bulk '%s' on %s, max %s", query, srcs, limit or "all")
        self.cancel_btn.show()
        conf, bl, root = self.conf, set(self.conf["blacklist"]), self.conf["download_dir"]

        def progress(stats, err):
            self.statusBar().showMessage(f"Bulk '{query}': {stats['new']} new, {stats['skip']} skipped, {stats['fail']} failed, page {stats['page']}"
                                         + (f"  |  {stats['err']}" if stats["err"] else ""))

        def work():
            st = {"new": 0, "skip": 0, "fail": 0, "page": 0, "posts": 0, "err": ""}
            active = srcs
            while active and not cancel.is_set() and not (limit and st["posts"] >= limit):
                st["page"] += 1
                lists, errs = fetch_all(active, conf, st["page"])
                st["err"] = "  ".join(errs.values())
                active = [s for s in active if lists[s]]
                posts = [p for p in interleave(lists.values()) if allowed(p, bl) and (not keep or keep(p))]
                if limit:
                    posts = posts[:limit - st["posts"]]
                st["posts"] += len(posts)
                # ponytail: 4 parallel downloads per page, polite enough for boorus; Pixiv may still 429 on huge runs
                with ThreadPoolExecutor(4) as ex:
                    for f in [ex.submit(download_post, p, root, query, cancel, conf) for p in posts]:
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
         "tag_string": "1girl solo", "rating": "g", "image_width": 10, "image_height": 20, "md5": "aa", "pixiv_id": 7,
         "tag_string_artist": "foo", "tag_string_general": "1girl solo",
         "media_asset": {"variants": [{"type": "360x360", "url": "https://cdn.donmai.us/360/a.jpg"}]}},
        {"id": 2, "tag_string": "gold_only"},  # no file -> skipped
    ])
    assert len(db) == 1 and db[0]["thumb"].endswith("360/a.jpg") and db[0]["samples"][0].endswith("sample/a.jpg")
    assert db[0]["tags"] == ["1girl", "solo"] and not db[0]["video"]
    assert db[0]["pixiv_id"] == "7" and db[0]["cats"]["artist"] == ["foo"] and db[0]["cats"]["copyright"] == []

    gb = parse_gel("gelbooru", {"@attributes": {"count": 1}, "post": [
        {"id": 5, "file_url": "https://img.gelbooru.com/x.webm", "preview_url": "p.jpg", "sample_url": "", "sample": 0,
         "tags": "a b", "rating": "sensitive", "md5": "bb", "source": "https://www.pixiv.net/en/artworks/7"}]})
    assert gb[0]["video"] and gb[0]["rating"] == "s" and gb[0]["samples"] == gb[0]["files"]
    assert gb[0]["pixiv_id"] == "7" and gb[0]["md5"] == "bb"
    assert parse_gel("gelbooru", {"@attributes": {"count": 0}}) == []
    r34 = parse_gel("rule34", [{"id": 9, "file_url": "https://r34/x.jpg", "sample_url": "https://r34/s.jpg", "sample": True,
                                "preview_url": "https://r34/t.jpg", "tags": " foo  bar ", "rating": "safe", "hash": "aa"}])
    assert r34[0]["samples"] == ["https://r34/s.jpg"] and r34[0]["rating"] == "g" and r34[0]["tags"] == ["foo", "bar"]
    assert "rule34.xxx" in r34[0]["page"] and parse_gel("rule34", None) == []
    assert px_id("https://i.pximg.net/img-original/img/2007/09/09/22/14/07/20_p0.jpg") == "20"
    assert px_id("https://www.pixiv.net/member_illust.php?mode=medium&illust_id=33") == "33"
    assert px_id("https://x.com/a/status/1") is None and px_id(None) is None
    assert PX_USER.search("https://www.pixiv.net/users/1039353")[1] == "1039353"
    assert PX_USER.search("http://www.pixiv.net/member.php?id=55")[1] == "55" and not PX_USER.search("https://sketch.pixiv.net/@igix")

    px = parse_pixiv({"illusts": [
        {"id": 7, "x_restrict": 1, "image_urls": {"square_medium": "sq", "large": "lg"}, "meta_single_page": {},
         "meta_pages": [{"image_urls": {"original": "https://i.pximg.net/o_p0.png", "large": "l0"}},
                        {"image_urls": {"original": "https://i.pximg.net/o_p1.png", "large": "l1"}}],
         "tags": [{"name": "オリジナル", "translated_name": "original"}, {"name": "女の子"}]},
        {"id": 8, "x_restrict": 0, "type": "ugoira", "image_urls": {"square_medium": "sq", "large": "lg"},
         "meta_single_page": {"original_image_url": "https://i.pximg.net/o.jpg"}, "meta_pages": [], "tags": []}]})
    assert len(px[0]["files"]) == 2 and px[0]["samples"] == ["l0", "l1"] and px[0]["rating"] == "e"
    assert px[0]["tags"] == ["オリジナル", "original", "女の子"] and px[1]["files"] == ["https://i.pximg.net/o.jpg"]
    assert px[1]["ugoira"] and not px[0]["ugoira"] and px[0]["pixiv_id"] == "7"

    assert [p["id"] for p in interleave([db, px, r34])] == ["1", "7", "9", "8"]
    assert not allowed(px[0], {"original"}) and not allowed(px[0], {"rating:e"}) and allowed(px[1], {"rating:e"})
    assert not allowed(db[0], {"1girl"}) and allowed(db[0], {"2girls"})
    assert rating_ok(px[1], {"s"}) and not rating_ok(px[0], {"g", "s"}) and rating_ok(px[0], {"q"})
    assert rating_ok(gb[0], {"s"}) and not rating_ok(gb[0], {"g"})

    # tag bridge, offline: the wiki cache is pre-filled and lookup is off
    conf = {"pixiv_map": {"hatsune_miku": "初音ミク", "1girl": ""}}
    assert pixiv_query(conf, "hatsune_miku 1girl -solo rating:g order:score re:zero_x", False) == ("初音ミク", True, True)
    assert pixiv_query(conf, "1girl blue_hair_(x) rating:e,q", False) == ("1girl blue hair", False, False)
    assert pixiv_query(conf, "初音ミク ordpool:5 -rating:e", False) == ("初音ミク", True, False)
    assert pixiv_query(conf, "id:>5 pool:3", False)[0] == ""
    assert sorted(["长发", "長髪", "ロングヘア"], key=lambda n: not KANA.search(n))[0] == "ロングヘア"

    # dedupe, both arrival orders: Danbooru #1 is Pixiv #7, R34 #9 has Danbooru's md5
    seen = {}
    a, new_a = fold(seen, db[0])
    assert new_a and fold(seen, px[0]) == (a, False) and a[0]["site"] == "danbooru" and a[0]["alt"]["id"] == "7"
    assert fold(seen, r34[0]) is None and fold(seen, px[0]) is None
    seen = {}
    b, _ = fold(seen, px[0])
    assert fold(seen, db[0]) == (b, False) and b[0]["site"] == "danbooru" and b[0]["alt"]["site"] == "pixiv"
    assert fold(seen, gb[0])[1] is True  # another booru post of the same Pixiv work keeps its own tile

    assert follow_srcs({"tag:miku": {"danbooru": 5, "rule34": 9}, "artist:x": {"uid": "3", "pixiv": 100, "danbooru": 0}}) == {
        ("danbooru", "tags", "miku id:>5"): ("tag:miku", "danbooru"), ("rule34", "tags", "miku id:>9"): ("tag:miku", "rule34"),
        ("pixiv", "user", "3>100"): ("artist:x", "pixiv"), ("danbooru", "tags", "x id:>0"): ("artist:x", "danbooru")}
    assert dtext("h4. Hi\n[[hatsune_miku|Miku]] and [[vocaloid]] [b]x[/b]") == "Hi\nMiku and vocaloid x"
    assert link("tag", "a b", "artist").startswith("<a href='tag:artist:a%20b' style='color:#e0474c'>")
    assert parse_gel_tags("rule34", b'<tags><tag type="4" count="3" name="miku" id="1"/></tags>') == [
        {"type": "4", "count": "3", "name": "miku", "id": "1"}]
    assert parse_gel_tags("gelbooru", b'{"@attributes":{}, "tag":[{"name":"a","count":1,"type":0}]}')[0]["name"] == "a"

    assert dest("/d", px[0], 1, "blue/hair *") == Path("/d/pixiv/blue_hair _/pixiv_7_p1.png")
    assert dest("/d", db[0], 0, "") == Path("/d/danbooru/_all/danbooru_1.png")
    import tempfile
    with tempfile.TemporaryDirectory() as root:  # cache -> favorites -> cache -> cleared, no network
        c = local(root, px[0], 1, "cache")
        assert c == Path(root, "cache", "pixiv_7_p1.png") and cached(root, px[0], 1) is None
        c.parent.mkdir()
        c.write_bytes(b"x")
        (c.parent / CACHE_MARK).touch()
        local(root, px[0], 0, "favorites").parent.mkdir()
        local(root, px[0], 0, "favorites").write_bytes(b"y")  # p0 already there: no download attempted
        move_favorite(root, px[0], True)
        assert not c.exists() and cached(root, px[0], 1) == local(root, px[0], 1, "favorites")
        move_favorite(root, px[0], False)
        assert cached(root, px[0], 1) == c and cached(root, px[0], 0) == local(root, px[0], 0, "cache")
        clear_cache(root)
        assert not c.parent.exists() and Path(root, "favorites").exists()
        Path(root, "cache").mkdir()  # a cache/ the app didn't make is never deleted
        clear_cache(root)
        assert Path(root, "cache").exists()
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


def shutdown(conf):
    save_conf(conf)  # keeps the Pixiv tag cache
    for name, pool in POOLS.items():
        if name != "fav":
            pool.shutdown(wait=False, cancel_futures=True)
    POOLS["fav"].shutdown(wait=True)  # a just-clicked ♥ must leave cache/ before cache/ goes
    # ponytail: a download still running finishes after this and lands in a fresh cache/, removed at next start
    for root in CACHE_ROOTS | {Path(conf["download_dir"])}:
        clear_cache(root)


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
    clear_cache(w.conf["download_dir"])  # left over from a crash
    app.aboutToQuit.connect(lambda: shutdown(w.conf))
    log.info("started")
    w.show()
    code = app.exec()
    log.info("quit")
    sys.exit(code)


if __name__ == "__main__":
    main()
