"""
朝ブリーフィング: 今日の予定(Googleカレンダー直API)とAnki残数を #秘書室 に投稿する

ChromaDBは経由しない（会話AI用のRAGとは別経路。毎朝その場でAPIから取る方が新鮮なため）。
Slackのトークンは personal-mcp 側の .env を共有する（鍵の置き場を1ヶ所にするため）。

設計:
  - Anki未起動は想定内の状態なので、その旨を本文に書くだけで正常終了する
  - カレンダー取得に失敗した場合は、エラー注記付きで投稿した上で exit 1 する
    （Slackには何かしら届く／cron側のエラー検知にも引っかかる、の両取り）
  - Slack投稿自体に失敗したら exit 1（何も届いていないので沈黙失敗にしない）

使い方:
  .venv/bin/python -m batch.morning_briefing
"""
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

# 天気はOpen WebUI用に作った既存モジュールを再利用する（現在地→OpenWeatherMap）
from src.weather_fetch import get_location, get_weather

SCOPES = ["https://www.googleapis.com/auth/calendar.readonly"]
BASE_DIR = Path(__file__).resolve().parent.parent
CREDENTIALS_FILE = str(BASE_DIR / "secrets" / "credentials.json")
TOKEN_FILE = str(BASE_DIR / "secrets" / "token.json")
# Slackトークンは personal-mcp の .env を共有する（再発行時もあちら1ファイル直せば済む）
MCP_ENV_FILE = Path.home() / "claude" / "application" / "MCP" / ".env"
SLACK_CHANNEL_ID = "C0BEN3MUMPU"  # #秘書室
ANKI_CONNECT_URL = "http://127.0.0.1:8765"
JST = timezone(timedelta(hours=9))
WEEKDAYS_JA = ["月", "火", "水", "木", "金", "土", "日"]


def load_slack_token() -> str:
    for line in MCP_ENV_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line.startswith("SLACK_BOT_TOKEN="):
            return line.split("=", 1)[1].strip()
    raise RuntimeError(f"SLACK_BOT_TOKEN が {MCP_ENV_FILE} にありません")


def get_calendar_service():
    # calendar_fetch.py と同じ認証方式（token.json＋期限切れ時リフレッシュ）
    creds = None
    if Path(TOKEN_FILE).exists():
        creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
            except RefreshError:
                print("トークンが失効していたため、ブラウザで再認証します...")
                creds = None
        if not creds or not creds.valid:
            flow = InstalledAppFlow.from_client_secrets_file(CREDENTIALS_FILE, SCOPES)
            creds = flow.run_local_server(port=0)
        with open(TOKEN_FILE, "w") as f:
            f.write(creds.to_json())
    return build("calendar", "v3", credentials=creds)


def fetch_today_events() -> list[dict]:
    service = get_calendar_service()
    now = datetime.now(JST)
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    day_end = day_start + timedelta(days=1)
    result = service.events().list(
        calendarId="primary",
        timeMin=day_start.isoformat(),
        timeMax=day_end.isoformat(),
        singleEvents=True,
        orderBy="startTime",
    ).execute()
    return result.get("items", [])


def format_events(events: list[dict]) -> str:
    if not events:
        return "今日の予定はありません。"
    lines = []
    for event in events:
        summary = event.get("summary", "（タイトルなし）")
        location = event.get("location", "")
        start = event["start"]
        if "dateTime" in start:
            begin = datetime.fromisoformat(start["dateTime"]).astimezone(JST)
            end = datetime.fromisoformat(event["end"]["dateTime"]).astimezone(JST)
            when = f"{begin:%H:%M}〜{end:%H:%M}"
        else:
            when = "終日"
        line = f"• {when}  {summary}"
        if location:
            line += f"（{location}）"
        lines.append(line)
    return "\n".join(lines)


def fetch_weather_summary() -> str:
    # 天気はおまけ情報なので、取れなくても注記だけでブリーフィング自体は続行する
    try:
        with open(BASE_DIR / "secrets" / "weather.json") as f:
            api_key = json.load(f)["openweathermap"]
        lat, lon = get_location()
        w = get_weather(lat, lon, api_key)
        desc = w["weather"][0]["description"]
        temp = round(w["main"]["temp"])
        feels = round(w["main"]["feels_like"])
        return f"{desc}・{temp}℃（体感 {feels}℃）"
    except Exception:
        return "（天気の取得に失敗しました）"


def fetch_anki_summary() -> str:
    # Ankiが起動していないのは朝として普通の状態なので、エラーにはしない
    def count(query: str) -> int:
        res = requests.post(
            ANKI_CONNECT_URL,
            json={"action": "findCards", "version": 6, "params": {"query": query}},
            timeout=5,
        ).json()
        if res.get("error"):
            raise RuntimeError(res["error"])
        return len(res["result"])

    try:
        due = count("is:due")
        new = count("is:new")
        return f"期限が来ているカード: {due}枚 / 未学習の新規カード: {new}枚"
    except Exception:
        return "（Ankiが起動していないため取得できませんでした）"


def post_to_slack(text: str):
    token = load_slack_token()
    res = requests.post(
        "https://slack.com/api/chat.postMessage",
        headers={"Authorization": f"Bearer {token}"},
        json={"channel": SLACK_CHANNEL_ID, "text": text},
        timeout=10,
    ).json()
    if not res.get("ok"):
        raise RuntimeError(f"Slack投稿に失敗: {res.get('error')}")


def main() -> int:
    now = datetime.now(JST)
    date_str = f"{now.month}月{now.day}日（{WEEKDAYS_JA[now.weekday()]}）"

    calendar_failed = False
    try:
        events_text = format_events(fetch_today_events())
    except Exception as e:
        calendar_failed = True
        events_text = f"⚠️ カレンダーの取得に失敗しました: {e}"

    anki_text = fetch_anki_summary()
    weather_text = fetch_weather_summary()

    message = (
        f"☀️ おはようございます。*{date_str}* のブリーフィングです\n"
        f"\n"
        f"🌤 *天気*\n{weather_text}\n"
        f"\n"
        f"📅 *今日の予定*\n{events_text}\n"
        f"\n"
        f"📚 *Anki*\n{anki_text}"
    )
    post_to_slack(message)
    print(f"ブリーフィングを投稿しました（カレンダー{'失敗' if calendar_failed else '成功'}）")
    return 1 if calendar_failed else 0


if __name__ == "__main__":
    sys.exit(main())
