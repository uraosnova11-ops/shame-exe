# Developer handoff only. Not a verified distribution artifact.
$ErrorActionPreference = 'Stop'
python -m pip install -r requirements.txt
python -m pip install pyinstaller
python -m PyInstaller --noconfirm --clean --onefile --windowed --name YuriCRM --collect-all telethon --add-data 'index.html;.' --add-data 'desktop.css;.' --add-data 'desktop.js;.' launcher.py
