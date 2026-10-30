"""Pixiv app API: refresh-token login, list sources paged by next_url (search with filters, rankings, recommended,
follow feed, bookmarks, users, series, related), account writes (bookmark / follow) and ugoira."""
import hashlib
import io
import json
import threading
import time
import urllib.error
import zipfile

from PySide6.QtGui import QImage

from .core import Source, dest, get_json, http, make_post, qs, save_conf

UA = "PixivAndroidApp/5.0.234 (Android 11; Pixel 5)"
CLIENT = ("MOBrBDS8blbauoSck0ZfDbtuzpyT", "lsACyCD94FhDUtGTXi3QzcFE2uU1hqtDaKeqrdwj")
HASH = "28c1fdd170a5204386cb1313c7077b34f83e4aaf4aa829ce78c231e05b0bae2c"
API = "https://app-api.pixiv.net/"
NO_TOKEN = "Pixiv: paste a refresh token in Settings (see README)"

TARGETS = {"Partial tags": "partial_match_for_tags", "Exact tags": "exact_match_for_tags", "Title & caption": "title_and_caption"}
SORTS = {"Newest": "date_desc", "Oldest": "date_asc", "Popular": "popular_desc"}
RANKINGS = {"Daily": "day", "Weekly": "week", "Monthly": "month", "Daily (male)": "day_male", "Daily (female)": "day_female",
            "Original": "week_original", "Rookie": "week_rookie", "AI": "day_ai", "Manga daily": "day_manga",
            "Manga weekly": "week_manga", "R-18 daily": "day_r18", "R-18 weekly": "week_r18", "R-18 male": "day_male_r18",
            "R-18 female": "day_female_r18", "R-18 AI": "day_r18_ai", "R-18G": "week_r18g"}

_auth = {"token": None, "exp": 0, "lock": threading.Lock()}


def headers(conf):
    """Bearer headers, refreshing the access token from the refresh token when it ran out.
    The token reply also says who the user is (needed for bookmarks) and whether they have Premium."""
    with _auth["lock"]:
        if time.time() > _auth["exp"]:
            c = conf["pixiv"]
            if not c["refresh_token"]:
                raise RuntimeError(NO_TOKEN)
            t = time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime())
            data = qs(client_id=CLIENT[0], client_secret=CLIENT[1], grant_type="refresh_token",
                      refresh_token=c["refresh_token"], include_policy="true").encode()
            try:
                r = json.loads(http("https://oauth.secure.pixiv.net/auth/token", data=data, headers={
                    "User-Agent": UA, "X-Client-Time": t, "X-Client-Hash": hashlib.md5((t + HASH).encode()).hexdigest(),
                    "Content-Type": "application/x-www-form-urlencoded"}))
            except urllib.error.HTTPError as e:
                raise RuntimeError(f"Pixiv login failed (HTTP {e.code}): the refresh token is wrong or expired") from None
            _auth["token"], _auth["exp"] = r["access_token"], time.time() + r.get("expires_in", 3600) - 60
            user = r.get("user") or {}
            changed = (r.get("refresh_token") or c["refresh_token"], str(user.get("id", c["user_id"])), bool(user.get("is_premium")))
            if changed != (c["refresh_token"], c["user_id"], c["premium"]):
                c["refresh_token"], c["user_id"], c["premium"] = changed
                save_conf(conf)
        return {"Authorization": "Bearer " + _auth["token"], "User-Agent": UA,
                "App-OS": "android", "App-OS-Version": "11", "Accept-Language": "en-us"}


def logout():
    _auth["exp"] = 0  # the token in Settings changed


def api(conf, path, **params):
    """GET an app-API path (or a full next_url)."""
    url = path if path.startswith("http") else API + path + "?" + qs(**params)
    return get_json(url, "Pixiv", headers=headers(conf))


def send(conf, path, **form):
    """POST (bookmark / follow writes); -> parsed reply."""
    try:
        body = http(API + path, data=qs(**form).encode(), headers={**headers(conf), "Content-Type": "application/x-www-form-urlencoded"})
    except urllib.error.HTTPError as e:
        try:
            msg = json.loads(e.read())["error"]["message"] or ""
        except Exception:
            msg = ""
        raise RuntimeError(f"Pixiv: HTTP {e.code} {msg}".strip()) from None
    return json.loads(body) if body.strip() else {}


def my_id(conf):
    if not conf["pixiv"]["user_id"]:
        headers(conf)  # logging in fills it
    return conf["pixiv"]["user_id"]


def parse(data):
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
        user = i.get("user") or {}
        out.append(make_post("pixiv", i["id"], i["image_urls"]["square_medium"], files, samples, tags,
                             "e" if i.get("x_restrict") else "g", f"https://www.pixiv.net/artworks/{i['id']}",
                             i.get("width", 0), i.get("height", 0), pixiv_id=str(i["id"]), ugoira=i.get("type") == "ugoira",
                             type=i.get("type", "illust"), title=i.get("title", ""), user=str(user.get("id", "")),
                             user_name=user.get("name", ""), bookmarks=i.get("total_bookmarks", 0), ai=i.get("illust_ai_type") == 2,
                             bookmarked=bool(i.get("is_bookmarked")), series=i.get("series") or None))
    return out


def keep_filter(r18="Show", ai=True, min_bm=0, kind="All"):
    """Client-side filters for the search options (also applied where Pixiv's own params are ignored)."""
    def keep(p):
        return ((r18 == "Show" or (r18 == "Hide") == (p["rating"] == "g")) and (ai or not p.get("ai"))
                and p.get("bookmarks", 0) >= min_bm and (kind == "All" or (p.get("type") == "manga") == (kind == "Manga")))
    return None if (r18, ai, min_bm, kind) == ("Show", True, 0, "All") else keep


def search_params(word, target="Partial tags", sort="Newest", start="", end="", min_bm=0, ai=True, premium=False):
    """Search options -> (path, params, note). Popular sorting is Premium-only: without it Pixiv's free
    popular-preview (one page of the most popular works) stands in."""
    path, note, sort = "v1/search/illust", "", SORTS[sort]
    if sort == "popular_desc" and not premium:
        path, sort, note = "v1/search/popular-preview/illust", None, "  (Popular without Premium: Pixiv's one-page popular preview)"
    return path, {"word": word, "search_target": TARGETS[target], "sort": sort, "start_date": start or None, "end_date": end or None,
                  "bookmark_num_min": min_bm or None, "search_ai_type": None if ai else 0, "filter": "for_android"}, note


class PixivSource(Source):
    """One Pixiv list endpoint, paged by the next_url Pixiv returns. params "user_id": "me" = the logged-in user."""
    label = "PX"

    def __init__(self, conf, path, keep=None, **params):
        self.conf, self.path, self.keep, self.params, self.next = conf, path, keep, params, None

    def fetch(self, page):
        if page == 1:
            params = {k: my_id(self.conf) if v == "me" else v for k, v in self.params.items()}
            url = API + self.path + "?" + qs(**params)
        else:
            url = self.next
        posts = []
        for _ in range(5):  # a strict filter can empty whole pages: look a little further before calling it the end
            if not url:
                break
            data = api(self.conf, url)
            url = self.next = data.get("next_url")
            posts = [p for p in parse(data) if not self.keep or self.keep(p)]
            if posts:
                break
        return posts


def search(conf, word, target="Partial tags", sort="Newest", start="", end="", min_bm=0, ai=True, r18="Show", kind="All"):
    path, params, note = search_params(word, target, sort, start, end, min_bm, ai, conf["pixiv"]["premium"])
    return PixivSource(conf, path, keep_filter(r18, ai, min_bm, kind), **params), note


def ranking(conf, mode, date=""):
    return PixivSource(conf, "v1/illust/ranking", mode=mode, date=date or None, filter="for_android")


def recommended(conf):
    return PixivSource(conf, "v1/illust/recommended", filter="for_android", include_ranking_illusts="false")


def following(conf, restrict="public"):
    return PixivSource(conf, "v2/illust/follow", restrict=restrict)


def bookmarks(conf, user="me", restrict="public"):
    return PixivSource(conf, "v1/user/bookmarks/illust", user_id=user, restrict=restrict)


def user_works(conf, uid, kind="illust"):
    return PixivSource(conf, "v1/user/illusts", user_id=uid, type=kind, filter="for_android")


def series(conf, sid):
    return PixivSource(conf, "v2/illust/series", illust_series_id=sid, filter="for_android")


def related(conf, iid):
    return PixivSource(conf, "v2/illust/related", illust_id=iid, filter="for_android")


def user_detail(conf, uid):
    return api(conf, "v1/user/detail", user_id=uid, filter="for_android")


def trending(conf):
    """-> [{"tag", "translated", "post"}] Pixiv's trending tags, each with a sample work."""
    tags = api(conf, "v1/trending-tags/illust", filter="for_android").get("trend_tags", [])
    return [{"tag": t["tag"], "translated": t.get("translated_name") or "", "post": parse({"illusts": [t["illust"]]})[0]}
            for t in tags if t.get("illust")]


def bookmark(conf, iid, on, private=False):
    if on:
        return send(conf, "v2/illust/bookmark/add", illust_id=iid, restrict="private" if private else "public")
    return send(conf, "v1/illust/bookmark/delete", illust_id=iid)


def follow(conf, uid, on, private=False):
    if on:
        return send(conf, "v1/user/follow/add", user_id=uid, restrict="private" if private else "public")
    return send(conf, "v1/user/follow/delete", user_id=uid)


def complete(conf, word):
    tags = (api(conf, "v2/search/autocomplete", word=word, merge_plain_keyword_results="true") or {}).get("tags", [])
    return [(t["name"], f"{t['name']}  ({t['translated_name']})" if t.get("translated_name") else t["name"]) for t in tags]


def ugoira(conf, iid):
    """-> (frames zip url, [(frame file, delay ms)])"""
    m = api(conf, "v1/ugoira/metadata", illust_id=iid)["ugoira_metadata"]
    return m["zip_urls"]["medium"], [(f["file"], f["delay"]) for f in m["frames"]]


def ugoira_frames(conf, iid):
    url, frames = ugoira(conf, iid)
    with zipfile.ZipFile(io.BytesIO(http(url))) as z:
        return [(QImage.fromData(z.read(f)), d) for f, d in frames]


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
