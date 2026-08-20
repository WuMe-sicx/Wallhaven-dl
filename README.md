# Wallhaven-dl

## UPDATE
###The script now comes with a search functionality, you can now search and download wallpapers from the command line.

---------------------------------------------------------------------

A wallhaven scraper which downloads all the wallpapers from the first page of [alpha.wallhaven.cc](http://alpha.wallhaven.cc/)

This Script now comes with categories and purity sort support.
###### NOTE- Downloading NSFW images requires a [Wallhaven API key](https://wallhaven.cc/settings/account) (sketchy works without one). Export it before running:
```
$ export WALLHAVEN_API_KEY=your_key_here
```


![](https://raw.githubusercontent.com/GeekSpin/Wallhaven-dl/master/Images/wallhaven-dl%20(1).gif)

## How to use:
  
  1. Download the wallhaven-dl.py
  2. Move wallhaven-dl.py to the folder in which you want wallpapers to download.
  3. Optionally `cp .env.example .env` and fill it in — `WALLHAVEN_API_KEY` (NSFW only) and, if you want uploads, your Lsky Pro `LSKY_URL` / `LSKY_TOKEN`. `.env` is gitignored.
  4. run script 
  5. Pick any combination of content presets (anime / landscape), then a size (phone / desktop / any), then optionally a keyword, purity and sorting. Every axis is independent — "phone wallpapers, any subject" works too.
  6. It reports how many wallpapers matched and caps the page count accordingly, then shows a confirmation you can amend one field at a time.
  7. Downloads land in `Wallhaven/<content+size>/`, e.g. `Wallhaven/anime+phone/`. enjoy!

  With Lsky Pro configured you also get an "upload to image host" step: each download
  group is mirrored to a same-named album (created on demand), and only files not
  already in that album are uploaded — so re-running backfills anything missed.

Downloads run concurrently (pick 4, 8 or 16 workers; images are served from a
  different host than the API, so the API rate limit does not apply). You get a
  live aggregate progress bar and a summary:

```
[24/24] ████████████████████ 100%  83.8 MB  5.9 MB/s  8 路并发

完成：共 24 张，新增 24、已存在 0、失败 0
      83.8 MB，用时 14.2s，平均 5.9 MB/s（8 路并发）
      保存于 Wallhaven/动漫+手机端/
```
```
$ uv run wallhaven-dl.py
```

## Dependencies:

  This project depends on Requests only. The script carries a PEP 723 inline
  dependency block, so [uv](https://docs.astral.sh/uv/) installs it on the fly —
  nothing to set up.

  To use your own interpreter instead:
  ```
  $ pip3 install -r requirements.txt
  $ python3 wallhaven-dl.py
  ```

  **Avoid the macOS system Python** (`/usr/bin/python3`). It links LibreSSL 2.8.3,
  which urllib3 v2 no longer supports, so every run prints a `NotOpenSSLWarning`.
  Downloads still work, but prefer any interpreter built against OpenSSL 1.1.1+
  (`brew install python@3.14`, pyenv, or uv as above).

## Tests:

  ```
  $ uv run test_wallhaven_dl.py
  ```
  


Wallhaven-dl © 2016, Saurabh Bhan. Released under the [MIT License](https://raw.githubusercontent.com/GeekSpin/Wallhaven-scraper/master/LICENSE).
