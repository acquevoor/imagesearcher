# imagesearcher

Desktop browser/downloader for Danbooru, Gelbooru, Rule34.xxx and Pixiv. One file, PySide6 + stdlib.

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

- Tick sites, type tags, Enter. Several sites = merged, interleaved results. Scroll for more.
- Double-click a thumb opens the viewer: ←/→ post, PgUp/PgDn Pixiv page, D download, F favorite. Double-click a tag to search it; right-click for add / exclude / blacklist.
- ⬇ Bulk downloads the whole query to `{download dir}/{site}/{query}/` and skips files you already have.
- Blacklist (⚙) accepts tags plus `rating:g|s|q|e`.
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
