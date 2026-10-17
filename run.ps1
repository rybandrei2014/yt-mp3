# yt-mp3 launcher: creates .venv on first run, then starts the app.
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

if (-not (Test-Path ".venv\Scripts\python.exe")) {
    python -m venv .venv
    .\.venv\Scripts\python.exe -m pip install -U pip
    if (Test-Path "requirements.lock") {
        .\.venv\Scripts\python.exe -m pip install --pre -r requirements.lock
    } else {
        .\.venv\Scripts\python.exe -m pip install --pre -r requirements.txt
    }
}

.\.venv\Scripts\python.exe app.py
