@echo off
py -3 -m pip install -r requirements-dev.txt
py -3 -m PyInstaller --noconfirm --clean --onefile --windowed --name FLAC2AAC --collect-all PyQt6 app.py
pause
