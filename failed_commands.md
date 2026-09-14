# Failed Commands Log

Format: `[n] command -- brief note on the failure`. Increment `[n]` if the same command/issue recurs again later.

[1] `py -3.12 -m venv .venv` - the `py` launcher listed a 3.12.11 runtime that no longer existed on disk (stale StabilityMatrix registry entry). Fixed with `py install 3.12`, then used `%LOCALAPPDATA%\Python\pythoncore-3.12-64\python.exe` directly.
[1] `& $vp -c @'...'@` (PowerShell here-string piped to `python -c`) - multi-line here-strings get mangled before reaching python and produce a SyntaxError. Write the script to a file and run `python file.py` instead.
[1] `Invoke-RestMethod -Form @{...}` - Windows PowerShell 5.1 has no `-Form` parameter (PS 7+ only), so multipart `POST /api/import` cannot be smoke-tested that way. Used an httpx ASGI client script instead.
[1] `docker exec yt-dlp-web ls -ln /downloads/...` from Git Bash - path mangled to `C:/Program Files/Git/downloads`. Prefix with `MSYS_NO_PATHCONV=1`.
[1] `mcp__playwright__browser_take_screenshot` to the scratchpad directory - refused as outside allowed roots; save inside the project and delete afterwards.
