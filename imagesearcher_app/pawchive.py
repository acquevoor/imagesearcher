"""Pawchive (pawchive.st -> pawchive.pw), a Kemono-style archive of paywalled creator posts: its JSON API
(recent posts + search, creators, creator pages, single posts with comments / revisions / flags, hash lookup,
account favorites with the pasted `session` cookie) plus the pages that only exist as HTML (popular, tags,
tag listings, random). One post = one tile; its images / videos are the tile's pages."""
import hashlib
import html
import json
import os
import re
import shutil
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from .core import CONF_LOCK, CONF_PATH, UA, Source, cached, log, make_post, open_url, save_conf
from .imageboard import safe

MEDIA = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".avif", ".bmp", ".mp4", ".webm", ".mov", ".m4v"}
PAGE = 50  # posts per API page (offset o)
CREATORS_PATH = CONF_PATH.with_name("paw_creators.json")
NOT_LOGGED_IN = "Pawchive: not logged in. Log in on pawchive.pw in your browser, copy the cookies (Flag Cookies) and press 📋 Paste cookies"


def host(conf):
    return conf["paw"]["host"].strip().rstrip("/") or "pawchive.pw"


def site(conf):
    return f"https://{host(conf)}"


# ---------- HTTP ----------

def _headers(conf, accept="application/json"):
    h = {"Accept": accept}
    if conf["paw"]["session"]:
        h["Cookie"] = f"session={conf['paw']['session']}"
    return h


def _send(conf, url, method, accept):
    if method == "GET":  # through core.open_url: logged, secrets masked
        return open_url(url, headers=_headers(conf, accept))
    req = urllib.request.Request(url, method=method, headers={**_headers(conf, accept), "User-Agent": UA})
    r = urllib.request.urlopen(req, timeout=30)
    log.info("%s %s -> %s", method, url, r.status)
    return r


def _open(conf, url, method="GET", accept="application/json", sleep=time.sleep, send=_send):
    """-> (status, body). A 429 waits for Retry-After (or 30 s, at most 5 min) and tries once more, like JDownloader."""
    for attempt in (1, 2):
        try:
            with send(conf, url, method, accept) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt == 1:
                ra = str(e.headers.get("Retry-After") or "")
                wait = min(int(ra) if ra.isdigit() else 30, 300)
                log.warning("pawchive: rate limited, waiting %d s then trying once more", wait)
                sleep(wait)
                continue
            return e.code, e.read()


def api(conf, path, **params):
    """GET /api/v1/<path> -> JSON. 404 -> None (Pawchive's answer to "nothing here")."""
    url = f"{site(conf)}/api/v1/{path.lstrip('/')}" + ("?" + urllib.parse.urlencode({k: v for k, v in params.items() if v not in ("", None)})
                                                       if params else "")
    status, body = _open(conf, url)
    if status == 404:
        return None
    if status in (401, 403):
        raise RuntimeError(f"Pawchive: HTTP {status}" + (" (log in: 📋 Paste cookies)" if not conf["paw"]["session"] else " (session expired? paste cookies again)"))
    if status >= 400:
        raise RuntimeError(f"Pawchive: HTTP {status} {body[:120].decode(errors='replace')}")
    data = json.loads(body) if body.strip() else None
    if isinstance(data, dict) and isinstance(data.get("error"), str):
        raise RuntimeError(f"Pawchive: {data['error']}")
    return data


def write(conf, path, method):
    """POST / DELETE an account action (favorites, flag)."""
    if path != "flag" and not conf["paw"]["session"]:
        raise RuntimeError(NOT_LOGGED_IN)
    status, body = _open(conf, f"{site(conf)}/api/v1/{path.lstrip('/')}", method)
    if status in (401, 403):
        raise RuntimeError("Pawchive refused it: not logged in or the session expired (📋 Paste cookies again)")
    if status >= 400:
        raise RuntimeError(f"Pawchive: HTTP {status} {body[:120].decode(errors='replace')}")
    return status


def page(conf, path, **params):
    """An HTML page (popular, tags, tag listings: they have no API)."""
    url = f"{site(conf)}/{path.lstrip('/')}" + ("?" + urllib.parse.urlencode({k: v for k, v in params.items() if v not in ("", None)}) if params else "")
    status, body = _open(conf, url, accept="text/html")
    if status >= 400:
        raise RuntimeError(f"Pawchive: HTTP {status} for /{path}")
    return body.decode(errors="replace")


def random_target(conf, kind="posts"):
    """/posts/random or /artists/random redirect -> (service, user, post id or None)."""
    with open_url(f"{site(conf)}/{kind}/random", headers=_headers(conf, "text/html")) as r:
        final = r.geturl()
    m = re.search(r"/(\w+)/user/([^/?#]+)(?:/post/([^/?#]+))?", final)
    if not m:
        raise RuntimeError(f"Pawchive: random went to {final}")
    log.info("pawchive: random %s -> %s", kind, final)
    return m[1], m[2], m[3]


# ---------- posts ----------

def pg_array(v):
    """Post tags: a list, or a Postgres array string like '{"a b",c}'."""
    if isinstance(v, list):
        return [str(t) for t in v]
    if isinstance(v, str) and v.startswith("{"):
        return [t.strip('"') for t in re.findall(r'"(?:[^"\\]|\\.)*"|[^,{}]+', v[1:-1])]
    return []


def text(content):
    content = re.sub(r"<br\s*/?>|</p>", "\n", content or "")
    return html.unescape(re.sub(r"<[^>]+>", "", content)).strip()


def file_url(conf, f):
    node = f.get("node") or conf["paw"]["node"]
    return f"https://n{node}.{host(conf)}/data{f['path']}?" + urllib.parse.urlencode({"f": f.get("name") or Path(f["path"]).name})


def thumb_url(conf, f):
    return f"https://img.{host(conf)}/thumbnail/data{f['path']}"


def parse_post(p, conf):
    """API post -> post dict, or None when it has no image / video. Preview-only files (the archive kept only
    the thumbnail) use the thumbnail as their page."""
    seen, media, other = set(), [], []
    for f in ([p["file"]] if p.get("file") else []) + list(p.get("attachments") or []):
        if not f or not f.get("path") or f["path"] in seen:
            continue
        seen.add(f["path"])
        ext = Path(f.get("name") or f["path"]).suffix.lower() or Path(f["path"]).suffix.lower()
        (media if ext in MEDIA else other).append(f)
    if not media:
        return None
    files = [thumb_url(conf, f) if f.get("preview_only") else file_url(conf, f) for f in media]
    still = next((f for f in media if Path(f["path"]).suffix.lower() not in (".mp4", ".webm", ".mov", ".m4v")), media[0])
    service, user, pid = p["service"], str(p["user"]), str(p["id"])
    tags = pg_array(p.get("tags")) + [f"service:{service}", f"creator:{service}/{user}"]
    return make_post("paw", f"{service}-{user}-{pid}", thumb_url(conf, still), files, files, tags, "",
                     f"{site(conf)}/{service}/user/{user}/post/{pid}", service=service, user=user, post_id=pid,
                     title=p.get("title") or "", content=text(p.get("content") or p.get("substring")),
                     published=p.get("published") or "", names=[f.get("name") or Path(f["path"]).name for f in media],
                     preview=sum(1 for f in media if f.get("preview_only")),
                     other=[{"name": f.get("name") or Path(f["path"]).name, "url": file_url(conf, f)} for f in other],
                     prev=p.get("prev"), next=p.get("next"), stub=False)


CARD = re.compile(r'<article\s+class="post-card[^"]*"\s*data-id="([^"]+)"\s*data-service="([^"]+)"\s*data-user="([^"]+)"[^>]*>(.*?)</article>', re.S)


def parse_cards(conf, page_html):
    """Post cards of an HTML page (popular, tag listings) -> stub posts (thumbnail + title; the full post is
    loaded when opened)."""
    out = []
    for pid, service, user, body in CARD.findall(page_html):
        img = re.search(r'<img[^>]*?class="post-card__image"[^>]*?src="([^"]+)"', body, re.S)
        if not img:  # text-only post
            continue
        title = html.unescape(re.sub(r"\s+", " ", (re.search(r'<header[^>]*>(.*?)</header>', body, re.S) or [None, ""])[1])).strip()
        favs = re.search(r"([\d,]+)\s+favorites", body)
        out.append(make_post("paw", f"{service}-{user}-{pid}", img[1], [img[1]], [img[1]], [f"service:{service}", f"creator:{service}/{user}"], "",
                             f"{site(conf)}/{service}/user/{user}/post/{pid}", service=service, user=user, post_id=pid, title=title,
                             content="", names=[], preview=0, other=[], prev=None, next=None, stub=True,
                             favorites=int(favs[1].replace(",", "")) if favs else 0))
    return out


TAG_ITEM = re.compile(r'<a href="/posts\?tag=([^"&]+)">\s*<span>(.*?)</span>\s*<span>(\d+)</span>', re.S)


def parse_tags(page_html):
    """/posts/tags -> [(tag, post count)], most used first."""
    return sorted(((html.unescape(urllib.parse.unquote_plus(t)), int(n)) for t, _, n in TAG_ITEM.findall(page_html)), key=lambda x: -x[1])


def load_post(conf, service, user, pid):
    """A full post (also turns a stub into the real thing)."""
    d = api(conf, f"{service}/user/{user}/post/{pid}")
    if not d:
        raise RuntimeError(f"Pawchive: post {service}/{user}/{pid} not found")
    p = parse_post(d.get("post", d), conf)
    if not p:
        raise RuntimeError("Pawchive: this post has no images or videos (see Open on site)")
    return p


# ---------- sources ----------

class RecentSource(Source):
    label = "PW"

    def __init__(self, conf, q=""):
        self.conf, self.q = conf, q

    def fetch(self, page_no):
        posts = api(self.conf, "posts", q=self.q, o=(page_no - 1) * PAGE) or []
        out = [p for p in (parse_post(x, self.conf) for x in posts) if p]
        log.info("pawchive: recent%s page %d: %d posts, %d with images", f" '{self.q}'" if self.q else "", page_no, len(posts), len(out))
        return out


class CreatorSource(Source):
    label = "PW"

    def __init__(self, conf, service, user, q="", tag=""):
        self.conf, self.service, self.user, self.q, self.tag = conf, service, user, q, tag

    def fetch(self, page_no):
        posts = api(self.conf, f"{self.service}/user/{self.user}", q=self.q, tag=self.tag, o=(page_no - 1) * PAGE) or []
        out = [p for p in (parse_post(x, self.conf) for x in posts) if p]
        log.info("pawchive: creator %s/%s page %d: %d posts, %d with images", self.service, self.user, page_no, len(posts), len(out))
        return out


class CardSource(Source):
    """An HTML listing (popular, a tag) -> stub posts. paged: by o=50 offsets; else one page only."""
    label = "PW"

    def __init__(self, conf, path, paged=True, **params):
        self.conf, self.path, self.paged, self.params = conf, path, paged, params

    def fetch(self, page_no):
        if page_no > 1 and not self.paged:
            return []
        params = {**self.params, **({"o": (page_no - 1) * PAGE} if self.paged and page_no > 1 else {})}
        out = parse_cards(self.conf, page(self.conf, self.path, **params))
        log.info("pawchive: /%s %s page %d: %d posts", self.path, params, page_no, len(out))
        return out


class ListSource(Source):
    """A fixed list of API posts (account favorites, hash results)."""
    label = "PW"

    def __init__(self, conf, load):
        self.conf, self.load = conf, load

    def fetch(self, page_no):
        if page_no > 1:
            return []
        return [p for p in (parse_post(x, self.conf) for x in self.load() or []) if p]


def account_favorites(conf, kind="post"):
    if not conf["paw"]["session"]:
        raise RuntimeError(NOT_LOGGED_IN)
    return api(conf, "account/favorites", type=kind) or []


# ---------- creators (one big list, cached a day, searched locally) ----------

def creators(conf, max_age=86400, force=False):
    try:
        if not force and time.time() - CREATORS_PATH.stat().st_mtime < max_age:
            return json.loads(CREATORS_PATH.read_text())
    except (OSError, ValueError):
        pass
    log.info("pawchive: downloading the creators list (~16 MB, once a day)")
    data = api(conf, "creators") or []
    CREATORS_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = CREATORS_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(data))
    os.replace(tmp, CREATORS_PATH)
    log.info("pawchive: %d creators cached", len(data))
    return data


SORTS = {"Updated": lambda c: -_ts(c.get("updated")), "Favorited": lambda c: -int(c.get("favorited") or 0),
         "Name": lambda c: (c.get("name") or "").lower()}


def _ts(s):
    """A creator's updated / indexed time: epoch seconds in the full list, ISO text elsewhere."""
    if isinstance(s, (int, float)):
        return s
    try:
        return time.mktime(time.strptime((s or "")[:19], "%Y-%m-%dT%H:%M:%S"))
    except ValueError:
        return 0


def find_creators(all_, q="", service="", sort="Updated", limit=500):
    q = q.strip().lower()
    hits = [c for c in all_ if (not service or c.get("service") == service)
            and (not q or q in (c.get("name") or "").lower() or q in str(c.get("public_id") or "").lower() or q == str(c.get("id")))]
    return sorted(hits, key=SORTS[sort])[:limit]


def creator_info(conf, service, user):
    """Profile + linked accounts + tags, for a creator tab's header."""
    prof = api(conf, f"{service}/user/{user}/profile") or {"name": user, "service": service, "id": user}
    return {"profile": prof, "links": api(conf, f"{service}/user/{user}/links") or [],
            "tags": api(conf, f"{service}/user/{user}/tags") or []}


# ---------- account / moderation ----------

def cookie_login(conf, text_):
    """Keep only Pawchive's `session` cookie from pasted cookies (Flag Cookies JSON, cookies.txt, header)."""
    from .x import parse_cookies
    sess = parse_cookies(text_, ("pawchive.pw", "pawchive.st", host(conf))).get("session", "")
    if not sess:
        raise RuntimeError("no 'session' cookie in the paste (logged in on pawchive.pw in that browser?)")
    with CONF_LOCK:
        conf["paw"]["session"] = sess
    log.info("pawchive: session cookie pasted")
    return "session"


def check_login(conf):
    """-> number of favorited posts and artists (proves the session works)."""
    posts, artists = account_favorites(conf, "post"), account_favorites(conf, "artist")
    return len(posts), len(artists)


def fav_post(conf, p, on):
    write(conf, f"favorites/post/{p['service']}/{p['user']}/{p['post_id']}", "POST" if on else "DELETE")
    log.info("pawchive: %s post %s", "favorite" if on else "unfavorite", p["id"])


def fav_creator(conf, service, user, on):
    write(conf, f"favorites/creator/{service}/{user}", "POST" if on else "DELETE")
    log.info("pawchive: %s creator %s/%s", "favorite" if on else "unfavorite", service, user)


def flagged(conf, p):
    status, _ = _open(conf, f"{site(conf)}/api/v1/{p['service']}/user/{p['user']}/post/{p['post_id']}/flag")
    return status == 200


def flag(conf, p):
    write(conf, f"{p['service']}/user/{p['user']}/post/{p['post_id']}/flag", "POST")
    log.info("pawchive: flagged post %s", p["id"])


def comments(conf, p):
    return api(conf, f"{p['service']}/user/{p['user']}/post/{p['post_id']}/comments") or []


def revisions(conf, p):
    return api(conf, f"{p['service']}/user/{p['user']}/post/{p['post_id']}/revisions") or []


# ---------- hash lookup ----------

def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def lookup_hash(conf, sha):
    """Posts holding the file with this SHA-256 (Pawchive stores files by it)."""
    d = api(conf, f"search_hash/{sha}") or {}
    log.info("pawchive: hash %s… found in %d posts", sha[:12], len(d.get("posts") or []))
    return d.get("posts") or []


def sha_of_url(url):
    """Pawchive file paths are the file's SHA-256."""
    m = re.search(r"/([0-9a-f]{64})\.", url)
    return m[1] if m else ""


# ---------- saving under the original filenames ----------

def creator_name(conf, service, user):
    try:
        for c in json.loads(CREATORS_PATH.read_text()) if CREATORS_PATH.exists() else []:
            if c.get("service") == service and str(c.get("id")) == str(user):
                return c.get("name") or user
    except (OSError, ValueError):
        pass
    return user


def folder(conf, p, root):
    return Path(root) / "pawchive" / safe(f"{creator_name(conf, p['service'], p['user'])} ({p['service']})") / safe(
        f"{p['post_id']} - {p['title']}" if p.get("title") else p["post_id"])


def same_file(path, url):
    """Is the file on disk the one at url? Pawchive URLs carry the file's SHA-256; without it, the name decides."""
    sha = sha_of_url(url)
    return not sha or sha256_file(path) == sha


def target(conf, p, root, name, url):
    """<root>/pawchive/<creator> (<service>)/<post id> - <title>/<original name>. A different file already
    holding the name -> "<name> (2)", "(3)", …; the same file (by SHA-256) is reused."""
    base = folder(conf, p, root) / safe(name)
    path, n = base, 1
    while path.exists() and not same_file(path, url):
        n += 1
        path = base.with_name(f"{base.stem} ({n}){base.suffix}")
    return path


def jobs(p, everything=False):
    """[(page index or None, original name, url)] of what a download of p saves."""
    out = [(i, p["names"][i] if i < len(p["names"]) else Path(urllib.parse.urlparse(u).path).name, u) for i, u in enumerate(p["files"])]
    return out + ([(None, o["name"], o["url"]) for o in p.get("other") or []] if everything else [])


def save(conf, p, root, cancel=None, everything=False):
    """-> (new, skipped): the post's images / videos, plus its other attachments (zip, psd…) if everything."""
    if p.get("stub"):
        p = load_post(conf, p["service"], p["user"], p["post_id"])
    new = skip = 0
    for i, name, url in jobs(p, everything):
        if cancel and cancel.is_set():
            break
        path = target(conf, p, root, name, url)
        if path.exists():
            skip += 1
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".part")
        if i is not None and (c := cached(root, p, i)):
            shutil.copyfile(c, tmp)
        else:
            with open_url(url, timeout=120) as r, open(tmp, "wb") as f:
                shutil.copyfileobj(r, f)
        tmp.replace(path)
        new += 1
    return new, skip


def saved_paths(conf, p, root):
    """The image / video files of p on disk (for the live bulk tab)."""
    return [path for i, name, url in jobs(p) if (path := target(conf, p, root, name, url)).exists()]


# ---------- bulk: every post of one creator ----------

def bulk_creator(conf, service, user, root, cancel, on_post=lambda p: None, everything=False, workers=3, progress=lambda st: None):
    """Page the creator's posts and save them, `workers` posts at a time. The offset is kept in
    conf["paw"]["bulk"] after every page, so Continue resumes there (files on disk are skipped anyway)."""
    key = f"{service}/{user}"
    b = conf["paw"].setdefault("bulk", {})
    st = b.get(key) or {"o": 0, "new": 0, "skip": 0, "fail": 0, "posts": 0, "done": False, "everything": everything}
    st["done"] = False
    b[key] = st
    log.info("pawchive: bulk %s from offset %d (%s)", key, st["o"], "everything" if everything else "images and videos")
    while not cancel.is_set():
        raw = api(conf, f"{service}/user/{user}", o=st["o"]) or []
        if not raw:
            st["done"] = True
            break
        posts = [p for p in (parse_post(x, conf) for x in raw) if p]
        with ThreadPoolExecutor(workers) as ex:
            futs = {ex.submit(save, conf, p, root, cancel, everything): p for p in posts}
            for f in as_completed(futs):
                try:
                    n, s = f.result()
                    st["new"] += n
                    st["skip"] += s
                    if n + s:
                        on_post(futs[f])
                except Exception as e:
                    st["fail"] += 1
                    log.warning("pawchive: bulk %s: post %s failed: %s", key, futs[f]["post_id"], e)
        if cancel.is_set():
            break
        st["o"] += len(raw)
        st["posts"] += len(posts)
        with CONF_LOCK:
            save_conf(conf)
        log.info("pawchive: bulk %s: %d posts done, %d files new, %d already had, %d failed", key, st["o"], st["new"], st["skip"], st["fail"])
        progress(dict(st))
    with CONF_LOCK:
        save_conf(conf)
    log.info("pawchive: bulk %s %s: %d files new, %d already had, %d failed", key, "finished" if st["done"] else "stopped",
             st["new"], st["skip"], st["fail"])
    return st


def day(v):
    """Epoch seconds or ISO text -> YYYY-MM-DD."""
    return time.strftime("%Y-%m-%d", time.gmtime(v)) if isinstance(v, (int, float)) else str(v or "")[:10]
