import os
import re
import json
import hashlib
from datetime import datetime, timedelta, timezone
from urllib.parse import quote, urlparse

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

# ✅ 지금 테스트할 때만 1로 켜세요. (state.json 무시하고 강제 발행)
FORCE_SEND = os.getenv("FORCE_SEND", "0") == "1"

# ---------------------------
# 1) 국내 독자용: Google News RSS (KR)
# ---------------------------
# Google News RSS 검색 URL:
# https://news.google.com/rss/search?q=<QUERY>&hl=ko&gl=KR&ceid=KR:ko
GN_BASE = "https://news.google.com/rss/search?q={q}&hl=ko&gl=KR&ceid=KR:ko"

GOOGLE_NEWS_QUERIES = [
    # 사람 모이는 토픽(돈 되는/이슈 중심)
    "HBM OR 고대역폭메모리 OR AI반도체",
    "삼성전자 반도체 OR SK하이닉스 OR 마이크론",
    "TSMC OR 파운드리 OR 2나노 OR 3나노",
    "ASML OR EUV OR 노광장비",
    "CoWoS OR 첨단패키징 OR 칩렛",
    "반도체 장비 OR 소재 OR 수율",
    "반도체 수출규제 OR 중국 반도체 OR 제재",
    "엔비디아 데이터센터 OR AI 서버 OR GPU",
]

def build_google_news_feeds():
    feeds = []
    for q in GOOGLE_NEWS_QUERIES:
        feeds.append(("GoogleNews", GN_BASE.format(q=quote(q))))
    return feeds

RSS_FEEDS = build_google_news_feeds()

# ---------------------------
# 2) 점수화: “돈 되는 관점” 가중치
# ---------------------------
KEYWORD_WEIGHTS = {
    # 메모리/HBM
    "hbm": 12, "dram": 7, "ddr5": 5, "sk하이닉스": 8, "하이닉스": 7, "삼성전자": 6, "마이크론": 6,
    # 파운드리/공정
    "tsmc": 10, "파운드리": 9, "2나노": 10, "3나노": 8, "gaa": 7, "gate-all-around": 7,
    # 장비/EUV/패키징
    "asml": 10, "euv": 10, "노광": 8, "cowos": 9, "첨단패키징": 8, "칩렛": 7,
    "장비": 6, "소재": 5, "수율": 6,
    # 정책/리스크/시장
    "수출규제": 9, "제재": 8, "중국": 6, "관세": 6, "규제": 6,
    # 실적/가이던스/투자
    "실적": 9, "가이던스": 10, "전망": 7, "매출": 6, "capex": 9, "투자": 7, "증설": 7,
    # AI 수요
    "엔비디아": 8, "nvidia": 8, "ai": 6, "데이터센터": 7, "서버": 6, "gpu": 6,
}

POSITIVE_HINTS = [
    "증설", "투자", "수주", "확대", "호조", "상향", "반등", "급증", "사상", "최대", "채택", "양산",
    "돌파", "회복", "강세", "개선", "확정"
]
NEGATIVE_HINTS = [
    "경고", "하향", "감소", "둔화", "부진", "악화", "지연", "차질", "리스크", "제재", "규제", "불확실",
    "폭락", "우려", "중단", "취소"
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
    with httpx.Client(timeout=25) as client:
        r = client.post(url, json=payload)
        print("Telegram status:", r.status_code)
        print("Telegram response:", r.text[:300])
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

def is_recent(entry, now_kst: datetime, max_hours: int = 48) -> bool:
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

def dedupe_key(title: str, link: str) -> str:
    base = normalize_text(title) + "|" + (link or "")
    return hashlib.md5(base.encode("utf-8")).hexdigest()

def short_domain(link: str) -> str:
    try:
        return urlparse(link).netloc.replace("www.", "")
    except Exception:
        return ""

def clean_title(title: str, max_len: int = 62) -> str:
    t = re.sub(r"\s+", " ", (title or "").strip())
    # 너무 길면 줄이기
    if len(t) > max_len:
        t = t[: max_len - 3] + "..."
    return t

def sentiment_label(title: str, summary: str) -> str:
    text = (title or "") + " " + (summary or "")
    pos = sum(1 for w in POSITIVE_HINTS if w in text)
    neg = sum(1 for w in NEGATIVE_HINTS if w in text)
    if pos > neg:
        return "수혜"
    if neg > pos:
        return "리스크"
    return "중립"

def score_item(title: str, summary: str) -> int:
    text = normalize_text(f"{title} {summary}")
    score = 0
    for k, w in KEYWORD_WEIGHTS.items():
        if normalize_text(k) in text:
            score += w
    # 숫자/실적 느낌 가산점
    if re.search(r"\b(\d+(\.\d+)?)(nm|%|조|억|만|B|M|T)\b", text):
        score += 3
    return score

def fetch_top_news(now_kst: datetime, top_k: int = 3):
    items = []
    seen = set()

    for source_name, url in RSS_FEEDS:
        try:
            feed = feedparser.parse(url)
            for e in feed.entries[:40]:
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

    items.sort(key=lambda x: x["score"], reverse=True)
    filtered = [x for x in items if x["score"] >= 1]
    if len(filtered) < top_k:
        filtered = items[:top_k]
    return filtered[:top_k]

def pick_signal_bucket(top_title: str) -> str:
    t = normalize_text(top_title)
    if "hbm" in t or "dram" in t or "하이닉스" in t or "sk하이닉스" in t:
        return "HBM/메모리"
    if "tsmc" in t or "파운드리" in t or "2나노" in t or "3나노" in t:
        return "파운드리/공정"
    if "asml" in t or "euv" in t or "노광" in t:
        return "장비/EUV"
    if "cowos" in t or "첨단패키징" in t or "칩렛" in t:
        return "패키징/칩렛"
    if "수출규제" in t or "제재" in t or "규제" in t:
        return "정책/리스크"
    return "AI수요/공급망"

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
            "✅ 결론: 오늘은 상위 토픽이 뚜렷하지 않습니다.\n"
            "🔎 체크포인트: 주요 기업 실적/가이던스, 수출규제, HBM/패키징 이슈를 확인해 주세요.\n"
            "#반도체 #HBM #파운드리 #장비 #패키징"
        )
        return f"{title}\n{body}"

    bucket = pick_signal_bucket(news[0]["title"])
    lines = []
    lines.append(f"✅ 결론: 오늘의 시그널은 <b>{bucket}</b> 쪽으로 모입니다.\n")

    # 핵심 3개: 수혜/리스크 라벨 + 제목(짧게) + 출처 도메인 + 원문 링크(짧게)
    for i, item in enumerate(news, start=1):
        lab = sentiment_label(item["title"], item["summary"])
        t = clean_title(item["title"], max_len=70)
        dom = short_domain(item["link"])
        # 텔레그램 HTML 링크로 URL 길이 숨김
        lines.append(f"{i}) <b>({lab})</b> {t}")
        lines.append(f"   - 출처: {dom} | <a href=\"{item['link']}\">원문</a>\n")

    # 산업 디테일 20%: “내일 체크포인트” 1~2개(고정 템플릿이지만 가독성/습관에 도움)
    lines.append("🔎 체크포인트(내일):")
    lines.append("• 같은 키워드가 연속 노출되는지(수요/공급/규제/수율/증설) 확인")
    lines.append("• 실적/가이던스/투자(CAPEX) 변화가 나오면 바로 방향 전환 가능\n")
    lines.append("#반도체 #HBM #AI #파운드리 #장비 #패키징 #수혜 #리스크")

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
        if (not FORCE_SEND) and sent[today].get(name):
            continue

        if within_window(now_kst, slot["hour"], slot["minute"], slot["window_minutes"]) or FORCE_SEND:
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
