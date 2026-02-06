import os
import re
import json
import hashlib
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

import httpx
import feedparser

STATE_FILE = "state.json"

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")  # 예: "@chipsignal"

KST = timezone(timedelta(hours=9))

# 발행 시간(한국시간)과 허용 윈도우(분)
SCHEDULE = [
    {"name": "AM", "hour": 8, "minute": 30, "window_minutes": 720},
    {"name": "PM", "hour": 20, "minute": 30, "window_minutes": 720},
]

# === RSS 소스 (MVP: 안정적인 무료 소스 위주) ===
RSS_FEEDS = [
    # 반도체/하드웨어/테크
    ("Tom's Hardware", "https://www.tomshardware.com/feeds/all"),
    ("AnandTech", "https://www.anandtech.com/rss/"),
    ("TechPowerUp", "https://www.techpowerup.com/rss/"),
    ("The Verge", "https://www.theverge.com/rss/index.xml"),
    ("Ars Technica", "https://feeds.arstechnica.com/arstechnica/index"),
    # 기업/시장 공시·뉴스는 RSS가 없는 곳도 많아서, 추후 확장(공식 뉴스룸/IR) 추천
]

# === “돈 되는” 키워드 가중치 (원하시면 계속 튜닝) ===
KEYWORD_WEIGHTS = {
    # AI/수요
    "nvidia": 10, "ai": 8, "gpu": 7, "datacenter": 7, "data center": 7,
    # 메모리/HBM
    "hbm": 12, "dram": 8, "ddr5": 6, "gddr": 6, "micron": 6, "sk hynix": 8, "samsung": 6,
    # 파운드리/공정
    "tsmc": 10, "foundry": 8, "3nm": 9, "2nm": 10, "gate-all-around": 9, "gaa": 7, "euv": 10,
    # 장비/패키징
    "asml": 10, "applied materials": 7, "lam research": 7, "kokusai": 6,
    "cowos": 10, "advanced packaging": 9, "chiplet": 8, "interposer": 7,
    # 정책/리스크
    "export": 7, "restriction": 8, "sanction": 8, "china": 6, "taiwan": 6, "geopolit": 7,
    # 실적/가이던스
    "earnings": 9, "guidance": 10, "forecast": 7, "revenue": 6, "capex": 9,
}

STOPWORDS = {
    "review", "rumor", "leak", "hands-on", "hands on", "giveaway"
}

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
    with httpx.Client(timeout=25) as client:
        r = client.post(url, json=payload)
        print("Telegram status:", r.status_code)
        print("Telegram response:", r.text[:400])
        r.raise_for_status()

def within_window(now_kst: datetime, hour: int, minute: int, window_minutes: int) -> bool:
    target = now_kst.replace(hour=hour, minute=minute, second=0, microsecond=0)
    start = target - timedelta(minutes=window_minutes)
    end = target + timedelta(minutes=window_minutes)
    return start <= now_kst <= end

def normalize_text(s: str) -> str:
    s = (s or "").strip().lower()
    s = re.sub(r"\s+", " ", s)
    return s

def is_recent(entry, now_kst: datetime, max_hours: int = 36) -> bool:
    # RSS의 published/updated가 없는 경우도 많아서, 없으면 “최근으로 가정”하지 않고 통과시킵니다.
    # (너무 엄격하게 걸면 빈 결과가 잦아짐)
    dt = None
    for key in ("published_parsed", "updated_parsed"):
        if getattr(entry, key, None):
            t = getattr(entry, key)
            try:
                dt = datetime(*t[:6], tzinfo=timezone.utc).astimezone(KST)
                break
            except Exception:
                pass
    if dt is None:
        return True
    return (now_kst - dt) <= timedelta(hours=max_hours)

def score_item(title: str, summary: str) -> int:
    text = normalize_text(f"{title} {summary}")
    score = 0

    # 너무 가벼운 컨텐츠 필터(완전 배제는 아니고 감점)
    for w in STOPWORDS:
        if w in text:
            score -= 5

    for k, w in KEYWORD_WEIGHTS.items():
        if k in text:
            score += w

    # 숫자/공급/실적 느낌 가산점
    if re.search(r"\b(\d+(\.\d+)?)(nm|%|billion|million|trillion)\b", text):
        score += 3

    return score

def dedupe_key(title: str, link: str) -> str:
    base = normalize_text(title) + "|" + (link or "")
    return hashlib.md5(base.encode("utf-8")).hexdigest()

def short_domain(link: str) -> str:
    try:
        return urlparse(link).netloc.replace("www.", "")
    except Exception:
        return ""

def summarize_one(title: str, summary: str) -> str:
    # LLM 없이 “한 문장 느낌”으로: 제목 기반 + 핵심 키워드 힌트
    t = (title or "").strip()
    t = re.sub(r"\s+", " ", t)
    if len(t) > 95:
        t = t[:92] + "..."
    return t

def fetch_top_news(now_kst: datetime, top_k: int = 3):
    items = []
    seen = set()

    for source_name, url in RSS_FEEDS:
        try:
            feed = feedparser.parse(url)
            for e in feed.entries[:30]:
                title = getattr(e, "title", "") or ""
                link = getattr(e, "link", "") or ""
                summary = getattr(e, "summary", "") or getattr(e, "description", "") or ""

                if not title or not link:
                    continue
                if not is_recent(e, now_kst, max_hours=48):
                    continue

                key = dedupe_key(title, link)
                if key in seen:
                    continue
                seen.add(key)

                s = score_item(title, summary)
                items.append({
                    "source": source_name,
                    "title": title.strip(),
                    "summary": summary.strip(),
                    "link": link.strip(),
                    "score": s,
                })
        except Exception as ex:
            print(f"[WARN] feed error: {source_name} - {ex}")

    # 점수 순 정렬
    items.sort(key=lambda x: x["score"], reverse=True)
    # 너무 점수가 낮은 건 제외(완전 뉴스 없는 날 방지 위해 기준 낮게)
    filtered = [x for x in items if x["score"] >= 1]
    if len(filtered) < top_k:
        filtered = items[:top_k]
    return filtered[:top_k]

def build_post(slot_name: str, now_kst: datetime) -> str:
    # 날짜 표기(리눅스/윈도우 호환)
    if os.name == "nt":
        date_str = now_kst.strftime("%m/%d").lstrip("0").replace("/0", "/")
    else:
        date_str = now_kst.strftime("%-m/%-d")

    news = fetch_top_news(now_kst, top_k=3)

    title = f"<b>[Chip Signal | {slot_name}] {date_str} {'아침 브리핑' if slot_name=='AM' else '저녁 정리'}</b>"

    if not news:
        body = (
            "✅ 결론 1줄: 오늘은 주요 RSS 소스에서 뚜렷한 상위 토픽을 찾지 못했습니다.\n\n"
            "🔎 체크포인트: 소스 확장(공식 뉴스룸/IR/RSS 추가)을 하면 정확도가 올라갑니다.\n"
            "#HBM #파운드리 #패키징 #장비 #체크포인트"
        )
        return f"{title}\n{body}"

    # 결론 1줄: 상위 토픽의 키워드 기반 “시장 시그널” 느낌으로
    top_title = normalize_text(news[0]["title"])
    signal = "AI 수요/공급망"
    if "hbm" in top_title or "dram" in top_title:
        signal = "HBM/메모리"
    elif "tsmc" in top_title or "foundry" in top_title or "nm" in top_title:
        signal = "파운드리/공정"
    elif "asml" in top_title or "euv" in top_title or "equipment" in top_title:
        signal = "장비/EUV"
    elif "packaging" in top_title or "cowos" in top_title or "chiplet" in top_title:
        signal = "패키징/칩렛"

    lines = []
    lines.append(f"✅ 결론 1줄: 오늘의 시그널은 <b>{signal}</b> 쪽으로 모입니다.\n")

    for i, item in enumerate(news, start=1):
        one = summarize_one(item["title"], item["summary"])
        domain = short_domain(item["link"])
        lines.append(f"• 핵심 {i}: {one}")
        lines.append(f"  - 출처: {domain} / 점수: {item['score']}")
        lines.append(f"  - 링크: {item['link']}\n")

    lines.append("🔎 체크포인트: 내일도 같은 키워드(HBM/파운드리/장비/패키징)가 이어지는지 확인")
    lines.append("#HBM #파운드리 #패키징 #장비 #체크포인트")

    return f"{title}\n" + "\n".join(lines)

def main():
    now_kst = datetime.now(tz=KST)
    today = now_kst.strftime("%Y-%m-%d")

    state = load_state()
    sent = state.get("sent", {})  # {"YYYY-MM-DD": {"AM": true, "PM": true}}

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
