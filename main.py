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
# -------
