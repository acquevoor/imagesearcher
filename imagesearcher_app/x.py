"""X (Twitter), opt-in and read-only: an alt account's pasted session cookies read the internal GraphQL API the
web client uses. Ring 1 = the accounts your main follows (read from its public following list), ring 2 = the
accounts at least MIN_OVERLAP of them follow. Every request goes through one slow, randomized budget, and any
rate-limit or account warning pauses all X traffic (see Budget / pause_for)."""
import json
import os
import random
import re
import threading
import time
import urllib.error
import urllib.parse

from .core import CONF_PATH, CONF_LOCK, Source, http, log, make_post, save_conf

BEARER = ("AAAAAAAAAAAAAAAAAAAAANRILgAAAAAAnNwIzUejRCOuH5E6I8xnZz4puTs%3D"
          "1Zv7ttfk8LF81IUq16cHjhLTvJu4FA33AGWWjCpTnA")  # the public token of X's own web client
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:140.0) Gecko/20100101 Firefox/140.0"
GQL = "https://x.com/i/api/graphql/"
OPS = ("UserByScreenName", "Following", "UserTweets", "UserMedia")
# Used only when reading the current ones from x.com's main.js fails; X rotates them every few weeks.
# ponytail: stale fallbacks just fail with a clear error, the main.js read is what keeps this working
FALLBACK_QIDS = {"UserByScreenName": "x3RLKWW1Tl7JgU7YtGxuzw", "Following": "zx6e-TLzRkeDO_a7p4b3JQ",
                 "UserTweets": "bbmLh6qwlhCgVTDO4R9ngA", "UserMedia": "WJhR3BD7MuJyTjd6Qhh6qA"}

GAP, JITTER, HOURLY = 15.0, 10.0, 150  # seconds between requests (+ random 0..JITTER), requests per hour
MAX_PAUSE_MIN = 360
MIN_OVERLAP, RING2_MAX, RING2_DEPTH = 3, 150, 400  # ring 2: followed by >= 3 follows, top 150, first 400 of each list
REBUILD_DAYS, BATCH, KEEP_POSTS = 7, 40, 3000
STATE_PATH = CONF_PATH.with_name("x_state.json")
NOT_SET = "X: paste the alt account's auth_token and ct0 cookies and your main @handle in ⚙ Settings"


class Paused(RuntimeError):
    """Raised instead of sending anything while X traffic is paused or the app is quitting."""


STOP = threading.Event()  # set on quit: wakes sleeping requests so the app can exit


# ---------- the request budget ----------

class Budget:
    """One request at a time, GAP + random(0..JITTER) seconds apart, at most HOURLY per rolling hour.
    The lock is held while waiting, so callers queue up in order instead of bursting."""

    def __init__(self, clock=time.time, sleep=None, rand=random.random):
        self.clock, self.rand, self.lock = clock, rand, threading.Lock()
        self.sleep = sleep or (lambda s: STOP.wait(s))
        self.last, self.stamps = 0.0, []

    def next_ok(self):
        now = self.clock()
        self.stamps = [t for t in self.stamps if now - t < 3600]
        t = max(now, self.last + GAP + JITTER * self.rand())
        if len(self.stamps) >= HOURLY:
            t = max(t, self.stamps[0] + 3600)
        return t

    def wait(self, check=lambda: None):
        with self.lock:
            check()
            t = self.next_ok()
            if t - self.clock() > 0.5:
                log.info("x: next request in %.0f s (%d of %d left this hour)", t - self.clock(), self.left(), HOURLY)
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


def ready(conf):
    c = conf["x"]
    return bool(c["enabled"] and c["auth_token"] and c["ct0"] and c["seed_handle"])


# ---------- talking to X ----------

def headers(conf):
    c = conf["x"]
    return {"Authorization": "Bearer " + BEARER, "x-csrf-token": c["ct0"], "Cookie": f"auth_token={c['auth_token']}; ct0={c['ct0']}",
            "x-twitter-auth-type": "OAuth2Session", "x-twitter-active-user": "yes", "x-twitter-client-language": "en",
            "User-Agent": UA, "Referer": "https://x.com/", "Accept": "*/*"}


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


def request(conf, url):
    """One budgeted GET to X -> parsed JSON. Warnings pause X; nothing is ever retried automatically."""
    budget.wait(lambda: check(conf))
    try:
        body = http(url, headers=headers(conf))
    except urllib.error.HTTPError as e:
        kind, msg = error_kind(e.code, e.read())
        log.warning("x: request refused: HTTP %s, %s (%s)", e.code, kind, msg)
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
        budget.wait(lambda: check(conf))
        page = http("https://x.com/", headers={"User-Agent": UA, "Cookie": headers(conf)["Cookie"]}).decode(errors="replace")
        m = MAIN_JS.search(page)
        if m:
            budget.wait(lambda: check(conf))
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
    return request(conf, url)


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
    return {"id": d["rest_id"], "handle": handle, "name": core.get("name") or legacy.get("name") or handle} if handle else None


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
    out, cursor = [], None
    for _ in range(pages):
        v = {"userId": uid, "count": 100, "includePromotedContent": False}
        if cursor:
            v["cursor"] = cursor
        got, nxt = users(gql(conf, state, "Following", v))
        new = [u for u in got if u["id"] not in {o["id"] for o in out}]
        out += new
        log.info("x: following of %s: +%d accounts (%d so far)%s", uid, len(new), len(out), "" if nxt else ", no more pages")
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
                  "checked": {}, "newest": {}, "posts": [], "ops": {}}
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
    exclude = {state["seed"]["id"], *(f["id"] for f in follows)}
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
