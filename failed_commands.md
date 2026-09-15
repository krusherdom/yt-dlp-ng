# Failed Commands Log

Format: `[n] command -- brief note on the failure`. Increment `[n]` if the same command/issue recurs again later.

[1] `py -3.12 -m venv .venv` - the `py` launcher listed a 3.12.11 runtime that no longer existed on disk (stale StabilityMatrix registry entry). Fixed with `py install 3.12`, then used `%LOCALAPPDATA%\Python\pythoncore-3.12-64\python.exe` directly.
[1] `& $vp -c @'...'@` (PowerShell here-string piped to `python -c`) - multi-line here-strings get mangled before reaching python and produce a SyntaxError. Write the script to a file and run `python file.py` instead.
[1] `Invoke-RestMethod -Form @{...}` - Windows PowerShell 5.1 has no `-Form` parameter (PS 7+ only), so multipart `POST /api/import` cannot be smoke-tested that way. Used an httpx ASGI client script instead.
[1] `docker exec yt-dlp-web ls -ln /downloads/...` from Git Bash - path mangled to `C:/Program Files/Git/downloads`. Prefix with `MSYS_NO_PATHCONV=1`.
[1] `mcp__playwright__browser_take_screenshot` to the scratchpad directory - refused as outside allowed roots; save inside the project and delete afterwards.
[1] `cat >> changelog_detailed.md << 'EOF' ... EOF` with a long markdown body (Git Bash) - died with "unexpected EOF while looking for matching `'`" even though the heredoc delimiter was quoted; apostrophes/backticks in the body still tripped the parser. Worked around by writing the body to the scratchpad with the Write tool, then `cat scratchpad/entry.md >> changelog_detailed.md`.
[1] `PYTHONPATH=app python -m yt_dlp --list-extractors | grep -i imaglr` - returns nothing even though the plugin loads fine. Not a path problem: yt-dlp's `_real_main` runs `print_extractor_information()` *before* constructing `YoutubeDL`, and plugins are only loaded in `YoutubeDL.__init__`. Verify plugin registration with `python -c "import yt_dlp.plugins as p; p.load_all_plugins(); from yt_dlp.extractor import gen_extractor_classes; ..."` instead.
