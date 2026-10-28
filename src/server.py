"""
Open WebUI から使えるOpenAI互換APIサーバー（LangGraphエージェント版）
起動: uvicorn src.server:app --host 0.0.0.0 --port 8000

構成:
  Open WebUI → このサーバー → LangGraphエージェント(gemma)
                                ├─ note_search（LlamaIndex RAG: ノート・画像・カレンダー取り込みデータ）
                                ├─ claude_code_request（Mac側の受付役 batch/claude_bridge.py 経由でClaude CodeにPRを作らせる）
                                └─ calendar_* / anki_* / slack_* / github_*（mcpo経由でpersonal-mcpのツールを呼ぶ。起動時にopenapi.jsonから自動生成）
エージェントが「そのまま答える or 道具を使う」を判断し、道具の結果を踏まえて回答する。
会話履歴は毎回渡す（旧版は最後の1メッセージしか見ていなかった）。日時・天気は最後の質問の直前に差し込む（キャッシュを効かせるため）。
"""
import os
import json
import re
import uuid
import logging
from datetime import date, datetime
from pathlib import Path
from urllib.parse import quote

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
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import StructuredTool
from langchain_ollama import ChatOllama

# LangChain 1.x では create_agent、それ以前は langgraph 側の create_react_agent
try:
    from langchain.agents import create_agent
except ImportError:
    from langgraph.prebuilt import create_react_agent as create_agent

from src import transit, wake_time
from src.weather_fetch import get_location, get_weather

BASE_DIR = Path(__file__).resolve().parent.parent
CHROMA_DIR = str(BASE_DIR / "chroma_db")
SECRETS_DIR = BASE_DIR / "secrets"
COLLECTION_NAME = "personal_rag"
# Ollama公式の gemma4:12b はMLXエンジンで動き、読み込み(prefill)が約235tok/sと遅い。
# Google公式のGGUF版はllamacppで動き、約3倍速く、プロンプトのキャッシュも効く
LLM_MODEL = "hf.co/google/gemma-4-12B-it-qat-q4_0-gguf:latest"
OLLAMA_URL = os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434")
MCPO_URL = os.environ.get("MCPO_BASE_URL", "http://mcpo:8600")
MAX_HISTORY = 20  # gemmaのコンテキスト溢れ防止。直近20メッセージだけ渡す

# 天気情報のキャッシュ（1日1回だけ取得）
_weather_cache: dict = {"date": None, "info": None}

app = FastAPI()

# 起動時に一度だけ初期化（RAGの検索は埋め込みモデルだけあればよい）
Settings.embed_model = OllamaEmbedding(
    model_name="bge-m3",
    base_url=OLLAMA_URL,
)
chroma_client = chromadb.PersistentClient(path=CHROMA_DIR)
collection = chroma_client.get_or_create_collection(COLLECTION_NAME)
vector_store = ChromaVectorStore(chroma_collection=collection)
index = VectorStoreIndex.from_vector_store(vector_store)
retriever = index.as_retriever(similarity_top_k=4)

# temperature=0: 道具を使うか否かの判断がランダムにブレないようにする
# keep_alive: 既定の5分だと久々の質問でモデル読み込み(約4秒)からやり直しになるため長めに載せておく
# reasoning=False: 既定だとgemma4が画面に出ない「考え込み」を数百トークン書き、返事が数倍遅くなるため
# num_ctx: 既定4096だと「指示＋道具の説明(約2700)＋検索結果」で溢れ、先頭から切り捨てられて
#          質問ごと消える（ノート検索の答えが自己紹介になっていた原因）
llm = ChatOllama(model=LLM_MODEL, base_url=OLLAMA_URL, temperature=0, keep_alive="2h",
                 reasoning=False, num_ctx=16384)


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
    # 検索は「近い順の上位」を必ず返すので、無関係な文章が混ざる。
    # 関係なければ「見当たらない」と答えるよう、読み方を添えておく
    header = (
        f"【「{query}」でユーザーのノートを検索した結果（関連度順の上位{len(nodes)}件）】\n"
        "質問に関係する記述がなければ「ノートには見当たりませんでした」と答え、無関係な内容は紹介しないこと。\n"
    )
    return header + "\n\n---\n\n".join(node.get_content() for node in nodes)


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

def transit_search(destination: str, when: str = "", mode: str = "depart", config: RunnableConfig = None) -> str:
    """現在地（無ければ自宅）から指定駅までの電車の経路（乗換案内）を検索する"""
    # 現在地はLLMに座標を写させず、リクエストから抜いたものをconfig経由で受け取る（小型モデルは数字を崩すため）
    origin = ((config or {}).get("configurable") or {}).get("origin", "")
    return transit.search(destination, when, mode, origin)


TRANSIT_TOOL = StructuredTool.from_function(
    func=transit_search,
    name="transit_search",
    description=(
        "現在地（不明なら自宅）から目的の駅までの電車の経路（発着時刻・所要時間・乗換・運賃）を検索する。"
        "「難波行きたい」「明日9時に梅田に着きたい」などで使う。"
        "destinationは駅名（例: 難波）。"
        "whenは時刻で「YYYY-MM-DD HH:MM」形式、今すぐなら空文字。"
        "modeは出発時刻で探すなら'depart'、「◯時に着きたい」など到着時刻で探すなら'arrive'"
    ),
)

def map_link(place: str, mode: str = "search") -> str:
    """場所のGoogleマップURLを作る（iPhoneではタップでマップアプリが開く）"""
    if mode == "route":
        return f"https://www.google.com/maps/dir/?api=1&destination={quote(place)}"
    return f"https://www.google.com/maps/search/?api=1&query={quote(place)}"


MAP_LINK_TOOL = StructuredTool.from_function(
    func=map_link,
    name="map_link",
    description=(
        "場所のGoogleマップのリンクを作る。「なんばパークスの地図ちょうだい」などで使う。"
        "placeは施設名や住所（例: なんばパークス）。"
        "modeは地図を表示するだけなら'search'、現在地からの道順・ナビなら'route'"
    ),
)

# Claude Code への開発依頼は、Mac側の受付役(batch/claude_bridge.py)に投げるだけ。
# 作業は数分かかるので、ここでは待たずに「受け付けました」を返し、結果(PRのリンク)はSlackに届く
CLAUDE_BRIDGE_URL = os.environ.get("CLAUDE_BRIDGE_URL", "http://host.docker.internal:8611")
CLAUDE_BRIDGE_TOKEN_FILE = SECRETS_DIR / "claude_bridge_token"


def claude_code_request(repo: str, request: str) -> str:
    """Claude Codeに開発作業を依頼する（変更してGitHubにPRを出し、Slackに知らせる）"""
    try:
        token = CLAUDE_BRIDGE_TOKEN_FILE.read_text(encoding="utf-8").strip()
        res = httpx.post(
            f"{CLAUDE_BRIDGE_URL}/request",
            json={"repo": repo, "request": request},
            headers={"Authorization": f"Bearer {token}"},
            timeout=15,
        )
        return res.json()["message"]
    except Exception as e:
        logging.error(f"[claude_code_request] 受付役に届かない: {e}")
        return "Claude Codeの受付役に繋がりませんでした（Macで受付役が止まっている可能性）。再試行しないでください"


CLAUDE_CODE_TOOL = StructuredTool.from_function(
    func=claude_code_request,
    name="claude_code_request",
    description=(
        "Claude Codeに開発作業（コードの修正・機能追加など）を依頼する。"
        "Claude Codeが変更してGitHubにプルリクエストを出し、終わったらSlackの#秘書室にリンクが届く。"
        "「personalAIの〇〇を直して」「MCPに××を追加して」などで使う。"
        "repoはリポジトリ名（例: personalAI, MCP）。"
        "requestにはユーザーの依頼の言葉を要約や言い換えをせず、そのまま渡す"
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
    tools = [NOTE_SEARCH_TOOL, SET_WAKE_TOOL, GET_WAKE_TOOL, TRANSIT_TOOL, MAP_LINK_TOOL, CLAUDE_CODE_TOOL]
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
    """毎回同じ中身の指示だけを返す（日時・天気は build_context_message へ）。
    Ollamaは前回と先頭が同じ部分の読み込みを使い回すので、ここに毎分変わる値を入れると
    後ろに続く道具の説明(約2000トークン)まで毎回読み直しになる"""
    return (
        "あなたはユーザー専属のAI秘書です。日本語で簡潔に答えてください。\n"
        "「明日」「来週」などの相対的な日付は、質問の直前に渡す現在の日時を基準に計算してください。\n\n"
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
        "- 電車の経路・乗換案内（「難波行きたい」「明日9時に梅田に着きたい」など）には transit_search を使う。"
        "「◯時に着きたい」「◯時までに」は mode='arrive'、それ以外は mode='depart'。"
        "結果は候補ごとに発着時刻・所要時間・運賃・乗る路線を短くまとめて伝える\n"
        "- 地図・場所の表示（「なんばパークスの地図ちょうだい」など）には map_link を使う。"
        "道順・ナビを求められたら mode='route'。返ってきたURLは書き換えずにそのまま伝える\n"
        "- GitHubのリポジトリ一覧・最近のcommit（「最近なにやったっけ」「personalAIの最近のcommit見せて」など）には "
        "github_list_repos / github_list_recent_commits を使う。commit数の集計には github_count_commits を使う\n"
        "- コードの修正・機能追加など開発の依頼（「personalAIの〇〇を直して」など）には claude_code_request を使う。"
        "requestにはユーザーの言葉をそのまま渡す。作業は数分かかり、結果はSlackに届くと伝える。"
        "どのリポジトリか分からないときは、勝手に決めずユーザーに聞く\n"
        "- 重要: カレンダー・Anki・Slackを操作するときは、必ず該当する道具を実際に呼び出すこと。"
        "道具を呼ばずに「追加しました」「変更しました」「削除しました」と答えることは絶対に禁止\n"
        "- 削除・変更で候補が複数返ってきたら、勝手に選ばずユーザーに確認する\n"
        "- 道具の結果を踏まえて、最後は必ず自然な日本語で答える\n"
        "- 道具が不要な雑談や一般的な質問には、道具を使わずそのまま答える"
    )


# Open WebUIの位置情報（設定>インターフェース）をオンにし、システムプロンプトに
# 「現在地: {{USER_LOCATION}}」と書いておくと「34.669, 135.501 (lat, long)」の形で届く。
# オフ・取得失敗のときは「Unknown」等になり、マッチしないので自宅扱いになる。
_LOCATION_RE = re.compile(r"現在地[:：]\s*(-?\d+\.\d+)\s*,\s*(-?\d+\.\d+)")
# LLMには「現在地: …」の行ごと見せない（「Unknown」でも「現在地から」と言い出すため。座標はコードで渡す）
_LOCATION_LINE_RE = re.compile(r"^現在地[:：].*$\n?", re.MULTILINE)


def extract_origin(req: ChatRequest) -> str:
    """システムメッセージから現在地の "緯度,経度" を取り出す。無ければ空文字。"""
    for m in req.messages:
        if m.role == "system" and m.content:
            match = _LOCATION_RE.search(m.content)
            if match:
                return f"{match.group(1)},{match.group(2)}"
    return ""


def build_context_message() -> str:
    """毎回変わる情報（日時・天気）。キャッシュを壊さないよう、最後の質問の直前に置く"""
    now = datetime.now()
    today = f"{now.year}年{now.month}月{now.day}日（{_WEEKDAYS_JA[now.weekday()]}曜日）{now.strftime('%H:%M')}"
    weather_info = get_today_weather()
    weather_line = f"\n今日の天気: {weather_info}" if weather_info else ""
    return f"現在の日時: {today}{weather_line}"


def to_langchain_messages(req: ChatRequest) -> list:
    """Open WebUIからのOpenAI形式メッセージをLangChain形式に変換する。
    こちらのシステムプロンプトを先頭に置き、履歴は直近MAX_HISTORY件まで保持する。"""
    messages: list = [SystemMessage(content=build_system_prompt())]
    history = [m for m in req.messages if m.content]
    for m in history[-MAX_HISTORY:]:
        if m.role == "system":
            content = _LOCATION_LINE_RE.sub("", m.content).strip()
            if content:
                messages.append(SystemMessage(content=content))
        elif m.role == "assistant":
            messages.append(AIMessage(content=m.content))
        else:
            messages.append(HumanMessage(content=m.content))
    # 日時・天気は最後のユーザー発言の直前に差し込む（先頭側を毎回同じにしてキャッシュを効かせる）
    last_user = max((i for i, msg in enumerate(messages) if isinstance(msg, HumanMessage)), default=len(messages))
    messages.insert(last_user, SystemMessage(content=build_context_message()))
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
    origin = extract_origin(req)
    # 座標そのものはログに出さない（あり/なしだけ）
    logging.info(f"[req] 現在地: {'あり' if origin else 'なし（自宅扱い）'}")
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
                # recursion_limit: 道具の呼びすぎ暴走を防ぐ上限 / origin: transit_searchへ渡す現在地
                config={"recursion_limit": 10, "configurable": {"origin": origin}},
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
