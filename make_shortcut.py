"""
Ярлык на рабочем столе, который запускает агента без терминала.

    python make_shortcut.py            создать ярлык
    python make_shortcut.py --remove   убрать его

Ярлык указывает на pythonw.exe (интерпретатор без окна консоли) и передаёт ему
launch.pyw. Сам ярлык создаёт PowerShell: делать .lnk из Python можно только
через COM, а PowerShell умеет это одной строчкой и заодно правильно находит
рабочий стол, даже если тот перенесён в OneDrive.
"""

from __future__ import annotations

import base64
import os
import subprocess
import sys
import tempfile
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
LAUNCHER = BASE_DIR / "launch.pyw"
ICON = BASE_DIR / "icon.ico"
SHORTCUT_NAME = "Агент пар.lnk"


def find_pythonw() -> Path:
    """
    pythonw.exe это тот же Python, но без чёрного окна.

    Если Python поставлен новым установщиком, рядом есть папка bin со ссылками,
    которые переживают обновление версии. Тогда ярлык лучше вести туда: иначе
    после обновления Python ярлык будет указывать на исчезнувшую папку.
    """
    exe = Path(sys.executable)
    if exe.parent.name.startswith("pythoncore-"):
        shim = exe.parent.parent / "bin" / "pythonw.exe"
        if shim.exists():
            return shim
    twin = exe.with_name(exe.name.replace("python", "pythonw", 1))
    if twin.exists():
        return twin
    raise SystemExit(f"Рядом с {exe} нет pythonw.exe, ярлык без него не сделать.")


def run_powershell(script: str) -> str:
    """Результат забираем через файл: так кириллица не зависит от кодировки консоли."""
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "result.txt"
        full = f"$out = '{out}'\n{script}"
        encoded = base64.b64encode(full.encode("utf-16-le")).decode("ascii")
        try:
            done = subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
                capture_output=True,
            )
        except OSError:
            raise SystemExit("В системе не нашёлся powershell, без него ярлык не сделать. "
                             "Запускайте агента командой python agent.py ui")
        if done.returncode != 0:
            raise SystemExit("PowerShell не справился:\n"
                             + done.stderr.decode("utf-8", "replace"))
        # utf-8-sig: PowerShell 5.1 пишет UTF-8 с меткой в начале файла
        return out.read_text(encoding="utf-8-sig").strip() if out.exists() else ""


def ps_quote(value) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def create() -> None:
    if not LAUNCHER.exists():
        raise SystemExit(f"Нет файла {LAUNCHER}, ярлыку не на что указывать.")
    pythonw = find_pythonw()
    icon = ICON if ICON.exists() else pythonw
    script = f"""
$lnk = Join-Path ([Environment]::GetFolderPath('Desktop')) {ps_quote(SHORTCUT_NAME)}
$s = (New-Object -ComObject WScript.Shell).CreateShortcut($lnk)
$s.TargetPath = {ps_quote(pythonw)}
$s.Arguments = '"' + {ps_quote(LAUNCHER)} + '"'
$s.WorkingDirectory = {ps_quote(BASE_DIR)}
$s.IconLocation = {ps_quote(icon)}
$s.Description = 'Запись пар, расшифровка и конспекты'
$s.Save()
[IO.File]::WriteAllText($out, $lnk, [Text.Encoding]::UTF8)
"""
    path = run_powershell(script)
    print(f"Ярлык создан: {path}")
    print(f"Запускает:    {pythonw} {LAUNCHER}")
    print("\nДальше агент открывается двойным кликом по ярлыку.")
    print("Второй клик по нему же предложит открыть страницу или остановить агента.")


def remove() -> None:
    script = f"""
$lnk = Join-Path ([Environment]::GetFolderPath('Desktop')) {ps_quote(SHORTCUT_NAME)}
if (Test-Path $lnk) {{ Remove-Item $lnk -Force; $r = 'Ярлык убран: ' + $lnk }}
else {{ $r = 'Ярлыка и не было: ' + $lnk }}
[IO.File]::WriteAllText($out, $r, [Text.Encoding]::UTF8)
"""
    print(run_powershell(script))


def main() -> None:
    if os.name != "nt":
        raise SystemExit("Ярлык умеет делаться только на Windows: он опирается на pythonw.exe "
                         "и .lnk.\nНа macOS и Linux запускайте агента так: python agent.py ui")
    if "--remove" in sys.argv:
        remove()
    else:
        create()
    if "--quiet" not in sys.argv:
        try:
            input("\nEnter чтобы закрыть окно. ")
        except EOFError:
            pass  # запустили не из окна, ждать нечего


if __name__ == "__main__":
    main()
