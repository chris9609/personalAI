"""AIニュース便: 朝のテックニュースを「読む新聞」形式で #AIニュース に配信する

やること:
  1. RSSフィード（TechCrunch AI / The Verge AI）から新着記事を取得
  2. 記事本文を確保（The VergeはRSSに全文あり / TechCrunchは記事ページから抽出）
  3. Ollamaで「英語要約(約10行)」と「その日本語訳」を生成
  4. 本文メッセージ = 英語要約のみ、日本語訳はスレッドに投稿
     （英語で読む→答え合わせしたい時だけスレッドを開く、という学習動線のため）

設計:
  - 毎朝8:10にcronで1回実行（ポーリングではないのでロック不要）
  - 配信済みURLは data/news_digest_state.json に記録して重複配信を防ぐ
  - 記事単位の失敗はスキップして残りだけ配信する。1件も配信できなければ #エラー へ通知
  - フィードの増減は FEEDS のリストを編集するだけ

使い方:
  .venv/bin/python -m batch.news_digest
"""
import html
import json
import re
import sys
import traceback
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import requests

BASE_DIR = Path(__file__).resolve().parent.parent
STATE_FILE = BASE_DIR / "data" / "news_digest_state.json"
MCP_ENV_FILE = Path.home() / "claude" / "application" / "MCP" / ".env"

NEWS_CHANNEL_ID = "C0BFBBFFDC4"  # #AIニュース
ERROR_CHANNEL_ID = "C0BEQ1A52HX"  # #エラー

OLLAMA_URL = "http://localhost:11434"
OLLAMA_MODEL = "gemma4:e4b"

JST = timezone(timedelta(hours=9))
WEEKDAYS_JA = ["月", "火", "水", "木", "金", "土", "日"]

# ニュースサイトはUAなしのリクエストを弾くことがある
UA_HEADER = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"}
ATOM = "{http://www.w3.org/2005/Atom}"

# 配信元はここで増減する。count=その媒体から何件採るか
# body_in_feed=True ならRSS内の全文を使う / False なら記事ページを取りに行く
FEEDS = [
    {
        "name": "TechCrunch",
        "url": "https://techcrunch.com/category/artificial-intelligence/feed/",
        "count": 2,
        "body_in_feed": False,
    },
    {
        "name": "The Verge",
        "url": "https://www.theverge.com/rss/ai-artificial-intelligence/index.xml",
        "count": 1,
        "body_in_feed": True,
    },
]
MAX_BODY_CHARS = 4000  # 要約に渡す本文の上限（プロンプトの肥大防止）
POSTED_URLS_KEEP = 200  # stateに残す配信済みURLの件数

SUMMARY_PROMPT_TEMPLATE = (
    "You are an assistant that summarizes tech news for an English learner "
    "(intermediate level).\n\n"
    "Article title: {title}\n"
    "Article text: {body}\n\n"
    "Write an English summary of this article in about 8-10 sentences "
    "(roughly 150-180 words). Use clear, natural English. "
    "Do not use bullet points; write flowing paragraphs.\n"
    "Output only the summary, nothing else."
)

TRANSLATE_PROMPT_TEMPLATE = (
    "以下の英文ニュース要約を、自然な日本語に翻訳してください。\n"
    "訳文だけを出力し、前置きや注釈は不要です。\n\n"
    "{summary}"
)


# ---------- 共通ヘルパー（朝ブリーフィング・英語アシスタントと同じ方式） ----------

def load_slack_token() -> str:
    for line in MCP_ENV_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line.startswith("SLACK_BOT_TOKEN="):
            return line.split("=", 1)[1].strip()
    raise RuntimeError(f"SLACK_BOT_TOKEN が {MCP_ENV_FILE} にありません")


def slack_api(token: str, method: str, **params) -> dict:
    res = requests.post(
        f"https://slack.com/api/{method}",
        headers={"Authorization": f"Bearer {token}"},
        json=params,
        timeout=15,
    ).json()
    if not res.get("ok"):
        raise RuntimeError(f"Slack API {method} 失敗: {res.get('error')}")
    return res


def ollama_generate(prompt: str) -> str:
    resp = httpx.post(
        f"{OLLAMA_URL}/api/generate",
        json={"model": OLLAMA_MODEL, "prompt": prompt, "stream": False},
        timeout=600,
    )
    resp.raise_for_status()
    return resp.json()["response"].strip()


def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    return {"posted_urls": []}


def save_state(state: dict):
    state["posted_urls"] = state["posted_urls"][-POSTED_URLS_KEEP:]
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


# ---------- 記事の収集 ----------

def strip_html(text: str) -> str:
    text = re.sub(r"<script.*?</script>|<style.*?</style>", " ", text, flags=re.S)
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", html.unescape(text)).strip()


def parse_feed(xml_text: str) -> list[dict]:
    """RSS2.0とAtomの両対応で {title, link, excerpt, body} のリストを返す"""
    root = ET.fromstring(xml_text)
    entries = []
    # RSS 2.0 (TechCrunch)
    for item in root.findall(".//item"):
        entries.append({
            "title": (item.findtext("title") or "").strip(),
            "link": (item.findtext("link") or "").strip(),
            "excerpt": strip_html(item.findtext("description") or ""),
            "body": "",
        })
    # Atom (The Verge)
    for entry in root.findall(f".//{ATOM}entry"):
        link_el = entry.find(f"{ATOM}link")
        link = link_el.get("href") if link_el is not None and link_el.get("href") \
            else (entry.findtext(f"{ATOM}id") or "")
        entries.append({
            "title": (entry.findtext(f"{ATOM}title") or "").strip(),
            "link": link.strip(),
            "excerpt": strip_html(entry.findtext(f"{ATOM}summary") or ""),
            "body": strip_html(entry.findtext(f"{ATOM}content") or ""),
        })
    return [e for e in entries if e["title"] and e["link"]]


def fetch_article_body(url: str) -> str:
    """記事ページの<p>タグから本文を抽出する（TechCrunch用）"""
    page = requests.get(url, headers=UA_HEADER, timeout=15).text
    paragraphs = re.findall(r"<p[^>]*>(.*?)</p>", page, flags=re.S)
    return " ".join(strip_html(p) for p in paragraphs).strip()


def collect_articles(state: dict) -> list[dict]:
    """各フィードから未配信の記事を count 件ずつ集める"""
    posted = set(state["posted_urls"])
    articles = []
    for feed in FEEDS:
        try:
            xml_text = requests.get(feed["url"], headers=UA_HEADER, timeout=15).text
            entries = parse_feed(xml_text)
        except Exception as e:
            print(f"!!! フィード取得失敗（{feed['name']}）: {e}")
            continue
        picked = 0
        for entry in entries:
            if picked >= feed["count"]:
                break
            if entry["link"] in posted:
                continue
            if not feed["body_in_feed"]:
                try:
                    entry["body"] = fetch_article_body(entry["link"])
                except Exception as e:
                    print(f"!!! 本文取得失敗（{entry['link']}）: {e}")
            # 本文が取れなければRSSの抜粋で代用（短くても要約は成立する）
            if not entry["body"]:
                entry["body"] = entry["excerpt"]
            if not entry["body"]:
                continue  # 本文も抜粋も無い記事は要約できないので飛ばす
            entry["source"] = feed["name"]
            entry["body"] = entry["body"][:MAX_BODY_CHARS]
            articles.append(entry)
            picked += 1
    return articles


# ---------- 要約と翻訳 ----------

def summarize_and_translate(article: dict):
    """articleに summary（英語要約）と translation（日本語訳）を書き込む"""
    article["summary"] = ollama_generate(
        SUMMARY_PROMPT_TEMPLATE.format(title=article["title"], body=article["body"]))
    article["translation"] = ollama_generate(
        TRANSLATE_PROMPT_TEMPLATE.format(summary=article["summary"]))


# ---------- Slack投稿 ----------

def post_digest(token: str, articles: list[dict]):
    """本文=英語要約のみ、日本語訳は記事ごとにスレッドへ"""
    now = datetime.now(JST)
    date_str = f"{now.month}月{now.day}日（{WEEKDAYS_JA[now.weekday()]}）"
    blocks = [
        f"*■ {a['title']}*（{a['source']}）\n{a['summary']}\n🔗 {a['link']}"
        for a in articles
    ]
    message = (
        f"📰 おはようございます。今朝のAIニュースです（{date_str}）\n\n"
        + "\n\n".join(blocks)
        + "\n\n🇯🇵 日本語訳はスレッドにあるよ"
    )
    res = slack_api(token, "chat.postMessage", channel=NEWS_CHANNEL_ID,
                    text=message, unfurl_links=False, unfurl_media=False)
    thread_ts = res["ts"]
    for a in articles:
        slack_api(token, "chat.postMessage", channel=NEWS_CHANNEL_ID,
                  text=f"🇯🇵 *{a['title']}*\n{a['translation']}",
                  thread_ts=thread_ts, unfurl_links=False, unfurl_media=False)


def notify_error(message: str):
    try:
        token = load_slack_token()
        slack_api(token, "chat.postMessage", channel=ERROR_CHANNEL_ID,
                  text=f":rotating_light: AIニュース便の配信に失敗: {message[:300]}")
    except Exception:
        print("!!! エラー通知の送信にも失敗しました")


# ---------- main ----------

def main() -> int:
    state = load_state()
    try:
        articles = collect_articles(state)
        if not articles:
            raise RuntimeError("配信できる記事が1件も集められませんでした")

        delivered = []
        for a in articles:
            try:
                summarize_and_translate(a)
                delivered.append(a)
                print(f"要約完了: [{a['source']}] {a['title'][:50]}")
            except Exception as e:
                print(f"!!! 要約失敗（{a['title'][:50]}）: {e}")
        if not delivered:
            raise RuntimeError("全記事の要約に失敗しました")

        token = load_slack_token()
        post_digest(token, delivered)
        state["posted_urls"].extend(a["link"] for a in delivered)
        save_state(state)
        print(f"配信完了: {len(delivered)} 件（収集 {len(articles)} 件）")
        return 0
    except Exception as e:
        traceback.print_exc()
        notify_error(str(e))
        return 1


if __name__ == "__main__":
    sys.exit(main())
