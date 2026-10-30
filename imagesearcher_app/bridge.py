"""Opt-in links between the two sections: booru tags -> Pixiv words (from wiki other names), a Pixiv tag -> the booru
tag whose wiki lists it, and a booru artist -> their Pixiv account."""
import re

from .core import CJK, CONF_LOCK, KANA, PX_USER

# booru metatags (and Philomena's field:value); they have no Pixiv equivalent
META = re.compile(r"-?(rating|order|sort|score|favcount|id|pool|ordpool|user|fav|status|width|height|md5|source|pixiv"
                  r"|date|age|limit|is|has|parent|child|ratio|filetype|mpixels|tagcount|\w+\.(gt|gte|lt|lte)):")


def wiki_engine(engines):
    """The site that can name a tag in Japanese: first with batch wiki lookups (Danbooru), else first with other names (e621)."""
    return next((e for e in engines if e.has("wiki_names")), None) or next((e for e in engines if e.__class__.__name__ == "E621"), None)


def pick(names):
    """The Pixiv-style name among a wiki's other names: Japanese (kana) first, then any kanji/hangul one."""
    return next((n for n in sorted(names, key=lambda n: not KANA.search(n)) if CJK.search(n)), "")


def pixiv_names(conf, engine, tags, lookup=True):
    """booru tags -> {tag: Pixiv word}, cached in conf["pixiv_map"]. Tags without a known name fall back to the
    tag itself, underscores as spaces and any _(qualifier) dropped."""
    m = conf["pixiv_map"]
    todo = [t for t in dict.fromkeys(tags) if t not in m and not CJK.search(t)]
    if todo and lookup and engine:
        try:
            names = engine.wiki_names(todo) if engine.has("wiki_names") else {t: (engine.wiki(t) or {}).get("names", []) for t in todo}
            with CONF_LOCK:
                m.update({t: pick(names.get(t, [])) for t in todo})  # "" = no Pixiv name, don't ask again
        except (RuntimeError, OSError):
            pass  # site down: fall back for now, try again next time
    return {t: t if CJK.search(t) else m.get(t) or re.sub(r"_\([^)]*\)$", "", t).replace("_", " ") for t in tags}


def pixiv_word(conf, engine, q, lookup=True):
    """A booru query -> Pixiv search words. Exclusions and metatags are dropped; generic tags (1girl, solo) wreck a
    Pixiv search, so once any tag has a real Pixiv name only those are kept."""
    tags = [t for t in q.replace(",", " ").split() if not t.startswith("-") and not META.match(t)]
    words = list(pixiv_names(conf, engine, tags, lookup).values())
    return " ".join([w for w in words if CJK.search(w)] or words)


def booru_tag(engines, pixiv_tag):
    """The booru tag whose wiki lists pixiv_tag as an other name, else None."""
    e = next((e for e in engines if e.has("wiki_reverse")), None)
    return e.wiki_reverse(pixiv_tag) if e else None


def pixiv_uid(urls):
    """Pixiv user id among an artist's links, else None."""
    return next((m[1] for u in urls if (m := PX_USER.search(u))), None)
