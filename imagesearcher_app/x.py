"""X (Twitter), opt-in and read-only: an alt account's pasted session cookies read the internal GraphQL API the
web client uses. Ring 1 = the accounts your main follows (read from its public following list), ring 2 = the
accounts at least MIN_OVERLAP of them follow; plus bulk downloads of one @handle (and optionally its following /
followers). Every request goes through one budget (Budget): "jd" pace goes about as fast as X allows, like
JDownloader (waits only when X's rate-limit headers say a limit is used up); "careful" is slow and capped.
Lock / suspension replies stop all X traffic until Resume."""
import json
import os
import random
import re
import threading
import time
import urllib.error
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed

from .core import CONF_PATH, CONF_LOCK, Source, download_post, http, log, make_post, open_url, save_conf

BEARER = ("AAAAAAAAAAAAAAAAAAAAANRILgAAAAAAnNwIzUejRCOuH5E6I8xnZz4puTs%3D"
          "1Zv7ttfk8LF81IUq16cHjhLTvJu4FA33AGWWjCpTnA")  # the public token of X's own web client
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:140.0) Gecko/20100101 Firefox/140.0"
GQL = "https://x.com/i/api/graphql/"
OPS = ("UserByScreenName", "Following", "Followers", "UserTweets", "UserMedia")
# Used only when reading the current ones from x.com's main.js fails; X rotates them every few weeks.
# ponytail: stale fallbacks just fail with a clear error, the main.js read is what keeps this working
FALLBACK_QIDS = {"UserByScreenName": "x3RLKWW1Tl7JgU7YtGxuzw", "Following": "zx6e-TLzRkeDO_a7p4b3JQ",
                 "Followers": "oQWxG6XdR5SPvMBsPiKUPQ", "UserTweets": "bbmLh6qwlhCgVTDO4R9ngA",
                 "UserMedia": "WJhR3BD7MuJyTjd6Qhh6qA"}

PACES = {"jd": "JDownloader (fast)", "careful": "Careful (slow)"}
JD_GAP, JD_JITTER = 1.0, 0.5  # jd pace: seconds between requests; X's own rate-limit headers do the rest
JD_429_WAIT = 900  # a 429 without a reset time: wait 15 min
GAP, JITTER, HOURLY = 15.0, 10.0, 150  # careful pace: seconds between requests (+ random 0..JITTER), requests per hour
MAX_PAUSE_MIN = 360
MIN_OVERLAP, RING2_MAX, RING2_DEPTH = 3, 150, 400  # ring 2: followed by >= 3 follows, top 150, first 400 of each list
REBUILD_DAYS, BATCH, KEEP_POSTS = 7, 40, 3000
STATE_PATH = CONF_PATH.with_name("x_state.json")
NOT_SET = "X: not logged in. Copy the alt account's x.com cookies (Flag Cookies extension) and press 📋 Paste cookies"
NO_SEED = "X: Timeline / Discover need your main @handle in ⚙ Settings (its following list is read)"


class Paused(RuntimeError):
    """Raised instead of sending anything while X traffic is paused or the app is quitting."""


STOP = threading.Event()  # set on quit: wakes sleeping requests so the app can exit


# ---------- the request budget ----------

class Budget:
    """One request at a time; the lock is held while waiting, so callers queue up in order instead of bursting.
    careful: GAP + random(0..JITTER) s apart, at most HOURLY per rolling hour.
    jd: JD_GAP + random(0..JD_JITTER) s apart, and a request to an operation whose X limit is used up
    (x-rate-limit-remaining <= 1) waits until that limit's reset time."""

    def __init__(self, clock=time.time, sleep=None, rand=random.random):
        self.clock, self.rand, self.lock = clock, rand, threading.Lock()
        self.sleep = sleep or (lambda s: STOP.wait(s))
        self.last, self.stamps = 0.0, []
        self.limits = {}  # operation -> (remaining, reset epoch) from X's last reply

    def next_ok(self, pace="careful", op=None):
        now = self.clock()
        self.stamps = [t for t in self.stamps if now - t < 3600]
        if pace == "jd":
            t = max(now, self.last + JD_GAP + JD_JITTER * self.rand())
            rem, reset = self.limits.get(op, (None, 0))
            if rem is not None and rem <= 1 and reset > now:
                t = max(t, reset + 2)
            return t
        t = max(now, self.last + GAP + JITTER * self.rand())
        if len(self.stamps) >= HOURLY:
            t = max(t, self.stamps[0] + 3600)
        return t

    def note(self, op, hdrs):
        """Remember X's rate-limit headers for op."""
        try:
            self.limits[op] = (int(hdrs.get("x-rate-limit-remaining")), int(hdrs.get("x-rate-limit-reset")))
        except (TypeError, ValueError, AttributeError):
            pass

    def status(self, pace):
        """Short text for the banner."""
        if pace != "jd":
            return f"careful pace, {self.left()} of {HOURLY} requests left this hour"
        now = self.clock()
        out = [f"{op} limit used up, continuing {time.strftime('%H:%M', time.localtime(reset))}"
               for op, (rem, reset) in self.limits.items() if rem <= 1 and reset > now]
        return "JDownloader pace" + (" · " + " · ".join(out) if out else "".join(
            f" · {op} {rem} left" for op, (rem, reset) in sorted(self.limits.items()) if reset > now)[:200])

    def wait(self, check=lambda: None, pace="careful", op=None):
        with self.lock:
            check()
            t = self.next_ok(pace, op)
            wait = t - self.clock()
            if pace == "jd" and wait > 5:
                log.info("x: X's limit for %s is used up, waiting %.0f s (until %s)", op, wait, time.strftime("%H:%M:%S", time.localtime(t)))
            elif pace != "jd" and wait > 0.5:
                log.info("x: next request in %.0f s (%d of %d left this hour)", wait, self.left(), HOURLY)
            if t > self.clock():
                self.sleep(t - self.clock())
            if STOP.is_set():
                raise Paused("X: quitting")
            check()  # a pause may have started while this request waited
            self.last = self.clock()
            self.stamps.append(self.last)

    def left(self):
        """Requests still allowed in the rolling hour (for the banner)."""
        now = self.clock()
        return HOURLY - sum(1 for t in self.stamps if now - t < 3600)


budget = Budget()


# ---------- pausing ----------

def pause_minutes(conf, rand=random.random):
    m = min(max(int(conf["x"]["pause_min"]), 15), MAX_PAUSE_MIN)
    if conf["x"]["pause_random"]:
        m *= 0.75 + 0.5 * rand()
    return min(m, MAX_PAUSE_MIN)


def pause_for(conf, reason, locked=False):
    """Stop all X traffic: for the set (randomized, <= 6 h) time, or until Resume when the account was locked."""
    with CONF_LOCK:
        c = conf["x"]
        c["reason"], c["locked"] = reason, locked or c["locked"]
        mins = pause_minutes(conf)
        c["paused_until"] = 0 if locked else time.time() + mins * 60
        save_conf(conf)
    log.warning("x: paused %s (%s)", "until Resume" if locked else f"{mins:.0f} min", reason)


def resume(conf):
    with CONF_LOCK:
        conf["x"].update(paused_until=0, locked=False, reason="")
        save_conf(conf)
    log.info("x: resumed by hand")


def paused(conf):
    """-> reason text while paused, else ''."""
    c = conf["x"]
    if c["locked"]:
        return f"X paused until you press Resume: {c['reason']}"
    if time.time() < c["paused_until"]:
        return f"X paused until {time.strftime('%H:%M', time.localtime(c['paused_until']))}: {c['reason']}"
    return ""


def check(conf):
    if not ready(conf):
        raise RuntimeError(NOT_SET)
    if why := paused(conf):
        raise Paused(why)


def fix_pasted_export(conf):
    """A whole cookie export typed / pasted into the auth_token field: unpack it. -> True if it changed anything."""
    tok = conf["x"]["auth_token"]
    if len(tok) > 200 or tok.lstrip().startswith(("{", "[", "#")) or ";" in tok:
        try:
            log.info("x: the auth_token field holds a whole cookie export, unpacking it: %s", cookie_login(conf, tok))
            return True
        except RuntimeError as e:
            log.warning("x: the auth_token field holds something that isn't a cookie: %s", e)
    return False


def ready(conf):
    """Logged in (cookies present). The main @handle is only needed for Timeline / Discover (seed)."""
    c = conf["x"]
    return bool(c["enabled"] and c["auth_token"] and c["ct0"])


# ---------- talking to X ----------

def headers(conf):
    c = conf["x"]
    return {"Authorization": "Bearer " + BEARER, "x-csrf-token": c["ct0"], "Cookie": f"auth_token={c['auth_token']}; ct0={c['ct0']}",
            "x-twitter-auth-type": "OAuth2Session", "x-twitter-active-user": "yes", "x-twitter-client-language": "en",
            "User-Agent": c.get("ua") or UA, "Referer": "https://x.com/", "Accept": "*/*"}


def error_kind(status, body):
    """Classify a failed reply -> ("lock" | "pause" | "auth" | "txid" | "other", message)."""
    try:
        errs = json.loads(body).get("errors") or []
    except Exception:
        errs = []
    codes = {e.get("code") for e in errs}
    msg = "; ".join(str(e.get("message", "")) for e in errs)[:200] or f"HTTP {status}"
    text = msg.lower()
    if codes & {64, 326} or "locked" in text or "suspended" in text:
        return "lock", msg
    if status == 429 or 88 in codes or "rate limit" in text:
        return "pause", msg
    if status == 401 or codes & {32, 89, 239, 353}:
        return "auth", msg
    if status == 404 and not errs:  # X answers an unsigned request (missing x-client-transaction-id) with a bare 404
        return "txid", msg
    return "other", msg


def pace(conf):
    return conf["x"].get("pace") if conf["x"].get("pace") in PACES else "jd"


def request(conf, url, op=None):
    """One budgeted GET to X -> parsed JSON. Lock / suspension replies stop X until Resume. A rate limit:
    jd pace waits until X's reset time and tries once more (a second one pauses); careful pace pauses."""
    p = pace(conf)
    for attempt in (1, 2):
        budget.wait(lambda: check(conf), p, op)
        try:
            with open_url(url, headers=headers(conf)) as r:
                body = r.read()
                budget.note(op, r.headers)
            break
        except urllib.error.HTTPError as e:
            budget.note(op, e.headers)
            kind, msg = error_kind(e.code, e.read())
            log.warning("x: request refused: HTTP %s, %s (%s)", e.code, kind, msg)
            if kind == "pause" and p == "jd" and attempt == 1:
                reset = budget.limits.get(op, (0, 0))[1]
                until = reset if reset > time.time() else time.time() + JD_429_WAIT
                budget.limits[op] = (0, until)  # the next wait() sleeps until then
                log.warning("x: rate limit on %s, waiting until %s then trying once more (JDownloader pace)",
                            op, time.strftime("%H:%M:%S", time.localtime(until)))
                continue
            raise handle(conf, kind, msg) from None
        except OSError as e:
            log.warning("x: request failed: %s: %s", type(e).__name__, e)
            raise RuntimeError(f"X: network error: {e}") from None
    try:
        data = json.loads(body)
    except ValueError:
        log.warning("x: reply is not JSON (%d bytes): %r", len(body), body[:120])
        raise RuntimeError("X: unexpected reply (not JSON), see the log") from None
    if data.get("errors"):
        kind, msg = error_kind(200, body)
        log.warning("x: reply carries errors (%s): %s%s", kind, msg, "" if not data.get("data") else " (partial data kept)")
        if not data.get("data"):
            raise handle(conf, kind, msg)
    return data


def handle(conf, kind, msg):
    if kind in ("lock", "pause"):
        pause_for(conf, msg, locked=kind == "lock")
        return Paused(paused(conf))
    if kind == "auth":
        return RuntimeError(f"X refused the cookies ({msg}): copy fresh auth_token and ct0 from the alt's browser session")
    if kind == "txid":
        return RuntimeError("X: request refused (404). X may now require the x-client-transaction-id header, "
                            "which this version doesn't send yet")
    return RuntimeError(f"X: {msg}")


QID_RE = re.compile(r'queryId:"([\w-]+)",operationName:"(\w+)",operationType:"\w+",metadata:\{featureSwitches:\[([^\]]*)\]'
                    r'(?:,fieldToggles:\[([^\]]*)\])?')
MAIN_JS = re.compile(r"https://abs\.twimg\.com/responsive-web/client-web(?:-legacy)?/main\.\w+\.js")


def parse_ops(js):
    """main.js -> {operation: {"qid", "features": {name: True}, "toggles": {name: False}}} for OPS."""
    names = lambda s: re.findall(r'"(\w+)"', s)  # noqa: E731
    return {op: {"qid": qid, "features": dict.fromkeys(names(fs), True), "toggles": dict.fromkeys(names(ft), False)}
            for qid, op, fs, ft in QID_RE.findall(js) if op in OPS}


def ops(conf, state):
    """Current query ids + feature switches, read from x.com's main.js at most once a day (2 budgeted requests)."""
    cached = state.get("ops") or {}
    if cached.get("ops") and time.time() - cached.get("ts", 0) < 86400:
        return cached["ops"]
    found = {}
    log.info("x: reading the current query ids from x.com's main.js (once a day)")
    try:
        budget.wait(lambda: check(conf), pace(conf), "main.js")
        page = http("https://x.com/", headers={"User-Agent": UA, "Cookie": headers(conf)["Cookie"]}).decode(errors="replace")
        m = MAIN_JS.search(page)
        if m:
            budget.wait(lambda: check(conf), pace(conf), "main.js")
            found = parse_ops(http(m[0], headers={"User-Agent": UA}).decode(errors="replace"))
            log.info("x: main.js %s: found %s", m[0].rsplit("/", 1)[-1], ", ".join(f"{op}={o['qid']} ({len(o['features'])} features)"
                                                                                for op, o in found.items()) or "none of the needed operations")
        else:
            log.warning("x: x.com's page (%d bytes) has no main.js link; title: %s", len(page),
                        (re.search(r"<title>([^<]*)", page) or [None, "?"])[1][:80])
    except Paused:
        raise
    except Exception as e:
        log.warning("x: reading query ids from main.js failed (%s: %s)", type(e).__name__, e)
    for op in OPS:
        if op not in found:
            log.warning("x: %s: using the built-in query id %s (may be outdated)", op, FALLBACK_QIDS[op])
            found[op] = {"qid": FALLBACK_QIDS[op], "features": {}, "toggles": {}}
    state["ops"] = {"ts": time.time(), "ops": found}
    save_state(state)
    return found


NULL_FEATURES = re.compile(r"features cannot be null: ([\w, ]+)")


def gql(conf, state, op, variables):
    try:
        return gql_once(conf, state, op, variables)
    except RuntimeError as e:
        # X names the feature switches it wants (likely when main.js couldn't be read): add them, try once more
        m = NULL_FEATURES.search(str(e))
        if not m or isinstance(e, Paused):
            raise
        o = state["ops"]["ops"][op]
        names = [f.strip() for f in m[1].split(",") if f.strip()]
        log.info("x: %s wants feature switches %s; adding them and trying once more", op, ", ".join(names))
        o["features"].update(dict.fromkeys(names, True))
        save_state(state)
        return gql_once(conf, state, op, variables)


def gql_once(conf, state, op, variables):
    o = ops(conf, state)[op]
    log.info("x: %s %s", op, ", ".join(f"{k}={v}" for k, v in variables.items() if k in ("screen_name", "userId", "count"))
             + (" (next page)" if variables.get("cursor") else ""))
    url = GQL + f"{o['qid']}/{op}?" + urllib.parse.urlencode({
        "variables": json.dumps(variables, separators=(",", ":")),
        "features": json.dumps(o["features"], separators=(",", ":")),
        "fieldToggles": json.dumps(o["toggles"], separators=(",", ":"))})
    return request(conf, url, op)


# ---------- reading replies (walked, not path-matched: X moves fields around often) ----------

def walk(node, skip=("quoted_status_result",)):
    """Every dict in a reply, depth first, not descending into quoted tweets (not the author's media)."""
    if isinstance(node, dict):
        yield node
        for k, v in node.items():
            if k not in skip:
                yield from walk(v, skip)
    elif isinstance(node, list):
        for v in node:
            yield from walk(v, skip)


def user_of(d):
    """A User result -> {"id", "handle", "name"} or None."""
    if not isinstance(d, dict) or d.get("__typename") != "User" or not d.get("rest_id"):
        return None
    core, legacy = d.get("core") or {}, d.get("legacy") or {}
    handle = core.get("screen_name") or legacy.get("screen_name")
    if not handle:
        return None
    return {"id": d["rest_id"], "handle": handle, "name": core.get("name") or legacy.get("name") or handle,
            "following": legacy.get("friends_count", 0), "followers": legacy.get("followers_count", 0),
            "media": legacy.get("media_count", 0)}


def users(data):
    """Following reply -> (users, bottom cursor or None)."""
    out, seen, cursor = [], set(), None
    for d in walk(data):
        if d.get("cursorType") == "Bottom":
            cursor = d.get("value")
        u = user_of((d.get("user_results") or {}).get("result")) if d.get("itemType") == "TimelineUser" else None
        if u and u["id"] not in seen:
            seen.add(u["id"])
            out.append(u)
    return out, cursor


def unwrap(t):
    return t.get("tweet", t) if t.get("__typename") == "TweetWithVisibilityResults" else t


def media_post(t, rt_by=None):
    """A Tweet result with photos / videos / GIFs -> post, else None."""
    lg = t.get("legacy") or {}
    media = (lg.get("extended_entities") or {}).get("media") or []
    if not media:
        return None
    author = user_of(((t.get("core") or {}).get("user_results") or {}).get("result")) or {"id": lg.get("user_id_str", ""), "handle": "", "name": ""}
    files, samples = [], []
    for m in media:
        if m.get("type") == "photo":
            base = m["media_url_https"]
            files.append(base + "?name=orig")
            samples.append(base + "?name=large")
        else:  # video / animated_gif: best mp4, its poster as the still
            vs = [v for v in (m.get("video_info") or {}).get("variants", []) if v.get("content_type") == "video/mp4"]
            if vs:
                files.append(max(vs, key=lambda v: v.get("bitrate", 0))["url"])
                samples.append(m["media_url_https"])
    if not files:
        return None
    info = media[0].get("original_info") or {}
    tid = t.get("rest_id") or lg.get("id_str")
    tags = [h["text"] for h in (lg.get("entities") or {}).get("hashtags", [])] + ([f"user:{author['handle']}"] if author["handle"] else [])
    text = re.sub(r"\s*https://t\.co/\w+$", "", lg.get("full_text", "")).strip()
    return make_post("x", tid, media[0]["media_url_https"] + "?name=small", files, samples, tags,
                     "e" if lg.get("possibly_sensitive") else "g", f"https://x.com/{author['handle'] or 'i'}/status/{tid}",
                     info.get("width", 0), info.get("height", 0), user=author["id"], handle=author["handle"],
                     user_name=f"{author['name']} (@{author['handle']})" if author["handle"] else "", title=text[:120], rt_by=rt_by)


def tweets(data, by=None):
    """UserTweets / UserMedia reply -> (posts, bottom cursor, newest tweet id seen). Retweets count with the
    retweeted tweet's media, credited "rt_by" the account that retweeted; duplicates show once."""
    posts, ids, cursor, newest = [], set(), None, 0
    inner = set()
    for d in walk(data):
        if d.get("cursorType") == "Bottom":
            cursor = d.get("value")
        if d.get("__typename") not in ("Tweet", "TweetWithVisibilityResults"):
            continue
        t = unwrap(d)
        if not t.get("rest_id") or t["rest_id"] in inner:
            continue
        newest = max(newest, int(t["rest_id"]))
        rt = ((t.get("legacy") or {}).get("retweeted_status_result") or {}).get("result")
        p = None
        if rt:
            rt = unwrap(rt)
            inner.add(rt.get("rest_id"))
            p = media_post(rt, rt_by=by)
        else:
            p = media_post(t)
        if p and p["id"] not in ids:
            ids.add(p["id"])
            posts.append(p)
    return posts, cursor, newest


# ---------- API calls ----------

def user(conf, state, handle):
    data = gql(conf, state, "UserByScreenName", {"screen_name": handle.lstrip("@"), "withSafetyModeUserFields": True})
    u = next(filter(None, map(user_of, walk(data))), None)
    if not u:
        log.warning("x: @%s not found (reply had no user)", handle.lstrip("@"))
        raise RuntimeError(f"X: no account @{handle.lstrip('@')}")
    log.info("x: @%s is account %s (%s)", u["handle"], u["id"], u["name"])
    return u


def following(conf, state, uid, limit=None, pages=50, progress=None):
    """The accounts uid follows, newest-followed first, up to limit (None = all, bounded by pages)."""
    return follow_list(conf, state, uid, "Following", limit, pages, progress)


def follow_list(conf, state, uid, op="Following", limit=None, pages=50, progress=None):
    """op "Following" = who uid follows, "Followers" = who follows uid; newest first, up to limit."""
    out, cursor = [], None
    for _ in range(pages):
        v = {"userId": uid, "count": 100, "includePromotedContent": False}
        if cursor:
            v["cursor"] = cursor
        got, nxt = users(gql(conf, state, op, v))
        new = [u for u in got if u["id"] not in {o["id"] for o in out}]
        out += new
        log.info("x: %s of %s: +%d accounts (%d so far)%s", op.lower(), uid, len(new), len(out), "" if nxt else ", no more pages")
        if progress:
            progress(len(out))
        if not new or not nxt or nxt == cursor or (limit and len(out) >= limit):
            break
        cursor = nxt
    return out[:limit] if limit else out


def user_tweets(conf, state, uid, handle=""):
    """One page of an account's tweets (own + retweets) -> (posts with media, newest tweet id)."""
    data = gql(conf, state, "UserTweets", {"userId": uid, "count": 40, "includePromotedContent": False,
                                           "withQuickPromoteEligibilityTweetFields": False, "withVoice": True})
    posts, _, newest = tweets(data, by=handle)
    log.info("x: @%s: %d media posts on the latest page (%d retweets)", handle or uid, len(posts), sum(1 for p in posts if p.get("rt_by")))
    return posts, newest


class MediaSource(Source):
    """An artist tab: their media tab (own posts only), paged by cursor, one budgeted request per page."""
    label = "X"

    def __init__(self, conf, uid, handle=""):
        self.conf, self.uid, self.handle, self.cursor, self.state = conf, uid, handle, None, load_state()

    def fetch(self, page):
        if page == 1:
            self.cursor = None
        elif not self.cursor:
            return []
        v = {"userId": self.uid, "count": 40, "includePromotedContent": False, "withClientEventToken": False,
             "withBirdwatchNotes": False, "withVoice": True}
        if self.cursor:
            v["cursor"] = self.cursor
        posts, cursor, _ = tweets(gql(self.conf, self.state, "UserMedia", v))
        self.cursor = cursor if posts and cursor != self.cursor else None
        log.info("x: media of @%s page %d: %d posts%s", self.handle or self.uid, page, len(posts), "" if self.cursor else ", last page")
        return posts


# ---------- state: x_state.json next to config.json (0600, written atomically) ----------

STATE_DEFAULTS = {"seed": {}, "follows": [], "graph": {"built": 0, "next": 0, "counts": {}, "users": {}, "ring2": []},
                  "checked": {}, "newest": {}, "posts": [], "ops": {}, "bulk": None}
STATE_LOCK = threading.RLock()


def load_state():
    st = json.loads(json.dumps(STATE_DEFAULTS))
    try:
        st.update(json.loads(STATE_PATH.read_text()))
    except (FileNotFoundError, ValueError):
        pass
    return st


def save_state(st):
    with STATE_LOCK:
        STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = STATE_PATH.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(st, f)
        os.replace(tmp, STATE_PATH)


def seed(conf, state, refresh=False):
    """Ring 1: the main's following list (read by the alt). Re-read with the graph, or when the handle changed."""
    h = conf["x"]["seed_handle"].lstrip("@").lower()
    if not h:
        raise RuntimeError(NO_SEED)
    if not refresh and state["seed"].get("handle") == h and state["follows"]:
        return state["follows"]
    log.info("x: reading @%s's following list (ring 1)", h)
    me = user(conf, state, h)
    state["seed"] = {"handle": h, "id": me["id"]}
    state["follows"] = following(conf, state, me["id"])
    save_state(state)
    log.info("x: @%s follows %d accounts", h, len(state["follows"]))
    return state["follows"]


def rank_ring2(counts, users_, exclude, min_overlap=MIN_OVERLAP, top=RING2_MAX):
    """Overlap counts -> the top accounts followed by >= min_overlap of your follows, best first."""
    best = sorted(((n, uid) for uid, n in counts.items() if n >= min_overlap and uid not in exclude), key=lambda x: (-x[0], x[1]))
    return [{**users_[uid], "n": n} for n, uid in best[:top]]


def graph_due(state, now=None):
    g = state["graph"]
    return g["next"] > 0 or (now or time.time()) - g["built"] > REBUILD_DAYS * 86400


def build_ring2(conf, state, progress=lambda done, total: None):
    """Read the first RING2_DEPTH entries of each follow's following list and rank the overlap.
    Resumable: after a pause or quit it continues at state["graph"]["next"]."""
    g = state["graph"]
    if g["next"] == 0:
        follows = seed(conf, state, refresh=True)
        g.update(counts={}, users={})
    follows = state["follows"]
    exclude = {state["seed"]["id"], conf["x"].get("alt_id", ""), *(f["id"] for f in follows)}
    log.info("x: graph: reading the follows of %d accounts, starting at #%d", len(follows), g["next"] + 1)
    for i in range(g["next"], len(follows)):
        progress(i, len(follows))
        log.info("x: graph %d/%d: @%s", i + 1, len(follows), follows[i]["handle"])
        try:
            theirs = following(conf, state, follows[i]["id"], limit=RING2_DEPTH, pages=4)
        except Paused:
            raise
        except RuntimeError as e:  # protected / gone account: skip it, keep going
            log.warning("x: skipped @%s's follows (%s)", follows[i]["handle"], e)
            theirs = []
        for u in theirs:
            g["counts"][u["id"]] = g["counts"].get(u["id"], 0) + 1
            g["users"][u["id"]] = u
        g["next"] = i + 1
        save_state(state)
    g["ring2"] = rank_ring2(g["counts"], g["users"], exclude)
    g.update(built=time.time(), next=0, counts={}, users={})  # keep only the ranking
    save_state(state)
    log.info("x: ring 2 built, %d accounts", len(g["ring2"]))
    return g["ring2"]


def due(accounts, checked, n=BATCH):
    """The n accounts checked longest ago (never checked first)."""
    return sorted(accounts, key=lambda a: (checked.get(a["id"], 0), a["id"]))[:n]


def check_batch(conf, state, accounts, ring, n=BATCH, got=lambda posts: None):
    """Check the n least recently checked accounts; new media posts (newer than what each account showed
    before) go to got() as they arrive and into the state's post cache. -> number of new posts."""
    total = 0
    batch = due(accounts, state["checked"], n)
    log.info("x: ring %d: checking %d of %d accounts (least recently checked first)", ring, len(batch), len(accounts))
    for a in batch:
        posts, newest = user_tweets(conf, state, a["id"], a["handle"])
        last = int(state["newest"].get(a["id"], 0))
        have = {p["id"] for p in state["posts"]}
        new = [{**p, "ring": ring} for p in posts if p["id"] not in have and (not last or int(p["id"]) > last or p.get("rt_by"))]
        with STATE_LOCK:
            state["checked"][a["id"]] = time.time()
            state["newest"][a["id"]] = str(max(newest, last))
            state["posts"] = sorted(new + state["posts"], key=lambda p: -int(p["id"]))[:KEEP_POSTS]
            save_state(state)
        total += len(new)
        log.info("x: checked @%s: %d new (of %d on the page)", a["handle"], len(new), len(posts))
        if new:
            got(new)
    log.info("x: checked %d ring-%d accounts, %d new", min(n, len(accounts)), ring, total)
    return total


# ---------- cookie login (JDownloader style: copy the cookies with the Flag Cookies extension, paste) ----------

X_DOMAINS = ("x.com", "twitter.com")


def _json_in(text):
    """The JSON in text, also when a heading line or other text surrounds it; None if there is none."""
    try:
        return json.loads(text)
    except ValueError:
        pass
    starts = [i for i in (text.find("["), text.find("{")) if i >= 0]
    end = max(text.rfind("]"), text.rfind("}"))
    if starts and end > min(starts):
        try:
            return json.loads(text[min(starts):end + 1])
        except ValueError:
            return None
    return None


def parse_cookies(text, domains=X_DOMAINS):
    """Pasted cookies -> {name: value} for `domains` (default x.com / twitter.com). Takes the Flag Cookies /
    EditThisCookie / Cookie-Editor JSON export (a list of {name, value, domain}, maybe wrapped in an object),
    Netscape cookies.txt lines, or a raw "Cookie: a=b; c=d" header. Cookies of other domains are dropped."""
    text = (text or "").strip()
    ok = lambda dom: not dom or any(dom.lstrip(".").endswith(d) for d in domains)  # noqa: E731
    data = _json_in(text)
    if data is not None:
        # cookie objects ({name, value, domain…}) at any depth: a list (EditThisCookie, Cookie-Editor), a wrapper object,
        # or Flag Cookies' {"userAgent": …, ".x.com": {".x.com": {"auth_token": {name, value, …}}}}
        found = [c for c in walk(data) if isinstance(c.get("name"), str) and "value" in c and ok(str(c.get("domain", "")))]
        if found:
            return {c["name"]: str(c["value"]) for c in found}
        if isinstance(data, dict):  # a plain {name: value} object
            return {k: str(v) for k, v in data.items() if isinstance(v, (str, int)) and k != "userAgent"}
        return {}
    out = {}
    for line in text.splitlines():  # Netscape cookies.txt
        line = line.strip()
        if line.startswith("#HttpOnly_"):
            line = line[len("#HttpOnly_"):]
        f = line.split("\t")
        if len(f) >= 7 and not line.startswith("#") and ok(f[0]):
            out[f[5]] = f[6]
    if out:
        return out
    header = re.sub(r"^\s*cookie\s*:\s*", "", text, flags=re.I)  # a raw Cookie header
    for part in header.split(";"):
        if "=" in part:
            k, v = part.split("=", 1)
            out[k.strip()] = v.strip()
    return out


def export_user_agent(text):
    """The browser's user agent when the export carries one (Flag Cookies does): requests then look like that browser."""
    data = _json_in((text or "").strip())
    ua = data.get("userAgent") if isinstance(data, dict) else None
    return ua if isinstance(ua, str) and ua.startswith("Mozilla/") else ""


def cookie_login(conf, text):
    """Store auth_token + ct0 (+ the alt's own id from twid, + the browser's user agent if exported) from pasted
    cookies -> summary text."""
    cookies = parse_cookies(text)
    missing = [n for n in ("auth_token", "ct0") if not cookies.get(n)]
    if missing:
        found = ", ".join(sorted(cookies)[:12]) or "nothing that looks like cookies"
        raise RuntimeError(f"no {' / '.join(missing)} in the pasted cookies (logged in on x.com in that browser?). Found: {found}")
    m = re.search(r"u=(\d+)", urllib.parse.unquote(cookies.get("twid", "")))
    ua = export_user_agent(text)
    with CONF_LOCK:
        conf["x"].update(auth_token=cookies["auth_token"], ct0=cookies["ct0"], alt_id=m[1] if m else "")
        if ua:
            conf["x"]["ua"] = ua
    summary = "auth_token, ct0" + (f", alt account id {m[1]}" if m else "") + (", your browser's user agent" if ua else "")
    log.info("x: cookies pasted: %s (%d cookies in the paste)", summary, len(cookies))
    return summary


# ---------- bulk: one @handle's media tab, optionally with its following / followers ----------

def bulk_plan(conf, state, handle, following_max=0, followers_max=0, max_posts=0):
    """Resolve the accounts of a bulk run into state["bulk"] (the handle first, then its following and its
    followers, each capped, deduped, never the alt) -> the queue."""
    me = user(conf, state, handle)
    accts = [me]
    if following_max:
        accts += follow_list(conf, state, me["id"], "Following", limit=following_max)
    if followers_max:
        accts += follow_list(conf, state, me["id"], "Followers", limit=followers_max)
    alt, seen, queue = conf["x"].get("alt_id", ""), set(), []
    for a in accts:
        if a["id"] not in seen and a["id"] != alt:
            seen.add(a["id"])
            queue.append({"id": a["id"], "handle": a["handle"]})
    state["bulk"] = {"handle": me["handle"], "accounts": queue, "i": 0, "cursor": None, "done_posts": 0, "max_posts": max_posts,
                     "acct": {"new": 0, "skip": 0, "fail": 0}, "stats": {"new": 0, "skip": 0, "fail": 0, "posts": 0},
                     "started": time.time(), "finished": False}
    save_state(state)
    log.info("x: bulk @%s planned: %d accounts (the handle%s%s), %s", me["handle"], len(queue),
             f" + following, max {following_max}" if following_max else "",
             f" + followers, max {followers_max}" if followers_max else "", f"max {max_posts} posts each" if max_posts else "all posts")
    return queue


def bulk_run(conf, state, root, cancel, progress=lambda b: None, download=download_post, workers=3,
             on_file=lambda post, account: None):
    """Work through state["bulk"]: each account's media tab page by page, files `workers` at a time into
    <root>/x/<account>/. on_file(post, account) fires the moment a post's files are on disk (new or already
    there), so the UI can show it right away. Saved after every page, so it continues where it stopped."""
    b = state["bulk"]
    while b["i"] < len(b["accounts"]) and not cancel.is_set():
        a = b["accounts"][b["i"]]
        v = {"userId": a["id"], "count": 40, "includePromotedContent": False, "withClientEventToken": False,
             "withBirdwatchNotes": False, "withVoice": True}
        if b["cursor"]:
            v["cursor"] = b["cursor"]
        try:
            posts, cursor, _ = tweets(gql(conf, state, "UserMedia", v))
        except Paused:
            raise
        except RuntimeError as e:  # protected / deleted / suspended account: skip it
            log.warning("x: bulk: skipping @%s (%s)", a["handle"], e)
            b.update(i=b["i"] + 1, cursor=None, done_posts=0, acct={"new": 0, "skip": 0, "fail": 0})
            save_state(state)
            continue
        if b["max_posts"]:
            posts = posts[:max(0, b["max_posts"] - b["done_posts"])]
        with ThreadPoolExecutor(workers) as ex:
            futs = {ex.submit(download, p, root, a["handle"], cancel): p for p in posts}
            for f in as_completed(futs):  # in the order they finish, not the order they started
                try:
                    n, sk = f.result()
                    b["acct"]["new"] += n
                    b["acct"]["skip"] += sk
                    if n + sk:
                        on_file(futs[f], a["handle"])
                except Exception as e:
                    b["acct"]["fail"] += 1
                    log.warning("x: bulk: a file of @%s failed: %s", a["handle"], e)
        if cancel.is_set():  # stopped mid-page: this page runs again next time (files already on disk are skipped)
            break
        b["done_posts"] += len(posts)
        log.info("x: bulk @%s (account %d/%d): page of %d posts, %d posts so far", a["handle"], b["i"] + 1, len(b["accounts"]),
                 len(posts), b["done_posts"])
        if not posts or not cursor or cursor == b["cursor"] or (b["max_posts"] and b["done_posts"] >= b["max_posts"]):
            st = b["acct"]
            log.info("x: bulk: @%s done: %d posts, %d files new, %d already had, %d failed", a["handle"], b["done_posts"],
                     st["new"], st["skip"], st["fail"])
            for k in ("new", "skip", "fail"):
                b["stats"][k] += st[k]
            b["stats"]["posts"] += b["done_posts"]
            b.update(i=b["i"] + 1, cursor=None, done_posts=0, acct={"new": 0, "skip": 0, "fail": 0})
        else:
            b["cursor"] = cursor
        save_state(state)
        progress(json.loads(json.dumps(b)))
    if b["i"] >= len(b["accounts"]) and not b["finished"]:
        b["finished"] = True
        save_state(state)
        st = b["stats"]
        log.info("x: bulk @%s finished: %d accounts, %d posts, %d files new, %d already had, %d failed (%.0f min)", b["handle"],
                 len(b["accounts"]), st["posts"], st["new"], st["skip"], st["fail"], (time.time() - b["started"]) / 60)
    return b
