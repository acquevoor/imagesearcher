"""Comics: E-Hentai (search / front page / popular pages + its JSON API) and Hitomi (the .nozomi id lists +
one galleries/<id>.js per gallery). One gallery = one tile; its pages are the tile's pages.

Queries are one syntax for both: words and namespace:tag terms, _ for spaces, -term excludes
("female:big_breasts artist:shindol -male:yaoi language:english")."""
import html
import json
import re
import struct
import threading
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor

from .core import RESOLVERS, Source, http, log, make_post

EH = "https://e-hentai.org"
EH_API = "https://api.e-hentai.org/api.php"
EH_COOKIE = {"Cookie": "nw=1"}  # skips the "offensive content" warning page
EH_PAGE = re.compile(r"^https://e-hentai\.org/g/(\d+)/(\w+)/\?page=(\d+)$")  # page n of a gallery (see eh_resolve)
HI = "https://ltn.gold-usergeneratedcontent.net"
HI_DOMAIN = "gold-usergeneratedcontent.net"
PER_PAGE = 25  # Hitomi galleries per feed page
CATS = {"artist": "artist", "group": "artist", "parody": "copyright", "series": "copyright", "character": "character"}


def terms(q):
    """'female:big_breasts -male:yaoi words' -> [(neg, namespace, value)] with spaces back in values."""
    out = []
    for t in q.split():
        neg = t.startswith("-") and len(t) > 1
        t = t[1:] if neg else t
        ns, _, val = t.rpartition(":")
        out.append((neg, ns.lower(), val.replace("_", " ").lower()))
    return out


def cats_of(tags):
    out = {}
    for t in tags:
        ns = t.split(":", 1)[0] if ":" in t else ""
        out.setdefault(CATS.get(ns, "general"), []).append(t)
    return out


def comic(site, gid, title, thumb, files, tags, page, **extra):
    return make_post(site, gid, thumb, files, files, tags, extra.pop("rating", "e"), page, title=title, cats=cats_of(tags), ext=".webp", **extra)


# ---------- E-Hentai ----------

def eh_query(q):
    """Our syntax -> E-Hentai's: namespaced terms become exact tags (female:"big breasts$")."""
    out = []
    for neg, ns, val in terms(q):
        ns = {"series": "parody", "tag": ""}.get(ns, ns)
        out.append(("-" if neg else "") + (f'{ns}:"{val}$"' if ns else (f'"{val}"' if " " in val else val)))
    return " ".join(out)


def eh_gdata(ids):
    """[(gid, token)] -> E-Hentai API metadata, 25 a request."""
    out = []
    for i in range(0, len(ids), 25):
        body = json.dumps({"method": "gdata", "gidlist": [[int(g), t] for g, t in ids[i:i + 25]], "namespace": 1}).encode()
        out += json.loads(http(EH_API, data=body, headers={"Content-Type": "application/json"}))["gmetadata"]
    return [m for m in out if "error" not in m]


def eh_post(m):
    gid, token, n = m["gid"], m["token"], int(m["filecount"])
    title = html.unescape(m["title"])
    files = [f"{EH}/g/{gid}/{token}/?page={i}" for i in range(1, n + 1)]
    tags = [t if ":" in t else f"tag:{t}" for t in m.get("tags", [])]
    return comic("eh", gid, title, m["thumb"], files, tags, f"{EH}/g/{gid}/{token}/", rating="q" if m["category"] == "Non-H" else "e",
                 category=m["category"], uploaded=int(m.get("posted") or 0), score=float(m.get("rating") or 0), user_name=m.get("uploader", ""))


class EHSource(Source):
    """A gallery list page (search / front page; popular has no paging), paged by its own next= cursor."""
    label = "EH"

    def __init__(self, q="", path="/"):
        self.q, self.path, self.next = q, path, None

    def fetch(self, page):
        if page > 1 and not self.next:
            return []
        params = {"f_search": eh_query(self.q)} if self.q else {}
        if page > 1:
            params["next"] = self.next
        text = http(f"{EH}{self.path}?{urllib.parse.urlencode(params)}" if params else EH + self.path, headers=EH_COOKIE).decode(errors="replace")
        ids = list(dict.fromkeys(re.findall(r"https://e-hentai\.org/g/(\d+)/(\w+)/", text)))
        nxt = re.search(r'id="unext" href="[^"]*next=(\d+)', text)
        self.next = nxt and self.path == "/" and nxt[1]
        posts = [eh_post(m) for m in eh_gdata(ids)] if ids else []
        log.info("e-hentai %s'%s' page %d: %d galleries", self.path.strip("/") + " " if self.path != "/" else "", self.q, page, len(posts))
        return posts


_eh_index, _eh_lock = {}, threading.Lock()  # (gid, gallery page) -> {page n: /s/ url}


def eh_resolve(url):
    """Page n of a gallery -> its image URL: the gallery page listing it (20 a page, cached) -> the /s/ page -> <img id="img">."""
    gid, token, n = EH_PAGE.match(url).groups()
    n, per = int(n), 20
    for _ in range(2):
        p = (n - 1) // per
        with _eh_lock:
            links = _eh_index.get((gid, p))
        if links is None:
            text = http(f"{EH}/g/{gid}/{token}/?p={p}", headers=EH_COOKIE).decode(errors="replace")
            links = {int(k): s for s, k in re.findall(r"(https://e-hentai\.org/s/\w+/\d+-(\d+))", text)}
            with _eh_lock:
                _eh_index[(gid, p)] = links
        if n in links:
            break
        per = len(links) or per  # a different thumbnails-per-page setting: look again where it really is
    else:
        raise RuntimeError(f"e-hentai: page {n} not in gallery {gid}")
    page = http(links[n], headers=EH_COOKIE).decode(errors="replace")
    img = re.search(r'<img id="img" src="([^"]+)"', page)
    if not img:
        raise RuntimeError("e-hentai: no image on the page")
    if img[1].endswith("/509.gif"):
        raise RuntimeError("e-hentai: image limit reached for now (it resets over time)")
    return html.unescape(img[1])


# ---------- Hitomi ----------

_gg = {"t": 0}


def gg():
    """Hitomi's image-server rules (gg.js, changes every so often): -> (m(g), b)."""
    if time.time() - _gg["t"] > 600:
        js = http(f"{HI}/gg.js").decode()
        default, val = int(re.search(r"var o = (\d)", js)[1]), int(re.search(r"o = (\d); break;", js)[1])
        _gg.update(t=time.time(), cases={int(c) for c in re.findall(r"case (\d+):", js)}, default=default, val=val,
                   b=re.search(r"b: '(\d+)/'", js)[1])
    return (lambda g: _gg["val"] if g in _gg["cases"] else _gg["default"]), _gg["b"]


def hi_urls(h):
    """(image, thumbnail) URLs of a file hash."""
    m, b = gg()
    g = int(h[-1] + h[-3:-1], 16)
    return (f"https://w{1 + m(g)}.{HI_DOMAIN}/{b}/{g}/{h}.webp",
            f"https://{chr(97 + m(g))}tn.{HI_DOMAIN}/webpbigtn/{h[-1]}/{h[-3:-1]}/{h}.webp")


def hi_post(info):
    files = [hi_urls(f["hash"])[0] for f in info["files"]]
    tags = [f"{'female:' if t.get('female') in ('1', 1) else 'male:' if t.get('male') in ('1', 1) else 'tag:'}{t['tag']}" for t in info.get("tags") or []]
    for key, ns in (("artists", "artist"), ("groups", "group"), ("parodys", "series"), ("characters", "character")):
        tags += [f"{ns}:{x[ns if ns != 'series' else 'parody']}" for x in info.get(key) or []]
    tags += [f"language:{info['language']}"] * bool(info.get("language")) + [f"type:{info['type']}"] * bool(info.get("type"))
    return comic("hitomi", info["id"], info.get("title", ""), hi_urls(info["files"][0]["hash"])[1], files, tags,
                 f"https://hitomi.la/galleries/{info['id']}.html", category=info.get("type", ""),
                 w=info["files"][0].get("width", 0), h=info["files"][0].get("height", 0))


def hi_gallery(gid):
    text = http(f"{HI}/galleries/{gid}.js").decode()
    return hi_post(json.loads(text.split("=", 1)[1]))


def nozomi(path, start=None, count=None):
    """Gallery ids from a .nozomi list, newest first; start/count read just that slice."""
    h = {"Range": f"bytes={start * 4}-{(start + count) * 4 - 1}"} if start is not None else {}
    try:
        data = http(f"{HI}/n/{urllib.parse.quote(path)}.nozomi", headers=h)
    except Exception as e:
        if getattr(e, "code", 0) in (404, 416):  # no such tag / read past the end
            return []
        raise
    return list(struct.unpack(f">{len(data) // 4}i", data[:len(data) // 4 * 4]))


class HitomiSource(Source):
    """Newest, popular or a tag search. One positive term pages through its list in place; more terms or
    any -term read the whole lists once and intersect them."""
    label = "HI"

    def __init__(self, q="", popular=""):
        self.ids, self.lang, self.lists = None, "all", []
        for neg, ns, val in terms(q):
            if ns == "language" and not neg:
                self.lang = val
                continue
            area = {"female": "tag", "male": "tag", "": "tag", "tag": "tag", "parody": "series"}.get(ns, ns)
            tag = f"{ns}:{val}" if ns in ("female", "male") else val
            self.lists.append((neg, f"{area}/{tag}"))
        self.base = f"popular/{popular}" if popular else "index"

    def fetch(self, page):
        pos = [p for neg, p in self.lists if not neg]
        if len(self.lists) <= 1 and not any(neg for neg, _ in self.lists):
            ids = nozomi(f"{pos[0]}-{self.lang}" if pos else f"{self.base}-{self.lang}", (page - 1) * PER_PAGE, PER_PAGE)
        else:
            if self.ids is None:
                lists = {p: nozomi(f"{p}-{self.lang}") for _, p in self.lists}
                keep = lists[pos[0]] if pos else nozomi(f"index-{self.lang}")
                ok = set.intersection(*(set(lists[p]) for p in pos)) if pos else set(keep)
                ok -= {i for neg, p in self.lists if neg for i in lists[p]}
                self.ids = [i for i in keep if i in ok]
            ids = self.ids[(page - 1) * PER_PAGE:page * PER_PAGE]
        with ThreadPoolExecutor(8) as ex:
            posts = list(ex.map(lambda i: _try(hi_gallery, i), ids))
        posts = [p for p in posts if p]
        log.info("hitomi %s page %d: %d galleries", " ".join(p for _, p in self.lists) or self.base, page, len(posts))
        return posts


def _try(fn, *a):
    try:
        return fn(*a)
    except Exception as e:  # one broken gallery doesn't sink the page
        log.warning("hitomi: %s %s skipped: %s", fn.__name__, a, e)
        return None


def sources(conf, q="", popular=False):
    on = conf["comics"]["sources"]
    return ([EHSource(q, "/popular" if popular else "/")] if "eh" in on and not (popular and q) else []) + \
           ([HitomiSource(q, "week" if popular else "")] if "hitomi" in on else [])


RESOLVERS.append((EH_PAGE, eh_resolve))
