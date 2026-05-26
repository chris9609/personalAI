"""
Open WebUI から使えるOpenAI互換APIサーバー
起動: uvicorn src.server:app --host 0.0.0.0 --port 8000
"""
import os
import json
import time
import uuid
import logging
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

BASE_DIR = Path(__file__).resolve().parent.parent
CHROMA_DIR = str(BASE_DIR / "chroma_db")
COLLECTION_NAME = "personal_rag"
LLM_MODEL = "gemma4:e4b"
OLLAMA_URL = os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434")

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
retriever = index.as_retriever()


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
                "id": LLM_MODEL,
                "object": "model",
                "created": 1700000000,
                "owned_by": "ollama",
            }
        ],
    }


def _stream_ollama(prompt: str, req: ChatRequest):
    """Ollamaにストリーミングで投げてServer-Sent Eventsとして流す"""
    chunk_id = f"chatcmpl-{uuid.uuid4().hex}"

    def generate():
        with httpx.Client(timeout=120) as client:
            with client.stream("POST", f"{OLLAMA_URL}/api/chat", json={
                "model": LLM_MODEL,
                "messages": [{"role": "user", "content": prompt}],
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

    if context.strip():
        logging.info(f"[3] 関連情報あり → RAGプロンプトで Ollama にストリーミング")
        prompt = f"以下の情報を参考に質問に答えてください。\n\n{context}\n\n質問: {user_message}"
    else:
        logging.info(f"[3] 関連情報なし → そのまま Ollama にストリーミング")
        prompt = user_message

    logging.info(f"[4] Ollama にストリーミング開始...")
    return _stream_ollama(prompt, req)
