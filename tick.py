"""
Планер дня 🐝
- Ежедневные привычки (лист «Шаблон»: название, описание)
- Разовые дела (команда /add Название, описание)
- Утром форма с кнопками-галочками и полосой прогресса, вечером крутая сводка
Данные и состояние лежат в Google Таблице. Работает и в GitHub Actions
(main), и постоянно на Railway (worker.py).
"""
import html
import json
import logging
import os
import re
from collections import defaultdict
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import gspread
import requests
from google.oauth2.service_account import Credentials

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("tick")

TOKEN = os.environ["BOT_TOKEN"]
CHAT_ID = int(os.environ["CHAT_ID"])
SHEET_ID = os.environ["SHEET_ID"]
TZ = ZoneInfo(os.environ.get("TZ_NAME", "Europe/Moscow"))
MORNING = os.environ.get("MORNING_TIME", "08:00")
EVENING = os.environ.get("EVENING_TIME", "21:30")
CARRY_OVER = os.environ.get("CARRY_OVER", "1") == "1"  # переносить невыполненные дела

WEEKDAYS = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
WEEKDAYS_FULL = [
    "понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье",
]
MONTHS = [
    "января", "февраля", "марта", "апреля", "мая", "июня",
    "июля", "августа", "сентября", "октября", "ноября", "декабря",
]

# ───────────────────────── Telegram ─────────────────────────


def tg(method: str, **params):
    params = {k: v for k, v in params.items() if v is not None}
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TOKEN}/{method}", json=params, timeout=40
        )
        data = r.json()
    except Exception as e:
        log.warning("%s failed: %s", method, type(e).__name__)
        return {"ok": False}
    if not data.get("ok"):
        log.warning("%s: %s", method, data.get("description"))
    return data


def send(chat_id, text, kb=None):
    return tg("sendMessage", chat_id=chat_id, text=text, reply_markup=kb, parse_mode="HTML")


def esc(s: str) -> str:
    return html.escape(s or "", quote=False)


# ───────────────────────── Google Sheets ─────────────────────────

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]
_book = None
_ws_cache: dict = {}


def book():
    global _book
    if _book is None:
        info = json.loads(os.environ["GOOGLE_CREDS_JSON"])
        creds = Credentials.from_service_account_info(info, scopes=SCOPES)
        _book = gspread.authorize(creds).open_by_key(SHEET_ID)
    return _book


def sheet(name, header, sample=None):
    if name in _ws_cache:
        return _ws_cache[name]
    try:
        ws = book().worksheet(name)
    except gspread.WorksheetNotFound:
        ws = book().add_worksheet(title=name, rows=1000, cols=len(header))
        ws.append_row(header, value_input_option="RAW")
        for row in sample or []:
            ws.append_row(row, value_input_option="RAW")
    _ws_cache[name] = ws
    return ws


TIME_RE = re.compile(r"^\s*\d{1,2}:\d{2}(:\d{2})?\s*$")


def template_ws():
    ws = sheet(
        "Шаблон",
        ["Привычка", "Описание"],
        [
            ["Зарядка", "15 минут утренней разминки"],
            ["Чтение", "20 страниц книги"],
            ["Вода", "Выпить 2 литра за день"],
        ],
    )
    if "tpl_hdr" not in _ws_cache:  # старый формат: «Задача | Напоминание»
        _ws_cache["tpl_hdr"] = True
        hdr = ws.row_values(1)
        if hdr and hdr[0].strip() == "Задача":
            ws.update_cell(1, 1, "Привычка")
        if len(hdr) > 1 and hdr[1].lower().startswith("напомин"):
            ws.update_cell(1, 2, "Описание")
    return ws


def days_ws():
    # A дата | B тип | C название | D описание | E готово | F перенесено | G удалено
    ws = sheet(
        "Дни", ["Дата", "Тип", "Название", "Описание", "Готово", "Перенесено", "Удалено"]
    )
    if "days_hdr" not in _ws_cache:
        _ws_cache["days_hdr"] = True
        if ws.col_count < 7:
            ws.add_cols(7 - ws.col_count)
        if len(ws.row_values(1)) < 7:
            ws.update_cell(1, 7, "Удалено")
    return ws


def norm_date(s: str) -> str:
    s = (s or "").strip()
    for fmt in ("%Y-%m-%d", "%d.%m.%Y", "%d/%m/%Y"):
        try:
            return datetime.strptime(s, fmt).strftime("%Y-%m-%d")
        except ValueError:
            pass
    return s


# ── состояние бота (лист «Состояние»)

_state = None


def _state_ws():
    return sheet("Состояние", ["Ключ", "Значение"])


def state_get(key: str, default: str = "") -> str:
    global _state
    if _state is None:
        _state = {
            r[0]: (i, r[1] if len(r) > 1 else "")
            for i, r in enumerate(_state_ws().get_all_values()[1:], start=2)
            if r and r[0]
        }
    return _state.get(key, (0, default))[1]


def state_set(key: str, value: str):
    state_get(key)
    ws = _state_ws()
    if key in _state:
        row = _state[key][0]
        ws.update_cell(row, 2, value)
    else:
        row = len(_state) + 2
        ws.append_row([key, value], value_input_option="RAW")
    _state[key] = (row, value)


# ── привычки и дела


def template_habits() -> list[tuple[str, str]]:
    out = []
    for r in template_ws().get_all_values()[1:]:
        r = r + [""] * (2 - len(r))
        name, desc = r[0].strip(), r[1].strip()
        if not name:
            continue
        if TIME_RE.match(desc):  # остатки старого формата (время напоминания)
            desc = ""
        out.append((name, desc))
    return out


def load_rows() -> list[dict]:
    out = []
    for i, r in enumerate(days_ws().get_all_values()[1:], start=2):
        r = r + [""] * (7 - len(r))
        if not r[2].strip() or r[6].strip() == "1":
            continue
        out.append(
            {
                "row": i,
                "date": norm_date(r[0]),
                "kind": "habit" if r[1].strip().lower().startswith("прив") else "task",
                "name": r[2].strip(),
                "desc": r[3].strip(),
                "done": r[4].strip() == "1",
                "carried": r[5].strip() == "1",
            }
        )
    return out


def group_by_date(rows: list[dict]) -> dict:
    by = defaultdict(list)
    for r in rows:
        by[r["date"]].append(r)
    return by


def get_day(date_str: str) -> list[dict]:
    return [r for r in load_rows() if r["date"] == date_str]


def ensure_day(date_str: str) -> list[dict]:
    """Создаёт список дня: привычки из шаблона + невыполненные дела вчерашнего дня."""
    rows = load_rows()
    today_rows = [r for r in rows if r["date"] == date_str]
    if today_rows:
        return sync_habits(date_str, today_rows)
    new = [[date_str, "привычка", n, d, "", ""] for n, d in template_habits()]
    if CARRY_OVER:
        prev = [r["date"] for r in rows if r["date"] < date_str]
        if prev:
            last = max(prev)
            for r in rows:
                if r["date"] == last and r["kind"] == "task" and not r["done"]:
                    new.append([date_str, "дело", r["name"], r["desc"], "", "1"])
    if new:
        days_ws().append_rows(new, value_input_option="RAW")
    return get_day(date_str)


def sync_habits(date_str: str, today_rows: list[dict]) -> list[dict]:
    """Подтягивает правки из листа «Шаблон» в уже созданный список дня:
    новые привычки добавляются, изменённые описания обновляются."""
    have = {r["name"].lower(): r for r in today_rows if r["kind"] == "habit"}
    ws = days_ws()
    new, changed = [], False
    for name, desc in template_habits():
        key = name.lower()
        if key in have:
            r = have[key]
            if r.get("row") and r["desc"] != desc:
                ws.update_cell(r["row"], 4, desc)
                changed = True
        else:
            new.append([date_str, "привычка", name, desc, "", ""])
            have[key] = {"row": None, "desc": desc}
    if new:
        ws.append_rows(new, value_input_option="RAW")
        changed = True
    return get_day(date_str) if changed else today_rows


def remove_from_template(name: str):
    ws = template_ws()
    key = name.strip().lower()
    hits = [
        i
        for i, r in enumerate(ws.get_all_values()[1:], start=2)
        if r and r[0].strip().lower() == key
    ]
    for i in reversed(hits):
        ws.delete_rows(i)


def delete_item(row: int):
    """Привычка удаляется из «Шаблона» и из сегодняшнего списка, дело — только из списка.
    Строки в «Дни» не удаляем, а помечаем, чтобы номера строк у кнопок не сдвигались."""
    rows = load_rows()
    it = next((r for r in rows if r["row"] == row), None)
    if not it:
        return None
    if it["kind"] == "habit":
        remove_from_template(it["name"])
    ws = days_ws()
    for r in rows:
        if r["date"] == it["date"] and r["kind"] == it["kind"] and r["name"].lower() == it["name"].lower():
            ws.update_cell(r["row"], 7, "1")
    return it


def find_items(items: list[dict], query: str) -> list[dict]:
    q = query.strip().lower()
    exact = [i for i in items if i["name"].lower() == q]
    return exact or [i for i in items if q in i["name"].lower()]


def toggle(row: int) -> str:
    ws = days_ws()
    vals = ws.row_values(row) + [""] * 6
    ws.update_cell(row, 5, "" if vals[4].strip() == "1" else "1")
    return norm_date(vals[0])


def add_task(date_str: str, name: str, desc: str):
    days_ws().append_row([date_str, "дело", name, desc, "", ""], value_input_option="RAW")


def add_habit(date_str: str, name: str, desc: str):
    template_ws().append_row([name, desc], value_input_option="RAW")
    # если список на сегодня уже создан, добавим привычку и в него
    if any(r["date"] == date_str for r in load_rows()):
        days_ws().append_row(
            [date_str, "привычка", name, desc, "", ""], value_input_option="RAW"
        )


# ───────────────────────── Оформление ─────────────────────────


def bee_bar(pct: int, n: int = 10) -> str:
    """🟩🟩🟩🐝⬜⬜⬜ — пчёлка летит по полосе; на 100% её ждёт мёд."""
    pos = min(n - 1, int(pct / 100 * (n - 1) + 0.5)) if pct < 100 else n - 1
    head = "🍯" if pct >= 100 else "🐝"
    return "🟩" * pos + head + "⬜" * (n - pos - 1)


def plural_days(n: int) -> str:
    if 11 <= n % 100 <= 14:
        return f"{n} дней"
    return f"{n} " + {1: "день", 2: "дня", 3: "дня", 4: "дня"}.get(n % 10, "дней")


def pct_of(done: int, total: int) -> int:
    return round(100 * done / total) if total else 0


def split(items: list[dict]):
    return (
        [i for i in items if i["kind"] == "habit"],
        [i for i in items if i["kind"] == "task"],
    )


def item_line(i: dict, show_state: bool = True) -> str:
    mark = ("✅" if i["done"] else "▫️") if show_state else "•"
    line = f"{mark} {esc(i['name'])}"
    if i.get("carried"):
        line += " ↩️"
    if i["desc"]:
        line += f" — <i>{esc(i['desc'])}</i>"
    return line


def render_form(date_str: str, items: list[dict]):
    d = datetime.strptime(date_str, "%Y-%m-%d")
    habits, tasks = split(items)
    done = sum(i["done"] for i in items)
    pct = pct_of(done, len(items))
    lines = [
        f"🐝 <b>План на {d.day} {MONTHS[d.month - 1]}, {WEEKDAYS_FULL[d.weekday()]}</b>",
        "",
        f"{bee_bar(pct)}  <b>{pct}%</b> · {done}/{len(items)}",
    ]
    if habits:
        lines += ["", f"🌿 <b>Привычки</b> · {sum(h['done'] for h in habits)}/{len(habits)}"]
        lines += [item_line(h) for h in habits]
    if tasks:
        lines += ["", f"📌 <b>Дела</b> · {sum(t['done'] for t in tasks)}/{len(tasks)}"]
        lines += [item_line(t) for t in tasks]
    lines += ["", "Жми на кнопки, чтобы отметить 👇"]
    if not os.environ.get("INSTANT"):
        lines.append("(отметка обновится в течение нескольких минут)")

    kb = []
    for i in items:
        label = ("✅ " if i["done"] else "⬜ ") + ("📌 " if i["kind"] == "task" else "") + i["name"]
        kb.append([{"text": label[:48], "callback_data": f"t:{i['row']}"}])
    kb.append([{"text": "🐝 Меню команд", "callback_data": "m"}])
    return "\n".join(lines), {"inline_keyboard": kb}


def streak_ending(by: dict, end: datetime) -> int:
    n = 0
    d = end
    while True:
        habits = [i for i in by.get(d.strftime("%Y-%m-%d"), []) if i["kind"] == "habit"]
        if habits and all(h["done"] for h in habits):
            n += 1
            d -= timedelta(days=1)
        else:
            return n


def day_emoji(items: list[dict]) -> str:
    if not items:
        return "▫️"
    pct = pct_of(sum(i["done"] for i in items), len(items))
    return "🟩" if pct >= 80 else "🟨" if pct >= 50 else "🟧" if pct > 0 else "🟥"


def mood(pct: int) -> str:
    if pct >= 100:
        return "🍯 Идеальный день! Весь мёд — в улей!"
    if pct >= 80:
        return "🐝 Отличная работа — улей гудит от радости!"
    if pct >= 50:
        return "🌼 Хороший день: пчёлка славно потрудилась!"
    if pct > 0:
        return "🌱 Маленькие шаги — тоже мёд. Завтра полетим дальше!"
    return "🌙 Бывают и такие дни. Отдыхаем — завтра новый цветочек 🌼"


def render_summary(date_str: str, rows: list[dict]) -> str:
    by = group_by_date(rows)
    d = datetime.strptime(date_str, "%Y-%m-%d")
    items = by.get(date_str, [])
    head = f"🐝 <b>Итоги дня · {d.day} {MONTHS[d.month - 1]}</b>"
    if not items:
        return head + "\n\nСегодня план был пуст. Добавь дело командой /add 🌼"

    habits, tasks = split(items)
    done = sum(i["done"] for i in items)
    pct = pct_of(done, len(items))
    hd = sum(h["done"] for h in habits)
    td = sum(t["done"] for t in tasks)

    out = [head, "", f"{bee_bar(pct)}  <b>{pct}%</b> · {done}/{len(items)}", ""]
    if habits:
        out.append(f"🌿 Привычки: <b>{hd}/{len(habits)}</b> · {pct_of(hd, len(habits))}%")
    if tasks:
        out.append(f"📌 Дела: <b>{td}/{len(tasks)}</b> · {pct_of(td, len(tasks))}%")

    ok = [i for i in items if i["done"]]
    miss = [i for i in items if not i["done"]]
    if ok:
        out += ["", "✅ <b>Сделано</b>"] + [f"• {esc(i['name'])}" for i in ok[:12]]
        if len(ok) > 12:
            out.append(f"…и ещё {len(ok) - 12}")
    if miss:
        out += ["", "❌ <b>Не сделано</b>"]
        for i in miss[:12]:
            note = " <i>(перенесу на завтра ↩️)</i>" if CARRY_OVER and i["kind"] == "task" else ""
            out.append(f"• {esc(i['name'])}{note}")
        if len(miss) > 12:
            out.append(f"…и ещё {len(miss) - 12}")

    # серия
    s_now = streak_ending(by, d)
    s_prev = streak_ending(by, d - timedelta(days=1))
    if s_now >= 2:
        out += ["", f"🔥 <b>Серия: {plural_days(s_now)}</b> подряд — все привычки выполнены!"]
    elif s_now == 1:
        out += ["", "🔥 Серия началась: 1 день, все привычки выполнены!"]
    elif s_prev >= 2:
        out += ["", f"💔 Серия в {plural_days(s_prev)} прервалась — завтра начнём новую 🐝"]

    # неделя
    dates = [(d - timedelta(days=k)).strftime("%Y-%m-%d") for k in range(6, -1, -1)]
    w_items = [i for ds in dates for i in by.get(ds, [])]
    if w_items:
        wpct = pct_of(sum(i["done"] for i in w_items), len(w_items))
        cells = []
        for ds in dates:
            wd = WEEKDAYS[datetime.strptime(ds, "%Y-%m-%d").weekday()]
            cells.append(f"{wd}{day_emoji(by.get(ds, []))}")
        out += ["", f"📅 <b>Неделя</b> · {wpct}%", " ".join(cells)]

    # сетка привычек за 7 дней
    if habits:
        out += ["", "🌿 <b>Привычки за 7 дней</b>"]
        for h in habits[:10]:
            row_cells, cnt = [], 0
            for ds in dates:
                rec = next(
                    (x for x in by.get(ds, []) if x["kind"] == "habit" and x["name"] == h["name"]),
                    None,
                )
                ok_day = bool(rec and rec["done"])
                cnt += ok_day
                row_cells.append("🟩" if ok_day else "⬜")
            out.append(f"{esc(h['name'])}  {''.join(row_cells)} {cnt}/7")

    out += ["", mood(pct)]
    return "\n".join(out)


# ───────────────────────── Действия ─────────────────────────


def post_form(now: datetime, chat_id=CHAT_ID):
    d = now.strftime("%Y-%m-%d")
    items = ensure_day(d)
    if not items:
        send(chat_id, "План на сегодня пуст 🌼 Добавь дело: /add Название, описание")
        return
    text, kb = render_form(d, items)
    send(chat_id, text, kb)


def post_summary(now: datetime, chat_id=CHAT_ID):
    send(chat_id, render_summary(now.strftime("%Y-%m-%d"), load_rows()))


def build_menu():
    text = (
        "🐝 <b>Меню планера</b>\n\n"
        "📋 /today — форма на сегодня\n"
        "📊 /stats — сводка прямо сейчас\n"
        "➕ /add Название, описание — разовое дело\n"
        "🌿 /habit Название, описание — новая привычка\n"
        "🗑 /del — удалить привычку или дело\n\n"
        "🌼 Привычки можно править и в листе «Шаблон» таблицы.\n"
        f"⏰ Утром ~{MORNING} шлю форму, вечером ~{EVENING} — итоги."
    )
    if not os.environ.get("INSTANT"):
        text += "\n\nЯ просыпаюсь раз в несколько минут, поэтому ответы приходят с небольшой задержкой."
    kb = {
        "inline_keyboard": [
            [
                {"text": "📋 Форма на сегодня", "callback_data": "m:today"},
                {"text": "📊 Сводка дня", "callback_data": "m:stats"},
            ],
            [
                {"text": "➕ Дело", "callback_data": "m:add"},
                {"text": "🌿 Привычка", "callback_data": "m:habit"},
            ],
            [
                {"text": "🗑 Удалить", "callback_data": "m:del"},
                {"text": "✖️ Закрыть", "callback_data": "m:close"},
            ],
        ]
    }
    return text, kb


HINTS = {
    "m:add": "➕ Разовое дело\n\nНапиши в чат:\n/add Купить молоко, у дома\n\nОписание писать необязательно.",
    "m:habit": "🌿 Новая привычка\n\nНапиши в чат:\n/habit Медитация, 10 минут тишины\n\nОна появится в шаблоне и в сегодняшнем списке.",
}


def split_name_desc(rest: str) -> tuple[str, str]:
    """«Название, описание» или «Название | описание»."""
    if "|" in rest:
        a, _, b = rest.partition("|")
    elif "," in rest:
        a, _, b = rest.partition(",")
    else:
        a, b = rest, ""
    return a.strip()[:80], b.strip()[:200]


def handle_message(m: dict, now: datetime):
    text = (m.get("text") or "").strip()
    if not text.startswith("/"):
        return
    chat = m["chat"]["id"]
    cmd, _, rest = text.partition(" ")
    cmd = cmd.split("@")[0].lower()
    rest = rest.strip()

    if cmd == "/id":
        send(chat, f"CHAT_ID этого чата: {chat}")
        return
    if chat != CHAT_ID:
        return

    today = now.strftime("%Y-%m-%d")
    if cmd in ("/start", "/menu", "/help"):
        send(chat, *build_menu())
    elif cmd == "/today":
        post_form(now, chat)
    elif cmd == "/stats":
        post_summary(now, chat)
    elif cmd == "/add":
        name, desc = split_name_desc(rest)
        if not name:
            send(chat, "Формат: /add Название, описание")
            return
        ensure_day(today)
        add_task(today, name, desc)
        send(chat, "Добавила в дела 📌 Обновляю форму:")
        post_form(now, chat)
    elif cmd == "/del":
        items = ensure_day(today)
        if not items:
            send(chat, "Удалять пока нечего 🌼")
        elif not rest:
            send(chat, *delete_menu(items))
        else:
            found = find_items(items, rest)
            if not found:
                send(chat, "Не нашла такого в сегодняшнем списке 🤔 Посмотри /del")
            elif len(found) == 1:
                send(chat, *delete_confirm(found[0]))
            else:
                send(chat, *delete_menu(found))
    elif cmd == "/habit":
        name, desc = split_name_desc(rest)
        if not name:
            send(chat, "Формат: /habit Название, описание")
            return
        ensure_day(today)
        add_habit(today, name, desc)
        send(chat, "Новая привычка добавлена 🌿 Обновляю форму:")
        post_form(now, chat)


def delete_menu(items: list[dict]):
    kb = [
        [{
            "text": f"🗑 {'🌿' if i['kind'] == 'habit' else '📌'} {i['name']}"[:48],
            "callback_data": f"d:{i['row']}",
        }]
        for i in items
    ]
    kb.append([{"text": "↩️ Отмена", "callback_data": "dn"}])
    text = (
        "Что удалить? 🗑\n"
        "🌿 Привычка пропадёт совсем (и из «Шаблона»), 📌 дело — из сегодняшнего списка."
    )
    return text, {"inline_keyboard": kb}


def delete_confirm(item: dict):
    what = "привычку" if item["kind"] == "habit" else "дело"
    extra = (
        "Она исчезнет из шаблона и больше не будет появляться."
        if item["kind"] == "habit"
        else "Оно исчезнет из сегодняшнего списка."
    )
    text = f"Удалить {what} <b>«{esc(item['name'])}»</b>?\n{extra}"
    kb = {"inline_keyboard": [[
        {"text": "✅ Да, удалить", "callback_data": f"dy:{item['row']}"},
        {"text": "↩️ Отмена", "callback_data": "dn"},
    ]]}
    return text, kb


def handle_callback(q: dict):
    data = q.get("data", "")
    if data in HINTS:  # всплывающая подсказка вместо ответа
        tg("answerCallbackQuery", callback_query_id=q["id"], text=HINTS[data], show_alert=True)
        return
    tg("answerCallbackQuery", callback_query_id=q["id"])
    msg = q.get("message")
    if not msg or msg["chat"]["id"] != CHAT_ID:
        return
    chat_id, mid = msg["chat"]["id"], msg["message_id"]

    if data == "m":
        send(chat_id, *build_menu())
        return
    if data.startswith("m:"):
        tg("deleteMessage", chat_id=chat_id, message_id=mid)  # убираем меню
        now = datetime.now(TZ)
        if data == "m:today":
            post_form(now, chat_id)
        elif data == "m:stats":
            post_summary(now, chat_id)
        elif data == "m:del":
            items = ensure_day(now.strftime("%Y-%m-%d"))
            if items:
                send(chat_id, *delete_menu(items))
            else:
                send(chat_id, "Удалять пока нечего 🌼")
        return

    if data == "dn":
        tg("editMessageText", chat_id=chat_id, message_id=mid,
           text="Хорошо, ничего не удаляю 🌼", reply_markup={"inline_keyboard": []})
        return
    if data.startswith("d:"):
        row = int(data[2:])
        it = next((r for r in load_rows() if r["row"] == row), None)
        if not it:
            tg("editMessageText", chat_id=chat_id, message_id=mid,
               text="Это уже удалено 🌼", reply_markup={"inline_keyboard": []})
            return
        text, kb = delete_confirm(it)
        tg("editMessageText", chat_id=chat_id, message_id=mid, text=text,
           reply_markup=kb, parse_mode="HTML")
        return
    if data.startswith("dy:"):
        it = delete_item(int(data[3:]))
        text = (
            f"🗑 Удалила: <b>«{esc(it['name'])}»</b>" if it else "Это уже удалено 🌼"
        )
        tg("editMessageText", chat_id=chat_id, message_id=mid, text=text,
           reply_markup={"inline_keyboard": []}, parse_mode="HTML")
        if it:
            post_form(datetime.now(TZ), chat_id)
        return
    if not data.startswith("t:"):
        return
    d = toggle(int(data[2:]))
    items = [r for r in load_rows() if r["date"] == d]
    text, kb = render_form(d, items)
    tg(
        "editMessageText",
        chat_id=msg["chat"]["id"],
        message_id=msg["message_id"],
        text=text,
        reply_markup=kb,
        parse_mode="HTML",
    )


def process_updates(now: datetime):
    offset = int(state_get("offset", "0") or 0)
    res = tg(
        "getUpdates",
        offset=offset,
        timeout=0,
        allowed_updates=["message", "callback_query"],
    )
    updates = res.get("result", []) if res.get("ok") else []
    if not updates:
        return
    state_set("offset", str(updates[-1]["update_id"] + 1))
    for u in updates:
        try:
            if "callback_query" in u:
                handle_callback(u["callback_query"])
            elif "message" in u:
                handle_message(u["message"], now)
        except Exception:
            log.exception("update %s failed", u.get("update_id"))


def run_schedule(now: datetime):
    today = now.strftime("%Y-%m-%d")
    hm = now.strftime("%H:%M")

    if hm >= MORNING and state_get("form_date") != today:
        if hm < EVENING:
            post_form(now)
        state_set("form_date", today)

    if hm >= EVENING and state_get("summary_date") != today:
        post_summary(now)
        state_set("summary_date", today)


def main():
    now = datetime.now(TZ)
    process_updates(now)
    run_schedule(now)
    log.info("tick done")


if __name__ == "__main__":
    main()
