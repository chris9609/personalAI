"""
Googleカレンダーから予定を取得してChromaDBに取り込むスクリプト
初回実行時にブラウザでGoogle認証が走る（token.jsonが生成されて以後は不要）

使い方:
  cd src
  python calendar_fetch.py          # 今日から7日分を取得してRAGに取り込む
  python calendar_fetch.py --days 30 # 期間を変更
"""
import argparse
import os
from datetime import datetime, timezone, timedelta

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
CREDENTIALS_FILE = "../secrets/credentials.json"
TOKEN_FILE = "../secrets/token.json"
CHROMA_DIR = "../chroma_db"
COLLECTION_NAME = "personal_rag"
JST = timezone(timedelta(hours=9))


def get_calendar_service():
    creds = None
    if os.path.exists(TOKEN_FILE):
        creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(CREDENTIALS_FILE, SCOPES)
            creds = flow.run_local_server(port=0)
        with open(TOKEN_FILE, "w") as f:
            f.write(creds.to_json())
    return build("calendar", "v3", credentials=creds)


def fetch_events(service, days: int) -> list[dict]:
    now = datetime.now(JST)
    time_min = now.isoformat()
    time_max = (now + timedelta(days=days)).isoformat()

    result = service.events().list(
        calendarId="primary",
        timeMin=time_min,
        timeMax=time_max,
        singleEvents=True,
        orderBy="startTime",
    ).execute()
    return result.get("items", [])


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

        docs.append(Document(text=text, doc_id=event["id"]))
    return docs


def main(days: int):
    Settings.embed_model = OllamaEmbedding(
        model_name="nomic-embed-text",
        base_url="http://localhost:11434",
    )
    Settings.llm = None

    print("Googleカレンダーに接続中...")
    service = get_calendar_service()
    events = fetch_events(service, days)
    print(f"{len(events)} 件の予定を取得しました")

    if not events:
        print("予定がありませんでした")
        return

    docs = events_to_documents(events)

    chroma_client = chromadb.PersistentClient(path=CHROMA_DIR)
    collection = chroma_client.get_or_create_collection(COLLECTION_NAME)
    vector_store = ChromaVectorStore(chroma_collection=collection)
    storage_context = StorageContext.from_defaults(vector_store=vector_store)

    VectorStoreIndex.from_documents(docs, storage_context=storage_context)
    print(f"ChromaDBへの取り込み完了（{days}日分）")

    print("\n--- 取り込んだ予定 ---")
    for doc in docs:
        print(doc.text)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=7, help="何日先まで取得するか（デフォルト7日）")
    args = parser.parse_args()
    main(args.days)
