import os
import json
from datetime import datetime, timedelta, timezone
import httpx

STATE_FILE = "state.json"

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

KST = timezone(timedelta(hours=9))

SCHEDULE = [
    {"name": "AM", "hour": 8, "minute": 30, "window_minutes": 720},
    {"name": "PM", "hour": 20, "minute": 30, "window_minutes": 720},
]

def load_state():
    if not os.path.exists(STATE_FILE):
        return {}
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}

def save_state(state: dict):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)

def send_message(text: str):
    if not BOT_TOKEN or not CHAT_ID:
        raise RuntimeError("Missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID")

    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True
    }
    with httpx.Client(timeout=20) as client:
        r = client.post(url, json=payload)
        print("Telegram status:", r.status_code)
        print("Telegram response:", r.text[:500])
        r.raise_for_status()

def within_window(now_kst: datetime, hour: int, minute: int, window_minutes: int) -> bool:
    target = now_kst.replace(hour=hour, minute=minute, second=0, microsecond=0)
    start = target - timedelta(minutes=window_minutes)
    end = target + timedelta(minutes=window_minutes)
    return start <= now_kst <= end

def build_post(slot_name: str, now_kst: datetime) -> str:
    # 날짜 표기(리눅스/윈도우 호환)
    if os.name == "nt":
        date_str = now_kst.strftime("%m/%d").lstrip("0").replace("/0", "/")
    else:
        date_str = now_kst.strftime("%-m/%-d")

    title = f"<b>[Chip Signal | {slot_name}] {date_str} {'아침 브리핑' if slot_name=='AM' else '저녁 정리'}</b>"
    body = (
        "✅ 결론 1줄: 오늘의 핵심을 ‘시그널’로 요약해드립니다.\n\n"
        "• 핵심 1: (여기에 오늘 TOP 토픽)\n"
        "• 핵심 2: (수혜/리스크 포인트)\n"
        "• 핵심 3: (공급망/양산/실적 변화)\n\n"
        "🔎 체크포인트: 내일 확인할 것 1~2개\n"
        "#HBM #파운드리 #패키징 #장비 #체크포인트"
    )
    return f"{title}\n{body}"

def main():
    now_kst = datetime.now(tz=KST)
    today = now_kst.strftime("%Y-%m-%d")

    state = load_state()
    sent = state.get("sent", {})

    if today not in sent:
        sent[today] = {}

    for slot in SCHEDULE:
        name = slot["name"]
        if sent[today].get(name):
            continue

        if within_window(now_kst, slot["hour"], slot["minute"], slot["window_minutes"]):
            msg = build_post(name, now_kst)
            send_message(msg)
            sent[today][name] = True
            state["sent"] = sent
            save_state(state)
            print(f"✅ Sent {name} for {today}")
        else:
            print(f"⏳ Not in window for {name}. Now(KST): {now_kst.isoformat()}")

if __name__ == "__main__":
    main()
