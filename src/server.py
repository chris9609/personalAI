"""
Open WebUI から使えるOpenAI互換APIサーバー（LangGraphエージェント版）
起動: uvicorn src.server:app --host 0.0.0.0 --port 8000

構成:
  Open WebUI → このサーバー → LangGraphエージェント(gemma)
                                ├─ note_search（LlamaIndex RAG: ノート・画像・カレンダー取り込みデータ）
                                └─ calendar_* / anki_* / slack_*（mcpo経由でpersonal-mcpのツールを呼ぶ。起動時にopenapi.jsonから自動生成）
エージェントが「そのまま答える or 道具を使う」を判断し、道具の結果を踏まえて回答する。
会話履歴と今日の日付はシステムプロンプトで毎回渡す（旧版は最後の1メッセージしか見ていなかった）。
"""
import os
import json
import uuid
import logging
from datetime import date, datetime
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(message)s")

import httpx
import chromadb
from fastapi import FastAPI
from fastapi.responses import StreamingResponse
from typing import Annotated

from pydantic import BaseModel, BeforeValidator, Field, create_model
from llama_index.core import VectorStoreIndex, Settings
from llama_index.vector_stores.chroma import ChromaVectorStore
from llama_index.embeddings.ollama import OllamaEmbedding
from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import StructuredTool
from langchain_ollama import ChatOllama

# LangChain 1.x では create_agent、それ以前は langgraph 側の create_react_agent
try:
    from langchain.agents import create_agent
except ImportError:
    from langgraph.prebuilt import create_react_agent as create_agent

from src import wake_time
from src.weather_fetch import get_location, get_weather

BASE_DIR = Path(__file__).resolve().parent.parent
CHROMA_DIR = str(BASE_DIR / "chroma_db")
SECRETS_DIR = BASE_DIR / "secrets"
COLLECTION_NAME = "personal_rag"
LLM_MODEL = "gemma4:e4b"
OLLAMA_URL = os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434")
MCPO_URL = os.environ.get("MCPO_BASE_URL", "http://mcpo:8600")
MAX_HISTORY = 20  # gemmaのコンテキスト溢れ防止。直近20メッセージだけ渡す

# 天気情報のキャッシュ（1日1回だけ取得）
_weather_cache: dict = {"date": None, "info": None}

app = FastAPI()

# 起動時に一度だけ初期化（RAGの検索は埋め込みモデルだけあればよい）
Settings.embed_model = OllamaEmbedding(
    model_name="nomic-embed-text",
    base_url=OLLAMA_URL,
)
chroma_client = chromadb.PersistentClient(path=CHROMA_DIR)
collection = chroma_client.get_or_create_collection(COLLECTION_NAME)
vector_store = ChromaVectorStore(chroma_collection=collection)
index = VectorStoreIndex.from_vector_store(vector_store)
retriever = index.as_retriever(similarity_top_k=4)

# temperature=0: 道具を使うか否かの判断がランダムにブレないようにする
llm = ChatOllama(model=LLM_MODEL, base_url=OLLAMA_URL, temperature=0)


class Message(BaseModel):
    role: str
    content: str

class ChatRequest(BaseModel):
    model: str = LLM_MODEL
    messages: list[Message]
    stream: bool = False


# ---------------------------------------------------------------------------
# エージェントの道具
# ---------------------------------------------------------------------------

def note_search(query: str) -> str:
    """ユーザーの個人データを検索する"""
    nodes = retriever.retrieve(query)
    if not nodes:
        return "関連する情報は見つかりませんでした。"
    return "\n\n".join(node.get_content() for node in nodes)


NOTE_SEARCH_TOOL = StructuredTool.from_function(
    func=note_search,
    name="note_search",
    description=(
        "ユーザーの個人データ（Obsidianのノート・保存した画像の内容・カレンダーの予定の記録）から"
        "関連情報を検索する。ユーザー自身のメモ・記録・過去の予定に関する質問で使う"
    ),
)

def set_wake_time(date: str, time: str) -> str:
    """指定日の起床時刻（目覚まし）を手動設定・解除する"""
    try:
        return wake_time.set_override(date, time)
    except ValueError:
        return "形式が不正です。date=YYYY-MM-DD、time=HH:MM（解除は'auto'）で指定してください"


def get_wake_time(date: str) -> str:
    """指定日の起床時刻（目覚まし）を確認する"""
    try:
        target = datetime.strptime(date, "%Y-%m-%d").date()
    except ValueError:
        return "形式が不正です。date=YYYY-MM-DDで指定してください"
    info = wake_time.compute_wake_time(target)
    return f"{info['date']} の起床時刻は {info['wake_time']} です（理由: {info['reason']}）"


SET_WAKE_TOOL = StructuredTool.from_function(
    func=set_wake_time,
    name="set_wake_time",
    description=(
        "目覚まし（起床時刻）を手動で設定する。「明日は7時に起きる」「明日7時に起こして」などで使う。"
        "dateはYYYY-MM-DD形式、timeはHH:MM形式。"
        "「やっぱりカレンダー通りでいい」と言われたらtimeに'auto'を渡すと解除できる"
    ),
)

GET_WAKE_TOOL = StructuredTool.from_function(
    func=get_wake_time,
    name="get_wake_time",
    description=(
        "目覚まし（起床時刻）が何時にセットされる予定かを確認する。"
        "「明日何時に起きる？」「明日の目覚まし何時？」などで使う。dateはYYYY-MM-DD形式"
    ),
)

_JSON_TYPES = {"string": str, "number": float, "integer": int, "boolean": bool}

# mcpoが公開していても、スマホのgemmaには持たせない道具。
# 取り返しがつかない操作は、確認しながら進められるClaude Code側でだけ使う
AGENT_TOOL_DENYLIST = {"anki_delete_notes"}


def _split_if_str(v):
    """gemmaは配列を "test,english" のような文字列で渡しがちなので、リストに直して受け取る"""
    if isinstance(v, str):
        return [s.strip() for s in v.replace("、", ",").split(",") if s.strip()]
    return v


def _to_py_type(prop: dict):
    """JSON Schemaの型をPythonの型に変換する。配列は中身の型まで見る（例: タグ=list[str]）"""
    if prop.get("type") == "array":
        item_type = _JSON_TYPES.get(prop.get("items", {}).get("type"), str)
        return Annotated[list[item_type], BeforeValidator(_split_if_str)]
    return _JSON_TYPES.get(prop.get("type"), str)


def fetch_mcpo_tools() -> list[StructuredTool]:
    """mcpoのopenapi.jsonを読んで、公開中のツールをLangChainの道具として自動生成する。
    mcpo側のTOOLS設定を変えてもこのコードは修正不要。"""
    spec = httpx.get(f"{MCPO_URL}/openapi.json", timeout=10).json()
    schemas = spec.get("components", {}).get("schemas", {})
    tools: list[StructuredTool] = []
    for path, methods in spec.get("paths", {}).items():
        post = methods.get("post")
        if not post:
            continue
        name = path.lstrip("/")
        if name in AGENT_TOOL_DENYLIST:
            continue
        description = (post.get("description") or post.get("summary") or name).strip()

        body = (
            post.get("requestBody", {})
            .get("content", {})
            .get("application/json", {})
            .get("schema", {})
        )
        if "$ref" in body:
            body = schemas.get(body["$ref"].split("/")[-1], {})
        required = set(body.get("required", []))
        fields = {}
        for prop_name, prop in body.get("properties", {}).items():
            py_type = _to_py_type(prop)
            desc = prop.get("description", "")
            if prop_name in required:
                fields[prop_name] = (py_type, Field(description=desc))
            else:
                fields[prop_name] = (py_type | None, Field(default=None, description=desc))
        args_schema = create_model(f"{name}_args", **fields)

        def make_caller(tool_name: str):
            def call(**kwargs) -> str:
                payload = {k: v for k, v in kwargs.items() if v is not None}
                try:
                    res = httpx.post(f"{MCPO_URL}/{tool_name}", json=payload, timeout=30)
                    res.raise_for_status()
                    result = res.json()
                    return result if isinstance(result, str) else json.dumps(result, ensure_ascii=False)
                except Exception as e:
                    # エージェントが失敗を認識してユーザーに伝えられるよう、例外は文字列で返す
                    return f"ツール実行エラー: {e}"
            return call

        tools.append(
            StructuredTool.from_function(
                func=make_caller(name),
                name=name,
                description=description,
                args_schema=args_schema,
            )
        )
    return tools


# エージェントは初回リクエスト時に組み立ててキャッシュする。
# （compose起動直後はmcpoが立ち上がり中のことがあるため、起動時ではなく遅延生成にする）
_agent_cache: dict = {"agent": None}


def get_agent():
    if _agent_cache["agent"] is not None:
        return _agent_cache["agent"]
    tools = [NOTE_SEARCH_TOOL, SET_WAKE_TOOL, GET_WAKE_TOOL]
    try:
        mcpo_tools = fetch_mcpo_tools()
        tools += mcpo_tools
        logging.info(f"[agent] mcpoツール取得: {[t.name for t in mcpo_tools]}")
        _agent_cache["agent"] = create_agent(llm, tools)
    except Exception as e:
        # mcpoに届かない間はノート検索だけで動く（キャッシュせず次のリクエストで再試行）
        logging.error(f"[agent] mcpoツール取得失敗（note_searchのみで応答）: {e}")
        return create_agent(llm, tools)
    return _agent_cache["agent"]


# ---------------------------------------------------------------------------
# プロンプト組み立て
# ---------------------------------------------------------------------------

_WEEKDAYS_JA = "月火水木金土日"


def build_system_prompt() -> str:
    now = datetime.now()
    today = f"{now.year}年{now.month}月{now.day}日（{_WEEKDAYS_JA[now.weekday()]}曜日）{now.strftime('%H:%M')}"
    weather_info = get_today_weather()
    weather_line = f"今日の天気: {weather_info}\n" if weather_info else ""
    return (
        "あなたはユーザー専属のAI秘書です。日本語で簡潔に答えてください。\n\n"
        f"現在の日時: {today}\n"
        "「明日」「来週」などの相対的な日付は、この日時を基準に計算してください。\n"
        f"{weather_line}\n"
        "道具の使い方:\n"
        "- ユーザーのノートやメモ・過去の記録に関する質問には note_search を使う\n"
        "- カレンダーの予定の追加・確認・変更・削除には calendar_* の道具を使う\n"
        "- Ankiの学習状況（今日何枚やったか・残り枚数）の確認には anki_get_review_stats、"
        "カードの検索には anki_find_notes、カードの追加には anki_add_card を使う。"
        "追加先のデッキ名がわからないときは、先に anki_list_decks で実在するデッキ名を確認する\n"
        "- 「◯時にリマインドして」「◯時に知らせて」には slack_schedule_message を使う"
        "（チャンネル指定がなければ #秘書室、post_atは「YYYY-MM-DD HH:MM」の日本時間）。"
        "カレンダーに予定を入れるのとは別物なので、リマインドの依頼で calendar_add_event は使わない\n"
        "- Slackへの投稿は slack_send_message、チャンネルのメッセージを読むのは slack_read_messages を使う\n"
        "- 目覚まし・起床時刻の設定（「明日は7時に起きる」など）には set_wake_time を使う。"
        "何時に起きるかの確認には get_wake_time を使う\n"
        "- 重要: カレンダー・Anki・Slackを操作するときは、必ず該当する道具を実際に呼び出すこと。"
        "道具を呼ばずに「追加しました」「変更しました」「削除しました」と答えることは絶対に禁止\n"
        "- 削除・変更で候補が複数返ってきたら、勝手に選ばずユーザーに確認する\n"
        "- 道具の結果を踏まえて、最後は必ず自然な日本語で答える\n"
        "- 道具が不要な雑談や一般的な質問には、道具を使わずそのまま答える"
    )


def to_langchain_messages(req: ChatRequest) -> list:
    """Open WebUIからのOpenAI形式メッセージをLangChain形式に変換する。
    こちらのシステムプロンプトを先頭に置き、履歴は直近MAX_HISTORY件まで保持する。"""
    messages: list = [SystemMessage(content=build_system_prompt())]
    history = [m for m in req.messages if m.content]
    for m in history[-MAX_HISTORY:]:
        if m.role == "system":
            messages.append(SystemMessage(content=m.content))
        elif m.role == "assistant":
            messages.append(AIMessage(content=m.content))
        else:
            messages.append(HumanMessage(content=m.content))
    return messages


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


# ---------------------------------------------------------------------------
# OpenAI互換エンドポイント
# ---------------------------------------------------------------------------

@app.get("/wake_time")
def wake_time_endpoint(date: str | None = None):
    """iPhoneショートカットが毎晩叩く起床時刻API。
    dateを省略すると、正午以降なら「明日」・午前中なら「今日」を対象にする。"""
    if date:
        target = datetime.strptime(date, "%Y-%m-%d").date()
    else:
        target = wake_time.default_target_date()
    info = wake_time.compute_wake_time(target)
    logging.info(f"[wake_time] {info}")
    return info


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


@app.post("/v1/chat/completions")
def chat_completions(req: ChatRequest):
    user_message = next(
        (m.content for m in reversed(req.messages) if m.role == "user"), ""
    )

    # Open WebUI が自動送信するフォローアップ質問生成リクエストは無視する
    if user_message.startswith("### Task:"):
        logging.info("[SKIP] フォローアップ質問生成リクエストをスキップ")
        return StreamingResponse(iter(["data: [DONE]\n\n"]), media_type="text/event-stream")

    logging.info(f"[req] 「{user_message[:100]}」（履歴 {len(req.messages)} 件）")
    agent = get_agent()
    lc_messages = to_langchain_messages(req)
    chunk_id = f"chatcmpl-{uuid.uuid4().hex}"

    def sse(content: str) -> str:
        chunk = {
            "id": chunk_id,
            "object": "chat.completion.chunk",
            "choices": [{"delta": {"content": content}, "index": 0, "finish_reason": None}],
        }
        return f"data: {json.dumps(chunk)}\n\n"

    def generate():
        try:
            # stream_mode="messages": エージェント内のLLMトークンとツール結果が逐次流れてくる
            for chunk, _meta in agent.stream(
                {"messages": lc_messages},
                config={"recursion_limit": 10},  # 道具の呼びすぎ暴走を防ぐ上限
                stream_mode="messages",
            ):
                if isinstance(chunk, AIMessageChunk):
                    for tc in chunk.tool_call_chunks:
                        if tc.get("name"):
                            logging.info(f"[tool] 呼び出し: {tc['name']} {tc.get('args') or ''}")
                    if isinstance(chunk.content, str) and chunk.content:
                        yield sse(chunk.content)
                elif isinstance(chunk, ToolMessage):
                    logging.info(f"[tool] {chunk.name} 結果: {str(chunk.content)[:200]}")
        except Exception as e:
            logging.exception("[agent] 実行エラー")
            yield sse(f"（エラーが発生しました: {e}）")
        yield "data: [DONE]\n\n"

    return StreamingResponse(generate(), media_type="text/event-stream")
