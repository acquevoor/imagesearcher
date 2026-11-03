# imagesearcher

Browser and downloader in two sections:
- **Boorus**: search any number of booru sites together. Any site running one of the supported engines can be added by URL:

  | Engine | For example |
  |---|---|
  | Danbooru | danbooru, aibooru |
  | Gelbooru 0.2 | gelbooru, rule34.xxx, safebooru.org, xbooru, tbib |
  | Moebooru | konachan, yande.re, sakugabooru |
  | e621 | e621, e926 |
  | Philomena | derpibooru, furbooru |

- **Pixiv**: Pixiv's own search filters, rankings, trending tags, recommended, the works of artists you follow, your bookmarks, and artist / series pages, with bookmark and follow buttons that act on your account.

PySide6 + Python stdlib.

    nix run github:acquevoor/imagesearcher
    nix flake check            # runs the offline --selftest

## Install via flake

```nix
# flake.nix
inputs.imagesearcher.url = "github:acquevoor/imagesearcher";

# home-manager (or environment.systemPackages)
home.packages = [ inputs.imagesearcher.packages.${pkgs.stdenv.hostPlatform.system}.default ];
```

Includes a desktop entry. Without Nix: `python imagesearcher.py` with PySide6 installed. The app lives in the `imagesearcher_app/` folder next to it.

## The window

**Boorus | Pixiv** at the top switches section (Ctrl+1 / Ctrl+2). Each section has its own search bar and two rows of tabs, like a browser:
- **Top row**: one tab per search, tag, artist, pool or Pixiv user, plus the section's fixed tabs. Searching the same thing again switches to its tab.
- **Second row**: inside each top tab, its ▦ Grid plus one tab per image you open.
  - Double-click a thumb to open it, Ctrl+double-click or middle-click to open it in the background.
  - In an image tab: ←/→ walk the grid, PgUp/PgDn flip pages, D download, F favorite, Esc back to the grid, Ctrl+W close, Ctrl+Tab cycle.
  - Multi-page works get **▤ Strip view**: all pages stacked to scroll through, for manga.
  - Opening an image downloads its original into `{download dir}/cache/` and shows it from there. The next image is fetched ahead, so → is instant. **`cache/` is deleted when the app closes.**
  - ♥ moves the post's originals into `{download dir}/favorites/`, next to `cache/`, and downloads any pages you haven't opened. Un-♥ moves them back to `cache/`.

## Boorus

- **Site bar**: tick the sites to search. Results interleave and the same file on several sites shows once, labelled with every site, e.g. `KC+SB+DB 410185`.
- **Sites…**: add a site from the presets, or paste any URL and press **Detect**, which finds the engine and, for rule34, the separate API host. Edit names, tile labels and keys, and use ↑/↓ to set the order. The order is the interleave order, and the first site with a wiki supplies tag wikis.
- **Tag tabs**:
  - a header with the tag's wiki, other names and related tags (click one to narrow the tab, Ctrl+click to open it in its own tab)
  - sort by newest / score / random, and filter by rating g / s / q / e
  - ☆ Follow, and **Search on Pixiv** (once a Pixiv token is set)
- **Artist tabs** (artist tags, red): the artist's posts plus their links. **Pixiv account** opens their Pixiv page in the Pixiv section.
- **Tags**: browse a site's tags by category, count, name or date. A Pixiv column shows each tag's Japanese name. **Pools** lists pools on sites that have them.
- **New**: posts newer than the last ones you saw, for every tag or artist you follow, with an unread count. **Favorites**.
- What each site offers depends on its engine:
  - Danbooru and e621: wikis, related tags, pools, artists
  - Moebooru: the same, without other names
  - Gelbooru 0.2: tags only
  - Philomena: tags only. Its tags show with `_` in the app and are translated back to its `a, b` query syntax.
- An anonymous Danbooru search allows only 2 tags, and `order:` counts as one. A Danbooru API key raises the limit.

## Pixiv

- **Search** opens a tab per query. **Filters ▾** offers:
  - match mode: partial tags / exact tags / title & caption
  - sort: newest / oldest / popular. Popular needs Premium; without it Pixiv's one-page popular preview is used and the tab says so.
  - type: illust / manga
  - R-18: show / hide / only
  - AI works on/off
  - a minimum number of bookmarks
  - a date range
- **Rankings** (daily … monthly, male / female, original, rookie, AI, manga, and the R-18 ones, for any past date), **Trending** tags, **Recommended**.
- **Following**: new works from the artists you follow, with an unread count. **Bookmarks**: yours, public or private.
- Image tabs:
  - ☆ **Bookmark on Pixiv** / ★ Unbookmark
  - 👤 the artist's tab (works / manga / bookmarks, + Follow / ✓ Following)
  - ▤ the work's series
  - **More like this**
  - **Find on boorus**: the booru copies, usually better tagged
  - animations (ugoira) play in place and download as frames `.zip` + delays `.json`
- The local ♥ Favorite (files on disk) is separate from the Pixiv bookmark.
- A Pixiv tag's right-click menu has **Open on boorus**: the booru tag whose wiki lists that Japanese name.

## X (opt-in)
Off by default: switch it on in ⚙, then restart. The app never posts, follows, likes or bookmarks anything.

> **Use a separate (alt) account.** Reading X this way goes through the same internal API x.com itself uses,
> which is against X's terms and can get the account rate-limited or locked.

**Log in like JDownloader**
1. Install the **Flag Cookies** browser extension (JDownloader's cookie exporter). Exports from EditThisCookie, Cookie-Editor or a `cookies.txt` file work too.
2. Open x.com logged in as the alt, then use Flag Cookies to copy the cookies.
3. Press **📋 Paste cookies**, either on the X banner or in ⚙ → X login. Then press **Test login** to make one request and confirm it works.

The cookies are stored only in your config file (mode 0600) and are never logged. If you log out of x.com in that browser, the session ends; copy and paste the cookies again.

**What's in the X section**
- **⬇ Bulk @handle…** downloads every photo, video and GIF from an account's media tab into `<download dir>/x/<account>/`. Tick **include the accounts it follows** and/or **include its followers** to add those accounts too, each with its own limit, plus an optional limit on posts per account. Runs pick up where they stopped: after quitting, a lock or **Cancel bulk**, press **Continue bulk** on the banner. Files already on disk are skipped. Files download 3 at a time.
- **Timeline** shows media from the accounts your **main** account follows. Set your main @handle in ⚙; its following list must be public.
- **Discover** shows accounts that at least 3 of your follows also follow, up to 150 of them. **Build graph** reads the first 400 follows of each of your follows.
- **@handle** in the search box opens that account's media tab.

**Pace** (⚙ → X pace)
- **JDownloader (fast)**, the default: requests go about a second apart. When X's own rate limit runs out (its `x-rate-limit-*` headers say so), the app waits until X resets it and carries on. A 429 reply also waits for the reset and is retried once.
- **Careful (slow)**: requests go 15–25 s apart, at most 150 an hour. A rate-limit reply pauses X for the time you set (randomized, up to 6 h).
- At either pace, a "locked" or "suspended" reply stops all X traffic until you press **Resume X**.

**Sensitive media** gets `rating:e`, so blacklist `rating:e` to hide it. Turn on "Display media that may contain sensitive content" in the alt's X settings.

## Everything else

- ⬇ Bulk downloads everything in the current tab to `{download dir}/{site}/{tab}/` and skips files you already have.
- Blacklist (⚙) applies to both sections and accepts tags plus `rating:g|s|q|e`.
- Every action is logged in the console window. Tick **History** to also keep one log file per day in the `history` folder next to `imagesearcher.py` (📂 opens it). Keys and tokens are never logged.
- Config is `~/.config/imagesearcher/config.json` (mode 0600, holds your keys). A config from version 1 is converted on first start; the old file is kept as `config.v1.json`.

## Windows (no terminal needed)
1. Install Python from https://www.python.org/downloads/ . In the installer, tick **"Add python.exe to PATH"** and click *Install Now*.
2. On this GitHub page click the green **Code** button → **Download ZIP**, then right-click the ZIP → **Extract All**.
3. Double-click **`imagesearcher.py`** in the extracted folder. Keep the `imagesearcher_app` folder next to it.
   - The first start installs PySide6 by itself (a black window shows progress for a minute or two). Later starts open right away.
   - If Windows asks which app should open the file, choose **Python**.
   - Leave the black window open while you use the app; closing it closes the app.
4. Videos open in your web browser unless you have [mpv](https://mpv.io) installed.

Settings and favorites are saved in `C:\Users\<you>\.config\imagesearcher\`, and downloads go to `Pictures\imagesearcher`.

To update, download the ZIP again and replace the folder (your settings are kept).

## Credentials
**Gelbooru, Rule34 and Pixiv: the key fetcher.**
- It opens on launch while any of their keys is missing. Tick **Don't show this again** to stop that; it stays under ⚙ → **Fetch keys…**.
- **Fetch…** opens the site's own login in a small window. Log in, solving the CAPTCHA if one shows.
- The app then reads the API key + user id from your account page, or catches Pixiv's login token, checks it, and saves it.
- Your password goes only to the site. The window keeps no cookies.
- If your Gelbooru / Rule34 account has no key yet, tick *Generate new key* on the page it shows, then Save.

Doing it by hand instead:
- **Booru sites** (Boorus → Sites… → Edit):
  - Danbooru and e621: login + API key (My Account → API Key). Optional, but it lifts Danbooru's 2-tag limit and shows e621 posts hidden from guests.
  - Gelbooru and Rule34 (required): My Account → Options → *API Access Credentials*. Put `user_id` in Login and `api_key` in API key.
  - Moebooru: login + password hash (optional).
  - Philomena: your API key, plus optionally a filter id; the site's default filter hides explicit posts.
- **Pixiv** (required for the Pixiv section): use the key fetcher above. Without a built-in browser (a PySide6 build lacking QtWebEngine), Fetch… opens your browser instead:
  1. Press F12 → Network, then click Continue.
  2. Copy the URL of the `callback?state=…&code=…` request and paste it into the app within a minute.
  
  R-18 results need R-18 enabled in your Pixiv account settings.
