# vibecoded app, use at your will

Browser and downloader in two sections:
- **Boorus**: search any number of booru sites together. Any site running one of the supported engines can be added by URL:

  | Engine | For example |
  |---|---|
  | Danbooru | danbooru, aibooru |
  | Gelbooru 0.2 | gelbooru, rule34.xxx, safebooru.org, xbooru, tbib |
  | Moebooru | konachan, yande.re, sakugabooru |
  | e621 | e621, e926 |
  | Philomena | derpibooru, furbooru |

- **Comics**: E-Hentai and Hitomi galleries, searched together.
- **Pixiv**: Pixiv's own search filters, rankings, trending tags, recommended, the works of artists you follow, your bookmarks, and artist / series pages, with bookmark and follow buttons that act on your account.

PySide6 + Python stdlib.

## Which way to run it
| You're on | Do this |
|---|---|
| **Linux** (including Bazzite, SteamOS, Fedora Atomic) | Download the **AppImage** from Releases: nothing to install. See [Linux AppImage](#linux-appimage-no-install). |
| **Windows** | Install Python, then double-click `imagesearcher.py`. See [Windows](#windows-no-terminal-needed). |
| **NixOS / Nix** | `nix run github:acquevoor/imagesearcher` |
| **Linux with Python, from source** | `python3 imagesearcher.py` in this folder. It installs PySide6 with pip on first run. |

`imagesearcher.py` is the only file you start. The files in `appimage/` are build tools, not the app.

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
  - Middle-click works the same on tags, tag lists, creator lists and links: the tab opens behind, you stay where you are.
  - In an image tab: ←/→ walk the grid, PgUp/PgDn flip pages, D download, C copy the image (a Pixiv animation is copied as a GIF), R reload (retries what failed), L copy the link, F favorite, B site favourite, Esc back to the grid, Ctrl+W close, Ctrl+Tab cycle.
- **Copy link**: L on a selected thumbnail or in an image tab, the 🔗 button, or right-click a thumbnail → Copy link.
- **Select several thumbnails** by click+dragging across them (or Ctrl / Shift+click, Ctrl+A). Right-click the selection to copy the images (as files) or the links (one per line), download them, ♥ or ★ them all, or open them in background tabs; C, L, F and B act on the whole selection too.
- **Hotkeys** are configurable: ⚙ Settings → ⌨ Hotkeys… lists every shortcut; changes apply at once, open tabs included.
- **Two kinds of favourite**, on every thumb's top-right corner (hover to see them) and as keys on the selected thumb or open image:
  - ♥ **F**: ImageSearcher's own favourite (the file goes in your favorites folder; works for every site).
  - ★ **B**: the site's own favourite, on your account there: Pixiv bookmarks, Danbooru / e621 / Moebooru favourites (your login + API key), Gelbooru / Rule34 favourites (log in through ⚙ → Fetch keys…: their API key can't do it, so the app keeps their login cookie in your private config), Pawchive favourites (pasted session). Not on X (read-only) or Imageboard.
  - Booru sites don't tell the app what you favourited on the website, so ★ shows filled only for what you favourited from here.
  - Multi-page works get **▤ Strip view**: all pages stacked to scroll through, for manga. Ctrl+wheel or −/+ zooms out to up to 8 pages a row. Click a page to copy it; click+drag across pages (or Shift+click) copies the range, Ctrl+click adds or removes one (several pages are copied as files). Pages still loading show a turning ◐.
  - Each section's **Favorites** tab sorts your ♥ favourites into categories by their tags (the category box, most used tags first) and searches them by tag: every word must be part of a tag, `-word` excludes.
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
- **Rankings** (daily … monthly, male / female, original, rookie, AI, manga, and the R-18 ones, for any past date), **Trending** tags, **Recommended** (with a maturity filter: All / SFW only / R-18 only. Pixiv never puts R-18 works in its recommendations, so R-18 only shows works related to your R-18 bookmarks).
- **Following**: new works from the artists you follow, with an unread count. **Bookmarks**: yours, public or private.
- Image tabs:
  - ★ **Bookmark on Pixiv** (B), or the ★ on the thumb
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
- **⬇ Bulk @handle…** downloads every photo, video and GIF from an account's media tab into `<download dir>/x/<account>/`. Tick **include the accounts it follows** and/or **include its followers** to add those accounts too, each with its own limit, plus an optional limit on posts per account. Each image appears in a live **⬇ @handle** tab the moment its file is saved, so you can open it while the run goes on. Runs pick up where they stopped: after quitting, a lock or **Cancel bulk**, press **Continue bulk** on the banner. Files already on disk are skipped. Files download 3 at a time.
- **Timeline** shows media from the accounts your **main** account follows. Set your main @handle in ⚙; its following list must be public.
- **Discover** shows accounts that at least 3 of your follows also follow, up to 150 of them. **Build graph** reads the first 400 follows of each of your follows.
- **@handle** in the search box opens that account's media tab.

**Pace** (⚙ → X pace)
- **JDownloader (fast)**, the default: requests go about a second apart. When X's own rate limit runs out (its `x-rate-limit-*` headers say so), the app waits until X resets it and carries on. A 429 reply also waits for the reset and is retried once.
- **Careful (slow)**: requests go 15–25 s apart, at most 150 an hour. A rate-limit reply pauses X for the time you set (randomized, up to 6 h).
- At either pace, a "locked" or "suspended" reply stops all X traffic until you press **Resume X**.

**Sensitive media** gets `rating:e`, so blacklist `rating:e` to hide it. Turn on "Display media that may contain sensitive content" in the alt's X settings.

## Reader
What the wikis say about a tag or character. Type it in any form (`hakurei_reimu`, `Reimu Hakurei`, `初音ミク`), or right-click a tag on any image → **📖 Read in Reader**. Booru images also get a 📖 button for their characters.

The list on the left shows what was found: the **Pixiv encyclopedia** summary, the wikis of your ticked **booru sites**, then **Fandom** articles from all wikis. The best hit opens by itself. Links inside an article stay in the tab (◀ ▶), and related tags open a new one.

- **Full article** opens the whole Pixiv encyclopedia page in your browser; `dic.pixiv.net` is behind Cloudflare's browser check.
- **Fandom images** also sit behind that check. The text always loads. For the pictures, use ⚙ → **☁ Pass Cloudflare…** and pick `static.wikia.nocookie.net`.
- **Booru wikis** need a site that's up, and they also supply the Japanese name for the Pixiv lookup. If Danbooru is down, English tags may find no Pixiv entry; the status line says which sources had nothing.

## Pawchive
Pawchive (pawchive.st, now **pawchive.pw**) is a Kemono-style archive of creators' paywalled posts from Patreon, Fanbox and similar sites. One post is one tile, and its images and videos are the tile's pages (PgUp/PgDn, ▤ strip view).

**Browsing**
- **Recent** shows the latest posts, and the search box searches them; type `@name` to open an artist.
- **Popular** shows the top posts of a day, week or month; pick an end date.
- **Artists** lists every creator: filter by name or service, sort by updated, favorites or name. The list is about 16 MB and is downloaded at most once a day.
- **Tags** lists the site's 2,000 most used tags.
- 🎲 opens a random post or a random artist.

**Creator tab:** the creator's posts, which you can search or filter by their tags; links to their accounts on other sites; **Announcements**; **Fancards** (Fanbox only); **♥ Favorite**; and **⬇ Bulk creator**.

**⬇ Bulk creator** saves every post into `<download dir>/pawchive/<creator> (<service>)/<post> - <title>/`, under the original filenames. Choose "Images and videos" or "Everything", which also includes zip and psd attachments. It downloads 3 posts at a time, and images show in a live tab as they're saved. Use **Continue bulk** to resume where it stopped.

**On an image:** 📄 **Post details** shows the full text, the other attachments, comments, revisions and the previous/next post. 🔍 **Same file elsewhere** looks the file up by its hash, ⚑ **Flag post** asks Pawchive to re-import it. ★ / B adds it to your account's favourites.

**🔍 Find by hash…** checks which posts hold the files you pick from your disk.

**"Preview only"** pages are posts where the archive kept only a small preview of a file.

**Account (optional)**: account favorites need you to be logged in.
1. Log in on pawchive.pw in your browser.
2. Copy the cookies with the **Flag Cookies** extension.
3. Press **📋 Paste cookies**, then **Test login** in ⚙. Only the `session` cookie is kept, and it's never logged.

**Importer** opens the site's own importer page in your browser; the app never touches your Patreon or Fanbox keys.

**Thumbnails need WebP support.** Pawchive's thumbnails are WebP files. The Nix package and the Windows PySide6 install include it. If thumbnails show blank, the log will say that WebP support is missing.

## Comics
**E-Hentai** and **Hitomi**, side by side (tick either or both on the bar). NSFW. No accounts needed, nothing is logged in.

- **Latest**, **Popular** (E-Hentai's popular page, Hitomi's popular this week) and **Favorites** (your ♥), plus a tab per search or tag.
- **One search syntax for both sites**: words and `namespace:tag` terms, `_` for spaces, `-term` excludes. For example `female:big_breasts artist:shindol language:english -male:yaoi`. Namespaces: female, male, mixed, other, artist, group, parody / series, character, language, type. On Hitomi a plain word is looked up as a tag, not as title text.
- **A gallery is one tile**; open it and turn pages with PgUp/PgDn, or ▤ strip view to read it scrolling (zoom out, click / drag pages to copy them). Tags open a search, and the gallery's artist and series get quick-search buttons.
- **E-Hentai** pages are the site's normal-size images (about 1280 px wide), fetched only when you open them. E-Hentai limits how many images one address can view; past it, pages fail with "image limit reached" until it resets. ExHentai and site favourites need an account and aren't supported.
- Downloads go to `<download dir>/eh/` or `hitomi/`, one file per page.

## Imageboard (opt-in)
Browse 4chan's **/trash/**, **/d/** and **/h/**, live and through the archives. Switch it on in ⚙, then restart. These boards are NSFW.

- **Board tabs** (`/trash/`, `/d/`, `/h/`): switch between **Live (4chan)**, which shows every thread on the board right now, and **Archive**, which shows recently bumped threads page by page. Each tile is a thread; open one to get a **thread tab** with every image in it.
- **Thread tab**: ☆ **Watch**, ⬇ **Bulk thread**, **Open on site** and ⟳ Refresh. A thread that has disappeared from 4chan is read from its archive instead.
- **Search** (Enter in the search box) searches the archives by text. Under **Filters ▾** you can tick boards and add a subject, filename, tripcode, "images only" or a date range.
- **Watched** shows new images in the threads you watch since you last marked them seen.
- **⬇ Bulk thread** saves the thread's images 3 at a time into `<download dir>/imageboard/<board>/<thread no> - <subject>/`, under the poster's **original filenames**. A different file with the same name gets ` (<post no>)` added. Each image appears in a live tab as soon as it's saved. ⚙ can switch filenames to 4chan's timestamp names.
- **Archives** (⚙ → Boards → archive, one `board host` per line): by default desuarchive.org serves /trash/ and /d/, and archiveofsins.com serves /h/.
- **Cloudflare**: archived.moe, and archiveofsins' search, show Cloudflare's browser check. Press **☁ Pass Cloudflare…** (on the Imageboard bar or in ⚙) and choose the archive. A small browser window opens, passes the check, keeps the cookie and closes, like JDownloader. Do it again when the pass expires; the error message will tell you.

## Everything else

- ⬇ Bulk downloads everything in the current tab to `{download dir}/{site}/{tab}/` and skips files you already have.
- Blacklist (⚙) applies to both sections and accepts tags plus `rating:g|s|q|e`.
- Every action is logged in the console window. Tick **History** to also keep one log file per day in the `history` folder next to `imagesearcher.py` (📂 opens it). Keys and tokens are never logged.
- Config is `~/.config/imagesearcher/config.json` (mode 0600, holds your keys). A config from version 1 is converted on first start; the old file is kept as `config.v1.json`.

## Linux AppImage (no install)
Download `imagesearcher-x86_64.AppImage` from the repo's **Releases** page, make it executable (`chmod +x imagesearcher-x86_64.AppImage`, or tick "Allow executing" in your file manager's Properties) and run it.

It's the same app as every other version, and it carries what a desktop would otherwise have to provide: Python, Qt, the built-in browser, its own libraries and fonts (Japanese and emoji included), and the dark look, so it appears the same on every distro. It needs only a Linux desktop with glibc 2.35 or newer (Ubuntu 22.04+, Debian 12+, recent Fedora, Arch, Steam Deck). Each release is tested on Ubuntu 24.04, Debian 12, Fedora and Arch.

- **Settings** start fresh on a new machine. X and Imageboard are off until you tick them in ⚙, and keys and logins are empty. To bring your own, copy `~/.config/imagesearcher/` across; it holds your keys, so never share it.
- **Videos** play in mpv if it's installed, otherwise in your browser.
- **History logs** go to `~/.config/imagesearcher/history/` (📂 opens the folder), because an AppImage can't write next to itself.

To build it yourself, run `appimage/build.sh` on a Debian or Ubuntu machine (on NixOS: `steam-run appimage/build.sh`); `.github/workflows/appimage.yml` lists the packages it needs. On GitHub, **Actions → AppImage → Run workflow** builds and tests it, and pushing a `v*` tag publishes it on that release once all four distros pass.

## Updates
Every copy updates itself when it starts, so there's nothing to re-download:
- A `git clone` runs `git pull`.
- The ZIP, the AppImage and the Nix package download the latest `imagesearcher_app/` from this repository's `master` into `~/.config/imagesearcher/code/` and run it from there.

If you're offline, or an update is broken, the app starts with the version it has. This runs code from this repository on your machine each time it changes.

## Windows (no terminal needed)
1. Install Python from https://www.python.org/downloads/ . In the installer, tick **"Add python.exe to PATH"** and click *Install Now*.
2. On this GitHub page click the green **Code** button → **Download ZIP**, then right-click the ZIP → **Extract All**.
3. Double-click **`imagesearcher.py`** in the extracted folder. Keep the `imagesearcher_app` folder next to it.
   - The first start installs PySide6 by itself (a black window shows progress for a minute or two). Later starts open right away.
   - If Windows asks which app should open the file, choose **Python**.
   - Leave the black window open while you use the app; closing it closes the app.
4. Videos open in your web browser unless you have [mpv](https://mpv.io) installed.

Settings and favorites are saved in `C:\Users\<you>\.config\imagesearcher\`, and downloads go to `Pictures\imagesearcher`.

Updates are automatic, see [Updates](#updates).

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
