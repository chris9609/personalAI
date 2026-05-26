"""
Open WebUI から使えるOpenAI互換APIサーバー
起動: uvicorn src.server:app --host 0.0.0.0 --port 8000
"""
import time
import uuid
from pathlib import Path

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

app = FastAPI()

# 起動時に一度だけ初期化
Settings.embed_model = OllamaEmbedding(
    model_name="nomic-embed-text",
    base_url="http://localhost:11434",
)
Settings.llm = Ollama(
    model=LLM_MODEL,
    base_url="http://localhost:11434",
    request_timeout=120.0,
)
chroma_client = chromadb.PersistentClient(path=CHROMA_DIR)
collection = chroma_client.get_or_create_collection(COLLECTION_NAME)
vector_store = ChromaVectorStore(chroma_collection=collection)
index = VectorStoreIndex.from_vector_store(vector_store)
query_engine = index.as_query_engine()


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
        "data": [{"id": LLM_MODEL, "object": "model"}],
    }


@app.post("/v1/chat/completions")
def chat_completions(req: ChatRequest):
    user_message = next(
        (m.content for m in reversed(req.messages) if m.role == "user"), ""
    )
    response = query_engine.query(user_message)
    answer = str(response)

    if req.stream:
        def generate():
            chunk_id = f"chatcmpl-{uuid.uuid4().hex}"
            yield (
                f"data: {{\"id\":\"{chunk_id}\",\"object\":\"chat.completion.chunk\","
                f"\"choices\":[{{\"delta\":{{\"content\":{answer!r}}},\"index\":0,\"finish_reason\":null}}]}}\n\n"
            )
            yield "data: [DONE]\n\n"
        return StreamingResponse(generate(), media_type="text/event-stream")

    return {
        "id": f"chatcmpl-{uuid.uuid4().hex}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": req.model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": answer},
                "finish_reason": "stop",
            }
        ],
    }
