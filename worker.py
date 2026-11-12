"""
Постоянно работающая версия бота (для Railway).
Использует функции из tick.py: мгновенно отвечает на команды и нажатия,
а раз в минуту проверяет расписание (форма, напоминания, сводка).
"""
import logging
import time
from datetime import datetime

import tick

log = logging.getLogger("worker")
ALLOWED = ["message", "callback_query"]


def main():
    offset = int(tick.state_get("offset", "0") or 0)
    last_schedule = 0.0
    log.info("worker started")
    while True:
        try:
            if time.time() - last_schedule >= 60:
                last_schedule = time.time()
                tick.run_schedule(datetime.now(tick.TZ))

            res = tick.tg(
                "getUpdates", offset=offset, timeout=20, allowed_updates=ALLOWED
            )
            if not res.get("ok"):
                time.sleep(5)
                continue
            for u in res["result"]:
                offset = u["update_id"] + 1
                try:
                    if "callback_query" in u:
                        tick.handle_callback(u["callback_query"])
                    elif "message" in u:
                        tick.handle_message(u["message"], datetime.now(tick.TZ))
                except Exception:
                    log.exception("update %s failed", u.get("update_id"))
        except Exception:
            log.exception("loop error")
            time.sleep(10)


if __name__ == "__main__":
    main()
