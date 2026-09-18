"""
Запуск агента без терминала.

Файл открывается через pythonw.exe, поэтому у процесса нет ни окна, ни консоли.
Это значит две вещи, вокруг которых всё здесь и построено:
  sys.stdout равен None, print() при этом молчит. Поэтому вывод агента
  перенаправляется в .launcher/agent.log, иначе разбираться будет не с чем.
  Ctrl+C нажать негде. Поэтому остановка сделана через файл .launcher/stop:
  фоновый поток видит его и будит главный поток тем же KeyboardInterrupt,
  который агент уже умеет обрабатывать (дописывает файлы, доделывает очередь).

Повторный клик по ярлыку не поднимает второго агента, а показывает, чем агент
сейчас занят, и предлагает открыть страницу или остановиться.
"""

from __future__ import annotations

import _thread
import json
import os
import socket
import subprocess
import sys
import threading
import time
import traceback
import urllib.request
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
RUN_DIR = BASE_DIR / ".launcher"
PID_FILE = RUN_DIR / "agent.pid"
LOCK_FILE = RUN_DIR / "lock"
STOP_FILE = RUN_DIR / "stop"
LOG_FILE = RUN_DIR / "agent.log"

_lock_file = None  # держим открытым всё время работы, иначе замок снимется

TITLE = "Агент пар"

MB_OK = 0x00000000
MB_YESNOCANCEL = 0x00000003
MB_YESNO = 0x00000004
MB_ICONERROR = 0x00000010
MB_ICONQUESTION = 0x00000020
MB_ICONINFO = 0x00000040
MB_TOPMOST = 0x00040000
IDCANCEL, IDYES, IDNO = 2, 6, 7

CREATE_NEW_CONSOLE = 0x00000010

# Модуль -> как он называется при установке. Из всего этого агенту при старте
# нужен только yaml, остальное подгружается по ходу дела: sounddevice перед
# записью с микрофона, faster_whisper перед расшифровкой, anthropic перед
# конспектом. Поэтому нехватка остальных не повод не запускаться.
MODULES = {
    "yaml": "pyyaml",
    "numpy": "numpy",
    "sounddevice": "sounddevice",
    "soundfile": "soundfile",
    "faster_whisper": "faster-whisper",
    "anthropic": "anthropic",
}


# ------------------------------------------------------------------ разговор с человеком

def box(text: str, flags: int = MB_OK | MB_ICONINFO) -> int:
    """Окно с сообщением. Единственный способ что-то сказать, когда консоли нет."""
    import ctypes

    return ctypes.windll.user32.MessageBoxW(None, text, TITLE, flags | MB_TOPMOST)


def open_log():
    """
    Весь вывод агента уводим в файл, иначе он уходит в никуда. Ошибки тут
    гасим: остаться без журнала неприятно, но это не повод не запускать агента.
    """
    try:
        RUN_DIR.mkdir(exist_ok=True)
        if LOG_FILE.exists() and LOG_FILE.stat().st_size > 2_000_000:
            LOG_FILE.replace(RUN_DIR / "agent.log.old")
        stream = LOG_FILE.open("a", encoding="utf-8", buffering=1, errors="replace")
    except OSError:
        return None
    sys.stdout = sys.stderr = stream
    return stream


def tail_log(lines: int = 15, limit: int = 1500) -> str:
    """Хвост журнала для окна с ошибкой. Длинные строки режем, окно не резиновое."""
    try:
        text = LOG_FILE.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return "журнал недоступен"
    tail = "\n".join(line[:200] for line in text.splitlines()[-lines:])
    return tail[-limit:] if len(tail) > limit else tail


# ------------------------------------------------------------------ библиотеки

def console_python() -> str:
    """python.exe рядом с pythonw.exe: нужен, чтобы установка библиотек была видна."""
    exe = Path(sys.executable)
    twin = exe.with_name(exe.name.replace("pythonw", "python"))
    return str(twin if twin.exists() else exe)


def missing_modules() -> list[str]:
    import importlib.util

    missing = []
    for module, package in MODULES.items():
        try:
            found = importlib.util.find_spec(module) is not None
        except (ImportError, ValueError):
            found = False
        if not found:
            missing.append(package)
    return missing


def install_packages(packages: list[str]) -> None:
    """Ставим в отдельном окне, чтобы пользователь видел прогресс, а не пустой экран."""
    import importlib

    req = BASE_DIR / "requirements.txt"
    args = ["-r", str(req)] if req.exists() else packages
    try:
        subprocess.run([console_python(), "-m", "pip", "install", *args],
                       creationflags=CREATE_NEW_CONSOLE, check=False)
    except OSError as err:
        box(f"Не удалось запустить установку библиотек.\n\n{err}", MB_OK | MB_ICONERROR)
        return
    # Без этого find_spec не увидит то, что только что поставили: список папок
    # с модулями Python кеширует.
    importlib.invalidate_caches()


def ensure_packages() -> bool:
    """False значит запускаться нельзя или пользователь отказался."""
    missing = missing_modules()
    if not missing:
        return True
    critical = "pyyaml" in missing  # без него агент не стартует вообще
    answer = box(
        "Не хватает библиотек:\n\n  " + "\n  ".join(missing) + "\n\n"
        "Установить их сейчас? Откроется окно с установкой, это займёт\n"
        "несколько минут, дальше агент запустится сам.\n\n"
        "Да: установить\n"
        "Нет: " + ("выйти" if critical else "запустить без них"),
        MB_YESNO | MB_ICONQUESTION,
    )
    if answer != IDYES:
        return not critical

    install_packages(missing)
    still = missing_modules()
    if "pyyaml" in still:
        box("Установка не удалась, библиотеки pyyaml так и нет.\n"
            "Попробуйте установить вручную:\n\n"
            f'  "{console_python()}" -m pip install -r requirements.txt',
            MB_OK | MB_ICONERROR)
        return False
    if still:
        box("Часть библиотек установить не удалось:\n\n  " + "\n  ".join(still) + "\n\n"
            "Агент запустится, но без них не будет работать:\n"
            "sounddevice и soundfile нужны для записи микрофоном,\n"
            "faster-whisper для расшифровки, anthropic для конспекта.",
            MB_OK | MB_ICONINFO)
    return True


def ensure_schedule(cfg: dict) -> bool:
    """
    False значит запускаться сейчас не надо: человек пошёл заполнять расписание.
    Стартовать с примером бессмысленно, в нём выдуманные пары.
    """
    path = cfg["schedule_file"]
    if path.exists():
        return True
    example = BASE_DIR / "schedule.example.yaml"
    if not example.exists():
        box(f"Нет файла расписания:\n  {path}\n\n"
            "Без него агент не знает, какие у вас пары.", MB_OK | MB_ICONERROR)
        return False
    answer = box(
        f"Нет файла расписания:\n  {path}\n\n"
        "Рядом лежит пример с разобранным по комментариям форматом.\n"
        "Скопировать его и открыть, чтобы вписать свои пары?",
        MB_YESNO | MB_ICONQUESTION,
    )
    if answer != IDYES:
        return False
    path.write_bytes(example.read_bytes())
    try:
        os.startfile(path)
    except OSError:
        os.startfile(path.parent)  # с .yaml ничего не связано, откроем хотя бы папку
    box("Впишите свои пары, сохраните файл и запустите ярлык ещё раз.\n\n"
        "Пока в расписании пример, и пары в нём выдуманные.")
    return False


# ------------------------------------------------------------------ кто сейчас работает

def port_taken(port: int) -> bool:
    with socket.socket() as sock:
        sock.settimeout(0.4)
        return sock.connect_ex(("127.0.0.1", port)) == 0


def local_opener():
    """Свой открыватель без прокси: до 127.0.0.1 надо ходить напрямую."""
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))


def agent_state(port: int, timeout: float = 2.0) -> dict | None:
    """
    Отвечает ли на порту именно наш агент. Порт может занять любая программа,
    поэтому мало достучаться, надо узнать ответ.
    """
    try:
        with local_opener().open(f"http://127.0.0.1:{port}/api/state", timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception:
        return None
    return data if isinstance(data, dict) and "recording" in data else None


def stop_recording(port: int) -> bool:
    """Останавливаем запись микрофоном штатно, чтобы файл сохранился и ушёл в очередь."""
    request = urllib.request.Request(f"http://127.0.0.1:{port}/api/record/stop",
                                     data=b"{}", method="POST")
    try:
        with local_opener().open(request, timeout=15) as resp:
            return json.loads(resp.read().decode("utf-8")).get("ok") is True
    except Exception:
        return False


def describe(state: dict) -> str:
    """Чем агент занят прямо сейчас. Человеку это важнее, чем слово «работает»."""
    recording = state.get("recording") or {}
    if recording.get("active"):
        minutes = int(recording.get("seconds") or 0) // 60
        subject = recording.get("subject") or "без названия"
        return f"Идёт запись микрофоном: {subject}, уже {minutes} мин."
    if state.get("browser"):
        return "Идёт запись вкладки через расширение."
    stage = (state.get("status") or {}).get("stage")
    if stage == "transcribe":
        return "Идёт расшифровка записи."
    if stage == "summary":
        return "Пишется конспект."
    queue = state.get("queue") or 0
    if queue:
        return f"В очереди на обработку записей: {queue}."
    return "Сейчас агент ничем не занят, просто ждёт."


def read_pid() -> int | None:
    try:
        return int(PID_FILE.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


def process_alive(pid: int) -> bool:
    """Жив ли процесс с таким номером. Без сторонних библиотек, через ядро Windows."""
    import ctypes

    SYNCHRONIZE, WAIT_TIMEOUT = 0x00100000, 0x00000102
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.OpenProcess(SYNCHRONIZE, False, pid)
    if not handle:
        return False
    try:
        return kernel32.WaitForSingleObject(handle, 0) == WAIT_TIMEOUT
    finally:
        kernel32.CloseHandle(handle)


def acquire_lock() -> bool:
    """
    Замок на уровне системы: Windows держит его, пока жив процесс, и снимает сам,
    когда тот умирает. Поэтому после падения или выключения питания замок не
    остаётся висеть, а два быстрых клика по ярлыку не поднимают двух агентов.

    Отдельный файл, а не pid-файл: замок msvcrt мешает другим процессам читать
    заблокированные байты, а pid-файл читать надо.
    """
    global _lock_file
    import msvcrt

    RUN_DIR.mkdir(exist_ok=True)
    handle = LOCK_FILE.open("a+b")
    handle.seek(0)
    try:
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError:
        handle.close()
        return False  # замок держит другой агент
    _lock_file = handle
    PID_FILE.write_text(str(os.getpid()), encoding="utf-8")
    return True


def release_lock() -> None:
    """Только свой pid-файл: чужой может принадлежать уже новому агенту."""
    global _lock_file

    if read_pid() == os.getpid():
        PID_FILE.unlink(missing_ok=True)
    if _lock_file is None:
        return
    import msvcrt

    try:
        _lock_file.seek(0)
        msvcrt.locking(_lock_file.fileno(), msvcrt.LK_UNLCK, 1)
    except OSError:
        pass
    _lock_file.close()
    _lock_file = None


# ------------------------------------------------------------------ остановка

def watch_stop_file() -> None:
    """
    Будит главный поток тем же KeyboardInterrupt, что и Ctrl+C. Агент его уже
    ловит и закрывается штатно: дописывает куски записи и доделывает очередь.
    В файле лежит номер процесса, чтобы просьбу не перехватил не тот агент.
    """
    me = os.getpid()
    if STOP_FILE.exists():
        STOP_FILE.unlink(missing_ok=True)  # вдруг остался с прошлого раза
    while True:
        time.sleep(1)
        if not STOP_FILE.exists():
            continue
        try:
            wanted = int(STOP_FILE.read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            continue
        if wanted != me:
            continue
        STOP_FILE.unlink(missing_ok=True)
        print("\nПришла просьба остановиться через ярлык.")
        _thread.interrupt_main()
        return


def confirm_stop_while_busy(state: dict, port: int) -> bool:
    """
    Остановка посреди записи это потеря пары, поэтому спрашиваем отдельно.
    Микрофонную запись умеем закрыть правильно, запись вкладки нет: её ведёт
    расширение, и агент о ней узнаёт только кусками.
    """
    recording = state.get("recording") or {}
    if recording.get("active"):
        minutes = int(recording.get("seconds") or 0) // 60
        answer = box(
            f"Сейчас идёт запись микрофоном: {recording.get('subject') or 'без названия'}, "
            f"уже {minutes} мин.\n\n"
            "Да: закончить запись и остановить агента.\n"
            "     Записанное сохранится и уйдёт на расшифровку.\n"
            "Нет: не трогать, пусть пишет дальше",
            MB_YESNO | MB_ICONQUESTION,
        )
        if answer != IDYES:
            return False
        if not stop_recording(port):
            box("Не получилось закончить запись штатно, агент остаётся работать.\n"
                "Остановите запись кнопкой на странице.", MB_OK | MB_ICONERROR)
            return False
        return True

    if state.get("browser"):
        answer = box(
            "Сейчас идёт запись вкладки через расширение.\n\n"
            "Если остановить агента сейчас, звук останется на диске файлом,\n"
            "но расшифровки и конспекта не будет: их придётся запускать вручную.\n\n"
            "Да: всё равно остановить\n"
            "Нет: сначала остановлю запись в расширении",
            MB_YESNO | MB_ICONQUESTION,
        )
        return answer == IDYES
    return True


def ask_stop(pid: int, port: int) -> None:
    """Просим агента закрыться сам: он допишет начатое, чего не сделал бы taskkill."""
    state = agent_state(port)
    if state and not confirm_stop_while_busy(state, port):
        return

    RUN_DIR.mkdir(exist_ok=True)
    STOP_FILE.write_text(str(pid), encoding="utf-8")
    # Агент закрывает порт заметно раньше, чем завершается сам, поэтому после
    # освобождения порта даём ему ещё пару секунд: иначе обычная остановка
    # выглядела бы как "застрял на расшифровке".
    port_closed = None
    for _ in range(40):
        time.sleep(0.5)
        if not process_alive(pid):
            box("Агент остановлен.")
            return
        if not port_taken(port):
            if port_closed is None:
                port_closed = time.monotonic()
            elif time.monotonic() - port_closed > 3:
                break
    box("Приём записей остановлен, страница больше не открывается.\n\n"
        "Агент ещё не закрылся: он доделывает расшифровку прошлой пары.\n"
        "Это может занять до часа, закроется он сам.")


# ------------------------------------------------------------------ сценарии запуска

def open_page(port: int) -> None:
    import webbrowser

    webbrowser.open(f"http://127.0.0.1:{port}")


def handle_already_running(pid: int | None, port: int) -> None:
    """Второй клик по ярлыку. Разные ответы на разные состояния агента."""
    state = agent_state(port)

    if state is None and port_taken(port):
        box(f"Порт {port} занят другой программой, агент на него не встанет.\n\n"
            "Закройте программу, которая его держит, или поменяйте\n"
            "server.port в config.yaml.", MB_OK | MB_ICONERROR)
        return

    if state is None:
        # процесс жив, а страницы нет: агент уже закрыл сервер и доделывает очередь
        answer = box(
            "Агент уже не принимает записи, но ещё работает:\n"
            "доделывает расшифровку прошлой пары.\n\n"
            "Да: запустить приём записей заново (расшифровка продолжится\n"
            "     в фоне и будет отнимать процессор)\n"
            "Нет: подождать, пока он закончит сам",
            MB_YESNO | MB_ICONQUESTION,
        )
        if answer == IDYES:
            start_agent(port)
        return

    answer = box(
        f"Агент уже работает на http://127.0.0.1:{port}\n"
        f"{describe(state)}\n\n"
        "Да: открыть страницу в браузере\n"
        "Нет: остановить агента\n"
        "Отмена: ничего не делать",
        MB_YESNOCANCEL | MB_ICONQUESTION,
    )
    if answer == IDYES:
        open_page(port)
    elif answer == IDNO:
        if pid is None:
            box("Этот агент запущен не ярлыком, а из терминала.\n"
                "Остановите его там: Ctrl+C в том окне.", MB_OK | MB_ICONINFO)
        else:
            ask_stop(pid, port)


def start_agent(port: int) -> None:
    import agent

    if not acquire_lock():
        # Случайный двойной клик по ярлыку. Первый процесс уже поднимает агента
        # и сам откроет страницу, поэтому второму надо просто тихо уйти:
        # окно с ошибкой тут было бы на пустом месте.
        print("Агента уже поднимает соседний процесс, выхожу.")
        return
    try:
        cfg = agent.load_config(BASE_DIR / "config.yaml")
        threading.Thread(target=watch_stop_file, daemon=True).start()
        # Агент ниже напечатает "Остановить: Ctrl+C", но консоли тут нет, так что
        # поправка нужна сразу, чтобы журнал не сбивал с толку.
        print("Запущено ярлыком. Остановить: второй клик по ярлыку, там будет кнопка.")
        agent.run_server(cfg, open_browser=True)
    except OSError as err:
        box(f"Не удалось занять порт {port}.\n\n{err}\n\n"
            "Скорее всего, порт занят другой программой.\n"
            "Поменяйте server.port в config.yaml.", MB_OK | MB_ICONERROR)
    finally:
        release_lock()


def main() -> None:
    os.chdir(BASE_DIR)
    if not ensure_packages():
        return

    import agent

    cfg = agent.load_config(BASE_DIR / "config.yaml")
    port = int(cfg["server"]["port"])

    pid = read_pid()
    if pid is not None and (pid == os.getpid() or not process_alive(pid)):
        pid = None  # pid-файл остался от процесса, которого уже нет

    if pid is not None or port_taken(port):
        handle_already_running(pid, port)
        return

    if not ensure_schedule(cfg):
        return
    start_agent(port)


if __name__ == "__main__":
    if os.name != "nt":
        sys.exit("launch.pyw рассчитан на Windows. "
                 "На macOS и Linux запускайте агента командой: python agent.py ui")

    log = open_log()
    print(f"\n{'=' * 60}\nЗапуск через ярлык, {time.strftime('%Y-%m-%d %H:%M:%S')}")
    try:
        main()
    except SystemExit as err:  # так агент сообщает о проблемах с расписанием и конфигом
        if err.code not in (0, None):
            print(err.code)
            box(f"Агент не запустился.\n\n{err.code}", MB_OK | MB_ICONERROR)
    except KeyboardInterrupt:
        print("Остановлено.")
    except BaseException:
        if sys.stderr is not None:  # без журнала stderr так и остался None
            traceback.print_exc()
        box("Агент не запустился. Последние строчки журнала:\n\n"
            f"{tail_log()}\n\nВесь журнал: {LOG_FILE}", MB_OK | MB_ICONERROR)
    finally:
        if log is not None:
            log.close()
