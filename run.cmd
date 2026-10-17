@echo off
cd /d "%~dp0"
if not exist .venv\Scripts\python.exe (
    python -m venv .venv
    .venv\Scripts\python.exe -m pip install -U pip
    if exist requirements.lock (
        .venv\Scripts\python.exe -m pip install --pre -r requirements.lock
    ) else (
        .venv\Scripts\python.exe -m pip install --pre -r requirements.txt
    )
)
.venv\Scripts\python.exe app.py
