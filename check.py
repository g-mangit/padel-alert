#!/usr/bin/env python3
"""PadelAlert - בודק מגרשי פאדל פנויים דרך padelspot.org ושולח התראה (ntfy / טלגרם).

מריצים: python check.py            (רגיל)
        python check.py --dry-run  (בלי שליחה ובלי שמירת מצב, רק מדפיס)
"""
import html
import json
import os
import sys
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
import yaml

BASE = Path(__file__).parent
API = "https://padelspot.org/api/search"
TZ = ZoneInfo("Asia/Jerusalem")
UA = os.environ.get("USER_AGENT", "PadelAlertBot/1.0 (personal use, permission from site owner)")
WEEKDAYS = {"monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
            "friday": 4, "saturday": 5, "sunday": 6}
HEB_DAYS = ["שני", "שלישי", "רביעי", "חמישי", "שישי", "שבת", "ראשון"]
DRY = "--dry-run" in sys.argv


def load_clubs():
    return json.loads((BASE / "clubs.json").read_text(encoding="utf-8"))


def pick_clubs(rule, clubs):
    """מחזיר את רשימת המועדונים שהכלל מתייחס אליהם."""
    sel = clubs
    want = rule.get("clubs", "all")
    if want != "all":
        words = [str(w).strip() for w in want]
        sel = [c for c in sel if any(w == c["id"] or w in c["name"] for w in words)]
    prov = rule.get("providers", "all")
    if prov != "all":
        low = [str(p).lower() for p in prov]
        sel = [c for c in sel if c["provider"].lower() in low]
    if rule.get("indoor"):
        sel = [c for c in sel if c["indoor"]]
    if rule.get("shade"):
        sel = [c for c in sel if c["shade"]]
    return sel


def rule_dates(rule, today):
    """אילו תאריכים הכלל בודק."""
    kind = rule.get("type", "daily")
    if kind == "weekly":
        wd = WEEKDAYS[str(rule["weekday"]).lower()]
        weeks = int(rule.get("weeks_ahead", 2))
        first = today + timedelta(days=(wd - today.weekday()) % 7)
        return [first + timedelta(weeks=i) for i in range(weeks)]
    if kind == "date":
        return [datetime.strptime(str(rule["date"]), "%Y-%m-%d").date()]
    days = int(rule.get("days", 7))
    wanted = rule.get("weekdays")  # אופציונלי: רק ימים מסוימים
    out = [today + timedelta(days=i) for i in range(days)]
    if wanted:
        ok = {WEEKDAYS[str(w).lower()] for w in wanted}
        out = [d for d in out if d.weekday() in ok]
    return out


def search(date, rule, club_ids):
    body = {
        "search_id": str(uuid.uuid4()),
        "date": date.isoformat(),
        "start_from": rule["from"],
        "start_to": rule["to"],
        "duration": int(rule.get("duration", 90)),
        "clubs": club_ids,
        "area": "all",
        "app": "all",
    }
    last = None
    for attempt in range(3):
        try:
            r = requests.post(API, json=body, timeout=60,
                              headers={"User-Agent": UA, "Origin": "https://padelspot.org",
                                       "Referer": "https://padelspot.org/"})
            if r.status_code == 200:
                return r.json().get("results", [])
            last = f"HTTP {r.status_code}: {r.text[:200]}"
        except Exception as e:  # noqa: BLE001
            last = repr(e)
        time.sleep(3 * (attempt + 1))
    raise RuntimeError(last)


def slot_key(s):
    return f"{s['club']}|{s['date']}|{s['start']}|{s.get('duration')}"


def fmt_date(iso):
    d = datetime.strptime(iso, "%Y-%m-%d").date()
    return f"יום {HEB_DAYS[d.weekday()]} {d.strftime('%d/%m')}"


def build_message(rule_name, slots):
    lines = [f"🎾 <b>{html.escape(rule_name)}</b> – נמצאו מגרשים פנויים:"]
    for s in sorted(slots, key=lambda x: (x["date"], x["start"], x["club"])):
        price = s.get("mobile_price")
        price_txt = f" · ₪{int(price)}" if price else ""
        link = s.get("booking_url") or ""
        lines.append(
            f"\n📅 {fmt_date(s['date'])} ⏰ {s['start']}–{s['end']}{price_txt}\n"
            f"📍 {html.escape(s['club'])} ({html.escape(s['provider'])})\n"
            f"🔗 {link}"
        )
    return "\n".join(lines)


def ntfy_send(topic, title, message, click=None, priority=4):
    payload = {"topic": topic, "title": title, "message": message,
               "priority": priority, "tags": ["tennis"]}
    if click:
        payload["click"] = click
        payload["actions"] = [{"action": "view", "label": "להזמנה", "url": click}]
    r = requests.post("https://ntfy.sh/", json=payload, timeout=30,
                      headers={"User-Agent": UA})
    r.raise_for_status()


def notify_slots(topic, rule_name, slots):
    """פוש לטלפון (ntfy) + טלגרם אם הוגדר. הודעה נפרדת לכל מגרש, עם קישור ישיר להזמנה."""
    slots = sorted(slots, key=lambda x: (x["date"], x["start"], x["club"]))
    if DRY:
        print(build_message(rule_name, slots))
        return
    if topic:
        for s in slots[:8]:
            price = f" · ₪{int(s['mobile_price'])}" if s.get("mobile_price") else ""
            ntfy_send(topic, f"🎾 {fmt_date(s['date'])} {s['start']}–{s['end']}",
                      f"{s['club']} ({s['provider']}){price}\n{rule_name}",
                      click=s.get("booking_url"))
        if len(slots) > 8:
            ntfy_send(topic, f"🎾 ועוד {len(slots) - 8} מגרשים פנויים",
                      "יותר מדי חדשים בבת אחת – ראה באתר padelspot.org",
                      click="https://padelspot.org/")
    if os.environ.get("TELEGRAM_BOT_TOKEN"):
        telegram(build_message(rule_name, slots))


def notify_text(topic, title, text):
    if DRY:
        print(title, text)
        return
    if topic:
        ntfy_send(topic, title, text, priority=3)
    if os.environ.get("TELEGRAM_BOT_TOKEN"):
        telegram(f"{title}\n{text}")


def telegram(text):
    if DRY:
        print("---- הודעת טלגרם (dry-run) ----\n" + text + "\n")
        return
    token, chat = os.environ["TELEGRAM_BOT_TOKEN"], os.environ["TELEGRAM_CHAT_ID"]
    # טלגרם מגביל ל-4096 תווים להודעה
    chunks, cur = [], ""
    for part in text.split("\n\n"):
        if len(cur) + len(part) + 2 > 3800 and cur:
            chunks.append(cur)
            cur = ""
        cur += ("\n\n" if cur else "") + part
    chunks.append(cur)
    for c in chunks:
        r = requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                          json={"chat_id": chat, "text": c, "parse_mode": "HTML",
                                "disable_web_page_preview": True}, timeout=30)
        r.raise_for_status()


def main():
    if "--test-push" in sys.argv:
        cfg0 = yaml.safe_load((BASE / "rules.yaml").read_text(encoding="utf-8"))
        t = os.environ.get("NTFY_TOPIC") or cfg0.get("settings", {}).get("ntfy_topic")
        ntfy_send(t, "🎾 PadelAlert מחובר", "אם קיבלת את זה – ההתראות עובדות.", click="https://padelspot.org/")
        print("test push sent")
        return 0
    cfg = yaml.safe_load((BASE / "rules.yaml").read_text(encoding="utf-8"))
    settings = cfg.get("settings", {})
    state_path = BASE / "state.json"
    state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {}
    clubs = load_clubs()
    by_name = {c["name"]: c for c in clubs}
    today = datetime.now(TZ).date()
    topic = os.environ.get("NTFY_TOPIC") or settings.get("ntfy_topic")
    errors = 0

    for rule in cfg.get("rules", []):
        if not rule.get("enabled", True):
            continue
        name = rule["name"]
        selected = pick_clubs(rule, clubs)
        if not selected:
            print(f"[{name}] אין מועדונים שמתאימים לפילטרים")
            continue
        ids = [c["id"] for c in selected]
        rs = state.setdefault(name, {"seen": {}, "first_done": False})
        found_now = {}
        failed_dates = set()
        for d in rule_dates(rule, today):
            try:
                results = search(d, rule, ids)
            except Exception as e:  # noqa: BLE001
                print(f"[{name}] {d}: שגיאה {e}")
                failed_dates.add(d.isoformat())
                errors += 1
                continue
            for s in results:
                if s["club"] in by_name and by_name[s["club"]]["id"] not in ids:
                    continue
                found_now[slot_key(s)] = s
            time.sleep(1.5)  # נימוס לשרת

        # תאריכים שנכשלו: משאירים את מה שנראה קודם כדי לא להתריע שוב בטעות
        prev = rs["seen"]
        keep = {k: v for k, v in prev.items() if v["date"] in failed_dates}
        new_keys = [k for k in found_now if k not in prev]
        first_run = not rs["first_done"]
        if first_run and not settings.get("notify_existing_on_first_run", True):
            new_keys = []
        if new_keys:
            notify_slots(topic, name, [found_now[k] for k in new_keys])
        print(f"[{name}] נמצאו {len(found_now)} פנויים, חדשים: {len(new_keys)}")
        rs["seen"] = {**keep, **{k: {"date": s["date"]} for k, s in found_now.items()}}
        if len(failed_dates) < len(rule_dates(rule, today)):
            rs["first_done"] = True

    # התראה אם הבדיקה נכשלת שוב ושוב (למשל חסימה של האתר)
    fails = state.get("_fails", 0) + 1 if errors else 0
    state["_fails"] = fails
    if errors and fails == 3:
        notify_text(topic, "⚠️ PadelAlert נתקע",
                    "הבדיקה נכשלת 3 פעמים ברצף (ייתכן שהאתר חוסם או השתנה). אפשר לבקש ממני לתקן.")

    if not DRY:
        state_path.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
    return 1 if errors and fails >= 3 else 0


if __name__ == "__main__":
    sys.exit(main())
