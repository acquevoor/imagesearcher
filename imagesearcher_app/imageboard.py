"""Imageboard (opt-in): 4chan boards through FoolFuuka archives (desuarchive for /trash/ /d/, archiveofsins for
/h/, archived.moe after passing its Cloudflare check) and live 4chan's read-only JSON API. Board indexes, the
live catalog, threads (live first, the archive once a thread has died), archive search, a thread watcher, and
downloads under the poster's original filenames."""
import base64
import html
import json
import re
import shutil
import time
import urllib.error
import urllib.parse
from pathlib import Path

from .core import CONF_LOCK, Source, cached, log, make_post, open_url, save_conf

LIVE_API, LIVE_IMG, LIVE_BOARDS = "https://a.4cdn.org", "https://i.4cdn.org", "https://boards.4chan.org"
ARCHIVES = ["desuarchive.org", "archiveofsins.com", "archived.moe"]  # offered in Settings
# image servers of the archives: desuarchive's full images sit behind Cloudflare's check (thumbnails don't)
IMAGE_HOSTS = ["desu-usergeneratedcontent.xyz", "static.wikia.nocookie.net"]  # the latter: Fandom images, in the Reader
NSFW = {"d", "h", "hc", "hm", "e", "u", "aco", "gif", "r", "s", "t", "y", "b", "hr", "soc"}
CF_MSG = "Cloudflare check on {host}: press ☁ Pass Cloudflare… (Imageboard bar or ⚙) and pick {host}, then try again"


def cf_hosts(conf):
    """Archives a Cloudflare pass can be made for: the known ones plus any set in the board mapping."""
    return list(dict.fromkeys(ARCHIVES + IMAGE_HOSTS + list(conf["ib"]["boards"].values())))


def apply_passes(conf):
    """Send every Cloudflare pass with all requests to its host (images included), not only API calls."""
    from . import core
    for host, cf in conf["ib"]["cf"].items():
        core.HOST_HEADERS[host] = {"Cookie": f"cf_clearance={cf['cookie']}", "User-Agent": cf["ua"]}


def boards(conf):
    """[(board, archive host)] in the configured order."""
    return list(conf["ib"]["boards"].items())


def host_of(conf, board):
    return conf["ib"]["boards"].get(board, "")


# ---------- HTTP ----------

def headers(conf, host):
    """archived.moe (or any host with a passed Cloudflare check): its cf_clearance cookie + the exact UA it was issued to."""
    cf = conf["ib"]["cf"].get(host)
    return {"Cookie": f"cf_clearance={cf['cookie']}", "User-Agent": cf["ua"]} if cf else {}


def get(conf, url, host=""):
    """GET JSON; FoolFuuka {"error": …} replies and Cloudflare challenges become clear RuntimeErrors.
    "No results" style errors mean an empty page -> None."""
    try:
        with open_url(url, headers=headers(conf, host)) as r:
            body = r.read()
    except urllib.error.HTTPError as e:
        text = e.read()[:3000].decode(errors="replace")
        if e.code in (403, 503) and ("Just a moment" in text or "cf-chl" in text or "challenge-platform" in text):
            log.warning("ib: %s answered with a Cloudflare challenge", host)
            raise RuntimeError(CF_MSG.format(host=host)) from None
        if e.code == 404:
            return None
        try:
            msg = json.loads(text).get("error") or text[:120]
        except ValueError:
            msg = text[:120]
        raise RuntimeError(f"{host or 'imageboard'}: HTTP {e.code} {msg}") from None
    data = json.loads(body) if body.strip() else None
    if isinstance(data, dict) and isinstance(data.get("error"), str):
        err = data["error"]
        if re.search(r"no (results|threads|posts)|not found|nothing", err, re.I):
            return None
        raise RuntimeError(f"{host}: {err}")
    return data


# ---------- posts ----------

def md5_hex(b64):
    try:
        return base64.b64decode(b64).hex() if b64 else ""
    except ValueError:
        return ""


def plain(com):
    """4chan comment HTML -> text."""
    com = re.sub(r"<br\s*/?>", "\n", com or "")
    return html.unescape(re.sub(r"<[^>]+>", "", com)).strip()


def _post(board, num, thread, file, thumb, filename, md5, comment, subject, w, h, size, page, source, **extra):
    title = (subject or comment.replace("\n", " "))[:200]
    # the sample is the thumbnail: it always loads (archive full images may need a Cloudflare pass), the original follows
    return make_post("ib", f"{board}-{num}", thumb, [file], [thumb], [f"/{board}/", f"thread:{board}/{thread}", f"file:{filename}"],
                     "e" if board in NSFW else "", page, int(w or 0), int(h or 0), board=board, num=str(num), thread=str(thread),
                     filename=filename, comment=comment, subject=subject or "", size=int(size or 0), source=source, md5=md5,
                     title=title, op=str(num) == str(thread), **extra)


def ff_post(p, host):
    """A FoolFuuka (archive) post with an image -> post, else None."""
    m = p.get("media") or {}
    link = m.get("media_link") or m.get("remote_media_link")
    if not link or m.get("banned") == "1" or (p.get("subnum") not in (None, "0", 0)):
        return None
    board = (p.get("board") or {}).get("shortname", "")
    num, thread = p["num"], p.get("thread_num") or p["num"]
    return _post(board, num, thread, link, m.get("thumb_link") or link, m.get("media_filename") or m.get("media_orig") or "",
                 md5_hex(m.get("media_hash")), p.get("comment_sanitized") or "", p.get("title") or "", m.get("media_w"),
                 m.get("media_h"), m.get("media_size"), f"https://{host}/{board}/thread/{thread}/#{num}", host,
                 replies=int(p.get("nreplies") or 0), images=int(p.get("nimages") or 0))


def live_post(p, board, thread):
    """A live 4chan post with an image -> post, else None."""
    if "tim" not in p or p.get("filedeleted"):
        return None
    tim, ext = p["tim"], p["ext"]
    return _post(board, p["no"], thread, f"{LIVE_IMG}/{board}/{tim}{ext}", f"{LIVE_IMG}/{board}/{tim}s.jpg",
                 f"{p.get('filename', tim)}{ext}", md5_hex(p.get("md5")), plain(p.get("com")), html.unescape(p.get("sub") or ""),
                 p.get("w"), p.get("h"), p.get("fsize"), f"{LIVE_BOARDS}/{board}/thread/{thread}#p{p['no']}", "4chan",
                 replies=p.get("replies", 0), images=p.get("images", 0))


def tile_text(p):
    """Label of a thread tile in a board index."""
    head = re.sub(r"\s+", " ", p["subject"] or p["comment"] or p["filename"])[:26]
    counts = f"{p['replies']}r {p['images']}i" if p.get("replies") or p.get("images") else ""
    return f"{head}\n{counts}".strip()


# ---------- sources ----------

class CatalogSource(Source):
    """Live 4chan: the whole catalog (every thread's OP) on page 1."""

    def __init__(self, conf, board):
        self.conf, self.board, self.label = conf, board, "4chan"

    def fetch(self, page):
        if page > 1:
            return []
        pages = get(self.conf, f"{LIVE_API}/{self.board}/catalog.json") or []
        posts = [live_post(t, self.board, t["no"]) for pg in pages for t in pg.get("threads", [])]
        out = [p for p in posts if p]
        log.info("ib: /%s/ live catalog: %d threads", self.board, len(out))
        return out


class IndexSource(Source):
    """An archive's board index: OPs of the most recently bumped threads, paged."""

    def __init__(self, conf, board):
        self.conf, self.board, self.host = conf, board, host_of(conf, board)
        self.label = self.host.split(".")[0]

    def fetch(self, page):
        if not self.host:
            raise RuntimeError(f"/{self.board}/ has no archive set in ⚙")
        data = get(self.conf, f"https://{self.host}/_/api/chan/index/?" + urllib.parse.urlencode({"board": self.board, "page": page}), self.host) or {}
        out = [p for p in (ff_post(t["op"], self.host) for t in data.values() if isinstance(t, dict) and t.get("op")) if p]
        log.info("ib: /%s/ archive index page %d on %s: %d threads", self.board, page, self.host, len(out))
        return out


def archive_thread(conf, board, num):
    """-> (posts, op dict) from the board's archive, or (None, None) when it doesn't have the thread."""
    host = host_of(conf, board)
    if not host:
        return None, None
    data = get(conf, f"https://{host}/_/api/chan/thread/?" + urllib.parse.urlencode({"board": board, "num": num}), host)
    t = (data or {}).get(str(num))
    if not t:
        return None, None
    raw = [t["op"], *sorted((t.get("posts") or {}).values(), key=lambda p: (int(p["num"]), int(p.get("subnum") or 0)))]
    return [p for p in (ff_post(r, host) for r in raw) if p], t["op"]


def thread_posts(conf, board, num):
    """Every image of a thread -> (posts, info). Live 4chan while the thread lives, its archive copy once it
    died (404). info: {"alive", "source", "subject", "comment", "replies", "images"}."""
    data = get(conf, f"{LIVE_API}/{board}/thread/{num}.json")
    if data and data.get("posts"):
        raw = data["posts"]
        posts = [p for p in (live_post(r, board, num) for r in raw) if p]
        op = raw[0]
        info = {"alive": not op.get("archived"), "source": "4chan", "subject": html.unescape(op.get("sub") or ""),
                "comment": plain(op.get("com")), "replies": op.get("replies", len(raw) - 1), "images": op.get("images", len(posts))}
        log.info("ib: /%s/%s live: %d images", board, num, len(posts))
        return posts, info
    log.info("ib: /%s/%s is gone from 4chan, reading the archive (%s)", board, num, host_of(conf, board) or "none set")
    posts, op = archive_thread(conf, board, num)
    if posts is None:
        raise RuntimeError(f"/{board}/{num}: not on 4chan any more and not in {host_of(conf, board) or 'any archive'}")
    info = {"alive": False, "source": host_of(conf, board), "subject": op.get("title") or "", "comment": op.get("comment_sanitized") or "",
            "replies": int(op.get("nreplies") or 0), "images": len(posts)}
    return posts, info


class ThreadSource(Source):
    def __init__(self, conf, board, num):
        self.conf, self.board, self.num, self.label, self.info = conf, board, str(num), "thread", {}

    def fetch(self, page):
        if page > 1:
            return []
        posts, self.info = thread_posts(self.conf, self.board, self.num)
        self.label = self.info["source"]
        return posts


SEARCH_FIELDS = ("text", "subject", "filename", "tripcode")


def search_params(boards_, page, text="", subject="", filename="", tripcode="", images_only=True, start="", end=""):
    p = {"boards": ".".join(boards_), "page": page, "text": text, "subject": subject, "filename": filename, "tripcode": tripcode,
         "filter": "text" if images_only else "", "start": start, "end": end, "order": "desc"}
    return {k: v for k, v in p.items() if v not in ("", None)}


class SearchSource(Source):
    """FoolFuuka search on one archive host (its boards in one request), paged."""

    def __init__(self, conf, host, boards_, **query):
        self.conf, self.host, self.boards, self.query = conf, host, boards_, query
        self.label = host.split(".")[0]

    def fetch(self, page):
        url = f"https://{self.host}/_/api/chan/search/?" + urllib.parse.urlencode(search_params(self.boards, page, **self.query))
        data = get(self.conf, url, self.host) or {}
        raw = [p for k, v in data.items() if k != "meta" and isinstance(v, dict) for p in v.get("posts", [])]
        out = [p for p in (ff_post(r, self.host) for r in raw) if p]
        log.info("ib: search %s on %s page %d: %d posts with images (%s found in all)", self.query, self.host, page, len(out),
                 (data.get("meta") or {}).get("total_found", "?"))
        return out


def search_sources(conf, boards_, **query):
    """One SearchSource per archive host, each with the ticked boards it carries."""
    by_host = {}
    for b in boards_:
        if h := host_of(conf, b):
            by_host.setdefault(h, []).append(b)
    return [SearchSource(conf, h, bs, **query) for h, bs in by_host.items()]


# ---------- saving under the original filename ----------

def safe(name):
    return re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", name).strip(" .")[:150] or "_"


def target(conf, post, root, folder):
    """Where post's file goes: <root>/imageboard/<board>/<folder>/<original name>. A different file already
    holding that name -> "<name> (<post no>)<ext>". Setting "timestamp" keeps 4chan's <tim><ext> name instead."""
    d = Path(root) / "imageboard" / safe(post["board"]) / safe(folder or "_misc")
    remote = Path(urllib.parse.urlparse(post["files"][0]).path).name
    if conf["ib"].get("names", "original") != "original" or not post.get("filename"):
        return d / safe(remote)
    first = d / safe(post["filename"])
    if not first.exists() or not post["size"] or first.stat().st_size == post["size"]:
        return first
    return first.with_name(f"{first.stem} ({post['num']}){first.suffix}")


def save(conf, post, root, folder, cancel=None):
    """-> (new, skipped). Same name + same size on disk = already downloaded."""
    if cancel and cancel.is_set():
        return 0, 0
    path = target(conf, post, root, folder)
    if path.exists() and (not post["size"] or path.stat().st_size == post["size"]):
        return 0, 1
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".part")
    if c := cached(root, post, 0):  # already opened in the viewer: copy it
        shutil.copyfile(c, tmp)
    else:
        with open_url(post["files"][0], timeout=120) as r, open(tmp, "wb") as f:
            shutil.copyfileobj(r, f)
    tmp.replace(path)
    return 1, 0


def thread_folder(post_or_info, num):
    subject = post_or_info.get("subject") or ""
    return f"{num} - {subject}" if subject else str(num)


# ---------- watcher ----------

def watch_key(board, num):
    return f"{board}/{num}"


def watch(conf, board, num, title, on=True):
    with CONF_LOCK:
        w = conf["ib"]["watch"]
        if on:
            w[watch_key(board, num)] = {"title": title, "seen": 0, "alive": True, "added": time.time()}
        else:
            w.pop(watch_key(board, num), None)
        save_conf(conf)
    log.info("ib: %s /%s/%s %s", "watch" if on else "unwatch", board, num, title[:60])


def check_watched(conf):
    """Re-read every watched thread -> [(key, new posts)] (posts newer than what was marked seen).
    A thread gone from 4chan is read from its archive and marked dead; its images stay listed."""
    out = []
    for key, w in list(conf["ib"]["watch"].items()):
        board, num = key.split("/", 1)
        try:
            posts, info = thread_posts(conf, board, num)
        except RuntimeError as e:
            log.warning("ib: watched /%s/%s: %s", board, num, e)
            continue
        new = [p for p in posts if int(p["num"]) > int(w["seen"])]
        w["alive"], w["new"] = info["alive"], len(new)
        out.append((key, new))
        log.info("ib: watched /%s/%s: %d new images%s", board, num, len(new), "" if info["alive"] else " (thread archived)")
    with CONF_LOCK:
        save_conf(conf)
    return out


def mark_seen(conf, key, posts):
    if posts:
        with CONF_LOCK:
            w = conf["ib"]["watch"][key]
            w["seen"], w["new"] = max(int(w["seen"]), *(int(p["num"]) for p in posts)), 0
            save_conf(conf)
