"""Reader: what the wikis say about a tag or character. The Pixiv encyclopedia's summary (its full site is behind
Cloudflare, so that opens in your browser), the booru sites' wikis, and Fandom (a search across all wikis, articles
through each wiki's MediaWiki API). Articles are cleaned down to the HTML Qt's QTextBrowser can show."""
import html
import re
import urllib.parse
from html.parser import HTMLParser

from PySide6.QtCore import Qt
from PySide6.QtGui import QImage

from . import bridge
from .core import CJK, get_json, http, log

MAX_IMAGES, IMAGE_WIDTH = 8, 320
WIKI_CHARS = 20000  # a booru wiki page in full (the side panels only show an excerpt)
FANDOM_SEARCH = "https://services.fandom.com/unified-search/page-search"
PIXIV_TAGS = "https://www.pixiv.net/ajax/search/tags/"
DIC = "https://dic.pixiv.net/a/"


def names(conf, engines, q):
    """Any input -> {"tag": booru tag, "text": plain words, "pixiv": Pixiv word}. A Japanese name is mapped back to its
    booru tag through the wikis when one lists it; a booru tag gets its Japanese name from its wiki the same way."""
    q = q.strip()
    if CJK.search(q):
        try:
            tag = bridge.booru_tag(engines, q) or ""
        except (RuntimeError, OSError):
            tag = ""
        return {"tag": tag, "text": q, "pixiv": q}
    tag = q.lower().replace(" ", "_")
    return {"tag": tag, "text": re.sub(r"_\([^)]*\)$", "", tag).replace("_", " "),
            "pixiv": bridge.pixiv_names(conf, bridge.wiki_engine(engines), [tag])[tag]}


def plain(s):
    return html.unescape(re.sub(r"<[^>]+>", "", s or "")).strip()


# ---------- sources ----------

def pixiv_entry(word):
    """The Pixiv encyclopedia's summary of a tag, or None when it has no entry."""
    d = get_json(PIXIV_TAGS + urllib.parse.quote(word, safe="") + "?lang=en", "Pixiv", headers={"Accept": "application/json"})
    return parse_pixiv(d, word)


def parse_pixiv(d, word):
    b = (d or {}).get("body") or {}
    px = b.get("pixpedia") or {}
    if not px.get("abstract"):
        return None
    tr = (b.get("tagTranslation") or {}).get(word) or {}
    return {"source": "pixiv", "title": px.get("tag") or word, "abstract": px["abstract"], "image": px.get("image") or "",
            "reading": px.get("yomigana") or "", "translation": tr.get("en") or tr.get("romaji") or "",
            "siblings": px.get("siblingsTags") or [], "children": px.get("childrenTags") or [],
            "url": DIC + urllib.parse.quote(px.get("tag") or word, safe="")}


def booru_entry(engine, tag):
    """A booru site's wiki page, or None (no page, or the site is down)."""
    try:
        w = engine.wiki(tag, WIKI_CHARS)
    except (RuntimeError, OSError) as e:
        log.warning("reader: %s wiki: %s", engine.name, e)
        return None
    if not w or not (w.get("body") or w.get("names")):
        return None
    return {"source": "booru", "site": engine.name, "title": tag.replace("_", " "), "body": w.get("body") or "", "names": w.get("names") or [],
            "url": f"{engine.url}/wiki_pages/{urllib.parse.quote(tag, safe='')}"}


def fandom_search(text, limit=8):
    """Articles across all Fandom wikis -> [{"title", "wiki", "host", "page", "snippet", "url"}], best first."""
    url = FANDOM_SEARCH + "?" + urllib.parse.urlencode({"query": text, "lang": "ja" if CJK.search(text) else "en", "limit": limit, "namespace": 0})
    return parse_fandom((get_json(url, "Fandom") or {}).get("results") or [])


def parse_fandom(results):
    out = []
    for r in results:
        u = urllib.parse.urlsplit(r.get("url") or "")
        if "/wiki/" not in u.path:
            continue
        out.append({"source": "fandom", "title": r.get("title") or "", "wiki": r.get("sitename") or u.hostname, "host": u.hostname,
                    "page": urllib.parse.unquote(u.path.split("/wiki/", 1)[1]), "snippet": plain(r.get("content")), "url": r["url"]})
    return out


def fandom_article(host, page):
    """-> {"title", "html", "images", "url"} of one article, or None if the wiki has no such page."""
    url = f"https://{host}/api.php?" + urllib.parse.urlencode({"action": "parse", "page": page, "prop": "text|displaytitle", "redirects": 1,
                                                               "format": "json", "formatversion": 2})
    d = get_json(url, "Fandom") or {}
    p = d.get("parse")
    if not p:
        log.warning("reader: %s has no page %s (%s)", host, page, (d.get("error") or {}).get("info", ""))
        return None
    body, images = clean_html(p.get("text") or "", host)
    return {"title": plain(p.get("displaytitle")) or page.replace("_", " "), "html": body, "images": images,
            "url": f"https://{host}/wiki/{urllib.parse.quote(page)}"}


# ---------- Fandom HTML -> what QTextBrowser shows ----------

DROP_TAGS = {"script", "style", "noscript", "svg", "button", "input", "form", "iframe"}
DROP_CLASSES = {"mw-editsection", "toc", "navbox", "reference", "mw-references-wrap", "wds-tabs", "mw-empty-elt", "printfooter",
                "noprint", "article-media-placeholder", "gallerytext"}
RENAME = {"aside": "div", "section": "div", "figure": "div", "figcaption": "div"}
VOID = {"img", "br", "hr", "wbr", "col", "source", "meta", "link", "area", "embed", "track"}
KEEP_ATTRS = {"a": ("href",), "td": ("colspan", "rowspan"), "th": ("colspan", "rowspan")}


class Cleaner(HTMLParser):
    def __init__(self, host):
        super().__init__(convert_charrefs=False)
        self.host, self.out, self.images, self.skip = host, [], [], 0

    def link(self, href):
        """/wiki/Page (or a full link to this wiki) stays inside the Reader; everything else leaves it."""
        u = urllib.parse.urlsplit(href)
        if u.path.startswith("/wiki/") and (not u.netloc or u.netloc == self.host) and ":" not in u.path[6:]:
            return f"wiki://{self.host}/{u.path[6:]}"
        return href if u.netloc or u.scheme else f"https://{self.host}{href}" if href.startswith("/") else href

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if self.skip:
            self.skip += tag not in VOID
            return
        if tag in DROP_TAGS or DROP_CLASSES & set((a.get("class") or "").split()):
            self.skip = int(tag not in VOID)
            return
        if tag == "img":
            src = a.get("data-src") or a.get("src") or ""
            if src.startswith("http") and len(self.images) < MAX_IMAGES and src not in self.images:
                self.images.append(src)
                self.out.append(f'<img src="{html.escape(src, quote=True)}">')
            return
        keep = "".join(f' {k}="{html.escape(self.link(a[k]) if k == "href" else a[k], quote=True)}"' for k in KEEP_ATTRS.get(tag, ()) if a.get(k))
        self.out.append(f"<{RENAME.get(tag, tag)}{keep}>")

    def handle_endtag(self, tag):
        if self.skip:
            self.skip -= tag not in VOID
        elif tag not in VOID:
            self.out.append(f"</{RENAME.get(tag, tag)}>")

    def handle_data(self, data):
        if not self.skip:
            self.out.append(data)

    def handle_entityref(self, name):
        self.handle_data(f"&{name};")

    def handle_charref(self, name):
        self.handle_data(f"&#{name};")


def clean_html(body, host):
    """Fandom article HTML -> (html for QTextBrowser, the first MAX_IMAGES image URLs). Links to this wiki become
    wiki://host/Page, so they open inside the Reader."""
    c = Cleaner(host)
    c.feed(body)
    c.close()
    return "".join(c.out), c.images


# ---------- showing entries ----------

def tag_links(tags):
    # read:///tag: in the path, because a URL's host part would be lower-cased and punycoded
    return " · ".join(f"<a href='read:///{urllib.parse.quote(t, safe='')}'>{html.escape(t)}</a>" for t in tags)


def pixiv_html(e):
    img = f'<p><img src="{html.escape(e["image"], quote=True)}"></p>' if e["image"] else ""
    head = " · ".join(x for x in (e["reading"], e["translation"]) if x)
    return (f"<h2>{html.escape(e['title'])}</h2><p><small>Pixiv encyclopedia{' · ' + html.escape(head) if head else ''}</small></p>{img}"
            f"<p>{html.escape(e['abstract'])}</p>"
            + (f"<p><b>Related:</b> {tag_links(e['siblings'])}</p>" if e["siblings"] else "")
            + (f"<p><b>More specific:</b> {tag_links(e['children'])}</p>" if e["children"] else ""))


def booru_html(e):
    body = "".join(f"<p>{html.escape(p)}</p>" for p in re.split(r"\n\s*\n", e["body"]) if p.strip())
    return (f"<h2>{html.escape(e['title'])}</h2><p><small>{html.escape(e['site'])} wiki</small></p>"
            + (f"<p><b>Other names:</b> {tag_links(e['names'])}</p>" if e["names"] else "") + body)


def load_images(urls):
    """Download images for an article -> ({url: QImage}, the first failure's text); at most IMAGE_WIDTH wide."""
    out, why = {}, ""
    for u in urls:
        try:
            img = QImage.fromData(http(u))
        except OSError as e:
            why = why or str(getattr(e, "reason", e))
            if "Cloudflare" in why:  # the whole image host: the others would only fail the same way
                break
            continue
        if not img.isNull():
            out[u] = img.scaledToWidth(IMAGE_WIDTH, Qt.SmoothTransformation) if img.width() > IMAGE_WIDTH else img
    return out, why


def drop_images(body, keep):
    """Remove <img> tags whose image didn't load, instead of leaving broken-image boxes."""
    return re.sub(r'<p><img src="([^"]*)"></p>|<img src="([^"]*)">',
                  lambda m: m[0] if html.unescape(m[1] or m[2]) in keep else "", body)
