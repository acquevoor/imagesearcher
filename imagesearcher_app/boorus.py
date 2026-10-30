"""Booru engines, one class per API family, so any site running one of them can be added by its URL.

Every engine has search(); the optional features (complete, tags, count, wiki, related, pools, artist,
find_pixiv) are plain methods a subclass may define, and the UI only shows what has(feature) reports."""
import json
import re
import urllib.error
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from xml.etree import ElementTree

from .core import LABELS, NAMES, UA, Source, get_json, http, make_post, px_id, qs

ORDERS = ("Count", "Name", "Newest")  # tag list orders offered by the Tags tab


def dtext(s, n=600):
    """Danbooru-style wiki markup (DText / Textile / Markdown) -> plain excerpt."""
    s = re.sub(r"\[\[(?:[^]|]*\|)?([^]]*)\]\]", r"\1", s or "")  # [[tag|label]] -> label
    s = re.sub(r"\[/?\w+(?:=[^]]*)?\]|^h\d\.\s*|>>\d+\w?|thumb #\d+|[*_]{2,}", "", s, flags=re.M).strip()  # [b], h4., >>123s, thumb #1
    return s[:n] + ("…" if len(s) > n else "")


class Engine:
    CATS = {}  # tag category code -> name (see core.CAT_COLOR)
    SORT = {}  # "Score" / "Random" -> query token
    RATING = {"safe": "g", "general": "g", "sensitive": "s", "questionable": "q", "explicit": "e"}  # -> g / s / q / e
    hint = ""  # appended to 401/403/422 errors

    def __init__(self, site, limit=40):
        self.site, self.id, self.name = site, site["id"], site["name"]
        self.short = site["short"] or site["id"][:3].upper()
        self.url = site["url"].rstrip("/")
        self.api = (site.get("api_url") or self.url).rstrip("/")
        self.limit = limit

    def has(self, feature):
        return hasattr(self, feature)

    def auth(self):
        return {}

    def headers(self):
        return {}

    def get(self, path, **params):
        return get_json(f"{self.api}/{path}?" + qs(**params, **self.auth()), self.name, self.hint, headers=self.headers())

    def join(self, *parts):
        return " ".join(p for p in parts if p)

    def id_after(self, n):
        return f"id:>{n}"

    def search(self, q, page, sort=""):
        return [p for p in map(self.parse, self.posts(self.join(q, self.SORT.get(sort, "")), page)) if p]

    def rating(self, r):
        r = str(r or "").lower()
        return self.RATING.get(r, r[:1])

    def cat_code(self, name):
        return next((k for k, v in self.CATS.items() if v == name), None)


class Danbooru(Engine):
    CATS = {0: "general", 1: "artist", 3: "copyright", 4: "character", 5: "meta"}
    SORT = {"Score": "order:score", "Random": "order:random"}
    hint = " (a login + API key in Sites lifts this)"

    def auth(self):
        return {"login": self.site["login"], "api_key": self.site["key"]}

    def posts(self, q, page):
        return self.get("posts.json", tags=q, page=page, limit=self.limit) or []

    def parse(self, p):
        variants = {v["type"]: v["url"] for v in (p.get("media_asset") or {}).get("variants") or []}
        f = p.get("file_url") or variants.get("original")
        if not f:  # deleted / gold-only
            return None
        return make_post(self.id, p["id"], variants.get("360x360") or p.get("preview_file_url"), [f],
                         [p.get("large_file_url") or f], p.get("tag_string", "").split(), self.rating(p.get("rating")),
                         f"{self.url}/posts/{p['id']}", p.get("image_width", 0), p.get("image_height", 0),
                         pixiv_id=str(p["pixiv_id"]) if p.get("pixiv_id") else px_id(p.get("source")), md5=p.get("md5", ""),
                         cats={c: p.get(f"tag_string_{c}", "").split() for c in ("artist", "copyright", "character", "general", "meta")})

    def complete(self, word):
        res = self.get("autocomplete.json", **{"search[query]": word, "search[type]": "tag_query", "limit": 15})
        return [(t["value"], f"{t['value']}  ({t.get('post_count', 0)})") for t in res or []]

    def tags(self, pattern, cat, order, page):
        rows = self.get("tags.json", **{"search[name_matches]": pattern, "search[category]": self.cat_code(cat), "search[hide_empty]": "true",
                                        "search[order]": {"Count": "count", "Name": "name", "Newest": "date"}[order],
                                        "page": page, "limit": 40, "only": "name,post_count,category"})
        return [{"name": t["name"], "count": t["post_count"], "cat": self.CATS.get(t["category"], "general")} for t in rows or []]

    def count(self, q):
        return self.get("counts/posts.json", tags=q)["counts"]["posts"]

    def wiki(self, tag):
        try:
            w = self.get(f"wiki_pages/{urllib.parse.quote(tag, safe='')}.json") or {}
        except RuntimeError:
            return None  # no wiki page
        return {"body": dtext(w.get("body")), "names": w.get("other_names", [])}

    def wiki_names(self, tags):
        """{tag: other names} for many tags in one request (the Pixiv bridge)."""
        res = self.get("wiki_pages.json", **{"search[title_comma]": ",".join(tags), "only": "title,other_names", "limit": 100})
        return {w["title"]: w["other_names"] for w in res or []}

    def wiki_reverse(self, name):
        """The tag whose wiki lists name (e.g. a Pixiv tag) as an other name, else None."""
        res = self.get("wiki_pages.json", **{"search[other_names_match]": name, "only": "title", "limit": 1})
        return res[0]["title"] if res else None

    def related(self, tag):
        rel = self.get("related_tag.json", query=tag, limit=25) or {}
        return [(t["tag"]["name"], self.CATS.get(t["tag"]["category"], "general")) for t in rel.get("related_tags", []) if t["tag"]["name"] != tag]

    def pools(self, pattern, order, page):
        rows = self.get("pools.json", **{"search[name_matches]": pattern, "search[is_deleted]": "false", "page": page, "limit": 40,
                                         "search[order]": {"Count": "post_count", "Name": "name", "Newest": "created_at"}[order],
                                         "only": "id,name,post_count,description"})
        return [{"name": p["name"], "count": p["post_count"], "cat": "pool", "id": p["id"], "desc": dtext(p["description"])} for p in rows or []]

    def pool_query(self, pid):
        return f"ordpool:{pid}"

    def artist(self, tag):
        a = (self.get("artists.json", **{"search[name]": tag, "only": "name,other_names,urls"}) or [None])[0]
        return a and {"names": a.get("other_names", []), "urls": [u["url"] for u in a.get("urls", [])]}

    def find_pixiv(self, pid):
        return self.search(f"pixiv:{pid}", 1)


class Gelbooru(Engine):
    """Gelbooru 0.2 "dapi": gelbooru.com, rule34.xxx (API on api.rule34.xxx), safebooru.org, xbooru, tbib, …"""
    CATS = {0: "general", 1: "artist", 3: "copyright", 4: "character", 5: "meta"}
    SORT = {"Score": "sort:score:desc", "Random": "sort:random"}
    hint = " (set user id + API key in Sites)"

    def auth(self):
        return {"user_id": self.site["login"], "api_key": self.site["key"]}

    def posts(self, q, page):
        data = self.get("index.php", page="dapi", s="post", q="index", json=1, tags=q, pid=page - 1, limit=self.limit)
        return data.get("post", []) if isinstance(data, dict) else data or []

    def parse(self, p):
        if not p.get("file_url"):
            return None
        sample = p.get("sample_url") if p.get("sample") and p.get("sample_url") else p["file_url"]
        return make_post(self.id, p["id"], p.get("preview_url"), [p["file_url"]], [sample], p.get("tags", "").split(),
                         self.rating(p.get("rating")), f"{self.url}/index.php?page=post&s=view&id={p['id']}",
                         p.get("width", 0), p.get("height", 0), pixiv_id=px_id(p.get("source")), md5=p.get("md5") or p.get("hash") or "")

    def complete(self, word):
        try:  # Gelbooru itself
            res = self.get("index.php", page="autocomplete2", term=word, type="tag_query", limit=15)
        except RuntimeError:
            res = None
        if not isinstance(res, list):  # the other 0.2 sites
            res = get_json(f"{self.api}/autocomplete.php?" + qs(q=word), self.name) or []
        return [(t["value"], t.get("label") or t["value"]) for t in res if t.get("value")]

    def raw(self, **params):
        try:
            return http(f"{self.api}/index.php?" + qs(page="dapi", q="index", **params, **self.auth())).strip()
        except urllib.error.HTTPError as e:
            raise RuntimeError(f"{self.name}: HTTP {e.code}{self.hint if e.code in (401, 403) else ''}") from None

    def tags(self, pattern, cat, order, page):
        body = self.raw(s="tag", json=1, name_pattern=pattern.replace("*", "%"), pid=page - 1, limit=40,
                        orderby={"Count": "count", "Name": "name", "Newest": "date"}[order], order="ASC" if order == "Name" else "DESC")
        code = self.cat_code(cat)
        # ponytail: no server-side category filter, so a filtered page may come back short or empty
        return [{"name": t["name"], "count": int(t.get("count", 0)), "cat": self.CATS.get(int(t.get("type", 0)), "general")}
                for t in parse_gel_tags(self.name, body) if code is None or int(t.get("type", 0)) == code]

    def count(self, q):
        return int(ElementTree.fromstring(self.raw(s="post", limit=0, tags=q)).get("count", 0))


def parse_gel_tags(name, body):
    """Gelbooru answers the tag dapi in JSON, most other 0.2 sites only in XML."""
    if body.startswith(b"<"):
        return [dict(t.attrib) for t in ElementTree.fromstring(body).iter("tag")]
    try:
        data = json.loads(body) if body else []
    except ValueError:
        raise RuntimeError(f"{name}: {body[:150].decode(errors='replace')} (set user id + API key in Sites)") from None
    return data.get("tag", []) if isinstance(data, dict) else data


class Moebooru(Engine):
    """konachan.com / .net, yande.re, sakugabooru."""
    CATS = {0: "general", 1: "artist", 3: "copyright", 4: "character", 5: "artist", 6: "meta"}  # 5 = circle
    SORT = {"Score": "order:score", "Random": "order:random"}
    RATING = {"s": "g"}  # Moebooru's s is "safe"

    def auth(self):
        return {"login": self.site["login"], "password_hash": self.site["key"]}

    def posts(self, q, page):
        return self.get("post.json", tags=q, page=page, limit=self.limit) or []

    def parse(self, p):
        if not p.get("file_url"):
            return None
        return make_post(self.id, p["id"], p.get("preview_url"), [p["file_url"]], [p.get("sample_url") or p["file_url"]],
                         p.get("tags", "").split(), self.rating(p.get("rating")), f"{self.url}/post/show/{p['id']}",
                         p.get("width", 0), p.get("height", 0), pixiv_id=px_id(p.get("source")), md5=p.get("md5", ""))

    def complete(self, word):
        return [(t["name"], f"{t['name']}  ({t['count']})") for t in self.get("tag.json", name=f"{word}*", order="count", limit=15) or []]

    def tags(self, pattern, cat, order, page):
        rows = self.get("tag.json", name=pattern or "*", type=self.cat_code(cat), page=page, limit=40,
                        order={"Count": "count", "Name": "name", "Newest": "date"}[order])
        return [{"name": t["name"], "count": t["count"], "cat": self.CATS.get(t["type"], "general")} for t in rows or []]

    def count(self, q):
        return int(ElementTree.fromstring(http(f"{self.api}/post.xml?" + qs(tags=q, limit=1, **self.auth()))).get("count", 0))

    def wiki(self, tag):
        w = next((w for w in self.get("wiki.json", query=tag) or [] if w["title"] == tag), None)  # the search is fuzzy
        return w and {"body": dtext(w.get("body")), "names": []}

    def related(self, tag):
        res = self.get("tag/related.json", tags=tag) or {}
        return [(t, "") for t, _ in next(iter(res.values()), [])[:25] if t != tag]

    def pools(self, pattern, order, page):  # ponytail: Moebooru can't order pools, the order combo is ignored
        rows = self.get("pool.json", query=pattern.strip("*"), page=page)
        return [{"name": p["name"], "count": p["post_count"], "cat": "pool", "id": p["id"], "desc": dtext(p.get("description"))} for p in rows or []]

    def pool_query(self, pid):
        return f"pool:{pid}"

    def artist(self, tag):
        a = next((a for a in self.get("artist.json", name=tag) or [] if a["name"] == tag), None)
        return a and {"names": [], "urls": a.get("urls") or []}

    def find_pixiv(self, pid):
        return self.search(f"source:*{pid}*", 1)


class E621(Engine):
    """e621.net / e926.net."""
    CATS = {0: "general", 1: "artist", 2: "artist", 3: "copyright", 4: "character", 5: "species", 6: "meta", 7: "meta", 8: "general"}
    SORT = {"Score": "order:score", "Random": "order:random"}
    RATING = {"s": "g"}  # e621's s is "safe"
    hint = " (login + API key in Sites)"

    def headers(self):  # e621 asks every client to name itself and its user
        return {"User-Agent": f"{UA} (by {self.site['login'] or 'anonymous'} on e621)"}

    def auth(self):
        return {"login": self.site["login"], "api_key": self.site["key"]}

    def posts(self, q, page):
        return (self.get("posts.json", tags=q, page=page, limit=self.limit) or {}).get("posts", [])

    def parse(self, p):
        f = (p.get("file") or {}).get("url")
        if not f:  # hidden by e621's default blacklist for anonymous users
            return None
        t = p.get("tags") or {}
        cats = {"artist": t.get("artist", []) + t.get("contributor", []), "copyright": t.get("copyright", []),
                "character": t.get("character", []), "species": t.get("species", []),
                "general": t.get("general", []) + t.get("lore", []), "meta": t.get("meta", []) + t.get("invalid", [])}
        sample = (p.get("sample") or {}).get("url") if (p.get("sample") or {}).get("has") else None
        return make_post(self.id, p["id"], (p.get("preview") or {}).get("url"), [f], [sample or f], [x for v in cats.values() for x in v],
                         self.rating(p.get("rating")), f"{self.url}/posts/{p['id']}", p["file"].get("width", 0), p["file"].get("height", 0),
                         pixiv_id=next(filter(None, map(px_id, p.get("sources", []))), None), md5=p["file"].get("md5", ""), cats=cats)

    def complete(self, word):
        res = self.get("tags/autocomplete.json", **{"search[name_matches]": word, "expiry": 7})
        return [(t["name"], f"{t['name']}  ({t['post_count']})") for t in res or []]

    def tags(self, pattern, cat, order, page):
        rows = self.get("tags.json", **{"search[name_matches]": pattern or "*", "search[category]": self.cat_code(cat), "search[hide_empty]": "true",
                                        "search[order]": {"Count": "count", "Name": "name", "Newest": "date"}[order], "page": page, "limit": 40})
        return [{"name": t["name"], "count": t["post_count"], "cat": self.CATS.get(t["category"], "general")} for t in rows or [] if isinstance(t, dict)]

    def count(self, q):  # only a single tag has a cheap count here
        rows = self.get("tags.json", **{"search[name]": q}) if " " not in q.strip() else []
        return rows[0]["post_count"] if isinstance(rows, list) and rows else None

    def wiki(self, tag):
        try:
            w = self.get(f"wiki_pages/{urllib.parse.quote(tag, safe='')}.json") or {}
        except RuntimeError:
            return None
        return {"body": dtext(w.get("body")), "names": w.get("other_names", [])}

    def related(self, tag):
        rows = self.get("tags.json", **{"search[name]": tag})
        words = (rows[0].get("related_tags") or "").split() if isinstance(rows, list) and rows else []
        return [(t, "") for t in words[::2][:25] if t != tag]  # "name count name count …"

    def pools(self, pattern, order, page):
        rows = self.get("pools.json", **{"search[name_matches]": pattern, "page": page, "limit": 40,
                                         "search[order]": {"Count": "post_count", "Name": "name", "Newest": "created_at"}[order]})
        return [{"name": p["name"], "count": p["post_count"], "cat": "pool", "id": p["id"], "desc": dtext(p.get("description"))} for p in rows or []]

    def pool_query(self, pid):
        return f"pool:{pid}"

    def artist(self, tag):
        a = (self.get("artists.json", **{"search[name]": tag}) or [None])[0]
        return a and {"names": a.get("other_names", []), "urls": [u["url"] for u in a.get("urls", []) if u.get("is_active", True)]}

    def find_pixiv(self, pid):
        return self.search(f"source:*{pid}*", 1)


class Philomena(Engine):
    """derpibooru, furbooru, ponybooru, … Tags have spaces and queries are comma-separated, so the app shows
    tags with underscores and translates "a b -c_d" to "a, b, -c d"."""
    RATING = {"safe": "g", "suggestive": "s", "questionable": "q", "semi-grimdark": "q", "explicit": "e", "grimdark": "e", "grotesque": "e"}
    TAG_CAT = {"origin": "artist", "character": "character", "oc": "character", "species": "species", "rating": "meta",
               "spoiler": "meta", "error": "meta", "content-official": "copyright", "content-fanmade": "copyright"}
    CATS = {0: "general", 1: "artist", 3: "copyright", 4: "character", 5: "species"}  # names offered by the category filter
    SORT = {"Score": "score", "Random": "random"}  # sf= values

    def auth(self):
        return {"key": self.site["key"], "filter_id": self.site.get("filter_id")}

    def join(self, *parts):
        return ", ".join(p for p in parts if p)

    def id_after(self, n):
        return f"id.gt:{n}"

    def query(self, q):
        if "," in q:
            return q
        return ", ".join(t[:1] + t[1:].replace("_", " ") for t in q.split()) or "*"

    def search(self, q, page, sort=""):
        sf = self.SORT.get(sort)
        res = self.get("api/v1/json/search/images", q=self.query(q), page=page, per_page=min(self.limit, 50), sf=sf, sd=sf and "desc")
        return [p for p in map(self.parse, (res or {}).get("images", [])) if p]

    def parse(self, i):
        if i.get("hidden_from_users") or not i.get("processed", True):
            return None
        reps = i.get("representations") or {}
        full = reps.get("full") or i.get("view_url")
        if not full:
            return None
        tags = [t.replace(" ", "_") for t in i.get("tags", [])]
        rating = next((self.RATING[t] for t in tags if t in self.RATING), "")
        cats = {"artist": [t for t in tags if t.startswith("artist:")], "character": [t for t in tags if t.startswith("oc:")],
                "meta": [t for t in tags if t in self.RATING]}
        cats["general"] = [t for t in tags if not any(t in v for v in cats.values())]
        sources = i.get("source_urls") or [i.get("source_url")]
        return make_post(self.id, i["id"], reps.get("thumb") or reps.get("small"), [full], [reps.get("large") or full], tags, rating,
                         f"{self.url}/images/{i['id']}", i.get("width", 0), i.get("height", 0),
                         pixiv_id=next(filter(None, map(px_id, sources)), None), cats=cats)

    def complete(self, word):
        res = self.get("api/v1/json/search/tags", q=f"{word.replace('_', ' ')}*", sf="images", sd="desc", per_page=15) or {}
        return [(t["name"].replace(" ", "_"), f"{t['name']}  ({t['images']})") for t in res.get("tags", [])]

    def tags(self, pattern, cat, order, page):
        q = f"name:{pattern.replace('_', ' ')}" if pattern else "*"
        res = self.get("api/v1/json/search/tags", q=q, page=page, per_page=40,
                       sf={"Count": "images", "Name": "name", "Newest": "id"}[order], sd="asc" if order == "Name" else "desc") or {}
        rows = [{"name": t["name"].replace(" ", "_"), "count": t["images"], "cat": self.TAG_CAT.get(t.get("category"), "general")}
                for t in res.get("tags", [])]
        return [r for r in rows if not cat or r["cat"] == cat]

    def count(self, q):
        return (self.get("api/v1/json/search/images", q=self.query(q), per_page=1) or {}).get("total")

    def wiki(self, tag):
        slug = tag.replace("_", " ")
        for a, b in (("-", "-dash-"), ("/", "-fwslash-"), (":", "-colon-"), (".", "-dot-"), ("+", "-plus-"), (" ", "+")):
            slug = slug.replace(a, b)
        try:
            t = (self.get(f"api/v1/json/tags/{urllib.parse.quote(slug, safe='+')}") or {}).get("tag") or {}
        except RuntimeError:
            return None
        return {"body": dtext(t.get("description") or t.get("short_description")), "names": []}


ENGINES = {"danbooru": Danbooru, "gelbooru": Gelbooru, "moebooru": Moebooru, "e621": E621, "philomena": Philomena}
ENGINE_NAMES = {"danbooru": "Danbooru", "gelbooru": "Gelbooru 0.2", "moebooru": "Moebooru", "e621": "e621", "philomena": "Philomena"}


def engines(conf):
    """An engine per registry site, in registry order (= interleave order, first wiki site = metadata site)."""
    out = []
    for s in conf["boorus"]:
        e = ENGINES[s["engine"]](s, conf["limit"])
        LABELS[e.id], NAMES[e.id] = e.short, e.name
        out.append(e)
    return out


class BooruSource(Source):
    def __init__(self, engine, q, sort=""):
        self.engine, self.q, self.sort, self.label = engine, q, sort, engine.short

    def fetch(self, page):
        return self.engine.search(self.q, page, self.sort)


class PixivMatchSource(Source):
    """Posts of one Pixiv work on a booru (the "Find on boorus" bridge); a single page."""

    def __init__(self, engine, pid):
        self.engine, self.pid, self.label = engine, pid, engine.short

    def fetch(self, page):
        return self.engine.find_pixiv(self.pid) if page == 1 else []


# ---------- adding a site by URL ----------

PROBES = [("posts.json", "/posts.json?limit=1"), ("post.json", "/post.json?limit=1"),
          ("philomena", "/api/v1/json/search/images?q=*&per_page=1"), ("dapi", "/index.php?page=dapi&s=post&q=index&limit=1")]


def classify(kind, body):
    """Engine behind a probe reply (kind = the PROBES endpoint that answered), else None."""
    try:
        data = json.loads(body)
    except ValueError:
        data = None
    if kind == "posts.json":
        if isinstance(data, list) and (not data or "tag_string" in data[0] or "file_url" in data[0]):
            return "danbooru"
        if isinstance(data, dict) and "posts" in data:
            return "e621"
    if kind == "post.json" and isinstance(data, list) and (not data or "jpeg_url" in data[0] or "preview_url" in data[0]):
        return "moebooru"
    if kind == "philomena" and isinstance(data, dict) and "images" in data:
        return "philomena"
    if kind == "dapi" and (b"<posts" in body[:300] or isinstance(data, (list, dict)) or b"authentication" in body[:200].lower()):  # keyless rule34
        return "gelbooru"
    return None


def detect(url):
    """Which engine serves url (also trying its api. host, like rule34) -> (engine, normalized url, api_url)."""
    if "://" not in url:
        url = "https://" + url
    u = urllib.parse.urlparse(url)
    url = f"{u.scheme}://{u.netloc}"
    bases = [url] + ([f"{u.scheme}://api.{u.netloc}"] if not u.netloc.startswith("api.") else [])

    def probe(base, kind, path):
        try:
            return classify(kind, http(base + path, timeout=12))
        except urllib.error.HTTPError as e:
            return "gelbooru" if kind == "dapi" and e.code == 401 else None  # Gelbooru without a key
        except OSError:
            return None
    jobs = [(b, k, p) for b in bases for k, p in PROBES]
    with ThreadPoolExecutor(len(jobs)) as ex:
        found = list(ex.map(lambda j: probe(*j), jobs))
    for (base, _, _), eng in zip(jobs, found):  # first hit in priority order
        if eng:
            return eng, url, "" if base == url else base
    raise RuntimeError(f"{url}: no Danbooru / Gelbooru / Moebooru / e621 / Philomena API answered")


def new_site(conf, name, engine, url, api_url=""):
    """A registry entry with a unique id and a short label made from the name."""
    base = re.sub(r"\W+", "_", name.lower()).strip("_") or "site"
    ids = {s["id"] for s in conf["boorus"]} | {"pixiv"}
    sid = base if base not in ids else next(f"{base}{i}" for i in range(2, 999) if f"{base}{i}" not in ids)
    short = "".join(w[0] for w in re.findall(r"[A-Za-z0-9]+", name)).upper()[:3]
    if len(short) < 2:  # one word: its first letters
        short = re.sub(r"\W", "", name).upper()[:3] or sid[:3].upper()
    return {"id": sid, "name": name, "short": short if short not in LABELS.values() else sid[:4].upper(), "engine": engine,
            "url": url, "api_url": api_url, "login": "", "key": "", "filter_id": "", "on": True}
