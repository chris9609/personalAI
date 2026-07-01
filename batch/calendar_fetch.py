"""
Googleカレンダーから予定を取得してChromaDBに取り込むスクリプト
初回実行時にブラウザでGoogle認証が走る（token.jsonが生成されて以後は不要）

毎晩の実行を前提に「全消し→再取込」方式を取る:
  ChromaDB内のカレンダー由来データ（source=calendar）だけを削除してから
  期間内の全予定を入れ直す。重複・変更・削除ずれが構造的に起きない。

使い方:
  .venv/bin/python -m batch.calendar_fetch                 # 過去365日〜未来365日
  .venv/bin/python -m batch.calendar_fetch --past-days 30 --future-days 90
"""
import argparse
import os
from pathlib import Path
from datetime import datetime, timezone, timedelta

from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

import chromadb
from llama_index.core import Document, VectorStoreIndex, StorageContext
from llama_index.vector_stores.chroma import ChromaVectorStore
from llama_index.embeddings.ollama import OllamaEmbedding
from llama_index.core import Settings

SCOPES = ["https://www.googleapis.com/auth/calendar.readonly"]
# 相対パスだと実行場所（cwd）によって指す先が変わるため、
# このファイルの位置を基準にした絶対パスで解決する（他のバッチと同じ方式）
BASE_DIR = Path(__file__).resolve().parent.parent
CREDENTIALS_FILE = str(BASE_DIR / "secrets" / "credentials.json")
TOKEN_FILE = str(BASE_DIR / "secrets" / "token.json")
CHROMA_DIR = str(BASE_DIR / "chroma_db")
COLLECTION_NAME = "personal_rag"
JST = timezone(timedelta(hours=9))


def get_calendar_service():
    creds = None
    if os.path.exists(TOKEN_FILE):
        creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
            except RefreshError:
                # リフレッシュトークン失効（「テスト中」のOAuthアプリは7日で切れる）。
                # ブラウザでの再認証にフォールバックする（手動実行時のみ有効。cronでは人がいないため失敗する）
                print("トークンが失効していたため、ブラウザで再認証します...")
                creds = None
        if not creds or not creds.valid:
            flow = InstalledAppFlow.from_client_secrets_file(CREDENTIALS_FILE, SCOPES)
            creds = flow.run_local_server(port=0)
        with open(TOKEN_FILE, "w") as f:
            f.write(creds.to_json())
    return build("calendar", "v3", credentials=creds)


def fetch_events(service, past_days: int, future_days: int) -> list[dict]:
    now = datetime.now(JST)
    time_min = (now - timedelta(days=past_days)).isoformat()
    time_max = (now + timedelta(days=future_days)).isoformat()

    # APIは1回最大250件しか返さないため、nextPageTokenがなくなるまで取り続ける
    events = []
    page_token = None
    while True:
        result = service.events().list(
            calendarId="primary",
            timeMin=time_min,
            timeMax=time_max,
            singleEvents=True,
            orderBy="startTime",
            pageToken=page_token,
        ).execute()
        events.extend(result.get("items", []))
        page_token = result.get("nextPageToken")
        if not page_token:
            return events


def events_to_documents(events: list[dict]) -> list[Document]:
    docs = []
    for event in events:
        start = event["start"].get("dateTime", event["start"].get("date", ""))
        end = event["end"].get("dateTime", event["end"].get("date", ""))
        summary = event.get("summary", "（タイトルなし）")
        location = event.get("location", "")
        description = event.get("description", "")

        try:
            dt = datetime.fromisoformat(start).astimezone(JST)
            date_str = dt.strftime("%Y年%m月%d日（%a）%H:%M")
        except Exception:
            date_str = start

        text = f"予定: {summary}\n日時: {date_str}\n"
        if location:
            text += f"場所: {location}\n"
        if description:
            text += f"メモ: {description}\n"

        # source=calendar は「全消し→再取込」の削除対象を絞り込むための目印
        docs.append(Document(text=text, doc_id=event["id"], metadata={"source": "calendar"}))
    return docs


def main(past_days: int, future_days: int):
    Settings.embed_model = OllamaEmbedding(
        model_name="nomic-embed-text",
        base_url="http://localhost:11434",
    )
    Settings.llm = None

    print("Googleカレンダーに接続中...")
    service = get_calendar_service()
    events = fetch_events(service, past_days, future_days)
    print(f"{len(events)} 件の予定を取得しました（過去{past_days}日〜未来{future_days}日）")

    if not events:
        # API不調で空が返った可能性もあるため、既存データの削除もせずに終了する
        # （全消し→再取込方式で「消しただけ」で終わる事故を防ぐ）
        print("予定が0件だったため、ChromaDBには手を付けずに終了します")
        return

    docs = events_to_documents(events)

    chroma_client = chromadb.PersistentClient(path=CHROMA_DIR)
    collection = chroma_client.get_or_create_collection(COLLECTION_NAME)

    # カレンダー由来の既存データだけを削除してから入れ直す。
    # Obsidianや画像など他ソースのデータは source が異なるので消えない。
    existing = collection.get(where={"source": "calendar"})
    if existing["ids"]:
        collection.delete(ids=existing["ids"])
        print(f"既存のカレンダーデータ {len(existing['ids'])} 件を削除しました")

    vector_store = ChromaVectorStore(chroma_collection=collection)
    storage_context = StorageContext.from_defaults(vector_store=vector_store)

    VectorStoreIndex.from_documents(docs, storage_context=storage_context)
    print(f"ChromaDBへの取り込み完了（{len(docs)} 件）")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--past-days", type=int, default=365, help="何日前まで取得するか（デフォルト365日）")
    parser.add_argument("--future-days", type=int, default=365, help="何日先まで取得するか（デフォルト365日）")
    args = parser.parse_args()
    main(args.past_days, args.future_days)
