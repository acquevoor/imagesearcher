"""Offline self-check (`imagesearcher.py --selftest`, also the Nix flake check): parsers fed with trimmed real replies."""
import hashlib
import io
import json
import logging
import tempfile
import threading
import time
import urllib.error
from pathlib import Path

from . import boorus, bridge, core, imageboard as ib, keys, pawchive as pw, pixiv, x
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

    # --- key fetcher ---
    page = "API Access Credentials … <p>&amp;api_key=" + "a1" * 20 + "&amp;user_id=9455</p>"
    assert keys.parse_creds(page) == ("9455", "a1" * 20) and keys.parse_creds("&api_key=&user_id=9455") is None and keys.parse_creds(None) is None
    kc = core.migrate(json.loads(json.dumps(v1)))
    kc["boorus"] = [{**core.SITE_FIELDS, **s} for s in kc["boorus"]]
    assert keys.missing(kc) == ["rule34"]  # gelbooru has user id 5 + key g; pixiv has token t
    kc["boorus"] = [s for s in kc["boorus"] if s["id"] != "rule34"]
    kc["pixiv"]["refresh_token"] = ""
    assert keys.missing(kc) == ["rule34", "pixiv"]
    saved = []
    orig_save = keys.save_conf
    keys.save_conf = saved.append
    try:
        keys.store(kc, "rule34", ("77", "f" * 40))
        keys.store(kc, "pixiv", "tok")
    finally:
        keys.save_conf = orig_save
    r = keys.site(kc, "rule34")
    assert r["on"] and r["login"] == "77" and r["api_url"] == "https://api.rule34.xxx" and kc["pixiv"]["refresh_token"] == "tok"
    assert keys.missing(kc) == [] and len(saved) == 2

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
    xtest()
    ibtest()
    pawtest()
    print("selftest ok")


def xtest():
    """X: reply walking (photos, video, retweet, quoted, visibility wrapper, cursors), main.js query ids, error
    classes, the budget with a fake clock, pause bounds, ring-2 ranking, rotation and a batch check."""
    def user(uid, handle):
        return {"__typename": "User", "rest_id": uid, "core": {"screen_name": handle, "name": handle.title()}, "legacy": {}}

    def tweet(tid, uid, handle, media=(), rt=None, quoted=None, sensitive=False, wrap=False):
        lg = {"id_str": tid, "full_text": "hi #art https://t.co/abc", "possibly_sensitive": sensitive,
              "entities": {"hashtags": [{"text": "art"}]}}
        if media:
            lg["extended_entities"] = {"media": list(media)}
        if rt:
            lg["retweeted_status_result"] = {"result": rt}
        t = {"__typename": "Tweet", "rest_id": tid, "core": {"user_results": {"result": user(uid, handle)}}, "legacy": lg}
        if quoted:
            t["quoted_status_result"] = {"result": quoted}
        return {"__typename": "TweetWithVisibilityResults", "tweet": t} if wrap else t

    photo = {"type": "photo", "media_url_https": "https://pbs.twimg.com/media/AAA.jpg", "original_info": {"width": 800, "height": 600}}
    video = {"type": "video", "media_url_https": "https://pbs.twimg.com/ext_tw_video_thumb/1/pu/img/V.jpg",
             "video_info": {"variants": [{"content_type": "application/x-mpegURL", "url": "m3u8"},
                                         {"content_type": "video/mp4", "bitrate": 256000, "url": "https://video.twimg.com/lo.mp4?tag=1"},
                                         {"content_type": "video/mp4", "bitrate": 2176000, "url": "https://video.twimg.com/hi.mp4?tag=1"}]}}
    entry = lambda t: {"content": {"itemContent": {"itemType": "TimelineTweet", "tweet_results": {"result": t}}}}  # noqa: E731
    reply = {"data": {"user": {"result": {"timeline": {"timeline": {"instructions": [{"type": "TimelineAddEntries", "entries": [
        entry(tweet("105", "1", "artist", [photo], sensitive=True)),
        entry(tweet("104", "1", "artist", [video], wrap=True)),
        entry(tweet("103", "1", "artist", rt=tweet("50", "9", "other", [photo]))),  # retweet: credited to the other artist
        entry(tweet("102", "1", "artist", quoted=tweet("40", "8", "quoted", [photo]))),  # text-only quote: nothing
        entry(tweet("101", "1", "artist")),  # text only
        {"content": {"cursorType": "Bottom", "value": "CUR2"}}]}]}}}}}}
    posts, cursor, newest = x.tweets(reply, by="artist")
    assert [p["id"] for p in posts] == ["105", "104", "50"] and cursor == "CUR2" and newest == 105, (posts, cursor, newest)
    p, v, rt = posts
    assert p["files"] == ["https://pbs.twimg.com/media/AAA.jpg?name=orig"] and p["samples"] == ["https://pbs.twimg.com/media/AAA.jpg?name=large"]
    assert p["rating"] == "e" and p["tags"] == ["art", "user:artist"] and p["title"] == "hi #art" and (p["w"], p["h"]) == (800, 600)
    assert p["page"] == "https://x.com/artist/status/105" and dest("/d", p, 0, "x").name == "x_105.jpg"
    assert v["video"] and v["files"] == ["https://video.twimg.com/hi.mp4?tag=1"] and v["rating"] == "g"
    assert rt["handle"] == "other" and rt["rt_by"] == "artist" and not p.get("rt_by")

    fol = {"data": {"user": {"result": {"timeline": {"timeline": {"instructions": [{"entries": [
        {"content": {"itemContent": {"itemType": "TimelineUser", "user_results": {"result": user("7", "seven")}}}},
        {"content": {"itemContent": {"itemType": "TimelineUser", "user_results": {"result": user("8", "eight")}}}},
        {"content": {"cursorType": "Top", "value": "TOP"}}, {"content": {"cursorType": "Bottom", "value": "B1"}}]}]}}}}}}
    us, cur = x.users(fol)
    assert [u["handle"] for u in us] == ["seven", "eight"] and cur == "B1"

    js = ('e.exports={queryId:"q1",operationName:"UserMedia",operationType:"query",metadata:{featureSwitches:["f_a","f_b"],'
          'fieldToggles:["t_a"]}};e.exports={queryId:"q2",operationName:"Following",operationType:"query",metadata:'
          '{featureSwitches:["f_c"]}};e.exports={queryId:"q3",operationName:"CreateTweet",operationType:"mutation",metadata:{featureSwitches:[]}}')
    o = x.parse_ops(js)
    assert set(o) == {"UserMedia", "Following"} and o["UserMedia"] == {"qid": "q1", "features": {"f_a": True, "f_b": True}, "toggles": {"t_a": False}}
    assert o["Following"]["toggles"] == {}

    assert x.error_kind(429, b"")[0] == "pause"
    assert x.error_kind(403, b'{"errors":[{"code":326,"message":"account is temporarily locked"}]}')[0] == "lock"
    assert x.error_kind(200, b'{"errors":[{"code":88,"message":"Rate limit exceeded"}]}')[0] == "pause"
    assert x.error_kind(401, b"")[0] == "auth" and x.error_kind(404, b"")[0] == "txid"
    assert x.error_kind(400, b'{"errors":[{"code":336,"message":"The following features cannot be null: a, b"}]}') == \
        ("other", "The following features cannot be null: a, b")

    now = [1000.0]
    slept = []
    b = x.Budget(clock=lambda: now[0], sleep=lambda s: (slept.append(s), now.__setitem__(0, now[0] + s)), rand=lambda: 0.5)
    for _ in range(x.HOURLY):
        b.wait()
    assert slept[0] == x.GAP + x.JITTER / 2 or b.stamps[0] == 1000.0  # first one goes at once
    assert all(abs(s - (x.GAP + x.JITTER / 2)) < 1e-9 for s in slept) and b.left() == 0
    first = b.stamps[0]
    b.wait()  # 151st within the hour waits for the oldest to age out
    assert now[0] >= first + 3600, (now[0], first)

    conf = {"x": {**core.DEFAULTS["x"], "pause_min": 9999, "pause_random": True}}
    assert x.pause_minutes(conf, rand=lambda: 1.0) == x.MAX_PAUSE_MIN
    conf["x"]["pause_min"] = 120
    assert x.pause_minutes(conf, rand=lambda: 0.0) == 90 and x.pause_minutes(conf, rand=lambda: 1.0) == 150
    conf["x"]["pause_random"] = False
    assert x.pause_minutes(conf) == 120
    saved, x.save_conf = x.save_conf, lambda c: None
    try:
        x.pause_for(conf, "429")
        assert 0 < conf["x"]["paused_until"] - time.time() <= 120 * 60 + 1 and "paused until" in x.paused(conf)
        x.pause_for(conf, "locked", locked=True)
        assert conf["x"]["locked"] and "Resume" in x.paused(conf)
        conf["x"].update(enabled=True, auth_token="a", ct0="c", seed_handle="me")
        try:
            x.check(conf)
            raise AssertionError("check() must refuse while paused")
        except x.Paused:
            pass
        x.resume(conf)
        x.check(conf)
    finally:
        x.save_conf = saved

    counts = {"a": 5, "b": 3, "c": 2, "me": 9, "f1": 7, "d": 3}
    users_ = {k: {"id": k, "handle": k, "name": k} for k in counts}
    r2 = x.rank_ring2(counts, users_, exclude={"me", "f1"}, top=2)
    assert [(u["id"], u["n"]) for u in r2] == [("a", 5), ("b", 3)]
    accts = [{"id": i, "handle": i} for i in "abcd"]
    assert [a["id"] for a in x.due(accts, {"a": 5, "b": 1, "d": 3}, 3)] == ["c", "b", "d"]

    with tempfile.TemporaryDirectory() as tmp:
        orig = x.STATE_PATH, x.user_tweets
        x.STATE_PATH = Path(tmp) / "x_state.json"
        x.user_tweets = lambda conf, state, uid, handle="": ([q for q in posts if q["user"] == uid or q.get("rt_by") == handle], 105)
        try:
            st = x.load_state()
            seen = []
            assert x.check_batch(conf, st, [{"id": "1", "handle": "artist"}], 1, got=seen.extend) == 3
            assert [q["ring"] for q in st["posts"]] == [1, 1, 1] and st["newest"]["1"] == "105" and len(seen) == 3
            assert x.check_batch(conf, st, [{"id": "1", "handle": "artist"}], 1) == 0  # nothing new the second time
            assert x.load_state()["checked"]["1"] and oct(x.STATE_PATH.stat().st_mode)[-3:] == "600"
        finally:
            x.STATE_PATH, x.user_tweets = orig
    xtest2()


def xtest2():
    """X login by pasted cookies (JDownloader / Flag Cookies style), both paces, the jd 429 wait-and-retry,
    and a resumable bulk run (lock mid-run, continue without repeating pages)."""
    flag = json.dumps([{"domain": ".x.com", "name": "auth_token", "value": "AT", "httpOnly": True},
                       {"domain": ".x.com", "name": "ct0", "value": "C0"},
                       {"domain": ".x.com", "name": "twid", "value": "u%3D4242"},
                       {"domain": ".google.com", "name": "ct0", "value": "WRONG"}])
    assert x.parse_cookies("x.com cookies:\n" + flag) == {"auth_token": "AT", "ct0": "C0", "twid": "u%3D4242"}
    assert x.parse_cookies(json.dumps({"url": "https://x.com", "cookies": json.loads(flag)}))["ct0"] == "C0"
    netscape = "# Netscape HTTP Cookie File\n#HttpOnly_.x.com\tTRUE\t/\tTRUE\t0\tauth_token\tAT\n.x.com\tTRUE\t/\tTRUE\t0\tct0\tC0\n"
    assert x.parse_cookies(netscape) == {"auth_token": "AT", "ct0": "C0"}
    assert x.parse_cookies("Cookie: guest_id=v1; auth_token=AT; ct0=C0") == {"guest_id": "v1", "auth_token": "AT", "ct0": "C0"}
    conf = {"x": {**core.DEFAULTS["x"], "enabled": True}}
    assert x.cookie_login(conf, flag) == "auth_token, ct0, alt account id 4242" and conf["x"]["alt_id"] == "4242"
    assert conf["x"]["auth_token"] == "AT" and x.ready(conf)  # logged in without a main @handle: bulk works
    try:
        x.cookie_login(conf, "guest_id=1; personalization_id=2")
        raise AssertionError("must name what it found")
    except RuntimeError as e:
        assert "auth_token / ct0" in str(e) and "guest_id" in str(e)
    try:
        x.seed(conf, {"seed": {}, "follows": []})
        raise AssertionError("seed needs a main handle")
    except RuntimeError as e:
        assert str(e) == x.NO_SEED

    # jd pace: ~1 s gaps, no hourly cap, waits for an exhausted operation's reset
    now, slept = [1000.0], []
    b = x.Budget(clock=lambda: now[0], sleep=lambda t: (slept.append(t), now.__setitem__(0, now[0] + t)), rand=lambda: 0.0)
    for _ in range(x.HOURLY + 10):
        b.wait(pace="jd", op="UserMedia")
    assert all(abs(t - x.JD_GAP) < 1e-9 for t in slept) and len(slept) == x.HOURLY + 9
    b.note("UserMedia", {"x-rate-limit-remaining": "1", "x-rate-limit-reset": str(int(now[0]) + 600)})
    b.wait(pace="jd", op="UserTweets")  # other operations are not held up
    assert slept[-1] == x.JD_GAP
    b.wait(pace="jd", op="UserMedia")
    assert now[0] >= b.limits["UserMedia"][1] + 2 and "JDownloader pace" in b.status("jd")
    b.note("UserMedia", {"x-rate-limit-remaining": "499", "x-rate-limit-reset": str(int(now[0]) + 900)})
    assert "UserMedia 499 left" in b.status("jd")

    # jd 429: wait until reset, retry once; a second 429 pauses. Careful 429: pause at once.
    import io, urllib.error  # noqa: E401
    calls = []

    class Reply(io.BytesIO):
        headers = {"x-rate-limit-remaining": "300", "x-rate-limit-reset": "0"}

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_open(seq):
        def op(url, headers=None, timeout=30):
            calls.append(url)
            item = seq.pop(0)
            if item == 429:
                raise urllib.error.HTTPError(url, 429, "Too Many Requests", {"x-rate-limit-reset": str(int(time.time()) + 1)},
                                             io.BytesIO(b'{"errors":[{"code":88,"message":"Rate limit exceeded"}]}'))
            return Reply(item)
        return op
    saved = x.open_url, x.budget, x.save_conf
    x.budget = x.Budget(sleep=lambda t: None, rand=lambda: 0.0)
    x.save_conf = lambda c: None
    try:
        conf["x"].update(pace="jd", paused_until=0, locked=False)
        x.open_url = fake_open([429, b'{"data": {"ok": 1}}'])
        assert x.request(conf, "https://x.com/i/api/graphql/q/UserMedia?v", "UserMedia") == {"data": {"ok": 1}} and len(calls) == 2
        assert not x.paused(conf)
        x.open_url = fake_open([429, 429])
        try:
            x.request(conf, "https://x.com/i/api/graphql/q/UserMedia?v", "UserMedia")
            raise AssertionError("second 429 must pause")
        except x.Paused:
            assert conf["x"]["paused_until"] > time.time()
        conf["x"].update(pace="careful", paused_until=0)
        calls.clear()
        x.open_url = fake_open([429])
        try:
            x.request(conf, "https://x.com/i/api/graphql/q/UserMedia?v", "UserMedia")
            raise AssertionError("careful 429 must pause")
        except x.Paused:
            assert len(calls) == 1
    finally:
        x.open_url, x.budget, x.save_conf = saved
    conf["x"].update(paused_until=0, pace="jd")

    # bulk: handle + following + followers, deduped, alt excluded, caps, lock mid-run -> continue where it stopped
    accts = {"main": ("1", [("2", "f2"), ("3", "f3"), ("4242", "alt")], [("3", "f3"), ("5", "w5"), ("6", "w6")])}
    pages = {"1": [["101", "102"], ["103"]], "2": [["201"]], "3": [], "5": "protected", "6": [["601", "602", "603"]]}
    gql_calls, state_box = [], {}

    def fake_gql(conf, state, op, v):
        gql_calls.append((op, v.get("userId") or v.get("screen_name"), v.get("cursor")))
        if op == "UserByScreenName":
            return {"data": {"user": {"result": {"__typename": "User", "rest_id": "1", "core": {"screen_name": "main"}}}}}
        if op in ("Following", "Followers"):
            lst = accts["main"][1 if op == "Following" else 2]
            return {"x": [{"itemType": "TimelineUser", "user_results": {"result": {"__typename": "User", "rest_id": i, "core": {"screen_name": h}}}}
                          for i, h in lst]}
        uid, cur = v["userId"], int(v.get("cursor") or 0)
        if pages[uid] == "protected":
            raise RuntimeError("X: protected")
        if uid == "6" and state_box.get("lock_once"):
            state_box["lock_once"] = False
            raise x.Paused("locked")
        pg = pages[uid][cur] if cur < len(pages[uid]) else []
        entries = [{"__typename": "Tweet", "rest_id": t, "core": {"user_results": {"result": {"__typename": "User", "rest_id": uid, "core": {"screen_name": "u" + uid}}}},
                    "legacy": {"id_str": t, "extended_entities": {"media": [{"type": "photo", "media_url_https": f"https://pbs.twimg.com/media/{t}.jpg"}]}}} for t in pg]
        nxt = [{"cursorType": "Bottom", "value": str(cur + 1)}] if cur + 1 < len(pages[uid]) else []
        return {"data": entries + nxt}
    got_files = []
    fake_dl = lambda p, root, q, cancel=None: (got_files.append((q, p["id"])), (1, 0))[1]  # noqa: E731
    with tempfile.TemporaryDirectory() as tmp:
        orig = x.STATE_PATH, x.gql
        x.STATE_PATH, x.gql = Path(tmp) / "x_state.json", fake_gql
        try:
            st = x.load_state()
            q = x.bulk_plan(conf, st, "@main", following_max=10, followers_max=2)
            assert [a["handle"] for a in q] == ["main", "f2", "f3", "w5"], q  # alt dropped, f3 once, followers capped at 2
            st["bulk"]["accounts"].append({"id": "6", "handle": "w6"})
            state_box["lock_once"] = True
            try:
                x.bulk_run(conf, st, tmp, threading.Event(), download=fake_dl)
                raise AssertionError("the lock must stop the run")
            except x.Paused:
                pass
            b = x.load_state()["bulk"]
            assert b["i"] == 4 and not b["finished"] and b["stats"]["new"] == 4, b  # main(3) + f2(1); f3 empty; w5 skipped
            done_pages = len([c for c in gql_calls if c[0] == "UserMedia"])
            st2, shown = x.load_state(), []
            b = x.bulk_run(conf, st2, tmp, threading.Event(), download=fake_dl, on_file=lambda p, acct: shown.append((acct, p["id"])))
            assert shown == [("w6", "601"), ("w6", "602"), ("w6", "603")] or sorted(shown) == [("w6", "601"), ("w6", "602"), ("w6", "603")]
            assert b["finished"] and b["stats"]["new"] == 7 and b["stats"]["posts"] == 7
            assert [c for c in gql_calls if c[0] == "UserMedia"][done_pages:] == [("UserMedia", "6", None)]  # no page read twice
            assert sorted(got_files) == sorted([("main", "101"), ("main", "102"), ("main", "103"), ("f2", "201"),
                                                ("w6", "601"), ("w6", "602"), ("w6", "603")])
            st3 = x.load_state()  # max posts per account
            x.bulk_plan(conf, st3, "main", max_posts=2)
            got_files.clear()
            assert x.bulk_run(conf, st3, tmp, threading.Event(), download=fake_dl)["stats"]["posts"] == 2 and len(got_files) == 2
        finally:
            x.STATE_PATH, x.gql = orig


def ibtest():
    """Imageboard: archive (FoolFuuka) and live 4chan parsing from trimmed real replies, md5 dedupe across them,
    live -> archive fallback for a dead thread, search params, original-filename saving, watcher, Cloudflare."""
    conf = {"ib": json.loads(json.dumps(core.DEFAULTS["ib"])), "blacklist": [], "download_dir": ""}
    board = {"name": "Hentai/Alternative", "shortname": "d"}
    ff_op = {"num": "11597785", "subnum": "0", "thread_num": "11597785", "op": "1", "title": "Elf thread", "board": board,
             "comment_sanitized": "Elves.\nPost elves.", "nreplies": None, "media": {
                 "media_filename": "01.jpg", "media_hash": "b6DPU6/e3wSlsnc2bmiLCA==", "media_size": "242560", "media_w": "1280",
                 "media_h": "1810", "banned": "0", "media_link": "https://desu-usergeneratedcontent.xyz/d/image/1793/17/1793172802742.jpg",
                 "thumb_link": "https://desu-usergeneratedcontent.xyz/d/thumb/1793/17/1793172802742s.jpg"}}
    ff_reply = {**ff_op, "num": "11597790", "op": "0", "title": None, "comment_sanitized": "nice", "media": {**ff_op["media"],
                "media_filename": "02.jpg", "media_hash": "l2BocYT12kUHK1JOvu14iA==", "media_size": "189704"}}
    ff_text = {**ff_op, "num": "11597791", "op": "0", "media": None}
    ff_ghost = {**ff_reply, "num": "11597790", "subnum": "1"}
    p = ib.ff_post(ff_op, "desuarchive.org")
    assert p["id"] == "d-11597785" and p["filename"] == "01.jpg" and p["md5"] == "6fa0cf53afdedf04a5b277366e688b08" and p["op"]
    assert p["tags"] == ["/d/", "thread:d/11597785", "file:01.jpg"] and p["rating"] == "e" and p["size"] == 242560 and (p["w"], p["h"]) == (1280, 1810)
    assert p["page"] == "https://desuarchive.org/d/thread/11597785/#11597785" and p["title"] == "Elf thread" and p["source"] == "desuarchive.org"
    assert ib.ff_post(ff_text, "h") is None and ib.ff_post(ff_ghost, "h") is None
    live_op = {"no": 11597785, "resto": 0, "sub": "Elf &amp; co", "com": "Elves.<br>Post <span class=\"quote\">&gt;elves</span>.",
               "tim": 1793172802742249, "ext": ".jpg", "filename": "01", "md5": "b6DPU6/e3wSlsnc2bmiLCA==", "fsize": 242560, "w": 1280,
               "h": 1810, "replies": 152, "images": 84}
    lp = ib.live_post(live_op, "d", 11597785)
    assert lp["files"] == ["https://i.4cdn.org/d/1793172802742249.jpg"] and lp["thumb"] == "https://i.4cdn.org/d/1793172802742249s.jpg"
    assert lp["comment"] == "Elves.\nPost >elves." and lp["subject"] == "Elf & co" and lp["md5"] == p["md5"] and lp["filename"] == "01.jpg"
    assert ib.tile_text(lp) == "Elf & co\n152r 84i" and ib.live_post({"no": 5, "com": "text only"}, "d", 5) is None
    seen = {}
    assert fold(seen, lp) and fold(seen, {**p, "site": "ib2"}) and len(seen) == 1  # same image from 4chan and the archive shows once
    assert ib.search_params(["d", "trash"], 2, text="elf", images_only=True) == {"boards": "d.trash", "page": 2, "text": "elf", "filter": "text", "order": "desc"}
    srcs = ib.search_sources(conf, ["d", "trash", "h"], text="elf")
    assert sorted((s.host, tuple(s.boards)) for s in srcs) == [("archiveofsins.com", ("h",)), ("desuarchive.org", ("d", "trash"))]

    replies = {"posts": {"11597790": ff_reply, "11597791": ff_text}, "op": ff_op}
    orig = ib.get
    calls = []

    def fake_get(conf, url, host=""):
        calls.append(url)
        if "a.4cdn.org" in url:
            return None  # 404: the thread died
        return {"11597785": replies}
    ib.get = fake_get
    try:
        posts, info = ib.thread_posts(conf, "d", "11597785")
        assert [x["num"] for x in posts] == ["11597785", "11597790"] and not info["alive"] and info["source"] == "desuarchive.org"
        assert calls[0].startswith("https://a.4cdn.org/d/thread/11597785.json") and "desuarchive.org/_/api/chan/thread/" in calls[1]
        ib.get = lambda conf, url, host="": {"posts": [live_op, {"no": 11597800, "resto": 11597785, "com": "no image"},
                                                       {**live_op, "no": 11597801, "resto": 11597785, "tim": 2, "md5": "", "filename": "x"}]}
        posts, info = ib.thread_posts(conf, "d", "11597785")
        assert [x["num"] for x in posts] == ["11597785", "11597801"] and info["alive"] and info["replies"] == 152
        saved = ib.save_conf
        ib.save_conf = lambda c: None
        try:
            ib.watch(conf, "d", "11597785", "Elf thread")
            new = dict(ib.check_watched(conf))["d/11597785"]
            assert len(new) == 2 and conf["ib"]["watch"]["d/11597785"]["new"] == 2
            ib.mark_seen(conf, "d/11597785", new)
            assert ib.check_watched(conf) == [("d/11597785", [])]
        finally:
            ib.save_conf = saved
    finally:
        ib.get = orig

    with tempfile.TemporaryDirectory() as tmp:  # original filenames: same size = have it, other size = "(no)"
        class R(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False
        orig_open = ib.open_url
        ib.open_url = lambda url, timeout=30, headers=None: R(b"x" * 242560)
        try:
            assert ib.save(conf, p, tmp, "11597785 - Elf thread") == (1, 0)
            f = Path(tmp, "imageboard", "d", "11597785 - Elf thread", "01.jpg")
            assert f.exists() and f.stat().st_size == 242560
            assert ib.save(conf, p, tmp, "11597785 - Elf thread") == (0, 1)
            other = {**p, "num": "11597999", "id": "d-11597999", "size": 1000}
            assert ib.target(conf, other, tmp, "11597785 - Elf thread").name == "01 (11597999).jpg"
            conf["ib"]["names"] = "timestamp"
            assert ib.target(conf, lp, tmp, "x").name == "1793172802742249.jpg"
            conf["ib"]["names"] = "original"

            def cf(url, timeout=30, headers=None):
                raise urllib.error.HTTPError(url, 403, "Forbidden", {}, io.BytesIO(b"<title>Just a moment...</title>"))
            ib.open_url = cf
            try:
                ib.get(conf, "https://archived.moe/_/api/chan/index/?board=h&page=1", "archived.moe")
                raise AssertionError("Cloudflare must be explained")
            except RuntimeError as e:
                assert "Pass Cloudflare" in str(e) and "archived.moe" in str(e)
            conf["ib"]["cf"]["archived.moe"] = {"cookie": "CF", "ua": "UA/1"}
            assert ib.headers(conf, "archived.moe") == {"Cookie": "cf_clearance=CF", "User-Agent": "UA/1"} and ib.headers(conf, "desuarchive.org") == {}
        finally:
            ib.open_url = orig_open



def pawtest():
    """Pawchive: post parsing (file servers, preview-only files, other attachments, Postgres tag arrays), HTML
    cards and tags, the creator list filter, cookie login, the 429 retry, original-filename saving by SHA-256,
    and a bulk run that resumes at its saved offset."""
    conf = {"paw": json.loads(json.dumps(core.DEFAULTS["paw"])), "favorites": []}
    sha_a = "aad7c3ee6d33066244c7279b5d731c01a084a58169f6c7b7954b20b4dfa84161"
    post = {"id": "174231470", "user": "103431", "service": "patreon", "title": "Sneak Peek #57", "content": "<p>Two <em>new</em> characters</p>",
            "published": "2026-11-03T04:00:00", "tags": '{"$10 Sneak Peeks","Character Bios"}', "prev": None, "next": "174228515",
            "file": {"name": "Veronica_OP2.png", "node": 2, "path": f"/aa/d7/{sha_a}.png"},
            "attachments": [{"name": "Veronica_OP2.png", "node": 2, "path": f"/aa/d7/{sha_a}.png"},  # same as the main file: once
                            {"name": "teaser.jpg", "path": "/58/f2/58f2.jpg", "preview_only": True},
                            {"name": "clip.mp4", "path": "/11/22/1122.mp4"},
                            {"name": "pack.zip", "node": 2, "path": "/33/44/3344.zip"}]}
    p = pw.parse_post(post, conf)
    assert p["id"] == "patreon-103431-174231470" and p["names"] == ["Veronica_OP2.png", "teaser.jpg", "clip.mp4"]
    assert p["files"][0] == f"https://n2.pawchive.pw/data/aa/d7/{sha_a}.png?f=Veronica_OP2.png"
    assert p["files"][1] == "https://img.pawchive.pw/thumbnail/data/58/f2/58f2.jpg" and p["preview"] == 1  # preview only: the thumbnail
    assert p["files"][2] == "https://n2.pawchive.pw/data/11/22/1122.mp4?f=clip.mp4"  # no node in the reply: the default server
    assert p["other"] == [{"name": "pack.zip", "url": "https://n2.pawchive.pw/data/33/44/3344.zip?f=pack.zip"}]
    assert p["tags"] == ["$10 Sneak Peeks", "Character Bios", "service:patreon", "creator:patreon/103431"]
    assert p["content"] == "Two new characters" and p["next"] == "174228515" and p["thumb"].startswith("https://img.pawchive.pw/thumbnail/")
    assert pw.parse_post({**post, "file": {}, "attachments": [{"name": "a.zip", "path": "/x/y/a.zip"}]}, conf) is None
    assert pw.pg_array(["a"]) == ["a"] and pw.pg_array("{solo,\"two words\"}") == ["solo", "two words"] and pw.sha_of_url(p["files"][0]) == sha_a

    card = ('<article class="post-card post-card--preview" data-id="173960633" data-service="patreon" data-user="92168361" >'
            '<a href="/patreon/user/92168361/post/173960633"><header class="post-card__header"> Trick or... Treat? </header>'
            '<div class="post-card__image-container"><img class="post-card__image" src="https://img.pawchive.pw/thumbnail/data/fa/24/fa24.jpg" '
            'alt="x" loading="lazy"></div><footer><div> 1 attachment <br> 1,185 favorites </div></footer></a></article>'
            '<article\n    class="post-card" data-id="9" data-service="fanbox" data-user="7"><header class="post-card__header">text only</header></article>'
            '<article\n    class="post-card post-card--preview"\n    data-id="10"\n    data-service="fanbox"\n    data-user="7"\n  >'
            '<header class="post-card__header">multi line</header><img class="post-card__image" src="https://img.pawchive.pw/t.jpg"></article>')
    cards = pw.parse_cards(conf, card)
    assert len(cards) == 2 and cards[1]["title"] == "multi line" and cards[0]["stub"] and cards[0]["title"] == "Trick or... Treat?" and cards[0]["favorites"] == 1185
    assert cards[0]["id"] == "patreon-92168361-173960633" and cards[0]["thumb"].endswith("/fa24.jpg")
    tags_html = ('<section id="tag-container"><article><a href="/posts?tag=wip"><span>wip</span><span>302777</span></a></article>'
                 '<article><a href="/posts?tag=nsfw"><span>nsfw</span><span>531579</span></a></article>'
                 '<article><a href="/posts?tag=Character+Bios"><span>Character Bios</span><span>12</span></a></article></section>')
    assert pw.parse_tags(tags_html) == [("nsfw", 531579), ("wip", 302777), ("Character Bios", 12)]

    rows = [{"id": "1", "name": "Jimjim", "service": "patreon", "updated": 1793682000, "favorited": 10, "public_id": "Jimjim3dx"},
            {"id": "2", "name": "jimmy", "service": "fanbox", "updated": "2026-10-01T00:00:00", "favorited": 50},
            {"id": "3", "name": "Other", "service": "patreon", "updated": "2026-11-04T00:00:00", "favorited": 1}]
    assert [c["id"] for c in pw.find_creators(rows, "JIM")] == ["1", "2"]
    assert [c["id"] for c in pw.find_creators(rows, "jim", sort="Favorited")] == ["2", "1"]
    assert [c["id"] for c in pw.find_creators(rows, "", "patreon")] == ["3", "1"] and pw.find_creators(rows, "3dx")[0]["id"] == "1"

    flag = json.dumps([{"domain": "pawchive.pw", "name": "session", "value": "SESS"}, {"domain": ".x.com", "name": "session", "value": "WRONG"}])
    assert pw.cookie_login(conf, flag) == "session" and conf["paw"]["session"] == "SESS"
    assert pw._headers(conf)["Cookie"] == "session=SESS"
    try:
        pw.cookie_login(conf, "guest=1")
        raise AssertionError("must refuse a paste without the session cookie")
    except RuntimeError:
        pass

    seq, waits = [], []

    class R(io.BytesIO):
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def send(conf, url, method, accept):
        item = seq.pop(0)
        if item == 429:
            raise urllib.error.HTTPError(url, 429, "Too Many Requests", {"Retry-After": "7"}, io.BytesIO(b""))
        return R(item)
    seq[:] = [429, b"[1]"]
    assert pw._open(conf, "u", sleep=waits.append, send=send) == (200, b"[1]") and waits == [7]
    seq[:] = [429, 429]
    assert pw._open(conf, "u", sleep=waits.append, send=send)[0] == 429  # retried once only

    with tempfile.TemporaryDirectory() as tmp:  # original filenames: same file (SHA-256) reused, a different one gets (2)
        body = b"pawchive test file"
        sha = hashlib.sha256(body).hexdigest()
        q = {**p, "files": [f"https://n2.pawchive.pw/data/aa/bb/{sha}.png?f=a.png"], "names": ["a.png"], "other": []}
        orig_open, orig_cn = pw.open_url, pw.creator_name
        pw.open_url = lambda url, timeout=30, headers=None: R(body)
        pw.creator_name = lambda conf, s, u: "Jimjim"
        try:
            assert pw.save(conf, q, tmp) == (1, 0)
            f = Path(tmp, "pawchive", "Jimjim (patreon)", "174231470 - Sneak Peek #57", "a.png")
            assert f.read_bytes() == body and pw.sha256_file(f) == sha
            assert pw.save(conf, q, tmp) == (0, 1)
            other = {**q, "files": [f"https://n2.pawchive.pw/data/cc/dd/{'0' * 64}.png?f=a.png"]}
            assert pw.target(conf, other, tmp, "a.png", other["files"][0]).name == "a (2).png"
            assert pw.saved_paths(conf, q, tmp) == [f]

            # bulk: page 1 saved, cancel during page 2, Continue resumes at offset 2 (not 0)
            pages = {0: [post, {**post, "id": "2"}], 2: [{**post, "id": "3"}], 3: []}
            calls, saved_ids = [], []
            orig_api, orig_save, orig_sc = pw.api, pw.save, pw.save_conf
            pw.api = lambda conf, path, **kw: (calls.append(kw.get("o")), pages[kw.get("o", 0)])[1]
            pw.save_conf = lambda c: None
            cancel = threading.Event()

            def fake_save(conf, post, root, cancel_=None, everything=False):
                saved_ids.append(post["post_id"])
                if post["post_id"] == "3" and not cancel.is_set() and calls.count(2) == 1:
                    cancel.set()
                return 1, 0
            pw.save = fake_save
            st = pw.bulk_creator(conf, "patreon", "103431", tmp, cancel)
            assert not st["done"] and st["o"] == 2 and conf["paw"]["bulk"]["patreon/103431"]["o"] == 2
            st = pw.bulk_creator(conf, "patreon", "103431", tmp, threading.Event())
            assert st["done"] and st["o"] == 3 and calls == [0, 2, 2, 3] and sorted(saved_ids) == ["174231470", "2", "3", "3"]
        finally:
            pw.open_url, pw.creator_name = orig_open, orig_cn
            pw.api, pw.save, pw.save_conf = orig_api, orig_save, orig_sc
