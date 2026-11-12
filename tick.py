"""
Планер дня для GitHub Actions: один «тик» за запуск.
Каждые ~5 минут: обрабатывает нажатия и команды, шлёт утреннюю форму,
напоминания и вечернюю сводку. Все данные и состояние лежат в Google Таблице.
"""
import json
import logging
import os
import re
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
STALE = timedelta(hours=3)  # слишком старые напоминания не шлём

# ───────────────────────── Telegram ─────────────────────────


def tg(method: str, **params):
    params = {k: v for k, v in params.items() if v is not None}
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TOKEN}/{method}", json=params, timeout=30
        )
        data = r.json()
    except Exception as e:  # сеть/JSON
        log.warning("%s failed: %s", method, type(e).__name__)
        return {"ok": False}
    if not data.get("ok"):
        log.warning("%s: %s", method, data.get("description"))
    return data


def send(chat_id, text, kb=None):
    return tg("sendMessage", chat_id=chat_id, text=text, reply_markup=kb)


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


def template_ws():
    return sheet(
        "Шаблон",
        ["Задача", "Напоминание (ЧЧ:ММ)"],
        [["Зарядка", "08:30"], ["Работа над главным делом", "10:00"], ["Чтение", "21:00"]],
    )


def log_ws():
    ws = sheet("Журнал", ["Дата", "Задача", "Готово", "Напоминание", "Напомнено"])
    if "hdr5" not in _ws_cache:
        _ws_cache["hdr5"] = True
        if ws.col_count < 5:
            ws.add_cols(5 - ws.col_count)
        if len(ws.row_values(1)) < 5:
            ws.update_cell(1, 5, "Напомнено")
    return ws


def once_ws():
    return sheet("Разовые", ["Когда", "Чат", "Текст", "Отправлено"])


# ── нормализация значений (Google Таблицы любят превращать текст в даты/время)


def norm_date(s: str) -> str:
    s = s.strip()
    for fmt in ("%Y-%m-%d", "%d.%m.%Y", "%d/%m/%Y"):
        try:
            return datetime.strptime(s, fmt).strftime("%Y-%m-%d")
        except ValueError:
            pass
    return s


def norm_time(s: str) -> str:
    m = re.search(r"(\d{1,2}):(\d{2})", s or "")
    if not m or int(m.group(1)) > 23 or int(m.group(2)) > 59:
        return ""
    return f"{int(m.group(1)):02d}:{m.group(2)}"


# ── состояние бота (в листе «Состояние»)

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
    state_get(key)  # загрузить
    ws = _state_ws()
    if key in _state:
        row = _state[key][0]
        ws.update_cell(row, 2, value)
    else:
        row = len(_state) + 2
        ws.append_row([key, value], value_input_option="RAW")
    _state[key] = (row, value)


# ── задачи


def get_day(date_str: str) -> list[dict]:
    items = []
    for i, r in enumerate(log_ws().get_all_values()[1:], start=2):
        r = r + [""] * (5 - len(r))
        if norm_date(r[0]) == date_str:
            items.append(
                {
                    "row": i,
                    "task": r[1],
                    "done": r[2].strip() == "1",
                    "remind": norm_time(r[3]),
                    "reminded": r[4].strip() == "1",
                }
            )
    return items


def ensure_day(date_str: str) -> list[dict]:
    items = get_day(date_str)
    if items:
        return items
    rows = []
    for r in template_ws().get_all_values()[1:]:
        r = r + [""] * (2 - len(r))
        if r[0].strip():
            rows.append([date_str, r[0].strip(), "", norm_time(r[1]), ""])
    if rows:
        log_ws().append_rows(rows, value_input_option="RAW")
    return get_day(date_str)


def toggle(row: int) -> str:
    ws = log_ws()
    vals = ws.row_values(row) + [""] * 5
    ws.update_cell(row, 3, "" if vals[2].strip() == "1" else "1")
    return norm_date(vals[0])


def add_task(date_str: str, text: str, remind: str):
    log_ws().append_row([date_str, text, "", remind, ""], value_input_option="RAW")


def week_stats(now: datetime) -> tuple[int, int]:
    start = (now - timedelta(days=6)).strftime("%Y-%m-%d")
    done = total = 0
    for r in log_ws().get_all_values()[1:]:
        r = r + [""] * 5
        if norm_date(r[0]) >= start:
            total += 1
            done += r[2].strip() == "1"
    return done, total


# ───────────────────────── Оформление ─────────────────────────


def bar(pct: int, n: int = 10) -> str:
    filled = round(pct / 100 * n)
    return "🟩" * filled + "⬜" * (n - filled)


def pretty(date_str: str) -> str:
    return datetime.strptime(date_str, "%Y-%m-%d").strftime("%d.%m")


def render_form(date_str: str, items: list[dict]):
    done = sum(i["done"] for i in items)
    total = len(items)
    pct = round(100 * done / total) if total else 0
    text = (
        f"📅 План на {pretty(date_str)}\n\n"
        f"{bar(pct)} {pct}% ({done}/{total})\n\n"
        "Нажимай на задачу, чтобы отметить ✅"
        + ("" if os.environ.get("INSTANT") else "\n(отметка обновится в течение нескольких минут)")
    )
    kb = []
    for i in items:
        label = ("✅ " if i["done"] else "⬜ ") + i["task"]
        if i["remind"]:
            label += f" ⏰{i['remind']}"
        kb.append([{"text": label, "callback_data": f"t:{i['row']}"}])
    return text, {"inline_keyboard": kb}


def render_summary(date_str, items, wk) -> str:
    done = sum(i["done"] for i in items)
    total = len(items)
    pct = round(100 * done / total) if total else 0
    wpct = round(100 * wk[0] / wk[1]) if wk[1] else 0
    if pct == 100:
        mood = "Идеально! 🎉"
    elif pct >= 70:
        mood = "Отличный день! 💪"
    elif pct >= 40:
        mood = "Неплохо, завтра добавим! 🌱"
    else:
        mood = "Бывают и такие дни — завтра новый шанс 🌅"
    text = (
        f"📊 Итоги {pretty(date_str)}\n\n"
        f"{bar(pct)} {pct}% ({done}/{total})\n"
        f"За 7 дней: {wpct}% ({wk[0]}/{wk[1]})\n\n{mood}"
    )
    left = [i["task"] for i in items if not i["done"]]
    if left:
        text += "\n\n❌ Не сделано:\n" + "\n".join(f"• {t}" for t in left)
    return text


# ───────────────────────── Действия ─────────────────────────


def post_form(now: datetime, chat_id=CHAT_ID):
    d = now.strftime("%Y-%m-%d")
    items = ensure_day(d)
    if not items:
        send(chat_id, "Задач на сегодня нет. Добавь: /add Текст [ЧЧ:ММ]")
        return
    text, kb = render_form(d, items)
    send(chat_id, text, kb)


def post_summary(now: datetime, chat_id=CHAT_ID):
    d = now.strftime("%Y-%m-%d")
    send(chat_id, render_summary(d, get_day(d), week_stats(now)))


HELP = (
    "Привет! Я планер дня 🌿\n\n"
    "/today — показать форму на сегодня\n"
    "/add Текст [ЧЧ:ММ] — добавить задачу на сегодня\n"
    "/remind ЧЧ:ММ Текст — разовая напоминалка\n"
    "/stats — сводка на сейчас\n\n"
    f"Утром ~{MORNING} шлю форму, вечером ~{EVENING} — итоги."
    + ("" if os.environ.get("INSTANT") else "\nЯ просыпаюсь раз в несколько минут, поэтому ответы приходят с небольшой задержкой.")
)


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

    if cmd == "/start":
        send(chat, HELP)
    elif cmd == "/today":
        post_form(now, chat)
    elif cmd == "/stats":
        post_summary(now, chat)
    elif cmd == "/add":
        words = rest.split()
        if not words:
            send(chat, "Формат: /add Текст задачи [ЧЧ:ММ]")
            return
        remind = ""
        if len(words) > 1 and norm_time(words[-1]) and re.fullmatch(r"\d{1,2}:\d{2}", words[-1]):
            remind = norm_time(words.pop())
        add_task(now.strftime("%Y-%m-%d"), " ".join(words), remind)
        send(chat, "Добавила в план ✍️ Обновляю форму:")
        post_form(now, chat)
    elif cmd == "/remind":
        words = rest.split(maxsplit=1)
        hm = norm_time(words[0]) if words and re.fullmatch(r"\d{1,2}:\d{2}", words[0]) else ""
        if not hm or len(words) < 2:
            send(chat, "Формат: /remind ЧЧ:ММ Текст")
            return
        when = datetime.strptime(now.strftime("%Y-%m-%d") + " " + hm, "%Y-%m-%d %H:%M").replace(tzinfo=TZ)
        if when <= now:
            when += timedelta(days=1)
        once_ws().append_row(
            [when.strftime("%Y-%m-%d %H:%M"), str(chat), words[1], ""],
            value_input_option="RAW",
        )
        send(chat, f"Напомню около {hm} ⏰")


def handle_callback(q: dict):
    tg("answerCallbackQuery", callback_query_id=q["id"])
    msg = q.get("message")
    data = q.get("data", "")
    if not msg or msg["chat"]["id"] != CHAT_ID or not data.startswith("t:"):
        return
    d = toggle(int(data[2:]))
    text, kb = render_form(d, get_day(d))
    tg(
        "editMessageText",
        chat_id=msg["chat"]["id"],
        message_id=msg["message_id"],
        text=text,
        reply_markup=kb,
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
    # сначала сохраняем смещение: лучше потерять команду, чем дважды переключить галочку
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

    # утренняя форма
    if hm >= MORNING and state_get("form_date") != today:
        state_set("form_date", today)
        if hm < EVENING:
            post_form(now)

    # напоминания по задачам дня
    for it in get_day(today):
        if not it["remind"] or it["done"] or it["reminded"]:
            continue
        when = datetime.strptime(f"{today} {it['remind']}", "%Y-%m-%d %H:%M").replace(tzinfo=TZ)
        if when <= now:
            if now - when < STALE:
                send(CHAT_ID, f"⏰ Напоминание: {it['task']}")
            log_ws().update_cell(it["row"], 5, "1")

    # разовые напоминалки
    ws = once_ws()
    for i, r in enumerate(ws.get_all_values()[1:], start=2):
        r = r + [""] * 4
        if r[3].strip() == "1":
            continue
        try:
            when = datetime.strptime(r[0].strip(), "%Y-%m-%d %H:%M").replace(tzinfo=TZ)
        except ValueError:
            continue
        if when <= now:
            if now - when < STALE:
                send(int(r[1]), f"⏰ Напоминание: {r[2]}")
            ws.update_cell(i, 4, "1")

    # вечерняя сводка
    if hm >= EVENING and state_get("summary_date") != today:
        state_set("summary_date", today)
        post_summary(now)


def main():
    now = datetime.now(TZ)
    process_updates(now)
    run_schedule(now)
    log.info("tick done")


if __name__ == "__main__":
    main()
