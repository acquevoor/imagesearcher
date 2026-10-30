"""Offline self-check (`imagesearcher.py --selftest`, also the Nix flake check): parsers fed with trimmed real replies."""
import json
import logging
import tempfile
import time
from pathlib import Path

from . import boorus, bridge, core, pixiv
from .booru_ui import follow_srcs
from .core import CACHE_MARK, cached, clear_cache, dest, fold, interleave, local, move_favorite, preset, rating_ok, allowed


def eng(sid, **over):
    return boorus.ENGINES[preset(sid)["engine"]]({**preset(sid), **over}, 5)


def selftest():
    # --- engines, each fed a trimmed real reply ---
    db = eng("danbooru")
    d = [db.parse(p) for p in [
        {"id": 1, "file_url": "https://cdn.donmai.us/original/a.png", "large_file_url": "https://cdn.donmai.us/sample/a.jpg",
         "tag_string": "1girl solo", "rating": "s", "image_width": 10, "image_height": 20, "md5": "aa", "pixiv_id": 7,
         "tag_string_artist": "foo", "tag_string_general": "1girl solo",
         "media_asset": {"variants": [{"type": "360x360", "url": "https://cdn.donmai.us/360/a.jpg"}]}},
        {"id": 2, "tag_string": "gold_only"}]]  # no file -> skipped
    assert d[1] is None and d[0]["thumb"].endswith("360/a.jpg") and d[0]["samples"][0].endswith("sample/a.jpg")
    assert d[0]["rating"] == "s" and d[0]["pixiv_id"] == "7" and d[0]["cats"]["artist"] == ["foo"] and d[0]["site"] == "danbooru"
    assert d[0]["page"] == "https://danbooru.donmai.us/posts/1" and eng("aibooru").parse({**{"id": 3, "file_url": "x.png"}})["page"].startswith("https://aibooru")

    sb = eng("safebooru")
    g = sb.parse({"id": 5, "file_url": "https://safebooru.org/images/x.webm", "preview_url": "p.jpg", "sample_url": "s.jpg", "sample": "True",
                  "tags": " a  b ", "rating": "general", "hash": "bb", "source": "https://www.pixiv.net/en/artworks/9", "width": 3, "height": 4})
    assert g["video"] and g["rating"] == "g" and g["samples"] == ["s.jpg"] and g["tags"] == ["a", "b"] and g["md5"] == "bb" and g["pixiv_id"] == "9"
    assert g["page"] == "https://safebooru.org/index.php?page=post&s=view&id=5"
    r34 = eng("rule34")
    assert r34.api == "https://api.rule34.xxx" and r34.parse({"id": 1, "file_url": "f.jpg"})["page"].startswith("https://rule34.xxx/")
    assert r34.parse({"id": 1, "file_url": "f.jpg", "rating": "sensitive"})["rating"] == "s"
    assert boorus.parse_gel_tags("x", b'<tags><tag type="4" count="3" name="miku" id="1"/></tags>') == [{"type": "4", "count": "3", "name": "miku", "id": "1"}]
    assert boorus.parse_gel_tags("x", b'{"@attributes":{}, "tag":[{"name":"a","count":1,"type":0}]}')[0]["name"] == "a"

    kc = eng("konachan")
    m = kc.parse({"id": 410185, "tags": "hano_1219 hatsune_miku", "source": "https://www.pixiv.net/en/artworks/151425011", "md5": "86f3",
                  "file_url": "https://konachan.net/image/86f3/K.jpg", "preview_url": "https://konachan.net/data/preview/86/f3/86f3.jpg",
                  "sample_url": "https://konachan.net/sample/86f3/K.jpg", "rating": "s", "width": 1500, "height": 839})
    assert m["rating"] == "g" and m["pixiv_id"] == "151425011" and m["page"] == "https://konachan.net/post/show/410185" and m["md5"] == "86f3"
    assert kc.CATS[5] == "artist" and kc.join("a", kc.id_after(5)) == "a id:>5"

    e9 = eng("e926")
    e = e9.parse({"id": 6812963, "rating": "s", "sources": ["https://x.com/a", "https://i.pximg.net/img-original/img/2026/01/01/00/00/00/123_p0.png"],
                  "file": {"width": 1400, "height": 1500, "ext": "png", "md5": "cc", "url": "https://static1.e926.net/data/cc.png"},
                  "preview": {"url": "https://static1.e926.net/data/preview/cc.jpg"}, "sample": {"has": True, "url": "https://static1.e926.net/data/sample/cc.jpg"},
                  "tags": {"general": ["anthro"], "artist": ["vovie"], "species": ["cat"], "lore": ["l"], "invalid": [], "meta": ["hi_res"]}})
    assert e["rating"] == "g" and e["pixiv_id"] == "123" and e["md5"] == "cc" and e["cats"]["species"] == ["cat"] and e["cats"]["general"] == ["anthro", "l"]
    assert e9.parse({"id": 1, "file": {"url": None}}) is None  # hidden from anonymous users
    assert "by anonymous on e621" in e9.headers()["User-Agent"]

    dp = eng("derpibooru")
    assert dp.query("twilight_sparkle -rarity safe") == "twilight sparkle, -rarity, safe" and dp.query("a b, c") == "a b, c" and dp.query("") == "*"
    assert dp.join("twilight_sparkle", dp.id_after(9)) == "twilight_sparkle, id.gt:9"
    p = dp.parse({"id": 3922492, "tags": ["safe", "artist:wing regent", "twilight sparkle"], "width": 9111, "height": 2,
                  "representations": {"full": "https://derpicdn.net/img/view/a.png", "large": "https://derpicdn.net/img/large.png", "thumb": "https://derpicdn.net/t.png"},
                  "source_url": "https://www.pixiv.net/artworks/55", "processed": True})
    assert p["rating"] == "g" and p["tags"] == ["safe", "artist:wing_regent", "twilight_sparkle"] and p["cats"]["artist"] == ["artist:wing_regent"]
    assert p["cats"]["general"] == ["twilight_sparkle"] and p["pixiv_id"] == "55" and p["samples"] == ["https://derpicdn.net/img/large.png"]
    assert dp.parse({"id": 1, "hidden_from_users": True}) is None

    for x in (db, sb, kc, e9, dp):  # capabilities the UI relies on
        assert x.has("search") and x.has("complete") and x.has("tags") and x.has("count")
    assert db.has("wiki_names") and db.has("pools") and not sb.has("wiki") and not dp.has("pools") and kc.has("find_pixiv")

    # --- adding a site: detection from probe replies, unique ids ---
    assert boorus.classify("posts.json", b'[{"id":1,"tag_string":"a"}]') == "danbooru"
    assert boorus.classify("posts.json", b'{"posts":[]}') == "e621"
    assert boorus.classify("post.json", b'[{"id":1,"jpeg_url":"x"}]') == "moebooru"
    assert boorus.classify("philomena", b'{"total":1,"images":[]}') == "philomena"
    assert boorus.classify("dapi", b'<?xml version="1.0"?><posts count="1">') == "gelbooru"
    assert boorus.classify("dapi", b'"Missing authentication. Go to api.rule34.xxx"') == "gelbooru"
    assert boorus.classify("posts.json", b"<html>404</html>") is None
    s = boorus.new_site({"boorus": [{"id": "my_booru"}]}, "My Booru", "danbooru", "https://my.booru")
    assert s["id"] == "my_booru2" and s["short"] == "MB" and s["on"]
    assert boorus.new_site({"boorus": []}, "Konachan", "moebooru", "https://k")["short"] == "KON"
    assert boorus.dtext("h4. Hi\n[[hatsune_miku|Miku]] and [[vocaloid]] [b]x[/b] thumb #12 >>9021s") == "Hi\nMiku and vocaloid x"

    # --- config v1 -> v2 ---
    v1 = {"danbooru": {"login": "me", "api_key": "k"}, "gelbooru": {"user_id": "5", "api_key": "g"}, "rule34": {"user_id": "", "api_key": ""},
          "pixiv": {"refresh_token": "t"}, "sites": ["danbooru", "rule34", "pixiv"], "favorites": [{"site": "rule34", "id": "1"}],
          "follows": {"artist:x": {"uid": "3", "pixiv": 100, "danbooru": 7}}}
    v2 = core.migrate(json.loads(json.dumps(v1)))
    by = {s["id"]: s for s in v2["boorus"]}
    assert v2["version"] == 2 and list(by) == ["danbooru", "gelbooru", "rule34"] and "sites" not in v2 and "danbooru" not in v2
    assert by["danbooru"]["login"] == "me" and by["danbooru"]["key"] == "k" and by["gelbooru"]["login"] == "5" and by["rule34"]["api_url"]
    assert by["danbooru"]["on"] and not by["gelbooru"]["on"] and by["rule34"]["on"]
    assert v2["follows"] == {"artist:x": {"danbooru": 7}} and v2["pixiv"]["refresh_token"] == "t" and v2["favorites"][0]["site"] == "rule34"
    assert core.migrate(v2) is v2

    # --- feeds: interleave, blacklist, ratings, cross-site dedupe by md5 ---
    assert [x["id"] for x in interleave([[d[0]], [m, g], [e]])] == ["1", "410185", "6812963", "5"]
    assert not allowed(d[0], {"1girl"}) and not allowed(d[0], {"rating:s"}) and allowed(d[0], {"2girls"})
    assert rating_ok(m, {"g"}) and not rating_ok(d[0], {"g"}) and rating_ok({"rating": ""}, {"e"})
    seen = {}
    a, new = fold(seen, d[0])
    copy = {**d[0], "site": "gelbooru", "id": "9"}
    assert new and fold(seen, copy) == (a, False) and a[0]["alts"][0]["site"] == "gelbooru"
    assert fold(seen, copy) is None and fold(seen, {**d[0], "id": "2"}) is None  # same site again / already riding along
    assert fold(seen, {**m, "md5": ""})[1] is True  # no md5: never merged
    srcs = follow_srcs({"tag:miku": {"danbooru": 5, "derpibooru": 9, "gone": 1}}, [db, dp])
    assert sorted((s.engine.id, s.q) for s in srcs) == [("danbooru", "miku id:>5"), ("derpibooru", "miku, id.gt:9")]

    # --- Pixiv ---
    px = pixiv.parse({"illusts": [
        {"id": 7, "x_restrict": 1, "type": "manga", "title": "T", "total_bookmarks": 500, "illust_ai_type": 2, "user": {"id": 3, "name": "U"},
         "image_urls": {"square_medium": "sq", "large": "lg"}, "meta_single_page": {}, "series": {"id": 4, "title": "S"},
         "meta_pages": [{"image_urls": {"original": "https://i.pximg.net/o_p0.png", "large": "l0"}},
                        {"image_urls": {"original": "https://i.pximg.net/o_p1.png", "large": "l1"}}],
         "tags": [{"name": "オリジナル", "translated_name": "original"}, {"name": "女の子"}]},
        {"id": 8, "x_restrict": 0, "type": "ugoira", "image_urls": {"square_medium": "sq", "large": "lg"}, "total_bookmarks": 5,
         "meta_single_page": {"original_image_url": "https://i.pximg.net/o.jpg"}, "meta_pages": [], "tags": []}]})
    assert len(px[0]["files"]) == 2 and px[0]["samples"] == ["l0", "l1"] and px[0]["rating"] == "e" and px[0]["ai"] and px[0]["user"] == "3"
    assert px[0]["tags"] == ["オリジナル", "original", "女の子"] and px[0]["series"]["id"] == 4 and px[1]["ugoira"] and px[0]["bookmarks"] == 500
    assert pixiv.keep_filter() is None
    keep = pixiv.keep_filter(r18="Hide", ai=False, min_bm=10, kind="All")
    assert not keep(px[0]) and not keep(px[1])  # px0: R-18 + AI, px1: 5 bookmarks
    assert pixiv.keep_filter(r18="Only")(px[0]) and not pixiv.keep_filter(r18="Only")(px[1])
    assert pixiv.keep_filter(kind="Manga")(px[0]) and not pixiv.keep_filter(kind="Illust")(px[0])
    path, params, note = pixiv.search_params("初音ミク", "Exact tags", "Popular", "2026-01-01", "", 100, False, premium=False)
    assert path == "v1/search/popular-preview/illust" and params["sort"] is None and note
    assert params["search_target"] == "exact_match_for_tags" and params["start_date"] == "2026-01-01" and params["end_date"] is None
    assert params["bookmark_num_min"] == 100 and params["search_ai_type"] == 0
    path, params, note = pixiv.search_params("x", sort="Popular", premium=True)
    assert path == "v1/search/illust" and params["sort"] == "popular_desc" and not note and params["search_ai_type"] is None

    class Paged(pixiv.PixivSource):  # next_url paging, and looking past pages a filter empties
        pages = {"first": {"illusts": [], "next_url": "p2"}, "p2": {"illusts": [{**{"id": 9, "x_restrict": 0, "image_urls": {"square_medium": "s", "large": "l"},
                 "meta_single_page": {"original_image_url": "o.jpg"}, "meta_pages": [], "tags": []}}], "next_url": None}}

    orig = pixiv.api
    pixiv.api = lambda conf, url, **kw: Paged.pages["first" if url.startswith(pixiv.API) else url]
    try:
        src = Paged({"pixiv": {"user_id": "1"}}, "v1/x", user_id="me")
        assert [x["id"] for x in src.fetch(1)] == ["9"] and src.fetch(2) == []
    finally:
        pixiv.api = orig

    verifier, url = pixiv.login_start()  # PKCE: the URL carries sha256(verifier), never the verifier
    import base64, hashlib, urllib.parse  # noqa: E401
    sent = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)["code_challenge"][0]
    assert sent == base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode() and verifier not in url
    for pasted in ("https://app-api.pixiv.net/web/v1/users/auth/pixiv/callback?state=s&code=AbC-1_x", "pixiv://account/login?code=AbC-1_x&via=login", " AbC-1_x "):
        assert pixiv.login_code(pasted) == "AbC-1_x", pasted

    # --- bridge: booru tags -> Pixiv words (offline: cache pre-filled, lookup off) ---
    conf = {"pixiv_map": {"hatsune_miku": "初音ミク", "1girl": ""}}
    assert bridge.pixiv_word(conf, None, "hatsune_miku 1girl -solo rating:g order:score re:zero_x", False) == "初音ミク"
    assert bridge.pixiv_word(conf, None, "1girl blue_hair_(x) rating:e", False) == "1girl blue hair"
    assert bridge.pixiv_word(conf, None, "twilight_sparkle, id.gt:5", False) == "twilight sparkle"
    assert bridge.pick(["长发", "長髪", "ロングヘア"]) == "ロングヘア" and bridge.pick(["long", "長髪"]) == "長髪" and bridge.pick(["x"]) == ""
    assert bridge.pixiv_uid(["https://x.com/a", "https://www.pixiv.net/users/1039353"]) == "1039353"
    assert bridge.pixiv_uid(["http://www.pixiv.net/member.php?id=55"]) == "55" and not bridge.pixiv_uid(["https://sketch.pixiv.net/@igix"])
    assert bridge.wiki_engine([sb, e9, db]) is db and bridge.wiki_engine([sb, e9]) is e9 and bridge.wiki_engine([sb]) is None

    # --- files: download paths, cache -> favorites -> cache -> cleared ---
    assert dest("/d", px[0], 1, "blue/hair *") == Path("/d/pixiv/blue_hair _/pixiv_7_p1.png")
    assert dest("/d", d[0], 0, "") == Path("/d/danbooru/_all/danbooru_1.png")
    with tempfile.TemporaryDirectory() as root:
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
        h = core.DailyHistory(tmp)
        core.log.setLevel(logging.INFO)
        core.log.addHandler(h)
        core.log.info("one")
        core.log.info("two")
        core.log.removeHandler(h)
        core.log.info("not saved")
        lines = (Path(tmp) / f"{time.strftime('%Y-%m-%d')}.txt").read_text().splitlines()
        assert [ln.split(" ", 2)[2] for ln in lines] == ["one", "two"], lines  # same day appends, toggle off stops
    print("selftest ok")
