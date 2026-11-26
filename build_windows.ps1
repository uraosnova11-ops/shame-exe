# Developer handoff only. Not a verified distribution artifact.
$ErrorActionPreference = 'Stop'
python -m pip install -r requirements.txt
python -m pip install pyinstaller
python -m PyInstaller --noconfirm --clean --onefile --windowed --name YuriCRM --add-data 'index.html;.' --collect-all telethon launcher.py
