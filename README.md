# imagesearcher

Danbooru-first tag / artist browser and downloader, with Gelbooru, Rule34.xxx and Pixiv folded in. One file, PySide6 + stdlib.

    nix run github:acquevoor/imagesearcher
    nix flake check            # runs the offline --selftest

## Install via flake

```nix
# flake.nix
inputs.imagesearcher.url = "github:acquevoor/imagesearcher";

# home-manager (or environment.systemPackages)
home.packages = [ inputs.imagesearcher.packages.${pkgs.stdenv.hostPlatform.system}.default ];
```

Includes a desktop entry. Without Nix: `python imagesearcher.py` with PySide6 installed.

Danbooru is the backbone, and Pixiv is reached through it:
- A booru tag is sent to Pixiv as its Pixiv name, taken from the tag's Danbooru wiki (`hatsune_miku` → `初音ミク`). The status line shows `PX via "…"`.
- Clicking a Japanese Pixiv tag opens the booru tag whose wiki lists it.
- Artists are linked through Danbooru's artist URLs.
- The same image found on two sites shows as one tile (`DB+PX`). The booru copy wins because its tags are better.

Two rows of tabs, like a browser:
- **Top row**: tags, artists, pools, Search, New, Favorites.
- **Second row**: inside each top tab, its ▦ Grid plus one tab per image you open.
  - Double-click a thumb to open it, Ctrl+double-click or middle-click to open it in the background.
  - In an image tab: ←/→ walk the grid, PgUp/PgDn flip pages, D download, F favorite, Esc back to the grid, Ctrl+W close, Ctrl+Tab cycle.
  - Image tabs keep their picture when the grid reloads.

Top tabs:
- **Tags**: browse Danbooru tags (or Gelbooru / R34) by category, count, name or date, with a Pixiv-name column. Pick **Pools** for Danbooru pools. Selecting a tag shows its wiki, Pixiv names and related tags. Double-click opens it in a tab.
- **Tag tabs**: one per tag, closable. They show the boorus and Pixiv side by side, or one of them, sorted newest / score (Pixiv popular) / random, filtered by rating. Click a related tag to narrow the tab, Ctrl+click to open it in its own tab. Click a Pixiv name to pin the Pixiv side to that word.
- **Artist tabs** (artist tags, shown red): the booru posts next to the artist's Pixiv works. Works already on Danbooru are marked ✓DB. Tick the checkbox to show only the ones that are missing.
- **New**: ☆ Follow a tag or an artist, and their posts newer than the last ones you saw collect here, with an unread count on the tab. ✓ Mark all seen resets it. The tab checks every 15 min while the app runs.
- **Search**: the free query box (several sites = merged results). **Favorites**.

- Image tabs: tags are coloured by category. Double-click a tag to open its tab; right-click for search / add / exclude / follow / blacklist. On Pixiv posts: *Find on Danbooru*, *More like this*. Pixiv animations (ugoira) play in place and download as frames `.zip` + delays `.json`.
- ⬇ Bulk downloads everything in the current tab to `{download dir}/{site}/{tab}/` and skips files you already have.
- An anonymous Danbooru search allows only 2 tags, and `order:` counts as one. A Danbooru API key raises the limit.
- Blacklist (⚙) accepts tags plus `rating:g|s|q|e`.
- Every action is logged in the console window. Tick **History** to also keep one log file per day in the `history` folder next to `imagesearcher.py` (📂 opens it). Keys and tokens are never logged.
- Config is `~/.config/imagesearcher/config.json` (mode 0600, holds your keys).

## Windows (no terminal needed)
1. Install Python from https://www.python.org/downloads/ . In the installer, tick **"Add python.exe to PATH"** and click *Install Now*.
2. On this GitHub page click the green **Code** button → **Download ZIP**, then right-click the ZIP → **Extract All**.
3. Double-click **`imagesearcher.py`** in the extracted folder.
   - The first start installs PySide6 by itself (a black window shows progress for a minute or two). Later starts open right away.
   - If Windows asks which app should open the file, choose **Python**.
   - Leave the black window open while you use the app; closing it closes the app.
4. Videos open in your web browser unless you have [mpv](https://mpv.io) installed.

Settings and favorites are saved in `C:\Users\<you>\.config\imagesearcher\`, and downloads go to `Pictures\imagesearcher`.

To update, download the ZIP again and replace the folder (your settings are kept).

## Credentials
- **Rule34 / Gelbooru** (required): My Account → Options → *API Access Credentials* → copy `api_key` and `user_id` into ⚙.
- **Danbooru** (optional, needed for >2 tags): My Account → API Key.
- **Pixiv** (required): needs an OAuth *refresh token*. One-time setup:
  `nix shell nixpkgs#python3Packages.pip -c pip install --user gppt && gppt login-interactive`
  (or the browser-devtools method from https://gist.github.com/ZipFile/c9ebedb224406f4f11845ab700124362).
  Paste the `refresh_token` into ⚙. R-18 results need R-18 enabled in your Pixiv account settings.
