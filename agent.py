#!/usr/bin/env python3
"""
Агент для записи пар и автоматического конспектирования.

Как работает:
  1. пишет звук с микрофона в файл FLAC;
  2. расшифровывает речь локально через Whisper (faster-whisper, бесплатно и без интернета);
  3. отдаёт расшифровку Claude, который пишет структурированный конспект в Markdown.

Команды:
  python agent.py devices                           список микрофонов
  python agent.py record -s "Матанализ"             записать пару (стоп: Ctrl+C)
  python agent.py record -s "Физика" -m 90          записать ровно 90 минут
  python agent.py process lecture.m4a -s "История"  обработать готовую запись (например, с телефона)
  python agent.py process transcript.txt -s "..."   сделать конспект из готовой расшифровки
  python agent.py ui                                открыть приложение в браузере
  python agent.py timetable                         расписание на сегодня (-w на неделю)
  python agent.py schedule                          самому записывать очные пары по расписанию
  python agent.py serve                             принимать записи вкладок из расширения для браузера
"""

from __future__ import annotations

import argparse
import json
import math
import os
import queue
import re
import sys
import threading
import time
from datetime import date, datetime, time as dtime, timedelta, timezone
from pathlib import Path

import yaml

BASE_DIR = Path(__file__).resolve().parent
WEEKDAYS = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]

DEFAULTS: dict = {
    "output_dir": "lectures",
    "audio": {"device": None, "samplerate": 16000},
    "transcription": {"model": "small", "language": "ru", "device": "cpu", "compute_type": "auto"},
    "summary": {
        "enabled": True,
        "model": "claude-sonnet-5",
        "effort": "medium",
        "max_tokens": 32000,
        "extra_instructions": "",
    },
    "schedule_file": "schedule.yaml",
    "schedule": {"start_early_min": 2, "extra_min": 5},
    "server": {"port": 8756, "default_subject": "Онлайн-пара"},
}


# ---------------------------------------------------------------- настройки

def deep_merge(base: dict, override: dict | None) -> dict:
    result = dict(base)
    for key, value in (override or {}).items():
        if value is None and isinstance(result.get(key), dict):
            continue  # пустая секция в YAML не должна затирать значения по умолчанию
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def load_config(path: Path) -> dict:
    data = {}
    if path.exists():
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
    cfg = deep_merge(DEFAULTS, data)
    for key in ("output_dir", "schedule_file"):
        value = Path(str(cfg[key])).expanduser()
        cfg[key] = value if value.is_absolute() else path.resolve().parent / value
    return cfg


# ---------------------------------------------------------------- мелочи

def fmt_ts(seconds: float) -> str:
    s = int(seconds)
    return f"{s // 3600:02d}:{s % 3600 // 60:02d}:{s % 60:02d}"


def fmt_wait(seconds: float) -> str:
    minutes = int(seconds // 60)
    days, minutes = divmod(minutes, 1440)
    hours, minutes = divmod(minutes, 60)
    parts = [f"{days} д"] if days else []
    if hours:
        parts.append(f"{hours} ч")
    parts.append(f"{minutes} мин")
    return " ".join(parts)


def safe_name(name: str) -> str:
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", str(name)).strip(" .")
    return name or "Без названия"


def new_session_dir(cfg: dict, subject: str, when: datetime, label: str = "") -> Path:
    """Папка вида lectures/<Предмет>/2026-09-14 Лекция 03 (без расписания: 2026-09-14_09-00)."""
    name = f"{when:%Y-%m-%d} {label}".strip() if label else when.strftime("%Y-%m-%d_%H-%M")
    base = cfg["output_dir"] / safe_name(subject) / safe_name(name)
    folder, n = base, 2
    while folder.exists() and any(folder.iterdir()):
        folder = base.with_name(f"{base.name}_{n}")
        n += 1
    folder.mkdir(parents=True, exist_ok=True)
    return folder


# ---------------------------------------------------------------- состояние для интерфейса

STATUS = {"stage": "idle", "detail": "", "percent": None}
_status_lock = threading.Lock()


def set_status(stage: str, detail: str = "", percent: float | None = None) -> None:
    with _status_lock:
        STATUS.update({"stage": stage, "detail": detail, "percent": percent})


def get_status() -> dict:
    with _status_lock:
        return dict(STATUS)


# ---------------------------------------------------------------- запись звука

def list_devices() -> None:
    import sounddevice as sd

    default_in = sd.default.device[0]
    print("Микрофоны (номер можно указать в config.yaml, поле audio.device):\n")
    for i, dev in enumerate(sd.query_devices()):
        if dev["max_input_channels"] > 0:
            mark = "   <- по умолчанию" if i == default_in else ""
            print(f"  [{i}] {dev['name']}{mark}")


def pick_samplerate(device, wanted: int) -> int:
    """Whisper хватает 16 кГц, но не каждый микрофон это умеет. Тогда пишем в его родной частоте."""
    import sounddevice as sd

    try:
        sd.check_input_settings(device=device, channels=1, samplerate=wanted)
        return wanted
    except Exception:
        return int(sd.query_devices(device, "input")["default_samplerate"])


def level_bar(peak: float, width: int = 20) -> str:
    db = 20 * math.log10(max(peak, 1e-6))
    filled = int(max(0.0, min(1.0, (db + 60) / 60)) * width)
    return "█" * filled + "░" * (width - filled)


def record_audio(path: Path, cfg: dict, max_seconds: float | None = None,
                 live: bool = True, stop_event=None, on_tick=None) -> tuple[float, bool]:
    """
    Пишет звук с микрофона в FLAC, пока не нажат Ctrl+C, не истекло max_seconds
    или не сработал stop_event (так запись останавливают из интерфейса).
    on_tick(секунды, громкость) вызывается раз в секунду, чтобы рисовать индикатор.
    Возвращает (длительность в секундах, был ли нажат Ctrl+C).
    """
    import numpy as np
    import sounddevice as sd
    import soundfile as sf

    device = cfg["audio"]["device"]
    sr = pick_samplerate(device, int(cfg["audio"]["samplerate"]))
    blocks: queue.Queue = queue.Queue()

    def on_audio(indata, frames, time_info, status):
        blocks.put(indata.copy())

    status_every = 1.0 if live else 600.0
    frames_written, peak, interrupted = 0, 0.0, False
    started = last_status = time.monotonic()

    with sf.SoundFile(str(path), mode="w", samplerate=sr, channels=1,
                      format="FLAC", subtype="PCM_16") as out:
        with sd.InputStream(device=device, channels=1, samplerate=sr,
                            dtype="float32", callback=on_audio):
            try:
                while max_seconds is None or time.monotonic() - started < max_seconds:
                    if stop_event is not None and stop_event.is_set():
                        break
                    try:
                        block = blocks.get(timeout=0.5)
                    except queue.Empty:
                        continue
                    out.write(block)
                    frames_written += len(block)
                    peak = max(peak, float(np.abs(block).max()))
                    now = time.monotonic()
                    if now - last_status >= 1.0 and on_tick:
                        on_tick(frames_written / sr, peak)
                    if now - last_status >= status_every:
                        line = f"● REC {fmt_ts(frames_written / sr)}  {level_bar(peak)}"
                        if live:
                            print(f"\r  {line}  Ctrl+C = стоп ", end="", flush=True)
                        else:
                            print(f"  {line}", flush=True)
                        peak, last_status = 0.0, now
                    elif now - last_status >= 1.0 and on_tick:
                        peak, last_status = 0.0, now
            except KeyboardInterrupt:
                interrupted = True
        # дописываем то, что микрофон успел отдать перед остановкой
        while not blocks.empty():
            block = blocks.get_nowait()
            out.write(block)
            frames_written += len(block)
    if live:
        print()
    return frames_written / sr, interrupted


# ---------------------------------------------------------------- расшифровка

_whisper = None
_whisper_lock = threading.Lock()


def get_whisper(cfg: dict):
    """Модель загружается один раз и дальше переиспользуется."""
    global _whisper
    with _whisper_lock:
        if _whisper is None:
            from faster_whisper import WhisperModel

            t = cfg["transcription"]
            device = t["device"]
            compute = t["compute_type"]
            if compute == "auto":
                compute = "float16" if device == "cuda" else "int8"
            print(f"Загружаю Whisper «{t['model']}» ({device}, {compute}). "
                  "При первом запуске модель скачивается, это может занять несколько минут.")
            _whisper = WhisperModel(t["model"], device=device, compute_type=compute)
        return _whisper


def transcribe(audio_path: Path, subject: str, cfg: dict) -> str:
    """Текст абзацами примерно по минуте, у каждого абзаца метка времени [чч:мм:сс]."""
    model = get_whisper(cfg)
    language = cfg["transcription"]["language"] or None
    hint = f"Лекция по предмету «{subject}»."  # помогает Whisper с терминами и пунктуацией

    print(f"Расшифровываю {audio_path.name}...")
    set_status("transcribe", subject, 0)
    paragraphs: list[str] = []
    current: list[str] = []
    current_start = 0.0
    with open(audio_path, "rb") as f:
        segments, info = model.transcribe(f, language=language, vad_filter=True,
                                          beam_size=5, initial_prompt=hint)
        for seg in segments:
            text = seg.text.strip()
            if not text:
                continue
            if not current:
                current_start = seg.start
            current.append(text)
            if seg.end - current_start >= 60:
                paragraphs.append(f"[{fmt_ts(current_start)}] {' '.join(current)}")
                current = []
            if info.duration:
                pct = min(100.0, seg.end / info.duration * 100)
                set_status("transcribe", subject, round(pct, 1))
                print(f"\r  {pct:5.1f}%  ({fmt_ts(seg.end)} из {fmt_ts(info.duration)})",
                      end="", flush=True)
    if current:
        paragraphs.append(f"[{fmt_ts(current_start)}] {' '.join(current)}")
    print()
    return "\n\n".join(paragraphs)


# ---------------------------------------------------------------- конспект

SYSTEM_PROMPT = """\
Ты помогаешь студенту: превращаешь автоматическую расшифровку пары в хороший конспект.

Про расшифровку:
- Её сделала программа распознавания речи, поэтому там бывают ошибки: неверно услышанные термины, фамилии, формулы, произнесённые словами. Исправляй их по смыслу и по контексту предмета.
- Если не можешь понять, что было сказано, не выдумывай, а пометь место как [неразборчиво].
- Не добавляй фактов, которых не было на паре. Если пояснение от себя действительно помогает, оформи его отдельно с пометкой «Примечание:».
- Метки вида [чч:мм:сс] показывают время в записи.

Как оформить конспект (Markdown):

# Тема пары

Кратко, 3-5 предложений: о чём была пара и что в ней главное.

## Содержание
Разделы в порядке лекции. У каждого свой подзаголовок ### с меткой времени начала, например: ### Предел последовательности [00:14:30]
Внутри: ключевые мысли, определения, теоремы, формулы, примеры, выводы. Формулы в LaTeX: $...$ внутри строки и $$...$$ отдельной строкой.

## Термины
Термины с короткими определениями.

## Задания и организационное
Домашка, дедлайны, контрольные, что будет на экзамене, что нужно прочитать или повторить. Если ничего такого не было, напиши «Не упоминалось».

## Вопросы для самопроверки
5-7 вопросов по материалу пары.

Убирай воду: приветствия, перекличку, отвлечения, повторы. Но всё, что преподаватель подчёркивал как важное («это будет на экзамене», «запишите», «запомните»), обязательно сохрани и выдели жирным.
Пиши на том языке, на котором шла пара.
"""


def build_request(transcript: str, subject: str, when: datetime, cfg: dict,
                  meta: dict | None = None) -> str:
    parts = [f"Предмет: {subject}", f"Дата: {when:%d.%m.%Y}"]
    if meta:
        if meta.get("title"):
            parts.append(f"Занятие: {meta['title']}")
        if meta.get("teacher"):
            parts.append(f"Преподаватель: {meta['teacher']}")
    extra = str(cfg["summary"].get("extra_instructions") or "").strip()
    if extra:
        parts.append(f"Дополнительные пожелания студента: {extra}")
    parts.append(f"<transcript>\n{transcript}\n</transcript>")
    parts.append("Сделай конспект по этой расшифровке.")
    return "\n\n".join(parts)


def api_key(cfg: dict) -> str | None:
    return os.environ.get("ANTHROPIC_API_KEY") or cfg["summary"].get("api_key") or None


def summarize(request: str, cfg: dict) -> str:
    import anthropic

    s = cfg["summary"]
    client = anthropic.Anthropic(api_key=api_key(cfg))
    extra_body = {"output_config": {"effort": s["effort"]}} if s.get("effort") else None
    print(f"Пишу конспект ({s['model']})...")
    set_status("summary", "", None)
    chunks: list[str] = []
    written = 0
    with client.messages.stream(
        model=s["model"],
        max_tokens=int(s["max_tokens"]),
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": request}],
        extra_body=extra_body,
    ) as stream:
        for text in stream.text_stream:
            chunks.append(text)
            written += len(text)
            print(f"\r  написано символов: {written}", end="", flush=True)
        final = stream.get_final_message()
    print()
    if final.stop_reason == "max_tokens":
        print("  Внимание: ответ упёрся в лимит, конспект может быть обрезан. "
              "Увеличь summary.max_tokens в config.yaml.")
    return "".join(chunks)


def process_session(source: Path, subject: str, when: datetime, cfg: dict,
                    folder: Path, make_notes: bool = True, meta: dict | None = None) -> None:
    """Расшифровка (если на входе аудио) и конспект. Результаты сохраняются в folder."""
    if source.suffix.lower() == ".txt":
        transcript = source.read_text(encoding="utf-8")
    else:
        transcript = transcribe(source, subject, cfg)
        (folder / "transcript.txt").write_text(transcript, encoding="utf-8")
        print(f"Расшифровка сохранена: {folder / 'transcript.txt'}")

    set_status("idle")
    if not transcript.strip():
        print("Речь в записи не найдена. Проверь микрофон: python agent.py devices")
        return
    if not (make_notes and cfg["summary"]["enabled"]):
        return

    request = build_request(transcript, subject, when, cfg, meta)
    notes = None
    if api_key(cfg):
        try:
            notes = summarize(request, cfg)
        except Exception as e:
            print(f"\nНе получилось сделать конспект через API: {e}")
    else:
        print("Ключ ANTHROPIC_API_KEY не задан, поэтому шаг с конспектом через API пропущен.")

    if not notes:
        fallback = folder / "prompt_for_claude.md"
        fallback.write_text(SYSTEM_PROMPT + "\n\n" + request, encoding="utf-8")
        print(f"Сохранил {fallback}\nЭтот файл можно загрузить в обычный чат с Claude, "
              "и он сделает конспект.")
        return

    source_ref = source.name if source.parent == folder else str(source)
    fields = {"subject": subject, "date": f"{when:%Y-%m-%d %H:%M}"}
    for key in ("title", "kind", "teacher", "room", "format"):
        if meta and meta.get(key):
            fields[key] = meta[key]
    fields["source"] = source_ref
    header = "---\n" + "".join(
        f"{k}: {json.dumps(v, ensure_ascii=False)}\n" for k, v in fields.items()) + "---\n\n"
    notes_path = folder / "notes.md"
    notes_path.write_text(header + notes.strip() + "\n", encoding="utf-8")
    set_status("idle")
    print(f"Готово! Конспект: {notes_path}")

# ---------------------------------------------------------------- расписание

DAY_ALIASES = {}
for _i, _names in enumerate([
    ("пн", "понедельник", "mon"), ("вт", "вторник", "tue"), ("ср", "среда", "wed"),
    ("чт", "четверг", "thu"), ("пт", "пятница", "fri"), ("сб", "суббота", "sat"),
    ("вс", "воскресенье", "sun"),
]):
    for _name in _names:
        DAY_ALIASES[_name] = _i

DAY_FULL = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]
MONTHS = ["января", "февраля", "марта", "апреля", "мая", "июня",
          "июля", "августа", "сентября", "октября", "ноября", "декабря"]
DATE_RE = re.compile(r"(\d{1,2})\.(\d{1,2})")


def schedule_tz(name: str):
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(name)
    except Exception:
        # Москва живёт на UTC+3 без перехода на летнее время, так что это безопасный запасной вариант
        return timezone(timedelta(hours=3))


def parse_time(value) -> dtime:
    # YAML без кавычек превращает 10:45 в число 645 (минуты), поэтому понимаем и такой вариант
    if isinstance(value, int):
        return dtime(*divmod(value, 60))
    hh, mm = str(value).strip().split(":")
    return dtime(int(hh), int(mm))


def expand_when(text: str, year: int) -> list[date]:
    """'14.09-19.10 к.н., 02.12' -> список конкретных дат."""
    def make(day: str, month: str) -> date:
        m = int(month)
        return date(year if m >= 9 else year + 1, m, int(day))

    dates: list[date] = []
    for token in str(text).split(","):
        token = token.strip()
        if not token:
            continue
        found = DATE_RE.findall(token)
        step = 14 if "ч.н" in token else 7  # ч.н. = через неделю, к.н. = каждую неделю
        if len(found) == 2 and "-" in token:
            first, last = make(*found[0]), make(*found[1])
            day = first
            while day <= last:
                dates.append(day)
                day += timedelta(days=step)
        elif len(found) == 1:
            dates.append(make(*found[0]))
        else:
            raise ValueError(f"не понял даты: {token!r}")
    return sorted(set(dates))


def load_schedule(cfg: dict) -> tuple[dict, list[dict]]:
    """Читает schedule.yaml и разворачивает каждую пару в список конкретных занятий."""
    path = cfg["schedule_file"]
    if not path.exists():
        raise SystemExit(f"Нет файла расписания: {path}")
    with open(path, encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}

    meta = {
        "group": raw.get("group", ""),
        "subgroup": str(raw.get("subgroup", "А")).strip(),
        "timezone": raw.get("timezone", "Europe/Moscow"),
        "year": int(raw.get("year") or datetime.now().year),
    }
    meta["tz"] = schedule_tz(meta["timezone"])

    pairs: list[dict] = []
    for n, item in enumerate(raw.get("pairs") or [], 1):
        try:
            weekday = DAY_ALIASES[str(item["day"]).strip().lower()]
            start_raw, end_raw = str(item["time"]).split("-")
            start, end = parse_time(start_raw), parse_time(end_raw)
            dates = expand_when(item["when"], meta["year"])
        except Exception as e:
            raise SystemExit(f"Пара №{n} в расписании описана неверно ({e}): {item}")

        subgroup = item.get("subgroup")
        subgroup = str(subgroup).strip() if subgroup else None
        if subgroup and subgroup != meta["subgroup"]:
            continue  # чужая подгруппа

        for day in dates:  # проверка, что даты не разъехались с днём недели
            if day.weekday() != weekday:
                raise SystemExit(
                    f"В расписании ошибка: «{item['subject']}» стоит в {DAY_FULL[weekday]}, "
                    f"а дата {day:%d.%m.%Y} это {DAY_FULL[day.weekday()]}.")

        pairs.append({
            "subject": str(item["subject"]).strip(),
            "kind": str(item.get("kind", "Пара")).strip(),
            "teacher": str(item.get("teacher", "")).strip(),
            "room": str(item.get("room", "")).strip(),
            "subgroup": subgroup,
            "weekday": weekday,
            "start": start,
            "end": end,
            "dates": dates,
            "remote": bool(item.get("remote", True)),
            "link": str(item.get("link", "")).strip(),
        })

    # сквозная нумерация: «Лекция 5 из 12» по каждому предмету и виду занятия
    order: dict[tuple, list] = {}
    for pair in pairs:
        key = (pair["subject"], pair["kind"])
        for day in pair["dates"]:
            order.setdefault(key, []).append(day)
    for key in order:
        order[key] = sorted(set(order[key]))
    meta["order"] = order
    return meta, pairs


def occurrence_number(meta: dict, pair: dict, day: date) -> tuple[int, int]:
    days = meta["order"].get((pair["subject"], pair["kind"]), [])
    return (days.index(day) + 1 if day in days else 0), len(days)


def pair_label(meta: dict, pair: dict, day: date) -> str:
    """Например «Лекция 05» для имени папки."""
    n, _ = occurrence_number(meta, pair, day)
    return f"{pair['kind']} {n:02d}" if n else pair["kind"]


def pair_title(meta: dict, pair: dict, day: date) -> str:
    n, total = occurrence_number(meta, pair, day)
    return f"{pair['kind']} {n} из {total}" if n else pair["kind"]


def occurrences(pairs: list[dict], meta: dict, day_from: date, day_to: date) -> list[tuple]:
    """Все занятия в промежутке дат, по возрастанию времени: (начало, конец, пара, дата)."""
    tz = meta["tz"]
    found = []
    for pair in pairs:
        for day in pair["dates"]:
            if day_from <= day <= day_to:
                found.append((datetime.combine(day, pair["start"], tz),
                              datetime.combine(day, pair["end"], tz), pair, day))
    return sorted(found, key=lambda x: (x[0], x[1]))


def now_msk(meta: dict) -> datetime:
    return datetime.now(timezone.utc).astimezone(meta["tz"])


def pair_at(pairs: list[dict], meta: dict, moment: datetime,
            before: int = 20, after: int = 25) -> tuple | None:
    """Занятие, которое идёт прямо сейчас (или вот-вот начнётся, или только что кончилось)."""
    day = moment.date()
    best = None
    for start, end, pair, when in occurrences(pairs, meta, day - timedelta(days=1), day + timedelta(days=1)):
        if start - timedelta(minutes=before) <= moment <= end + timedelta(minutes=after):
            distance = abs((start - moment).total_seconds())
            if best is None or distance < best[0]:
                best = (distance, (start, end, pair, when))
    return best[1] if best else None


# ---------------------------------------------------------------- просмотр расписания

def format_day(meta: dict, pairs: list[dict], day: date, moment: datetime) -> list[str]:
    items = occurrences(pairs, meta, day, day)
    head = f"{DAY_FULL[day.weekday()].capitalize()}, {day.day} {MONTHS[day.month - 1]}"
    if day == moment.date():
        head += " (сегодня)"
    lines = [head]
    if not items:
        lines.append("  пар нет")
        return lines
    for start, end, pair, when in items:
        mark = "  "
        if start <= moment <= end:
            mark = "▶ "
        elif moment > end:
            mark = "✓ "
        where = "очно" + (f", ауд. {pair['room']}" if pair["room"] else "") if not pair["remote"] else "дистанционно"
        detail = [pair_title(meta, pair, when), pair["teacher"], where]
        if pair["subgroup"]:
            detail.insert(1, f"подгруппа {pair['subgroup']}")
        lines.append(f"{mark}{start:%H:%M}-{end:%H:%M}  {pair['subject']}")
        lines.append(f"             {' · '.join(x for x in detail if x)}")
    return lines


def cmd_timetable(args, cfg: dict) -> None:
    meta, pairs = load_schedule(cfg)
    moment = now_msk(meta)
    print(f"{meta['group']}, подгруппа {meta['subgroup']}. "
          f"Время московское, сейчас {moment:%H:%M}.\n")

    if args.subject:
        needle = args.subject.lower()
        chosen = [p for p in pairs if needle in p["subject"].lower()]
        if not chosen:
            print("Такого предмета в расписании нет.")
            return
        for pair in chosen:
            print(f"{pair['subject']}, {pair['kind'].lower()}, {pair['teacher']}")
            print(f"  {DAY_FULL[pair['weekday']]}, {pair['start']:%H:%M}-{pair['end']:%H:%M}, "
                  f"{'дистанционно' if pair['remote'] else 'очно'}")
            dates = ", ".join(f"{d:%d.%m}" for d in pair["dates"])
            print(f"  всего {len(pair['dates'])}: {dates}")
            left = [d for d in pair["dates"] if d >= moment.date()]
            print(f"  осталось {len(left)}\n")
        return

    if args.date:
        try:
            day = date.fromisoformat(args.date)
        except ValueError:
            raise SystemExit("Дату пиши как 2026-10-14")
        print("\n".join(format_day(meta, pairs, day, moment)))
        return

    days = 7 if args.week else 1
    start_day = moment.date() - timedelta(days=moment.weekday()) if args.week else moment.date()
    for i in range(days):
        print("\n".join(format_day(meta, pairs, start_day + timedelta(days=i), moment)))
        print()

    upcoming = occurrences(pairs, meta, moment.date(), moment.date() + timedelta(days=14))
    following = [o for o in upcoming if o[1] > moment]
    if following:
        start, end, pair, when = following[0]
        when_text = "сейчас идёт" if start <= moment else f"через {fmt_wait((start - moment).total_seconds())}"
        print(f"Ближайшая пара ({when_text}): {pair['subject']}, "
              f"{pair_title(meta, pair, when)}, {start:%d.%m %H:%M}")
        if pair["remote"]:
            print("Дистанционно: открой вкладку с парой и нажми запись в расширении.")
        else:
            print(f"Очно{', ауд. ' + pair['room'] if pair['room'] else ''}: "
                  "агент запишет её микрофоном сам, если запущен python agent.py schedule")
    else:
        print("Дальше пар в расписании нет.")


# ---------------------------------------------------------------- фоновая обработка

def sleep_until(target: datetime) -> None:
    # спим короткими отрезками, чтобы нормально переживать сон ноутбука и перевод часов
    while (left := (target - datetime.now(timezone.utc).astimezone(target.tzinfo)).total_seconds()) > 0:
        time.sleep(min(left, 30))


def start_worker() -> queue.Queue:
    """Очередь, в которой записи расшифровываются и конспектируются по одной, не мешая записи следующей пары."""
    jobs: queue.Queue = queue.Queue()

    def worker():
        while True:
            job = jobs.get()
            try:
                process_session(**job)
            except Exception as e:
                print(f"\n[ошибка при обработке {job['source']}] {e}")
            finally:
                jobs.task_done()

    threading.Thread(target=worker, daemon=True).start()
    return jobs


def wait_jobs(jobs: queue.Queue) -> None:
    if not jobs.unfinished_tasks:
        return
    print("Жду, пока закончится обработка записи (ещё раз Ctrl+C, чтобы выйти сразу)...")
    try:
        while jobs.unfinished_tasks:
            time.sleep(1)
    except KeyboardInterrupt:
        pass


def run_schedule(cfg: dict) -> None:
    """Сам пишет очные пары микрофоном и напоминает про дистанционные."""
    meta, pairs = load_schedule(cfg)
    if not pairs:
        raise SystemExit("В расписании нет пар для твоей подгруппы.")

    early = timedelta(minutes=cfg["schedule"]["start_early_min"])
    extra = timedelta(minutes=cfg["schedule"]["extra_min"])
    jobs = start_worker()
    print(f"{meta['group']}, подгруппа {meta['subgroup']}. Занятий в расписании: "
          f"{sum(len(p['dates']) for p in pairs)}. Остановить: Ctrl+C.")
    print("Очные пары запишу микрофоном сам, про дистанционные напомню. "
          "Не давай компьютеру уходить в сон.\n")
    reminded: set = set()

    try:
        while True:
            moment = now_msk(meta)
            ahead = [o for o in occurrences(pairs, meta, moment.date(), moment.date() + timedelta(days=21))
                     if o[1] + extra > moment]
            if not ahead:
                print("Пар в ближайшие три недели нет. Проверю снова через час.")
                time.sleep(3600)
                continue

            start, end, pair, when = ahead[0]
            rec_start, rec_end = start - early, end + extra

            if pair["remote"]:
                key = (pair["subject"], pair["kind"], when)
                if moment < rec_start:
                    sleep_until(rec_start)
                    continue
                if key not in reminded:
                    reminded.add(key)
                    print(f"\n● {moment:%H:%M} Дистанционная пара: {pair['subject']}, "
                          f"{pair_title(meta, pair, when)} до {end:%H:%M}.")
                    print("  Открой вкладку и нажми запись в расширении "
                          "(агент должен быть запущен командой serve).")
                sleep_until(rec_end + timedelta(minutes=1))
                continue

            if moment < rec_start:
                print(f"Следующая очная пара: {pair['subject']}, {pair_title(meta, pair, when)}, "
                      f"{DAY_FULL[start.weekday()]} {start:%d.%m} в {start:%H:%M} "
                      f"(запись через {fmt_wait((rec_start - moment).total_seconds())})")
                sleep_until(rec_start)
                continue

            folder = new_session_dir(cfg, pair["subject"], start, pair_label(meta, pair, when))
            audio = folder / "audio.flac"
            print(f"\n● {now_msk(meta):%H:%M} Записываю: {pair['subject']}, "
                  f"{pair_title(meta, pair, when)} до {rec_end:%H:%M}")
            seconds, interrupted = record_audio(
                audio, cfg, max_seconds=(rec_end - now_msk(meta)).total_seconds(), live=False)
            print(f"  Запись закончена ({fmt_ts(seconds)}), расшифровка и конспект пойдут в фоне.")
            if seconds >= 30:
                jobs.put({"source": audio, "subject": pair["subject"], "when": start,
                          "cfg": cfg, "folder": folder,
                          "meta": {"kind": pair["kind"], "teacher": pair["teacher"],
                                   "room": pair["room"], "title": pair_title(meta, pair, when),
                                   "format": "очно"}})
            if interrupted:
                raise KeyboardInterrupt
    except KeyboardInterrupt:
        print("\nОстанавливаюсь.")
        wait_jobs(jobs)


# ---------------------------------------------------------------- приём записей из браузера

WEB_DIR = BASE_DIR / "web"
MAX_CHUNK = 64 * 1024 * 1024          # один кусок записи
MAX_SESSION = 4 * 1024 * 1024 * 1024  # одна запись целиком
SESSION_ID = re.compile(r"[A-Za-z0-9_.-]{1,64}")


def run_server(cfg: dict, open_browser: bool = False) -> None:
    """
    Слушает 127.0.0.1 и принимает звук вкладки из расширения для браузера.
    Расширение шлёт запись кусками по мере её появления, поэтому файл на диске
    остаётся целым, даже если браузер закроется посреди пары.
    """
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from urllib.parse import parse_qs, urlsplit

    port = int(cfg["server"]["port"])
    jobs = start_worker()
    sessions: dict[str, dict] = {}
    lock = threading.Lock()
    try:
        meta, pairs = load_schedule(cfg)
    except SystemExit as e:
        print(f"Расписание не загрузилось ({e}), названия придётся вводить руками.")
        meta, pairs = None, []

    def current_pair(moment: datetime) -> dict | None:
        """Что идёт по расписанию в этот момент, вместе с номером занятия."""
        if not pairs:
            return None
        found = pair_at(pairs, meta, moment.astimezone(meta["tz"]))
        if not found:
            return None
        start, end, pair, day = found
        n, total = occurrence_number(meta, pair, day)
        return {"subject": pair["subject"], "kind": pair["kind"], "number": n, "total": total,
                "title": pair_title(meta, pair, day), "label": pair_label(meta, pair, day),
                "teacher": pair["teacher"], "room": pair["room"],
                "format": "дистанционно" if pair["remote"] else "очно",
                "link": pair["link"],
                "start": start, "end": end}

    def open_session(sid: str, params: dict) -> dict:
        with lock:
            s = sessions.get(sid)
            if s is not None:
                return s
            tz = meta["tz"] if meta else timezone.utc
            when = datetime.now(tz)
            try:  # расширение присылает момент начала записи в миллисекундах
                sent = datetime.fromtimestamp(int(params["started"][0]) / 1000, tz)
                # верим часам браузера, только если они близки к нашим
                if abs((sent - when).total_seconds()) < 600:
                    when = sent
            except (KeyError, ValueError, OverflowError, OSError):
                pass

            auto = params.get("auto", ["0"])[0] == "1"
            info = current_pair(when) if auto else None
            if info:
                subject, label = info["subject"], info["label"]
                when = info["start"]
                note = f"{info['title']}"
            else:
                subject = (params.get("subject", [""])[0] or cfg["server"]["default_subject"]).strip()
                label, note = "", "вне расписания"

            folder = new_session_dir(cfg, subject, when.replace(tzinfo=None), label)
            s = {"folder": folder, "path": folder / "audio.webm", "subject": subject,
                 "when": when.replace(tzinfo=None), "bytes": 0, "lock": threading.Lock(),
                 "meta": {"kind": info["kind"], "teacher": info["teacher"], "room": info["room"],
                          "title": info["title"], "format": info["format"]} if info else None}
            sessions[sid] = s
            print(f"[{datetime.now(tz):%H:%M}] Пишу «{subject}» ({note}) из браузера в {folder.name}")
            return s

    mic = {"active": False, "subject": "", "title": "", "seconds": 0.0, "peak": 0.0,
           "stop": None, "thread": None, "folder": None, "error": ""}

    def start_mic(subject_override: str = "") -> dict:
        if mic["active"]:
            return {"error": "запись уже идёт"}
        tz = meta["tz"] if meta else timezone.utc
        now = datetime.now(tz)
        info = None if subject_override else current_pair(now)
        if info:
            subject, label, when = info["subject"], info["label"], info["start"]
            title = info["title"]
            job_meta = {"kind": info["kind"], "teacher": info["teacher"], "room": info["room"],
                        "title": info["title"], "format": info["format"]}
        else:
            subject = (subject_override or cfg["server"]["default_subject"]).strip()
            label, when, title, job_meta = "", now, "вне расписания", None

        folder = new_session_dir(cfg, subject, when.replace(tzinfo=None), label)
        audio = folder / "audio.flac"
        stop = threading.Event()

        def tick(seconds, peak):
            mic["seconds"], mic["peak"] = seconds, peak

        def worker():
            try:
                seconds, _ = record_audio(audio, cfg, live=False, stop_event=stop, on_tick=tick)
                print(f"[{datetime.now(tz):%H:%M}] Запись «{subject}» закончена ({fmt_ts(seconds)}).")
                if seconds >= 3:
                    jobs.put({"source": audio, "subject": subject, "when": when.replace(tzinfo=None),
                              "cfg": cfg, "folder": folder, "meta": job_meta})
            except Exception as e:
                message = str(e)
                if "PortAudio" in message:
                    message = "микрофон недоступен, проверь разрешения и устройство"
                print(f"[микрофон] запись не удалась: {e}")
                mic["error"] = message
                set_status("idle")
            finally:
                mic.update({"active": False, "stop": None, "thread": None})
                if not audio.exists() and not any(folder.iterdir()):
                    folder.rmdir()  # пустую папку от неудачной записи не оставляем

        thread = threading.Thread(target=worker, daemon=True)
        mic.update({"active": True, "subject": subject, "title": title, "seconds": 0.0,
                    "peak": 0.0, "stop": stop, "thread": thread, "folder": folder, "error": ""})
        thread.start()
        print(f"[{datetime.now(tz):%H:%M}] Пишу «{subject}» ({title}) микрофоном в {folder.name}")
        return {"ok": True, "subject": subject, "title": title}

    def stop_mic() -> dict:
        if not mic["active"]:
            return {"error": "запись не идёт"}
        seconds, folder = mic["seconds"], mic["folder"]
        mic["stop"].set()
        thread = mic["thread"]
        if thread:
            thread.join(timeout=10)
        return {"ok": True, "seconds": round(seconds), "folder": folder.name if folder else ""}

    def safe_path(raw: str) -> Path | None:
        """Путь внутри папки с записями и никуда больше."""
        try:
            target = (cfg["output_dir"] / raw).resolve()
            target.relative_to(cfg["output_dir"].resolve())
            return target
        except (ValueError, OSError):
            return None

    def list_lectures() -> list[dict]:
        root = cfg["output_dir"]
        items = []
        if not root.exists():
            return items
        for subject_dir in sorted(root.iterdir()):
            if not subject_dir.is_dir():
                continue
            for session in sorted(subject_dir.iterdir(), reverse=True):
                if not session.is_dir():
                    continue
                audio = next((f.name for f in session.iterdir()
                              if f.suffix in (".flac", ".webm", ".m4a", ".mp3", ".wav")), "")
                items.append({
                    "path": f"{subject_dir.name}/{session.name}",
                    "subject": subject_dir.name,
                    "session": session.name,
                    "notes": (session / "notes.md").exists(),
                    "transcript": (session / "transcript.txt").exists(),
                    "audio": audio,
                    "time": session.stat().st_mtime,
                })
        items.sort(key=lambda x: x["time"], reverse=True)
        return items

    def week_payload(offset: int) -> dict:
        if not meta:
            return {"days": [], "error": "расписание не загружено"}
        moment = now_msk(meta)
        monday = moment.date() - timedelta(days=moment.weekday()) + timedelta(days=7 * offset)
        done = {i["path"] for i in list_lectures()}
        days = []
        for i in range(7):
            day = monday + timedelta(days=i)
            entries = []
            for start, end, pair, when in occurrences(pairs, meta, day, day):
                n, total = occurrence_number(meta, pair, when)
                label = pair_label(meta, pair, when)
                match = next((p for p in done if p.endswith(f"/{day:%Y-%m-%d} {label}")), None)
                entries.append({
                    "subject": pair["subject"], "kind": pair["kind"],
                    "title": pair_title(meta, pair, when), "number": n, "total": total,
                    "teacher": pair["teacher"], "room": pair["room"],
                    "remote": pair["remote"],
                    "start": start.strftime("%H:%M"), "end": end.strftime("%H:%M"),
                    "state": ("now" if start <= moment <= end else
                              "past" if moment > end else "future"),
                    "link": pair["link"],
                    "recorded": match,
                })
            days.append({"date": day.isoformat(), "weekday": DAY_FULL[day.weekday()],
                         "day": day.day, "month": MONTHS[day.month - 1],
                         "today": day == moment.date(), "pairs": entries})
        return {"days": days, "offset": offset,
                "range": f"{monday:%d.%m} - {monday + timedelta(days=6):%d.%m}"}

    def state_payload() -> dict:
        tz = meta["tz"] if meta else timezone.utc
        moment = datetime.now(tz)
        info = current_pair(moment) if meta else None
        following = None
        if meta:
            ahead = [o for o in occurrences(pairs, meta, moment.date(), moment.date() + timedelta(days=14))
                     if o[0] > moment]
            if ahead:
                start, end, pair, when = ahead[0]
                following = {"subject": pair["subject"], "title": pair_title(meta, pair, when),
                             "start": start.strftime("%H:%M"), "remote": pair["remote"],
                             "date": start.date().isoformat(),
                             "in_minutes": int((start - moment).total_seconds() // 60)}
        return {
            "group": meta["group"] if meta else "",
            "subgroup": meta["subgroup"] if meta else "",
            "time": moment.strftime("%H:%M"),
            "date": moment.date().isoformat(),
            "current": {k: v for k, v in info.items() if k not in ("start", "end")} | {
                "start": info["start"].strftime("%H:%M"), "end": info["end"].strftime("%H:%M"),
                "link": info.get("link", ""),
            } if info else None,
            "next": following,
            "recording": {"active": mic["active"], "subject": mic["subject"], "title": mic["title"],
                          "seconds": round(mic["seconds"]), "peak": round(mic["peak"], 3),
                          "error": mic["error"]},
            "browser": bool(sessions),
            "status": get_status(),
            "queue": jobs.unfinished_tasks,
        }

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "lecture-agent"

        def log_message(self, *a):  # без строчки в консоли на каждый кусок записи
            pass

        def cors(self):
            self.send_header("Access-Control-Allow-Origin", "*")

        def reply(self, code: int, payload: dict):
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.cors()
            self.end_headers()
            self.wfile.write(body)

        def do_OPTIONS(self):
            self.send_response(204)
            self.cors()
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "*")
            self.send_header("Access-Control-Max-Age", "86400")
            self.send_header("Content-Length", "0")
            self.end_headers()

        def send_file(self, file: Path, mime: str):
            body = file.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            url = urlsplit(self.path)
            path, query = url.path, parse_qs(url.query)

            if path in ("/", "/index.html"):
                page = WEB_DIR / "index.html"
                if not page.exists():
                    self.reply(500, {"error": "нет файла web/index.html"})
                    return
                self.send_file(page, "text/html; charset=utf-8")
                return
            if path == "/api/state":
                self.reply(200, state_payload())
                return
            if path == "/api/week":
                try:
                    offset = int(query.get("offset", ["0"])[0])
                except ValueError:
                    offset = 0
                self.reply(200, week_payload(max(-30, min(30, offset))))
                return
            if path == "/api/lectures":
                self.reply(200, {"items": list_lectures()})
                return
            if path == "/api/lecture":
                target = safe_path(query.get("path", [""])[0])
                if not target or not target.is_dir():
                    self.reply(404, {"error": "запись не найдена"})
                    return
                def read(name):
                    file = target / name
                    return file.read_text(encoding="utf-8") if file.exists() else ""
                self.reply(200, {"path": query.get("path", [""])[0],
                                 "notes": read("notes.md"),
                                 "transcript": read("transcript.txt"),
                                 "folder": str(target)})
                return
            if path == "/api/audio":
                target = safe_path(query.get("path", [""])[0])
                if not target or not target.is_file():
                    self.reply(404, {"error": "файл не найден"})
                    return
                mime = {"flac": "audio/flac", "webm": "audio/webm", "mp3": "audio/mpeg",
                        "wav": "audio/wav", "m4a": "audio/mp4"}.get(target.suffix.lstrip("."), "audio/*")
                self.send_file(target, mime)
                return
            if path == "/ping":
                self.reply(200, {"ok": True, "app": "lecture-agent"})
            elif path == "/now":
                tz = meta["tz"] if meta else timezone.utc
                info = current_pair(datetime.now(tz))
                if not info:
                    self.reply(200, {"pair": None})
                    return
                self.reply(200, {"pair": {
                    "subject": info["subject"], "kind": info["kind"],
                    "number": info["number"], "total": info["total"],
                    "title": info["title"], "teacher": info["teacher"], "room": info["room"],
                    "format": info["format"],
                    "link": info.get("link", ""),
                    "start": info["start"].strftime("%H:%M"),
                    "end": info["end"].strftime("%H:%M"),
                }})
            else:
                self.reply(404, {"error": "unknown endpoint"})

        def do_POST(self):
            url = urlsplit(self.path)
            params = parse_qs(url.query)
            if url.path in ("/api/record/start", "/api/record/stop"):
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else b"{}"
                try:
                    data = json.loads(body.decode("utf-8") or "{}")
                except ValueError:
                    data = {}
                if url.path.endswith("start"):
                    self.reply(200, start_mic(str(data.get("subject", "")).strip()))
                else:
                    self.reply(200, stop_mic())
                return
            length = int(self.headers.get("Content-Length") or 0)
            if length > MAX_CHUNK:
                self.reply(413, {"error": "chunk too large"})
                return
            body = self.rfile.read(length) if length else b""

            sid = params.get("session", [""])[0]
            if not SESSION_ID.fullmatch(sid):
                self.reply(400, {"error": "bad session id"})
                return

            if url.path == "/chunk":
                s = open_session(sid, params)
                if s["bytes"] + len(body) > MAX_SESSION:
                    self.reply(413, {"error": "session too large"})
                    return
                with s["lock"]:
                    with open(s["path"], "ab") as f:
                        f.write(body)
                    s["bytes"] += len(body)
                self.reply(200, {"ok": True, "bytes": s["bytes"]})

            elif url.path == "/finish":
                with lock:
                    s = sessions.pop(sid, None)
                if s is None:
                    self.reply(404, {"error": "unknown session"})
                    return
                size_mb = s["bytes"] / 1024 / 1024
                if s["bytes"] < 20_000:
                    print(f"[{datetime.now():%H:%M}] Запись «{s['subject']}» пустая "
                          "или совсем короткая, обрабатывать нечего. "
                          "Проверь, что во вкладке был звук.")
                    self.reply(200, {"ok": True, "queued": False})
                    return
                print(f"[{datetime.now():%H:%M}] Запись «{s['subject']}» получена "
                      f"({size_mb:.1f} МБ), обрабатываю.")
                jobs.put({"source": s["path"], "subject": s["subject"], "when": s["when"],
                          "cfg": cfg, "folder": s["folder"], "meta": s["meta"]})
                self.reply(200, {"ok": True, "queued": True, "folder": str(s["folder"])})
            else:
                self.reply(404, {"error": "unknown endpoint"})

    # На Windows SO_REUSEADDR разрешает второму процессу занять уже занятый порт,
    # и тогда два агента молча делят запросы между собой. Выключаем, чтобы второй
    # честно падал с ошибкой. На остальных системах оставляем: там этот флаг
    # означает совсем другое и без него порт залипает в TIME_WAIT после перезапуска.
    ThreadingHTTPServer.allow_reuse_address = os.name != "nt"
    httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    httpd.daemon_threads = True
    address = f"http://127.0.0.1:{port}"
    print(f"Приложение открыто на {address} (снаружи не видно).")
    print("Там расписание, кнопка записи и все конспекты. Остановить: Ctrl+C.\n")
    if open_browser:
        import webbrowser
        threading.Timer(0.7, lambda: webbrowser.open(address)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nОстанавливаюсь.")
    finally:
        httpd.shutdown()
        httpd.server_close()
        # Запись микрофоном идёт в отдельном потоке, и он daemon: без этого
        # выход из программы убил бы его прямо посреди файла, а задание на
        # расшифровку в очередь так и не попало бы. stop_mic закрывает файл
        # штатно и кладёт задание, поэтому вызываем его до wait_jobs.
        if mic["active"]:
            folder = mic["folder"]
            print("Дописываю запись с микрофона перед выходом.")
            stop_mic()
            if mic["active"] and folder:  # поток не успел закрыться за отведённое время
                print(f"Запись не закрылась сама. Потом: python agent.py process \"{folder}\"")
        with lock:
            leftovers = list(sessions.values())
        for s in leftovers:  # браузер не успел сказать «готово», но звук уже на диске
            print(f"Незавершённая запись осталась в {s['path']}")
        wait_jobs(jobs)


# ---------------------------------------------------------------- команды

def lookup_pair(cfg: dict, moment: datetime) -> tuple | None:
    """Что стоит в расписании на это время: (предмет, метка папки, данные занятия, начало пары)."""
    try:
        meta, pairs = load_schedule(cfg)
    except SystemExit:
        return None
    tz = meta["tz"]
    aware = moment if moment.tzinfo else moment.replace(tzinfo=tz)
    found = pair_at(pairs, meta, aware.astimezone(tz))
    if not found:
        return None
    start, end, pair, day = found
    info = {"kind": pair["kind"], "teacher": pair["teacher"], "room": pair["room"],
            "title": pair_title(meta, pair, day),
            "format": "дистанционно" if pair["remote"] else "очно"}
    return pair["subject"], pair_label(meta, pair, day), info, start.replace(tzinfo=None)


def resolve_subject(args, cfg: dict, moment: datetime) -> tuple:
    """Берём предмет из аргумента, а если его нет, ищем пару в расписании."""
    if args.subject:
        return args.subject, "", None, moment
    found = lookup_pair(cfg, moment)
    if found:
        subject, label, info, start = found
        print(f"По расписанию сейчас: {subject}, {info['title']}.")
        return subject, label, info, start
    print("В расписании на это время пары нет, назову запись «Пара». "
          "Можно задать название через -s.")
    return "Пара", "", None, moment


def cmd_record(args, cfg: dict) -> None:
    subject, label, info, when = resolve_subject(args, cfg, datetime.now())
    folder = new_session_dir(cfg, subject, when, label)
    audio = folder / "audio.flac"
    limit = f" или само через {args.minutes:g} мин" if args.minutes else ""
    print(f"Записываю «{subject}». Остановить: Ctrl+C{limit}.")
    seconds, _ = record_audio(audio, cfg, max_seconds=args.minutes * 60 if args.minutes else None)
    print(f"Сохранено: {audio} ({fmt_ts(seconds)})")
    if seconds < 3:
        print("Запись слишком короткая, обрабатывать нечего.")
        return
    process_session(audio, subject, when, cfg, folder, make_notes=not args.no_notes, meta=info)


def cmd_process(args, cfg: dict) -> None:
    source = Path(args.file).expanduser().resolve()
    if not source.exists():
        raise SystemExit(f"Файл не найден: {source}")
    subject, label, info, when = resolve_subject(
        args, cfg, datetime.fromtimestamp(source.stat().st_mtime))
    folder = new_session_dir(cfg, subject, when, label)
    process_session(source, subject, when, cfg, folder, make_notes=not args.no_notes, meta=info)


def main() -> None:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")

    parser = argparse.ArgumentParser(description="Агент для записи и конспектирования пар")
    parser.add_argument("-c", "--config", default=str(BASE_DIR / "config.yaml"),
                        help="путь к config.yaml")
    sub = parser.add_subparsers(dest="command", required=True)

    rec = sub.add_parser("record", help="записать пару с микрофона и сделать конспект")
    rec.add_argument("-s", "--subject", help="название предмета (по умолчанию берётся из расписания)")
    rec.add_argument("-m", "--minutes", type=float, help="остановить запись через N минут")
    rec.add_argument("--no-notes", action="store_true", help="только запись и расшифровка")

    proc = sub.add_parser("process", help="обработать готовую запись (mp3, m4a, wav...) или расшифровку .txt")
    proc.add_argument("file", help="путь к файлу")
    proc.add_argument("-s", "--subject", help="название предмета (по умолчанию берётся из расписания)")
    proc.add_argument("--no-notes", action="store_true", help="только расшифровка")

    tt = sub.add_parser("timetable", help="посмотреть расписание")
    tt.add_argument("-w", "--week", action="store_true", help="вся неделя, а не только сегодня")
    tt.add_argument("-d", "--date", help="конкретный день, например 2026-10-14")
    tt.add_argument("-s", "--subject", help="все занятия по предмету")

    sub.add_parser("schedule", help="самому записывать очные пары по расписанию")
    sub.add_parser("ui", help="открыть приложение в браузере (расписание, запись, конспекты)")
    sub.add_parser("serve", help="то же самое, но без открытия браузера")
    sub.add_parser("devices", help="показать список микрофонов")

    args = parser.parse_args()
    cfg = load_config(Path(args.config).expanduser())

    if args.command == "devices":
        list_devices()
    elif args.command == "record":
        cmd_record(args, cfg)
    elif args.command == "process":
        cmd_process(args, cfg)
    elif args.command == "timetable":
        cmd_timetable(args, cfg)
    elif args.command == "schedule":
        run_schedule(cfg)
    elif args.command in ("serve", "ui"):
        run_server(cfg, open_browser=args.command == "ui")


if __name__ == "__main__":
    main()
