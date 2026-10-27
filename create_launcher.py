#!/usr/bin/env python3
"""
NOVA AI Desktop Entry Generator
Creates a proper .desktop file for Linux or .app for macOS
"""
import os
import sys
import subprocess
import platform

# Папка проекта вычисляется от расположения этого файла. Прежний
# вариант содержал путь конкретной машины: у покупателя бандл
# собирался с чужими путями и не находил ни main.py, ни окружение.
PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))

def create_macos_app():
    """Create macOS .app bundle with proper structure"""
    app_dir = os.path.join(PROJECT_DIR, "NOVA AI.app")
    contents = os.path.join(app_dir, "Contents")
    macos = os.path.join(contents, "MacOS")
    resources = os.path.join(contents, "Resources")
    
    os.makedirs(macos, exist_ok=True)
    os.makedirs(resources, exist_ok=True)
    
    # Info.plist
    info_plist = '''<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleExecutable</key>
    <string>NOVA AI</string>
    <key>CFBundleIdentifier</key>
    <string>com.nova_ai.assistant</string>
    <key>CFBundleName</key>
    <string>NOVA AI</string>
    <key>CFBundleDisplayName</key>
    <string>NOVA AI</string>
    <key>CFBundleVersion</key>
    <string>1.0.0</string>
    <key>CFBundleShortVersionString</key>
    <string>1.0</string>
    <key>CFBundlePackageType</key>
    <string>APPL</string>
    <key>CFBundleSignature</key>
    <string>JARV</string>
    <key>LSMinimumSystemVersion</key>
    <string>12.0</string>
    <key>NSHighResolutionCapable</key>
    <true/>
    <key>LSUIElement</key>
    <false/>
    <key>NSAppleEventsUsageDescription</key>
    <string>NOVA AI needs access to control applications and browser.</string>
    <key>NSMicrophoneUsageDescription</key>
    <string>NOVA AI needs microphone access for voice commands.</string>
    <key>NSDesktopFolderUsageDescription</key>
    <string>NOVA AI may need access to Desktop for file operations.</string>
    <key>NSDocumentsFolderUsageDescription</key>
    <string>NOVA AI may need access to Documents for file operations.</string>
    <key>NSDownloadsFolderUsageDescription</key>
    <string>NOVA AI may need access to Downloads for file operations.</string>
</dict>
</plist>'''
    
    with open(os.path.join(contents, "Info.plist"), "w") as f:
        f.write(info_plist)
    
    # Executable — повторяет рабочий NOVA AI.app: скрытый старт
    # (LaunchAgent + супервизор) и панель управления.
    #
    # Прежний вариант делал `exec launch_nova_ai_terminal.sh` — такого
    # скрипта в проекте нет, и запуск create_launcher.py затирал рабочий
    # бандл нерабочим: двойной клик по иконке ничего не делал.
    # Микрофон здесь ни при чём — цепочку запуска и TCC держит
    # start_nova_ai_hidden.sh с бандлом NOVA AI Daemon.app.
    executable = '''#!/usr/bin/env bash
# NOVA AI.app — современное приложение NOVA AI (без окна Terminal).
#
# Двойной клик по иконке:
#   1. стартует NOVA AI скрыто (LaunchAgent + супервизор, без окна Terminal)
#      и «живую» голограмму позади всех окон;
#   2. открывает панель управления NOVA AI (control_gui.py).
#
# Исполняемый файл лежит в NOVA AI.app/Contents/MacOS/, поэтому папка
# проекта — это три уровня выше: MacOS → Contents → NOVA AI.app → проект.
# Путь вычисляется, а не прописан: прежний вариант содержал путь машины
# автора, и у покупателя бандл не находил сам себя.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/../../.." && pwd)"

LOG_DIR="$PROJECT_DIR/logs"
mkdir -p "$LOG_DIR"

export PATH="/usr/local/bin:/usr/bin:/bin:/opt/homebrew/bin:$PROJECT_DIR/.venv/bin"
export HOME="$HOME"

# Скрытый старт — в фоне, чтобы не задерживать открытие панели
/bin/bash "$PROJECT_DIR/start_nova_ai_hidden.sh" \\
    >> "$LOG_DIR/nova_ai-hidden.log" 2>&1 &

# Панель управления
exec "$PROJECT_DIR/.venv/bin/python3" "$PROJECT_DIR/control_gui.py"
'''
    
    exec_path = os.path.join(macos, "NOVA AI")
    with open(exec_path, "w") as f:
        f.write(executable)
    os.chmod(exec_path, 0o755)
    
    print(f"Created {app_dir}")

def create_desktop_shortcut():
    """Create .desktop file for Linux"""
    desktop_dir = os.path.expanduser("~/.local/share/applications")
    os.makedirs(desktop_dir, exist_ok=True)
    
    desktop_content = f'''[Desktop Entry]
Version=1.0
Type=Application
Name=NOVA AI
GenericName=AI Assistant
Comment=Voice-controlled AI Assistant
Exec={PROJECT_DIR}/launch_jarvis.sh --foreground
Icon={PROJECT_DIR}/assets/icon.png
Terminal=false
Categories=Utility;AudioVideo;
StartupNotify=true
Keywords=ai;assistant;voice;nova_ai;
'''
    
    desktop_path = os.path.join(desktop_dir, "nova_ai.desktop")
    with open(desktop_path, "w") as f:
        f.write(desktop_content)
    os.chmod(desktop_path, 0o755)
    print(f"Created {desktop_path}")

if __name__ == "__main__":
    if platform.system() == "Darwin":
        create_macos_app()
    else:
        create_desktop_shortcut()