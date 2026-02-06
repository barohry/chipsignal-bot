import os
import re
import json
import hashlib
import html
from datetime import datetime, timedelta, timezone
from urllib.parse import quote, urlparse

import httpx
import feedparser

# =========================
# Config
# =========================
STATE_FILE = "state.json"
ARCHIVE_FILE = "archive.jsonl"

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")  # e.g., "@chipsignal"

KST = timezone(timedelta(hours=9))
UTC = timezone.utc

# 한 번 실행될 때 새 기사 몇 개까지 올릴지
MAX_POSTS_PER_RUN = int(os.getenv("MAX_POSTS_PER_RUN", "2"))

# 최근 몇 시간 기사만 대상으로 할지 (너무 넓으면 중복/노이즈 증가)
LOOKBACK_HOURS = int(os.getenv("LOOKBACK_HOURS", "48"))

# 강제 발행(디버그): 1이면 이미 올린 기사라도 상위 n개를 그냥 보냄(테스트용)
FORCE_SEND = os.getenv("FORCE_SEND", "0") == "1"

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
# Money-ish keyword weights
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

TOPIC_BUCKETS = [
    ("HBM/메모리", ["hbm", "dram", "ddr5", "sk하이닉스", "하이닉스", "마이크론", "삼성전자", "메모리"]),
    ("파운드리/공정", ["tsmc", "파운드리", "2나노", "3나노", "gaa", "gate-all-around"]),
    ("장비/EUV", ["asml", "euv", "노광", "장비", "수율", "소재"]),
    ("패키징/CoWoS", ["cowos", "첨단패키징", "칩렛", "패키징"]),
    ("정책/리스크", ["수출규제", "제재", "규제", "관세", "중국"]),
    ("AI 수요", ["엔비디아", "nvidia", "ai", "데이터센터", "서버", "gpu"]),
    ("실적/투자", ["실적", "가이던스", "전망", "매출", "capex", "투자", "증설"]),
]

WHY_IMPORTANT_TEMPLATES = {
    "HBM/메모리": "AI 수요(서버/GPU)와 직결되는 메모리 공급·가격 기대가 같이 움직이는 구간입니다.",
    "파운드리/공정": "공정 경쟁은 고객사 수주·CAPEX·수율 이슈로 바로 이어질 수 있어 흐름 체크가 중요합니다.",
    "장비/EUV": "장비/노광은 증설 속도와 수율을 좌우해서 ‘실제 공급 능력’의 선행지표로 읽히는 편입니다.",
    "패키징/CoWoS": "패키징 병목은 출하량(=실적)과 연결되는 경우가 많아 단기 모멘텀으로 자주 언급됩니다.",
    "정책/리스크": "규제/제재는 공급망 재편과 비용 증가로 이어질 수 있어 변동성 요인으로 작동합니다.",
    "AI 수요": "AI 서버 투자와 연결되어 관련 기업의 가이던스/수주 기대가 같이 부각되기 쉽습니다.",
    "실적/투자": "실적·가이던스·투자는 시장 기대치가 바뀌는 지점이라 반응이 빠르게 나오는 편입니다.",
}

# =========================
# Utilities
# =========================
def load_state() -> dict:
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

def normalize_text(s: str) -> str:
    s = (s or "").strip().lower()
    s = re.sub(r"\s+", " ", s)
    return s

def short_domain(link: str) -> str:
    try:
        return urlparse(link).netloc.replace("www.", "")
    except Exception:
        return ""

def html_escape(s: str) -> str:
    return html.escape(s or "", quote=True)

def send_message(text_html: str):
    if not BOT_TOKEN or not CHAT_ID:
        raise RuntimeError("Missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID")

    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": CHAT_ID,
        "text": text_html,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    with httpx.Client(timeout=25) as client_http:
        r = client_http.post(url, json=payload)
        print("Telegram status:", r.status_code)
        print("Telegram response:", r.text[:300])
        r.raise_for_status()

def parse_entry_time(entry) -> datetime | None:
    # feedparser는 published_parsed / updated_parsed가 있으면 time.struct_time
    for key in ("published_parsed", "updated_parsed"):
        t = getattr(entry, key, None)
        if t:
            try:
                return datetime(*t[:6], tzinfo=UTC)
            except Exception:
                pass
    return None

def is_recent(entry, now_utc: datetime, max_hours: int) -> bool:
    dt = parse_entry_time(entry)
    if dt is None:
        return True
    return (now_utc - dt) <= timedelta(hours=max_hours)

def _is_google_news_url(u: str) -> bool:
    return "news.google.com" in (u or "")

def extract_media_name(title: str) -> str:
    # "... - 뉴데일리" 형태면 매체명만 뽑음
    m = re.search(r"\s-\s([^-]+)$", (title or "").strip())
    return m.group(1).strip() if m else ""

def strip_media_suffix(title: str) -> str:
    # 중복 제거용: [속보] + 끝의 "- 매체명" 제거 등
    t = (title or "").strip()
    t = re.sub(r"^\[[^\]]+\]\s*", "", t)
    t = re.sub(r"\s*-\s*[^-]+$", "", t)
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

def dedupe_key(title: str, orig_link: str) -> str:
    base = strip_media_suffix(title) + "|" + (orig_link or "")
    return hashlib.md5(base.encode("utf-8")).hexdigest()

def extract_original_url(entry, item_link: str, summary_html: str) -> str:
    # 0) entry.links에서 외부 링크 우선
    try:
        for l in getattr(entry, "links", []) or []:
            href = l.get("href") if isinstance(l, dict) else getattr(l, "href", None)
            if href and href.startswith("http") and (not _is_google_news_url(href)):
                return href
    except Exception:
        pass

    # 1) summary href
    m = re.search(r'href="(https?://[^"]+)"', summary_html or "")
    if m and (not _is_google_news_url(m.group(1))):
        return m.group(1)

    # 2) plain url
    m2 = re.search(r'(https?://[^\s"<]+)', summary_html or "")
    if m2 and (not _is_google_news_url(m2.group(1))):
        return m2.group(1)

    # 3) fallback
    return item_link

def score_item(title: str, summary: str) -> int:
    text = normalize_text(f"{title} {summary}")
    score = 0
    for k, w in KEYWORD_WEIGHTS.items():
        if normalize_text(k) in text:
            score += w
    if re.search(r"\b(\d+(\.\d+)?)(nm|%|조|억|만|B|M|T)\b", text):
        score += 3
    return score

def detect_top_topic(title: str, summary: str) -> str:
    text = normalize_text(f"{title} {summary}")
    best = ("핵심 이슈", 0)
    for name, keys in TOPIC_BUCKETS:
        c = 0
        for k in keys:
            if normalize_text(k) in text:
                c += 1
        if c > best[1]:
            best = (name, c)
    return best[0]

# =========================
# Human-like summary builder (NO API)
# =========================
def strip_html(s: str) -> str:
    s = re.sub(r"<[^>]+>", " ", s or "")
    s = html.unescape(s)
    s = re.sub(r"\s+", " ", s).strip()
    return s

def split_sentences(s: str) -> list[str]:
    s = strip_html(s)
    if not s:
        return []
    # 한국어/영문 혼합 문장 분리
    parts = re.split(r"(?<=[.!?。])\s+|(?<=다\.)\s+|(?<=다\?)\s+|(?<=다!)\s+", s)
    parts = [p.strip() for p in parts if p and p.strip()]
    return parts

def sentence_score(sent: str) -> int:
    t = normalize_text(sent)
    sc = 0
    # 숫자/단위/퍼센트가 있으면 정보량 가산
    if re.search(r"(\d+(\.\d+)?)(nm|%|조|억|만|B|M|T|배|원|달러)", sent):
        sc += 4
    # 돈 되는 키워드 가산
    for k, w in KEYWORD_WEIGHTS.items():
        if normalize_text(k) in t:
            sc += min(3, w // 4)  # 과도한 점수 폭주 방지
    # 행동 단어(투자/증설/가이던스 등)
    if re.search(r"(투자|증설|가이던스|실적|수주|양산|출하|규제|제재|관세|수율|공급|부족|급등|하락)", sent):
        sc += 2
    return sc

def pick_best_sentences(summary_html: str, k: int = 2, max_chars: int = 70) -> list[str]:
    sents = split_sentences(summary_html)
    if not sents:
        return []

    ranked = sorted(sents, key=sentence_score, reverse=True)
    picked = []
    for s in ranked:
        s = re.sub(r"\s+", " ", s).strip()
        if len(s) > max_chars:
            s = s[: max_chars - 3] + "..."
        if s and s not in picked:
            picked.append(s)
        if len(picked) >= k:
            break
    return picked

def make_one_line_summary(title: str, topic: str) -> str:
    # 단정 피하고 “~로 보입니다/~쪽이 부각됩니다” 톤
    core = strip_media_suffix(title)
    core = re.sub(r"\s+", " ", core).strip()
    if len(core) > 42:
        core = core[:39] + "..."
    # 사람같은 문장 템플릿
    return f"{topic} 이슈가 다시 부각됩니다: {core}"

def build_feed_message(item: dict) -> str:
    """
    item: {title, summary, media, orig_link, score, published_kst}
    텔레그램 HTML 메시지로 구성
    """
    title = item.get("title", "")
    summary = item.get("summary", "")
    media = item.get("media", "") or short_domain(item.get("orig_link", ""))
    link = item.get("orig_link", "")
    published_kst = item.get("published_kst", None)

    topic = detect_top_topic(title, summary)
    why = WHY_IMPORTANT_TEMPLATES.get(topic, "관련 기사들이 같은 방향으로 묶이는지 흐름을 체크해보시는 게 좋겠습니다.")
    one_line = make_one_line_summary(title, topic)

    # summary 발췌 2문장(인용 느낌)
    picks = pick_best_sentences(summary, k=2, max_chars=78)
    quote_lines = []
    for p in picks:
        quote_lines.append(f"• “{html_escape(p)}”")

    # 시간 표기(선택)
    time_line = ""
    if isinstance(published_kst, datetime):
        time_line = published_kst.strftime("%m/%d %H:%M")
        time_line = f"<i>{html_escape(time_line)} (KST)</i>\n"

    # 메시지 구성: 링크는 맨 마지막
    msg = []
    msg.append(f"<b>[Chip Signal] 업데이트</b>")
    if time_line:
        msg.append(time_line.strip())
    msg.append(f"🧩 <b>한 줄 요약</b>: {html_escape(one_line)}")
    if quote_lines:
        msg.append("📌 <b>기사 요약(발췌)</b>:")
        msg.extend(quote_lines)
    msg.append(f"💡 <b>왜 중요?</b> {html_escape(why)}")

    if media:
        msg.append(f"📰 <b>출처</b>: {html_escape(media)}")

    # 링크는 마지막 “첨부” 느낌
    msg.append(f"🔗 <b>원문</b>: <a href=\"{html_escape(link)}\">기사 보기</a>")

    msg.append("\n#반도체 #HBM #AI #파운드리 #장비 #패키징")
    return "\n".join(msg)

# =========================
# Fetch & post
# =========================
def fetch_candidates(now_utc: datetime) -> list[dict]:
    items = []
    seen_norm_titles = []

    for source_name, url in RSS_FEEDS:
        try:
            feed = feedparser.parse(url)
            for e in feed.entries[:100]:
                title = getattr(e, "title", "") or ""
                link = getattr(e, "link", "") or ""
                summary = getattr(e, "summary", "") or getattr(e, "description", "") or ""

                if not title or not link:
                    continue
                if not is_recent(e, now_utc, max_hours=LOOKBACK_HOURS):
                    continue

                norm = strip_media_suffix(title)
                if any(is_similar(norm, nt) for nt in seen_norm_titles):
                    continue
                seen_norm_titles.append(norm)

                orig = extract_original_url(e, link, summary)
                sc = score_item(title, summary)
                media = extract_media_name(title)

                dt_utc = parse_entry_time(e)
                dt_kst = dt_utc.astimezone(KST) if dt_utc else None

                items.append({
                    "source": source_name,
                    "title": title.strip(),
                    "summary": summary.strip(),
                    "orig_link": orig.strip(),
                    "score": sc,
                    "media": media,
                    "published_utc": dt_utc,
                    "published_kst": dt_kst,
                })
        except Exception as ex:
            print(f"[WARN] feed error: {source_name} - {ex}")

    # “실시간 피드”는 최신성도 중요하니 published 우선 + 점수 보조
    def sort_key(x):
        ts = x["published_utc"].timestamp() if x["published_utc"] else 0
        return (ts, x["score"])

    items.sort(key=sort_key, reverse=True)
    return items

def main():
    now_utc = datetime.now(tz=UTC)
    state = load_state()

    posted = state.get("posted", {})  # {dedupe_key: iso_ts}
    if not isinstance(posted, dict):
        posted = {}

    candidates = fetch_candidates(now_utc)

    to_post = []
    for it in candidates:
        key = dedupe_key(it["title"], it["orig_link"])
        if FORCE_SEND:
            to_post.append((key, it))
        else:
            if key in posted:
                continue
            to_post.append((key, it))
        if len(to_post) >= MAX_POSTS_PER_RUN:
            break

    if not to_post:
        print("No new items to post.")
        return

    archive_rows = []
    for key, it in to_post:
        msg = build_feed_message(it)
        send_message(msg)

        posted[key] = now_utc.isoformat()

        archive_rows.append({
            "ts": now_utc.astimezone(KST).isoformat(),
            "title": it["title"],
            "media": it["media"] or short_domain(it["orig_link"]),
            "link": it["orig_link"],
            "score": it["score"],
            "mode": "no_api_feed",
        })

    state["posted"] = posted
    save_state(state)
    append_archive(archive_rows)
    print(f"Posted {len(to_post)} item(s).")

if __name__ == "__main__":
    main()
