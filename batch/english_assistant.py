"""
英語学習アシスタント: #英語 チャンネルの見張り番

やること:
  1. #英語 への新規投稿に、翻訳・例文をスレッドで自動返信する（Ollamaローカル処理）
     - 単語1語なら「4000 Essential English Words に収録済みか」の判定も付ける
  2. 📌 リアクションが付いた単語を、4000 EEW と同一ノートタイプで
     「Wild Words」デッキにカード化する（英英定義・例文・IPA・音声3種を自動生成）
     - 音声は edge-tts（無料ニューラルTTS）。イラスト(Image)だけは自動で埋められないので空欄

設計:
  - cronで数分おきに起動するポーリング方式。多重起動はロックファイルで防ぐ
  - 返信済み位置は last_ts、カード化済みは carded として data/english_assistant_state.json に記録
  - Ankiが起動していない間は📌処理をスキップするだけ（次回起動時に自然に追いつく）
  - 失敗時は #エラー に通知するが、5分おきに走るので同じ通知は6時間に1回まで

使い方:
  .venv/bin/python -m batch.english_assistant
"""
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import traceback
from pathlib import Path

import httpx
import requests

BASE_DIR = Path(__file__).resolve().parent.parent
STATE_FILE = BASE_DIR / "data" / "english_assistant_state.json"
LOCK_FILE = BASE_DIR / "data" / "english_assistant.lock"
MCP_ENV_FILE = Path.home() / "claude" / "application" / "MCP" / ".env"

ENGLISH_CHANNEL_ID = "C0BF0RV759Q"  # #英語
ERROR_CHANNEL_ID = "C0BEQ1A52HX"  # #エラー
ERROR_NOTIFY_INTERVAL_SEC = 6 * 3600

OLLAMA_URL = "http://localhost:11434"
OLLAMA_MODEL = "gemma4:e4b"

ANKI_CONNECT_URL = "http://127.0.0.1:8765"
ANKI_MODEL_NAME = "4000 EEW"  # 4000 Essential の実物と同じノートタイプ
TARGET_DECK = "外国語::英語::単語::Wild Words"
ESSENTIAL_DECK = "外国語::英語::単語::4000 Essential English Words"

EDGE_TTS_BIN = str(BASE_DIR / ".venv" / "bin" / "edge-tts")
TTS_VOICE = "en-US-AriaNeural"

PIN_REACTION = "pushpin"  # 📌

REPLY_PROMPT_TEMPLATE = (
    "あなたは英語学習アシスタントです。以下の英語の投稿に対して日本語で解説してください。\n"
    "投稿が単語・熟語なら: 意味（品詞つき）、発音記号、例文を2つ（英文と日本語訳のセット）。\n"
    "投稿が文章なら: 自然な日本語訳、その後に重要表現があれば1〜2個だけ簡潔に解説。\n"
    "前置きや挨拶は不要。Slackに投稿するので、強調は *アスタリスク1つ* で囲む記法を使ってください。\n\n"
    "投稿: {text}"
)

CARD_PROMPT_TEMPLATE = (
    "You are creating an Anki card for the English word \"{word}\" "
    "in the exact style of the textbook \"4000 Essential English Words\".\n"
    "Return ONLY a JSON object with these keys:\n"
    '  "meaning": an English definition in the style '
    '"To <i>{word}</i> is to ..." or "A <i>{word}</i> is ..." (one sentence, simple words),\n'
    '  "example": one simple example sentence using the word, with the word wrapped in <b></b>,\n'
    '  "ipa": the IPA pronunciation of the word without slashes (e.g. əˈɡriː)\n'
)


# ---------- 共通ヘルパー ----------

def load_slack_token() -> str:
    # Slackトークンは personal-mcp の .env を共有（朝ブリーフィングと同じ方式）
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


def ollama_generate(prompt: str, json_format: bool = False) -> str:
    payload = {"model": OLLAMA_MODEL, "prompt": prompt, "stream": False}
    if json_format:
        payload["format"] = "json"
    resp = httpx.post(f"{OLLAMA_URL}/api/generate", json=payload, timeout=300)
    resp.raise_for_status()
    return resp.json()["response"]


def anki_request(action: str, **params):
    res = requests.post(
        ANKI_CONNECT_URL,
        json={"action": action, "version": 6, "params": params},
        timeout=30,
    ).json()
    if res.get("error"):
        raise RuntimeError(f"AnkiConnect {action} 失敗: {res['error']}")
    return res["result"]


def anki_is_up() -> bool:
    try:
        requests.get(ANKI_CONNECT_URL, timeout=3)
        return True
    except requests.RequestException:
        return False


def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    # 初回はチャンネルの過去ログを遡らない（今この瞬間から見張り開始）
    state = {"last_ts": str(time.time()), "carded": {}, "pin_skipped": [], "last_error_notify": 0}
    save_state(state)  # すぐ保存しないと毎回「今から」にリセットされてしまう
    return state


def save_state(state: dict):
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


# ---------- 投稿の分類 ----------

def extract_word(text: str) -> str | None:
    """投稿が「カード化できる単語・熟語（3語以内）」ならその正規化形を返す"""
    cleaned = re.sub(r"[^A-Za-z' \-]", " ", text).strip()
    words = cleaned.split()
    # 英字以外が混ざる投稿（日本語混じり等）や長い文はカード化対象外
    if not words or len(words) > 3 or re.search(r"[^\x00-\x7F]", text):
        return None
    # 元テキストがほぼ単語そのものであること（文の断片を誤爆させない）
    if len(text.strip()) > len(" ".join(words)) + 4:
        return None
    return " ".join(words).lower()


def is_in_deck(deck: str, word: str) -> bool:
    note_ids = anki_request("findNotes", query=f'deck:"{deck}" "Word:{word}"')
    return len(note_ids) > 0


# ---------- 1. 翻訳・例文のスレッド返信 ----------

def build_reply(text: str, anki_up: bool) -> str:
    reply = ollama_generate(REPLY_PROMPT_TEMPLATE.format(text=text)).strip()
    # モデルはMarkdownの **太字** で返しがちだが、Slackの太字は *一重* なので変換する
    reply = re.sub(r"\*\*(.+?)\*\*", r"*\1*", reply)
    word = extract_word(text)
    if word and anki_up:
        try:
            if is_in_deck(ESSENTIAL_DECK, word):
                reply += "\n\n✅ この単語は *4000 Essential English Words* に収録済みだよ"
            elif is_in_deck(TARGET_DECK, word):
                reply += "\n\n🃏 この単語は *Wild Words* にカード化済みだよ"
            else:
                reply += "\n\n🆕 *4000 Essential未収録* — 単語の投稿（スレッドの大元のメッセージ）に 📌 を付けると *Wild Words* デッキにカード化するよ"
        except RuntimeError:
            pass  # 判定はおまけなので、失敗しても返信自体は届ける
    return reply


def process_new_messages(token: str, state: dict, messages: list[dict], bot_user_id: str, anki_up: bool) -> int:
    replied = 0
    last_ts = float(state["last_ts"])
    for m in sorted(messages, key=lambda m: float(m["ts"])):
        if float(m["ts"]) <= last_ts:
            continue
        if m.get("type") != "message" or m.get("subtype") or m.get("bot_id"):
            continue
        if m.get("user") == bot_user_id:
            continue
        if m.get("thread_ts") and m["thread_ts"] != m["ts"]:
            continue  # スレッド内の返信には反応しない
        text = (m.get("text") or "").strip()
        if not text:
            continue

        reply = build_reply(text, anki_up)
        slack_api(token, "chat.postMessage", channel=ENGLISH_CHANNEL_ID, text=reply, thread_ts=m["ts"])
        print(f"返信: {text[:40]!r} への解説を投稿")
        replied += 1
        state["last_ts"] = m["ts"]
        save_state(state)  # 1件ごとに保存（途中で死んでも二重返信しない）
    return replied


# ---------- 2. 📌 → Wild Words カード化 ----------

def tts_to_media(text: str, filename: str) -> str:
    """edge-ttsでmp3を作り、Ankiのメディアフォルダに登録してファイル名を返す"""
    with tempfile.TemporaryDirectory() as tmp:
        mp3_path = Path(tmp) / filename
        subprocess.run(
            [EDGE_TTS_BIN, "--voice", TTS_VOICE, "--text", text, "--write-media", str(mp3_path)],
            check=True, capture_output=True, timeout=60,
        )
        anki_request("storeMediaFile", filename=filename, path=str(mp3_path))
    return filename


def generate_card_fields(word: str) -> dict:
    raw = ollama_generate(CARD_PROMPT_TEMPLATE.format(word=word), json_format=True)
    data = json.loads(raw)
    if not all(data.get(k) for k in ("meaning", "example", "ipa")):
        raise RuntimeError(f"カード生成の応答が不完全: {raw}")
    return data


def create_wild_words_card(word: str) -> dict:
    fields = generate_card_fields(word)
    plain_meaning = re.sub(r"<[^>]+>", "", fields["meaning"])
    plain_example = re.sub(r"<[^>]+>", "", fields["example"])

    slug = re.sub(r"[^a-z0-9]+", "_", word.lower()).strip("_")
    sound = tts_to_media(word, f"wildwords_{slug}.mp3")
    sound_meaning = tts_to_media(plain_meaning, f"wildwords_{slug}_meaning.mp3")
    sound_example = tts_to_media(plain_example, f"wildwords_{slug}_example.mp3")

    anki_request("createDeck", deck=TARGET_DECK)  # 既存なら何もしない
    anki_request("addNote", note={
        "deckName": TARGET_DECK,
        "modelName": ANKI_MODEL_NAME,
        "fields": {
            "Word": word,
            "Image": "",  # イラストだけは自動で埋められない（空欄 or あとで手貼り）
            "Sound": f"[sound:{sound}]",
            "Sound_Meaning": f"[sound:{sound_meaning}]",
            "Sound_Example": f"[sound:{sound_example}]",
            "Meaning": fields["meaning"],
            "Example": fields["example"],
            "IPA": fields["ipa"],
        },
        "tags": ["wild-words"],
    })
    return fields


def process_pins(token: str, state: dict, messages: list[dict], anki_up: bool) -> int:
    carded = 0
    for m in sorted(messages, key=lambda m: float(m["ts"])):
        if not any(r["name"] == PIN_REACTION for r in m.get("reactions", [])):
            continue
        if m["ts"] in state["carded"] or m["ts"] in state["pin_skipped"]:
            continue

        text = (m.get("text") or "").strip()
        word = extract_word(text)
        if word is None:
            # 文章への📌はどの単語か特定できないので、一度だけ案内して以後は無視
            slack_api(token, "chat.postMessage", channel=ENGLISH_CHANNEL_ID,
                      text="📌はカード化の合図だけど、この投稿からは単語を特定できなかったよ。"
                           "カード化したい単語だけを単独で投稿して📌を付けてね",
                      thread_ts=m["ts"])
            state["pin_skipped"].append(m["ts"])
            save_state(state)
            continue

        if not anki_up:
            print(f"📌検出（{word}）: Anki未起動のため次回に持ち越し")
            continue  # 記録しない＝次回リトライ

        if is_in_deck(TARGET_DECK, word):
            slack_api(token, "chat.postMessage", channel=ENGLISH_CHANNEL_ID,
                      text=f"🃏 *{word}* はすでに *Wild Words* にあるよ", thread_ts=m["ts"])
            state["carded"][m["ts"]] = word
            save_state(state)
            continue

        fields = create_wild_words_card(word)
        slack_api(token, "chat.postMessage", channel=ENGLISH_CHANNEL_ID,
                  text=(f"🃏 *Wild Words* に追加したよ！\n"
                        f"*{word}* /{fields['ipa']}/\n"
                        f"Meaning: {re.sub(r'<[^>]+>', '', fields['meaning'])}\n"
                        f"Example: {re.sub(r'<[^>]+>', '', fields['example'])}"),
                  thread_ts=m["ts"])
        print(f"カード化: {word}")
        state["carded"][m["ts"]] = word
        save_state(state)
        carded += 1
    return carded


# ---------- エラー通知（6時間に1回まで） ----------

def notify_error_rate_limited(state: dict, message: str):
    now = time.time()
    if now - state.get("last_error_notify", 0) < ERROR_NOTIFY_INTERVAL_SEC:
        return
    try:
        token = load_slack_token()
        slack_api(token, "chat.postMessage", channel=ERROR_CHANNEL_ID,
                  text=f":rotating_light: 英語アシスタント失敗: {message[:300]}\n（同種の通知は6時間に1回だけ送るよ）")
        state["last_error_notify"] = now
        save_state(state)
    except Exception:
        print("!!! エラー通知の送信にも失敗しました")


# ---------- main ----------

def acquire_lock() -> bool:
    if LOCK_FILE.exists():
        try:
            os.kill(int(LOCK_FILE.read_text().strip()), 0)
            return False  # 前回の実行がまだ生きている
        except (ProcessLookupError, ValueError, PermissionError):
            pass  # 死んだプロセスのロックなので奪う
    LOCK_FILE.write_text(str(os.getpid()))
    return True


def main() -> int:
    if not acquire_lock():
        print("前回の実行が動作中のためスキップ")
        return 0
    state = load_state()
    try:
        token = load_slack_token()
        bot_user_id = slack_api(token, "auth.test")["user_id"]
        history = slack_api(token, "conversations.history", channel=ENGLISH_CHANNEL_ID, limit=100)
        messages = history.get("messages", [])
        anki_up = anki_is_up()

        replied = process_new_messages(token, state, messages, bot_user_id, anki_up)
        carded = process_pins(token, state, messages, anki_up)
        print(f"完了: 返信 {replied} 件 / カード化 {carded} 件（Anki {'起動中' if anki_up else '未起動'}）")
        return 0
    except Exception as e:
        traceback.print_exc()
        notify_error_rate_limited(state, str(e))
        return 1
    finally:
        LOCK_FILE.unlink(missing_ok=True)


if __name__ == "__main__":
    sys.exit(main())
