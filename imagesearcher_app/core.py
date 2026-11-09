"""Config, logging, HTTP, the post dict, paged sources, cache/favorites files and the worker->UI bridge."""
import itertools
import json
import logging
import os
import re
import shutil
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from PySide6.QtCore import QObject, Signal

UA = "imagesearcher/2.0 (+https://github.com/acquevoor/imagesearcher)"
VIDEO_EXT = {".mp4", ".webm", ".zip"}  # .zip = Danbooru ugoira, its sample is a .webm
THUMB = 180
PIXIV_REFERER = "https://app-api.pixiv.net/"


def referer(url):
    """The Referer an image host wants, or "". Without it Gelbooru's image hosts redirect to hotlink.php, then a web page."""
    host = urllib.parse.urlsplit(url).hostname or ""
    return PIXIV_REFERER if host.endswith("pximg.net") else "https://gelbooru.com/" if host.endswith(".gelbooru.com") else ""

CAT_COLOR = {"artist": "#e0474c", "copyright": "#b05ce6", "character": "#2fb344", "species": "#ed7d31",
             "general": "#3b8eea", "meta": "#e8a33d"}
CJK = re.compile(r"[぀-ヿ㐀-鿿가-힯]")  # kana / kanji / hangul = a Pixiv-style tag
KANA = re.compile(r"[぀-ヿ]")  # hiragana / katakana: a Japanese name, what Pixiv tags use
PX_SRC = re.compile(r"pximg\.net/.*/(\d+)_(?:p\d|ugoira)|pixiv\.net/(?:\w\w/)?artworks/(\d+)|illust_id=(\d+)")
PX_USER = re.compile(r"pixiv\.net/(?:\w\w/)?(?:users/|member\.php\?id=)(\d+)")

# Booru sites offered by "Add site"; any other site works by URL + engine. Gelbooru: login = user id.
PRESETS = [
    {"id": "danbooru", "name": "Danbooru", "short": "DB", "engine": "danbooru", "url": "https://danbooru.donmai.us"},
    {"id": "gelbooru", "name": "Gelbooru", "short": "GB", "engine": "gelbooru", "url": "https://gelbooru.com"},
    {"id": "rule34", "name": "Rule34", "short": "R34", "engine": "gelbooru", "url": "https://rule34.xxx", "api_url": "https://api.rule34.xxx"},
    {"id": "safebooru", "name": "Safebooru", "short": "SB", "engine": "gelbooru", "url": "https://safebooru.org"},
    {"id": "konachan", "name": "Konachan (safe)", "short": "KC", "engine": "moebooru", "url": "https://konachan.net"},
    {"id": "konachan_com", "name": "Konachan", "short": "KC+", "engine": "moebooru", "url": "https://konachan.com"},
    {"id": "yandere", "name": "yande.re", "short": "YR", "engine": "moebooru", "url": "https://yande.re"},
    {"id": "sakugabooru", "name": "Sakugabooru", "short": "SKG", "engine": "moebooru", "url": "https://www.sakugabooru.com"},
    {"id": "e621", "name": "e621", "short": "E6", "engine": "e621", "url": "https://e621.net"},
    {"id": "e926", "name": "e926", "short": "E9", "engine": "e621", "url": "https://e926.net"},
    {"id": "derpibooru", "name": "Derpibooru", "short": "DP", "engine": "philomena", "url": "https://derpibooru.org"},
    {"id": "aibooru", "name": "AIBooru", "short": "AIB", "engine": "danbooru", "url": "https://aibooru.online"},
]
SITE_FIELDS = {"id": "", "name": "", "short": "", "engine": "", "url": "", "api_url": "", "login": "", "key": "", "filter_id": "", "on": False,
               "cookie": ""}  # cookie: the site's login cookies, for site favourites where the API key can't (Gelbooru family)

CONF_PATH = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "imagesearcher" / "config.json"
DEFAULTS = {
    "version": 2,
    "boorus": [{**SITE_FIELDS, **PRESETS[0], "on": True}],
    "pixiv": {"refresh_token": "", "user_id": "", "premium": False, "follow_seen": 0, "rec_r18": "Show"},
    "blacklist": [], "favorites": [], "saved": [], "px_saved": [], "limit": 40,
    "download_dir": str(Path.home() / "Pictures" / "imagesearcher"), "history": False, "section": "boorus",
    "keys_prompt": True,  # offer the key fetcher on launch while Gelbooru / Rule34 / Pixiv keys are missing
    "follows": {},  # "tag:x" / "artist:x" -> {booru site id: last seen post id}
    "pixiv_map": {},  # booru tag -> Pixiv tag from its wiki ("" = none known)
    # X: opt-in, an alt account's session cookies; pauses set by x.pause_for. Graph + post cache live in x_state.json
    "x": {"enabled": False, "auth_token": "", "ct0": "", "seed_handle": "", "pause_min": 120, "pause_random": True,
          "paused_until": 0, "locked": False, "reason": "", "pace": "jd", "alt_id": "", "ua": ""},
    "x_saved": [],
    # Imageboard: opt-in; board -> FoolFuuka archive host; cf: Cloudflare passes (host -> cookie + user agent)
    "ib": {"enabled": False, "boards": {"trash": "desuarchive.org", "d": "desuarchive.org", "h": "archiveofsins.com"},
           "cf": {}, "watch": {}, "names": "original"},
    "ib_saved": [],
    # Pawchive: host (pawchive.st moved to .pw), default file server node, the pasted session cookie, bulk progress
    "paw": {"host": "pawchive.pw", "node": 2, "session": "", "bulk": {}},
    "paw_saved": [],
    "rd_saved": [],
    "site_favs": [],  # "site:id" of posts you added to the site's own favourites from here (booru APIs don't say)
    "hotkeys": {},  # action -> key, only the ones changed in Settings → Hotkeys… (HOTKEYS has the defaults)
}
HOTKEYS = {  # action -> (what it does, default key)
    "prev": ("Image: previous post", "Left"), "next": ("Image: next post", "Right"),
    "page_prev": ("Image: previous page", "PgUp"), "page_next": ("Image: next page", "PgDown"),
    "download": ("Image: download", "D"), "copy": ("Image: copy the image / the selected strip pages", "C"),
    "copy_link": ("Image or thumbnail: copy the link", "L"), "reload": ("Image: reload", "R"),
    "fav": ("Image or thumbnail: ♥ favourite", "F"), "site_fav": ("Image or thumbnail: ★ site favourite", "B"),
    "zoom_out": ("Strip view: zoom out", "-"), "zoom_in": ("Strip view: zoom in (= works too)", "+"),
    "to_grid": ("Image tabs: back to the grid", "Esc"), "close_tab": ("Image tabs: close the image", "Ctrl+W"),
    "next_tab": ("Image tabs: next", "Ctrl+Tab"), "prev_tab": ("Image tabs: previous", "Ctrl+Shift+Tab"),
    "boorus": ("Show Boorus", "Ctrl+1"), "pixiv": ("Show Pixiv", "Ctrl+2"), "x": ("Show X", "Ctrl+3"),
    "ib": ("Show Imageboard", "Ctrl+4"), "paw": ("Show Pawchive", "Ctrl+5"), "reader": ("Show Reader", "Ctrl+6"),
}
CONF_LOCK = threading.RLock()  # workers add to pixiv_map / refresh the Pixiv token while the UI saves


def preset(sid):
    return {**SITE_FIELDS, **next(p for p in PRESETS if p["id"] == sid)}


def migrate(raw):
    """v1 config (fixed danbooru / gelbooru / rule34 key blocks + a "sites" list) -> v2 (a list of booru sites).
    Site ids stay the same, so old favorites and follows still point at the right site."""
    if raw.get("version", 1) >= 2:
        return raw
    on = raw.pop("sites", ["danbooru"])
    raw["boorus"] = []
    for sid in ("danbooru", "gelbooru", "rule34"):
        c = raw.pop(sid, None) or {}
        raw["boorus"].append({**preset(sid), "on": sid in on, "login": c.get("login") or c.get("user_id") or "", "key": c.get("api_key", "")})
    for f in raw.get("follows", {}).values():  # Pixiv follows now live in the Pixiv account
        f.pop("pixiv", None)
        f.pop("uid", None)
    raw["version"] = 2
    return raw


def load_conf():
    conf = json.loads(json.dumps(DEFAULTS))
    try:
        raw = json.loads(CONF_PATH.read_text())
    except FileNotFoundError:
        return conf
    if raw.get("version", 1) < 2:
        backup = CONF_PATH.with_name("config.v1.json")
        if not backup.exists():
            shutil.copyfile(CONF_PATH, backup)
    for k, v in migrate(raw).items():
        conf[k] = {**conf[k], **v} if isinstance(conf.get(k), dict) else v
    conf["boorus"] = [{**SITE_FIELDS, **s} for s in conf["boorus"]]
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
APP_DIR = Path(os.environ.get("IMAGESEARCHER_HOME") or Path(__file__).resolve().parents[1])
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

SECRET_PARAM = re.compile(r"key|token|pass|secret|auth|ct0|login|user_id|code", re.I)
MEDIA_EXT = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".avif", ".bmp", ".mp4", ".webm", ".zip", ".m4v"}


def redact(url):
    """URL for the log: secret-looking query values masked, long queries cut."""
    u = urllib.parse.urlsplit(url)
    q = "&".join(f"{k}=***" if SECRET_PARAM.search(k) else f"{k}={v[:60]}" for k, v in urllib.parse.parse_qsl(u.query, keep_blank_values=True))
    return f"{u.netloc}{u.path}" + (f"?{q[:240]}" if q else "")


def net_level(url):
    """Images, thumbnails, videos and autocomplete: DEBUG (hundreds per page); every API call: INFO."""
    path = urllib.parse.urlsplit(url).path.lower()
    return logging.DEBUG if Path(path).suffix in MEDIA_EXT or "autocomplete" in path or "pbs.twimg.com" in url else logging.INFO


# host -> extra headers for every request to it: a Cloudflare pass you made (cf_clearance + the user agent it was
# issued to), set by imageboard.apply_passes. Covers image downloads too, not only API calls.
HOST_HEADERS = {}


def open_url(url, data=None, headers=None, timeout=30, method=None):
    """Every request goes through here and is logged: method, URL (secrets masked), status and time.
    Bodies of POSTs are never logged (they carry tokens)."""
    host = urllib.parse.urlsplit(url).hostname or ""
    h = {"User-Agent": UA, **HOST_HEADERS.get(host, {}), **(headers or {})}
    if referer(url):
        h["Referer"] = referer(url)
    method, t0, lvl = method or ("POST" if data is not None else "GET"), time.monotonic(), net_level(url)
    try:
        r = urllib.request.urlopen(urllib.request.Request(url, data=data, headers=h, method=method), timeout=timeout)
    except urllib.error.HTTPError as e:
        log.warning("%s %s -> HTTP %s %s (%d ms)", method, redact(url), e.code, e.reason, (time.monotonic() - t0) * 1000)
        if e.code in (403, 503) and e.headers.get("cf-mitigated") == "challenge":
            log.warning("%s is behind Cloudflare's browser check%s: ⚙ Settings → ☁ Pass Cloudflare… and pick %s", host,
                        " (your pass expired)" if host in HOST_HEADERS else "", host)
            raise urllib.error.HTTPError(url, e.code, f"{host} is behind Cloudflare's browser check: ☁ Pass Cloudflare… → {host}",
                                         e.headers, None) from None
        raise
    except Exception as e:
        log.warning("%s %s -> failed: %s: %s (%d ms)", method, redact(url), type(e).__name__, e, (time.monotonic() - t0) * 1000)
        raise
    log.log(lvl, "%s %s -> %s (%d ms)", method, redact(url), r.status, (time.monotonic() - t0) * 1000)
    return r


def http(url, **kw):
    with open_url(url, **kw) as r:
        data = r.read()
    if net_level(url) == logging.INFO:
        log.debug("  %d bytes from %s", len(data), urllib.parse.urlsplit(url).netloc)
    return data


def get_json(url, label, hint="", **kw):
    """GET url as JSON; errors become RuntimeError("<label>: …") with the site's own message when it sends one."""
    try:
        body = http(url, **kw).strip()
    except urllib.error.HTTPError as e:
        try:
            err = json.loads(e.read())
            msg = " " + str(err.get("message") or err.get("reason") or err.get("error", {}).get("message") or "")
        except Exception:
            msg = ""
        raise RuntimeError(f"{label}: HTTP {e.code}{msg.rstrip()}{hint if e.code in (401, 403, 422) else ''}") from None
    try:
        data = json.loads(body) if body else None
    except ValueError:
        data = body[:150].decode(errors="replace")
    if isinstance(data, str):  # Gelbooru-family sites answer auth failures with a bare string
        raise RuntimeError(f"{label}: {data}{hint}")
    return data


def qs(**params):
    return urllib.parse.urlencode({k: v for k, v in params.items() if v not in ("", None)})


# ---------- posts ----------
LABELS = {"pixiv": "PX", "x": "X", "ib": "4ch", "paw": "PW"}  # site id -> short tile label, filled from the booru registry
NAMES = {"pixiv": "Pixiv", "x": "X", "ib": "Imageboard", "paw": "Pawchive"}


def make_post(site, pid, thumb, files, samples, tags, rating, page, w=0, h=0, **extra):
    ext = Path(urllib.parse.urlparse(files[0]).path).suffix.lower()
    return {"site": site, "id": str(pid), "thumb": thumb or (samples or files)[0], "files": files,
            "samples": samples or files, "tags": tags, "rating": rating, "page": page, "w": w, "h": h,
            "video": ext in VIDEO_EXT, "pixiv_id": None, "md5": "", **extra}


def px_id(source):
    """Pixiv illust id from a booru 'source' URL, else None."""
    m = PX_SRC.search(source or "")
    return next((g for g in m.groups() if g), None) if m else None


def same(a, b):
    return a["site"] == b["site"] and a["id"] == b["id"]


def interleave(lists):
    return [p for group in itertools.zip_longest(*lists) for p in group if p is not None]


def allowed(post, blacklist):
    return not ({t.lower() for t in post["tags"]} | {"rating:" + post["rating"]}) & blacklist


def rating_ok(post, ratings):
    """Client-side g/s/q/e filter (Danbooru's anonymous 2-tag limit counts rating: tokens). Unknown ratings pass."""
    return not post["rating"] or post["rating"] in ratings


def fold(seen, p):
    """Cross-site dedupe within one feed: the same file (md5) shows once, copies on other sites ride along
    in post["alts"]. seen: md5 -> slot, a list whose [0] is the shown post.
    -> (slot, new): new = add a tile, else slot[0] was updated in place; None = drop p (same site repeat)."""
    md5 = p.get("md5")
    slot = seen.get(md5) if md5 else None
    if slot is None:
        slot = [p]
        if md5:
            seen[md5] = slot
        return slot, True
    if slot[0]["site"] == p["site"] or any(a["site"] == p["site"] for a in slot[0].get("alts", [])):
        return None
    slot[0] = {**slot[0], "alts": slot[0].get("alts", []) + [p]}
    return slot, False


# ---------- sources: one paged stream of posts each, feeds interleave them ----------

class Source:
    """label shows in tile counts; fetch(page) -> posts (page 1 restarts the stream)."""
    label = ""

    def fetch(self, page):
        raise NotImplementedError


def fetch_all(srcs, page):
    """Query sources in parallel -> ({src: posts}, {src: error})."""
    lists, errs = {}, {}
    with ThreadPoolExecutor(len(srcs) or 1) as ex:
        futs = {s: ex.submit(s.fetch, page) for s in srcs}
        for s, f in futs.items():
            try:
                lists[s] = f.result()
            except Exception as e:
                lists[s], errs[s] = [], str(e)
    return lists, errs


# ---------- files: downloads, cache/ and favorites/ side by side in the download dir ----------

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


def mark_cache(folder):
    folder.mkdir(parents=True, exist_ok=True)
    (folder / CACHE_MARK).touch()
    CACHE_ROOTS.add(folder.parent)


def fetch_file(url, path):
    """Download url to path once, atomically (two tabs may fetch the same file) -> path."""
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.parent.name == "cache":
            mark_cache(path.parent)
        tmp = path.with_name(f"{path.name}.{threading.get_ident()}.part")
        with open_url(url, timeout=120) as r, open(tmp, "wb") as f:
            shutil.copyfileobj(r, f)
            headers = getattr(r, "headers", None) or {}
            want = headers.get("Content-Length")
            if (headers.get("Content-Type") or "").startswith("text/html"):  # or bulk saves a page as .jpg
                f.close()
                tmp.unlink()
                raise OSError(f"the server sent a web page instead of {path.name}")
        if want and tmp.stat().st_size != int(want):  # a dropped connection reads as a normal end of file
            got = tmp.stat().st_size
            tmp.unlink()
            raise OSError(f"download cut off: got {got // 1024} of {int(want) // 1024} kB")
        os.replace(tmp, path)
        log.info("saved %s (%.0f kB) to %s/", path.name, path.stat().st_size / 1024, path.parent.name)
    return path


def undecodable(path):
    """Why a downloaded file wouldn't decode, from its first bytes."""
    head = Path(path).read_bytes()[:32]
    size = Path(path).stat().st_size
    if head[4:8] == b"ftyp":
        return f"{head[8:12].decode(errors='replace').strip()} video/AVIF/HEIC file, which Qt can't decode"
    if head.lstrip()[:1] == b"<":
        return f"the server sent a web page ({size // 1024} kB) instead of the image"
    if head[:3] == b"\xff\xd8\xff" or head[:4] == b"\x89PNG" or head[:4] in (b"GIF8", b"RIFF"):
        return f"damaged or too large to decode ({size // 1024} kB file)"
    return f"unknown file type {head[:8].hex()} ({size // 1024} kB)"


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
                mark_cache(b.parent)
            os.replace(a, b)
        elif fav:
            fetch_file(url, b)


def clear_cache(root):
    d = Path(root) / "cache"
    if (d / CACHE_MARK).exists():
        shutil.rmtree(d, ignore_errors=True)


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
        if c := cached(root, post, i):  # already opened or favorited: copy instead of downloading again
            tmp = d.with_name(d.name + ".part")
            shutil.copyfile(c, tmp)
            tmp.rename(d)
        else:
            fetch_file(url, d)
        new += 1
    return new, skip


# ---------- worker threads -> UI thread ----------

class Bridge(QObject):
    done = Signal(object, object, object)

    def __init__(self):
        super().__init__()
        self.done.connect(self._call)

    def _call(self, cb, res, err):
        cb(res, err)


POOLS = {"net": ThreadPoolExecutor(4), "thumb": ThreadPoolExecutor(8), "view": ThreadPoolExecutor(4),  # view: sample + original
         "fav": ThreadPoolExecutor(1),  # favorite file moves: one at a time, in click order
         "x": ThreadPoolExecutor(1),  # X timeline / discover checks, one at a time
         "xgraph": ThreadPoolExecutor(1), "xbulk": ThreadPoolExecutor(1)}  # graph build and bulk run beside them
bridge = None  # set by app.main once the QApplication exists


def run(fn, cb, pool="net"):
    """Run fn() in a worker, then cb(result, error) on the UI thread."""
    def job():
        try:
            res, err = fn(), None
        except Exception as e:
            res, err = None, e
            log.warning("background task (%s pool) failed: %s: %s", pool, type(e).__name__, e)
            log.debug("traceback", exc_info=e)
        bridge.done.emit(cb, res, err)
    POOLS[pool].submit(job)


def to_ui(cb, res=None, err=None):
    """Call cb(res, err) on the UI thread from a worker."""
    bridge.done.emit(cb, res, err)


def shutdown(conf):
    save_conf(conf)  # keeps the Pixiv tag cache
    for name, pool in POOLS.items():
        if name != "fav":
            pool.shutdown(wait=False, cancel_futures=True)
    POOLS["fav"].shutdown(wait=True)  # a just-clicked ♥ must leave cache/ before cache/ goes
    # ponytail: a download still running finishes after this and lands in a fresh cache/, removed at next start
    for root in CACHE_ROOTS | {Path(conf["download_dir"])}:
        clear_cache(root)
