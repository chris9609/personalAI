"""
Open WebUI から使えるOpenAI互換APIサーバー
起動: uvicorn src.server:app --host 0.0.0.0 --port 8000
"""
import os
import json
import time
import uuid
import logging
from datetime import date
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(message)s")

import httpx
import chromadb
from fastapi import FastAPI
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from llama_index.core import VectorStoreIndex, Settings
from llama_index.vector_stores.chroma import ChromaVectorStore
from llama_index.embeddings.ollama import OllamaEmbedding
from llama_index.llms.ollama import Ollama
from src.weather_fetch import get_location, get_weather

BASE_DIR = Path(__file__).resolve().parent.parent
CHROMA_DIR = str(BASE_DIR / "chroma_db")
SECRETS_DIR = BASE_DIR / "secrets"
COLLECTION_NAME = "personal_rag"
LLM_MODEL = "gemma4:e4b"
OLLAMA_URL = os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434")

# 天気情報のキャッシュ（1日1回だけ取得）
_weather_cache: dict = {"date": None, "info": None}

app = FastAPI()

# 起動時に一度だけ初期化
Settings.embed_model = OllamaEmbedding(
    model_name="nomic-embed-text",
    base_url=OLLAMA_URL,
)
Settings.llm = Ollama(
    model=LLM_MODEL,
    base_url=OLLAMA_URL,
    request_timeout=120.0,
)
chroma_client = chromadb.PersistentClient(path=CHROMA_DIR)
collection = chroma_client.get_or_create_collection(COLLECTION_NAME)
vector_store = ChromaVectorStore(chroma_collection=collection)
index = VectorStoreIndex.from_vector_store(vector_store)
retriever = index.as_retriever(similarity_top_k=4)


class Message(BaseModel):
    role: str
    content: str

class ChatRequest(BaseModel):
    model: str = LLM_MODEL
    messages: list[Message]
    stream: bool = False


@app.get("/v1/models")
def list_models():
    return {
        "object": "list",
        "data": [
            {
                "id": "personal-ai",
                "object": "model",
                "created": 1700000000,
                "owned_by": "ollama",
            }
        ],
    }


def _stream_ollama(prompt: str, req: ChatRequest, system: str | None = None):
    """Ollamaにストリーミングで投げてServer-Sent Eventsとして流す"""
    chunk_id = f"chatcmpl-{uuid.uuid4().hex}"
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})

    def generate():
        with httpx.Client(timeout=120) as client:
            with client.stream("POST", f"{OLLAMA_URL}/api/chat", json={
                "model": LLM_MODEL,
                "messages": messages,
                "stream": True,
            }) as response:
                for line in response.iter_lines():
                    if not line:
                        continue
                    data = json.loads(line)
                    content = data.get("message", {}).get("content", "")
                    if content:
                        chunk = {
                            "id": chunk_id,
                            "object": "chat.completion.chunk",
                            "choices": [{"delta": {"content": content}, "index": 0, "finish_reason": None}],
                        }
                        yield f"data: {json.dumps(chunk)}\n\n"
        yield "data: [DONE]\n\n"

    return StreamingResponse(generate(), media_type="text/event-stream")


def get_today_weather() -> str | None:
    """今日の天気を返す。未取得なら API を叩いてキャッシュする。"""
    try:
        today = str(date.today())
        if _weather_cache["date"] == today:
            return _weather_cache["info"]

        with open(SECRETS_DIR / "weather.json") as f:
            api_keys = json.load(f)

        lat, lon = get_location()
        weather = get_weather(lat, lon, api_keys["openweathermap"])
        city = weather.get("name", "")
        info = f"{city}の天気は{weather['weather'][0]['description']}、気温{weather['main']['temp']}℃"

        _weather_cache["date"] = today
        _weather_cache["info"] = info
        logging.info(f"[天気] 取得完了: {info}")
        return info
    except Exception as e:
        logging.error(f"[天気] 取得失敗: {e}")
        return None


@app.post("/v1/chat/completions")
def chat_completions(req: ChatRequest):
    user_message = next(
        (m.content for m in reversed(req.messages) if m.role == "user"), ""
    )

    # Open WebUI が自動送信するフォローアップ質問生成リクエストは無視する
    if user_message.startswith("### Task:"):
        logging.info("[SKIP] フォローアップ質問生成リクエストをスキップ")
        return StreamingResponse(iter(["data: [DONE]\n\n"]), media_type="text/event-stream")

    logging.info(f"[1] Open WebUI からリクエスト受信: 「{user_message[:100]}」")
    logging.info(f"[2] ChromaDB で関連情報を検索中...")

    nodes = retriever.retrieve(user_message)
    context = "\n\n".join([node.get_content() for node in nodes])

    weather_info = get_today_weather()
    weather_prefix = f"【今日の天気】{weather_info}\n\n" if weather_info else ""
    if weather_info:
        logging.info(f"[天気] プロンプトに追加: {weather_info}")

    if context.strip():
        logging.info(f"[3] 関連情報あり → RAGプロンプトで Ollama にストリーミング")
        prompt = f"{weather_prefix}参考情報:\n{context}\n\n質問: {user_message}"
    else:
        logging.info(f"[3] 関連情報なし → そのまま Ollama にストリーミング")
        prompt = f"{weather_prefix}{user_message}"

    logging.info(f"[4] Ollama にストリーミング開始...")
    logging.info(f"[prompt]\n{prompt}")
    return _stream_ollama(prompt, req)
