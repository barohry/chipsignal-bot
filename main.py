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

# 실시간 실행: 한 번 실행 때 몇 개 올릴지
MAX_POSTS_PER_RUN = int(os.getenv("MAX_POSTS_PER_RUN", "2"))

# 최근 몇 시간만 볼지
LOOKBACK_HOURS = int(os.getenv("LOOKBACK_HOURS", "48"))

# 강제 발행(디버그)
FORCE_SEND = os.getenv("FORCE_SEND", "0") == "1"

# 같은 토픽 연속 업로드 제한(사람 운영 느낌)
BLOCK_SAME_TOPIC_STREAK = int(os.getenv("BLOCK_SAME_TOPIC_STREAK", "2"))  # 2면 2개 연속까지만 허용

# =========================
# Sources (Google News RSS)
# =========================
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

# =========================
# Whitelist (권장: 국내 미디어 중심)
# 비우면(=환경변수 미설정) 전체 허용
# =========================
DEFAULT_WHITELIST = [
    # 경제/종합
    "hankyung.com", "mk.co.kr", "chosun.com", "joongang.co.kr", "donga.com",
    "naver.com", "news.naver.com",
    # IT/테크
    "etnews.com", "zdnet.co.kr", "thelec.kr", "bloter.net", "it.chosun.com",
    "news1.kr", "yonhapnews.co.kr",
    # 방송/기타
    "sbs.co.kr", "kbs.co.kr", "mbc.co.kr",
]
WHITELIST = os.getenv("DOMAIN_WHITELIST", "").strip()
DOMAIN_WHITELIST = [d.strip() for d in WHITELIST.split(",") if d.strip()] or DEFAULT_WHITELIST

# =========================
# Scoring weights (money-ish)
# =========================
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
    "HBM/메모리": "요즘 시장은 AI 수요를 ‘메모리 공급·가격’으로 바로 번역하는 구간입니다.",
    "파운드리/공정": "공정 경쟁은 수주·CAPEX·수율 이슈로 연결돼, 뒤늦게 따라붙는 기사들이 많습니다.",
    "장비/EUV": "장비/노광은 증설 속도와 수율을 좌우해서 ‘실제 공급 능력’의 선행지표로 읽히는 편입니다.",
    "패키징/CoWoS": "패키징 병목은 출하량(=실적)과 연결되는 경우가 많아 단기 모멘텀이 자주 붙습니다.",
    "정책/리스크": "규제/제재는 공급망 재편과 비용 증가로 이어져 변동성 요인으로 작동합니다.",
    "AI 수요": "AI 서버 투자와 연결되어, 관련 섹터로 모멘텀이 번지는 속도가 빠릅니다.",
    "실적/투자": "실적·가이던스·투자는 시장 기대치가 바뀌는 지점이라 반응이 즉각적으로 나오는 편입니다.",
}

SEMICON_CORE = [
    "반도체", "hbm", "dram", "ddr", "낸드", "nand",
    "파운드리", "tsmc", "asml", "euv", "노광",
    "2나노", "3나노", "칩렛", "cowos", "패키징",
    "gpu", "엔비디아", "nvidia", "데이터센터", "서버"
]

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

def html_escape(s: str) -> str:
    return html.escape(s or "", quote=True)

def short_domain(link: str) -> str:
    try:
        return urlparse(link).netloc.replace("www.", "")
    except Exception:
        return ""

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
    m = re.search(r"\s-\s([^-]+)$", (title or "").strip())
    return m.group(1).strip() if m else ""

def strip_media_suffix(title: str) -> str:
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
    try:
        for l in getattr(entry, "links", []) or []:
            href = l.get("href") if isinstance(l, dict) else getattr(l, "href", None)
            if href and href.startswith("http") and (not _is_google_news_url(href)):
                return href
    except Exception:
        pass

    m = re.search(r'href="(https?://[^"]+)"', summary_html or "")
    if m and (not _is_google_news_url(m.group(1))):
        return m.group(1)

    m2 = re.search(r'(https?://[^\s"<]+)', summary_html or "")
    if m2 and (not _is_google_news_url(m2.group(1))):
        return m2.group(1)

    return item_link

def strip_html(s: str) -> str:
    s = re.sub(r"<[^>]+>", " ", s or "")
    s = html.unescape(s)
    s = re.sub(r"\s+", " ", s).strip()
    return s

def split_sentences(s: str) -> list[str]:
    s = strip_html(s)
    if not s:
        return []
    parts = re.split(r"(?<=[.!?。])\s+|(?<=다\.)\s+|(?<=다\?)\s+|(?<=다!)\s+", s)
    parts = [p.strip() for p in parts if p and p.strip()]
    return parts

def score_item(title: str, summary: str) -> int:
    text = normalize_text(f"{title} {summary}")
    score = 0
    for k, w in KEYWORD_WEIGHTS.items():
        if normalize_text(k) in text:
            score += w
    if re.search(r"\b(\d+(\.\d+)?)(nm|%|조|억|만|B|M|T|배|원|달러)\b", text):
        score += 3
    return score

def detect_top_topic(title: str, summary: str) -> str:
    text = normalize_text(f"{title} {summary}")
    best = ("반도체 시장", 0)
    for name, keys in TOPIC_BUCKETS:
        c = 0
        for k in keys:
            if normalize_text(k) in text:
                c += 1
        if c > best[1]:
            best = (name, c)
    return best[0]

def sentence_score(sent: str) -> int:
    t = normalize_text(sent)
    sc = 0
    if re.search(r"(\d+(\.\d+)?)(nm|%|조|억|만|배|원|달러|B|M|T)", sent):
        sc += 4
    for k, w in KEYWORD_WEIGHTS.items():
        if normalize_text(k) in t:
            sc += min(3, w // 4)
    if re.search(r"(투자|증설|가이던스|실적|수주|양산|출하|규제|제재|관세|수율|공급|부족|급등|하락)", sent):
        sc += 2
    return sc

def too_similar_to_title(title: str, sent: str) -> bool:
    a = strip_media_suffix(title)
    b = strip_media_suffix(sent)
    sa = set(re.findall(r"[0-9a-zA-Z가-힣]+", a))
    sb = set(re.findall(r"[0-9a-zA-Z가-힣]+", b))
    if not sa or not sb:
        return False
    j = len(sa & sb) / len(sa | sb)
    return j >= 0.80

def extract_summary_keywords(title: str, summary: str, top_n: int = 6) -> list[str]:
    text = normalize_text(f"{title} {strip_html(summary)}")
    hits = []
    for k, w in KEYWORD_WEIGHTS.items():
        if normalize_text(k) in text:
            hits.append((k, w))
    hits.sort(key=lambda x: x[1], reverse=True)
    out = []
    for k, _ in hits:
        kk = k.upper() if k.isalpha() else k
        if kk not in out:
            out.append(kk)
        if len(out) >= top_n:
            break
    return out

def build_long_summary(title: str, summary_html: str) -> tuple[list[str], list[str]]:
    """
    사람처럼 보이게: 문장 4~6개 + 포인트 1~2개
    - 첫 문장은 “발췌” 느낌으로 따옴표 처리(가장 점수 높은 문장)
    - 나머지는 요약 문장(기사에 있는 문장 편집)
    """
    sents = split_sentences(summary_html)
    ranked = sorted(sents, key=sentence_score, reverse=True)

    picked = []
    for s in ranked:
        if too_similar_to_title(title, s):
            continue
        s = re.sub(r"\s+", " ", s).strip()
        if len(s) > 110:
            s = s[:107] + "..."
        if s and s not in picked:
            picked.append(s)
        if len(picked) >= 6:  # ✅ 길게
            break

    bullets = []
    kws = extract_summary_keywords(title, summary_html, top_n=7)
    if kws:
        bullets.append("키워드: " + " · ".join(kws[:7]))

    clean = strip_html(summary_html)
    nums = re.findall(r"(\d+(?:\.\d+)?\s*(?:nm|%|조|억|만|배|원|달러|B|M|T))", clean)
    nums = list(dict.fromkeys(nums))
    if nums:
        bullets.append("수치: " + ", ".join(nums[:5]))

    return picked[:6], bullets[:2]

def build_why_long(topic: str, title: str, summary: str) -> str:
    base = WHY_IMPORTANT_TEMPLATES.get(
        topic,
        "관련 기사들이 같은 방향으로 묶이는지 흐름을 체크해보시는 게 좋겠습니다."
    )

    text = normalize_text(f"{title} {strip_html(summary)}")
    impact = []

    if any(k in text for k in ["실적", "가이던스", "전망", "매출"]):
        impact.append("실적/가이던스는 기대치가 바뀌는 지점이라, 단기 가격 반응이 커질 수 있습니다.")
    if any(k in text for k in ["투자", "capex", "증설", "양산"]):
        impact.append("투자·증설은 공급 능력과 직결돼, 중장기 사이클 판단에 도움이 됩니다.")
    if any(k in text for k in ["규제", "제재", "수출규제", "관세"]):
        impact.append("규제 이슈는 공급망 재편·비용 증가로 이어져, 변동성 요인이 될 수 있습니다.")
    if any(k in text for k in ["수율", "노광", "euv", "asml", "장비"]):
        impact.append("장비·수율은 ‘실제 출하 가능 물량’에 영향을 줘, 모멘텀의 근거가 되기 쉽습니다.")
    if any(k in text for k in ["엔비디아", "nvidia", "서버", "데이터센터", "gpu", "ai"]):
        impact.append("AI 수요는 메모리/패키징 병목과 이어지며, 관련 섹터로 번지는 속도가 빠릅니다.")

    extra = " ".join(impact[:2]).strip()
    if extra:
        return f"{base} {extra}"
    return base

def is_semiconductor_relevant(title: str, summary: str) -> bool:
    text = normalize_text(f"{title} {strip_html(summary)}")
    return any(normalize_text(k) in text for k in SEMICON_CORE)

def domain_allowed(domain: str) -> bool:
    if not DOMAIN_WHITELIST:
        return True
    d = (domain or "").lower()
    return any(d == w or d.endswith("." + w) for w in DOMAIN_WHITELIST)

def make_one_line_conclusion(topic: str, title: str) -> str:
    core = strip_html(title)
    core = re.sub(r"\s+", " ", core).strip()
    core = re.sub(r"\s*-\s*[^-]+$", "", core).strip()
    if len(core) > 58:
        core = core[:55] + "..."
    # 사람 말투
    return f"오늘은 <b>{html_escape(topic)}</b> 쪽이 다시 힘을 받는 흐름입니다 — <b>{html_escape(core)}</b>"

# =========================
# Message Builder (사람 편집 느낌)
# =========================
def build_feed_message(item: dict) -> tuple[str, str]:
    title = item.get("title", "")
    summary = item.get("summary", "")
    media = item.get("media", "") or short_domain(item.get("orig_link", ""))
    link = item.get("orig_link", "")
    published_kst = item.get("published_kst", None)

    topic = detect_top_topic(title, summary)
    conclusion = make_one_line_conclusion(topic, title)

    # 길게 요약
    sents, bullets = build_long_summary(title, summary)

    # 첫 문장은 따옴표 발췌(정보량 높은 문장)
    lines = []
    if sents:
        first = sents[0]
        lines.append(f"• “<b>{html_escape(first)}</b>”")
        for s in sents[1:]:
            lines.append(f"• {html_escape(s)}")

    # 하이라이트(코드 박스 느낌)
    if bullets:
        for b in bullets:
            lines.append(f"• <code>{html_escape(b)}</code>")

    why = build_why_long(topic, title, summary)

    # 시간 표시
    time_line = ""
    if isinstance(published_kst, datetime):
        time_line = published_kst.strftime("%m/%d %H:%M")
        time_line = f"<i>{html_escape(time_line)} (KST)</i>"

    msg = []
    msg.append(f"<b>[Chip Signal] 업데이트</b>")
    if time_line:
        msg.append(time_line)

    msg.append(f"🧩 <b>한 줄 결론</b>: {conclusion}")
    msg.append("📌 <b>핵심 요약</b>:")
    msg.extend(lines if lines else ["• <code>요약 데이터가 부족해 키워드 중심으로 정리합니다.</code>"])
    msg.append(f"💡 <b>왜 중요?</b> {html_escape(why)}")
    msg.append(f"📰 <b>출처</b>: {html_escape(media)}")
    msg.append(f"🔗 <b>원문</b>: <a href=\"{html_escape(link)}\">기사 보기</a>")
    msg.append("\n#반도체 #HBM #AI #파운드리 #장비 #패키징")

    return "\n".join(msg), topic

# =========================
# Fetch candidates
# =========================
def fetch_candidates(now_utc: datetime) -> list[dict]:
    items = []
    seen_norm_titles = []

    for source_name, url in RSS_FEEDS:
        try:
            feed = feedparser.parse(url)
            for e in feed.entries[:140]:
                title = getattr(e, "title", "") or ""
                link = getattr(e, "link", "") or ""
                summary = getattr(e, "summary", "") or getattr(e, "description", "") or ""

                if not title or not link:
                    continue
                if not is_recent(e, now_utc, max_hours=LOOKBACK_HOURS):
                    continue
                if not is_semiconductor_relevant(title, summary):
                    continue

                orig = extract_original_url(e, link, summary)
                dom = short_domain(orig)
                if not domain_allowed(dom):
                    continue

                norm = strip_media_suffix(title)
                if any(is_similar(norm, nt) for nt in seen_norm_titles):
                    continue
                seen_norm_titles.append(norm)

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

    # 최신성 우선 + 점수 보조
    def sort_key(x):
        ts = x["published_utc"].timestamp() if x["published_utc"] else 0
        return (ts, x["score"])

    items.sort(key=sort_key, reverse=True)
    return items

# =========================
# Main
# =========================
def main():
    now_utc = datetime.now(tz=UTC)
    state = load_state()

    posted = state.get("posted", {})
    if not isinstance(posted, dict):
        posted = {}

    # 토픽 연속 제한용
    last_topics = state.get("last_topics", [])
    if not isinstance(last_topics, list):
        last_topics = []

    candidates = fetch_candidates(now_utc)

    to_post = []
    streak_topic = None
    streak_count = 0

    # 현재 last_topics 기준으로 streak 계산
    if last_topics:
        streak_topic = last_topics[-1]
        streak_count = 1
        for i in range(len(last_topics) - 2, -1, -1):
            if last_topics[i] == streak_topic:
                streak_count += 1
            else:
                break

    for it in candidates:
        key = dedupe_key(it["title"], it["orig_link"])
        if (not FORCE_SEND) and (key in posted):
            continue

        msg, topic = build_feed_message(it)

        # 같은 토픽 연속 제한
        if (not FORCE_SEND) and streak_topic == topic and streak_count >= BLOCK_SAME_TOPIC_STREAK:
            continue

        to_post.append((key, it, msg, topic))
        # streak 업데이트(루프 내부용)
        if streak_topic == topic:
            streak_count += 1
        else:
            streak_topic = topic
            streak_count = 1

        if len(to_post) >= MAX_POSTS_PER_RUN:
            break

    if not to_post:
        print("No new items to post.")
        return

    archive_rows = []
    for key, it, msg, topic in to_post:
        send_message(msg)
        posted[key] = now_utc.isoformat()

        last_topics.append(topic)
        last_topics = last_topics[-20:]  # 최근 20개만 보관

        archive_rows.append({
            "ts": now_utc.astimezone(KST).isoformat(),
            "title": it["title"],
            "media": it["media"] or short_domain(it["orig_link"]),
            "link": it["orig_link"],
            "score": it["score"],
            "topic": topic,
            "mode": "no_api_feed_human_format_long",
        })

    state["posted"] = posted
    state["last_topics"] = last_topics
    save_state(state)
    append_archive(archive_rows)

    print(f"Posted {len(to_post)} item(s).")

if __name__ == "__main__":
    main()
