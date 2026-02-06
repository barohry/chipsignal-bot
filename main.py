import os
import re
import json
import hashlib
from datetime import datetime, timedelta, timezone
from urllib.parse import quote, urlparse

import httpx
import feedparser
from openai import OpenAI

# =========================
# Config
# =========================
STATE_FILE = "state.json"
ARCHIVE_FILE = "archive.jsonl"

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")  # e.g., "@chipsignal"

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-5-mini")  # 기본을 5-mini로
client = OpenAI(api_key=OPENAI_API_KEY) if OPENAI_API_KEY else None

KST = timezone(timedelta(hours=9))

# 발행 시간(한국시간)과 허용 윈도우(분)
# ⚠️ window_minutes=720(12시간)은 "아침/저녁 둘 다 언제든 발행"이 되어 중복 발행/원치 않는 타이밍이 생길 수 있습니다.
# 정상 운영은 10~20분 권장입니다. (필요하면 workflow에서 env로 관리하세요)
SCHEDULE = [
    {"name": "AM", "hour": 8, "minute": 30, "window_minutes": 720},
    {"name": "PM", "hour": 20, "minute": 30, "window_minutes": 720},
]

# 테스트용: 1이면 state 무시하고 강제 발행
FORCE_SEND = os.getenv("FORCE_SEND", "0") == "1"

# 몇 개 올릴지
TOP_K = int(os.getenv("TOP_K", "7"))

# ---------------------------
# Google News RSS (KR)
# ---------------------------
GN_BASE = "https://news.google.com/rss/search?q={q}&hl=ko&gl=KR&ceid=KR:ko"

GOOGLE_NEWS_QUERIES = [
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
    return [("GoogleNews", GN_BASE.format(q=quote(q))) for q in GOOGLE_NEWS_QUERIES]

RSS_FEEDS = build_google_news_feeds()

# ---------------------------
# Scoring weights (money-ish)
# ---------------------------
KEYWORD_WEIGHTS = {
    # Memory/HBM
    "hbm": 12, "dram": 7, "ddr5": 5, "sk하이닉스": 8, "하이닉스": 7, "삼성전자": 6, "마이크론": 6,
    # Foundry/process
    "tsmc": 10, "파운드리": 9, "2나노": 10, "3나노": 8, "gaa": 7, "gate-all-around": 7,
    # Equip/EUV/packaging
    "asml": 10, "euv": 10, "노광": 8, "cowos": 9, "첨단패키징": 8, "칩렛": 7,
    "장비": 6, "소재": 5, "수율": 6,
    # Policy/risk
    "수출규제": 9, "제재": 8, "규제": 6, "관세": 6, "중국": 6,
    # Earnings/investment
    "실적": 9, "가이던스": 10, "전망": 7, "매출": 6, "capex": 9, "투자": 7, "증설": 7,
    # AI demand
    "엔비디아": 8, "nvidia": 8, "ai": 6, "데이터센터": 7, "서버": 6, "gpu": 6,
}

# =========================
# Utilities
# =========================
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

def append_archive(records: list[dict]):
    if not records:
        return
    with open(ARCHIVE_FILE, "a", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

def send_message(text: str):
    if not BOT_TOKEN or not CHAT_ID:
        raise RuntimeError("Missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID")

    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    with httpx.Client(timeout=25) as client_http:
        r = client_http.post(url, json=payload)
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

def clean_title(title: str, max_len: int = 78) -> str:
    t = re.sub(r"\s+", " ", (title or "").strip())
    if len(t) > max_len:
        t = t[: max_len - 3] + "..."
    return t

def score_item(title: str, summary: str) -> int:
    text = normalize_text(f"{title} {summary}")
    score = 0
    for k, w in KEYWORD_WEIGHTS.items():
        if normalize_text(k) in text:
            score += w
    if re.search(r"\b(\d+(\.\d+)?)(nm|%|조|억|만|B|M|T)\b", text):
        score += 3
    return score

# ---------------------------
# Better dedupe: normalize title + simple similarity
# ---------------------------
def strip_media_suffix(title: str) -> str:
    # [속보] 같은 머리 제거
    t = (title or "").strip()
    t = re.sub(r"^\[[^\]]+\]\s*", "", t)
    # 끝의 " - 매체명" 제거(대부분의 구글뉴스 제목에 붙음)
    t = re.sub(r"\s*-\s*[^-]+$", "", t)
    # 따옴표/특수따옴표 제거
    t = re.sub(r"[\"“”’‘]", "", t)
    t = re.sub(r"\s+", " ", t)
    return t.strip().lower()

def is_similar(a: str, b: str) -> bool:
    sa = set(re.findall(r"[0-9a-zA-Z가-힣]+", a))
    sb = set(re.findall(r"[0-9a-zA-Z가-힣]+", b))
    if not sa or not sb:
        return False
    j = len(sa & sb) / len(sa | sb)
    return j >= 0.85

def extract_media_name(title: str) -> str:
    # "... - 뉴데일리" -> "뉴데일리"
    m = re.search(r"\s-\s([^-]+)$", (title or "").strip())
    return m.group(1).strip() if m else ""

def _is_google_news_url(u: str) -> bool:
    return "news.google.com" in (u or "")

# Google News RSS는 link가 news.google.com인 경우가 많아서 "진짜 원문 링크"를 뽑습니다.
def extract_original_url(entry, item_link: str, summary_html: str) -> str:
    # 0) entry.links에서 외부 링크 우선
    try:
        for l in getattr(entry, "links", []) or []:
            href = None
            if isinstance(l, dict):
                href = l.get("href")
            else:
                href = getattr(l, "href", None)
            if href and href.startswith("http") and (not _is_google_news_url(href)):
                return href
    except Exception:
        pass

    # 1) summary에서 href 추출
    m = re.search(r'href="(https?://[^"]+)"', summary_html or "")
    if m and (not _is_google_news_url(m.group(1))):
        return m.group(1)

    # 2) summary에 plain url이 있는 경우
    m2 = re.search(r'(https?://[^\s"<]+)', summary_html or "")
    if m2 and (not _is_google_news_url(m2.group(1))):
        return m2.group(1)

    # 3) fallback: item link 자체 (이 경우 google 링크일 수 있음)
    return item_link

def fetch_top_news(now_kst: datetime, top_k: int = 7) -> list[dict]:
    items = []
    seen = set()
    norm_titles = []  # 유사도 중복 제거용

    for source_name, url in RSS_FEEDS:
        try:
            feed = feedparser.parse(url)
            for e in feed.entries[:80]:
                title = getattr(e, "title", "") or ""
                link = getattr(e, "link", "") or ""
                summary = getattr(e, "summary", "") or getattr(e, "description", "") or ""

                if not title or not link:
                    continue
                if not is_recent(e, now_kst, max_hours=48):
                    continue

                # 1차: title+link 해시 중복
                key = dedupe_key(title, link)
                if key in seen:
                    continue

                # 2차: 제목 정규화 유사도 중복(속보/따옴표/매체 꼬리 차이 제거)
                norm = strip_media_suffix(title)
                if any(is_similar(norm, nt) for nt in norm_titles):
                    continue

                seen.add(key)
                norm_titles.append(norm)

                orig = extract_original_url(e, link, summary)
                s = score_item(title, summary)

                items.append({
                    "source": source_name,
                    "title": title.strip(),
                    "summary": summary.strip(),
                    "link": link.strip(),         # google news link (backup)
                    "orig_link": orig.strip(),    # publisher link (preferred)
                    "score": s,
                })
        except Exception as ex:
            print(f"[WARN] feed error: {source_name} - {ex}")

    items.sort(key=lambda x: x["score"], reverse=True)

    filtered = [x for x in items if x["score"] >= 1]
    if len(filtered) < top_k:
        filtered = items[:top_k]
    return filtered[:top_k]

# =========================
# LLM Insight
# =========================
def make_insight_block(items: list[dict]) -> str:
    """
    items: [{"title":..., "media":..., "orig_link":...}, ...]
    Output:
      ✅ 결론 1줄
      🔥 핵심 3줄
      📌 관전 포인트 1문장
    """
    if not client:
        return ""

    lines = []
    for i, it in enumerate(items, 1):
        title = (it.get("title") or "").strip()
        media = (it.get("media") or "").strip()
        link = (it.get("orig_link") or "").strip()
        # 링크는 프롬프트에만 제공(출력에는 링크 넣지 말라고 지시)
        lines.append(f"{i}. [{media or '매체미상'}] {title} ({link})")

    prompt = f"""
너는 한국어로 '돈 되는 반도체 뉴스' 텔레그램 브리핑을 쓰는 에디터다.

규칙(중요):
- 아래 기사 목록만 근거로 사용한다. 목록에 없는 사실/숫자/날짜/기업관계는 만들지 않는다.
- 단정 금지: "~이다/확정" 대신 "~로 보입니다/가능성이 있습니다" 톤.
- 너무 전문용어 금지. 초보도 이해 가능한 말로 쓴다.
- 출력은 아래 형식만(링크/URL 출력 금지).

형식:
✅ 결론: (20~40자 1문장)
🔥 핵심:
- (1문장)
- (1문장)
- (1문장)
📌 관전 포인트: (1문장)

기사 목록:
{chr(10).join(lines)}
""".strip()

    try:
        resp = client.responses.create(
            model=OPENAI_MODEL,
            input=prompt,
            max_output_tokens=240,
        )
        out = getattr(resp, "output_text", "") or ""
        return out.strip()
    except Exception as ex:
        print("[WARN] OpenAI error:", ex)
        return ""

# =========================
# Post builder
# =========================
def build_post(slot_name: str, now_kst: datetime) -> tuple[str, list[dict]]:
    if os.name == "nt":
        date_str = now_kst.strftime("%m/%d").lstrip("0").replace("/0", "/")
    else:
        date_str = now_kst.strftime("%-m/%-d")

    header = f"<b>[Chip Signal | {slot_name}] {date_str} {'아침 브리핑' if slot_name=='AM' else '저녁 정리'}</b>"
    news = fetch_top_news(now_kst, top_k=TOP_K)

    items = []
    for it in news:
        link = (it.get("orig_link") or it.get("link") or "").strip()
        media = extract_media_name(it.get("title", ""))
        items.append({
            "title": it.get("title", ""),
            "media": media,           # ✅ 도메인 대신 매체명
            "orig_link": link,
            "score": it.get("score", 0),
        })

    insight = make_insight_block(items)

    lines = [header]
    if insight:
        lines.append(insight)
        lines.append("")

    for i, it in enumerate(items, 1):
        t = clean_title(it["title"], max_len=82)
        lines.append(f"{i}) {t}")

        # ✅ '출처: news.google.com' 같은 표기 없앰
        # 매체명이 있으면 매체명, 없으면 출처 줄 생략하고 원문만.
        if it["media"]:
            lines.append(f"   - 출처: {it['media']} | <a href=\"{it['orig_link']}\">원문</a>\n")
        else:
            lines.append(f"   - <a href=\"{it['orig_link']}\">원문</a>\n")

    lines.append("#반도체 #HBM #AI #파운드리 #장비 #패키징")
    text = "\n".join(lines)
    return text, items

# =========================
# Main
# =========================
def main():
    now_kst = datetime.now(tz=KST)
    today = now_kst.strftime("%Y-%m-%d")

    state = load_state()
    sent = state.get("sent", {})

    if today not in sent:
        sent[today] = {}

    for slot in SCHEDULE:
        name = slot["name"]

        if (not FORCE_SEND) and sent[today].get(name):
            print(f"Skip {name}: already sent today.")
            continue

        if within_window(now_kst, slot["hour"], slot["minute"], slot["window_minutes"]) or FORCE_SEND:
            msg, items = build_post(name, now_kst)

            records = []
            for it in items:
                records.append({
                    "ts": now_kst.isoformat(),
                    "slot": name,
                    "title": it["title"],
                    "media": it["media"],
                    "link": it["orig_link"],
                    "score": it["score"],
                    "model": OPENAI_MODEL if OPENAI_API_KEY else None,
                })
            append_archive(records)

            send_message(msg)
            sent[today][name] = True
            state["sent"] = sent
            save_state(state)
            print(f"✅ Sent {name} for {today}")
        else:
            print(f"⏳ Not in window for {name}. Now(KST): {now_kst.isoformat()}")

if __name__ == "__main__":
    main()
