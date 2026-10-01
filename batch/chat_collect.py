"""
Open WebUIの会話をLLMで要約してObsidian vaultに保存するスクリプト（脳構想第2弾）。
「昨日AIと話したこと」が翌朝にはRAGとauto_linkに編み込まれ、AI自身の記憶になる。

- ソース: open-webuiコンテナ内の webui.db（chatテーブル）。docker exec で
        読み取り専用クエリを実行してJSONで受け取る（DBファイルには直接触らない）
- 対象:  過去26時間に更新があったチャット（毎晩実行の24時間+余裕2時間）
- 要約:  gemmaに「記録する価値があるか」から判断させ、決めたこと・学んだこと・
        宿題だけを抽出（全文転記はしない）。雑談だけの会話はスキップ
- 保存:  vault の conversations/ に「YYYY-MM-DD タイトル.md」。会話に続きが
        生えたら（updated_atが進んだら）同じファイルを再要約で上書き
- 状態:  data/chat_collect_processed.json に {chat_id: {updated_at, file}} を記録
- Ollama必須のため自前で死活チェックし、落ちていたらスキップして正常終了する
  （personalAI.sh のOllamaガードより前段の memo_collect 直後に置くための設計）
"""
import json
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

import requests

BASE_DIR = Path(__file__).resolve().parent.parent
VAULT_DIR = Path.home() / "Library" / "Mobile Documents" / "iCloud~md~obsidian" / "Documents" / "Obsidian Vault"
CONV_DIR = VAULT_DIR / "conversations"
STATE_PATH = BASE_DIR / "data" / "chat_collect_processed.json"

OLLAMA_URL = "http://localhost:11434"
LLM_MODEL = "gemma4:e4b"
WEBUI_CONTAINER = "personalai-open-webui-1"
WINDOW_SEC = 26 * 60 * 60  # 毎晩実行の24時間+余裕2時間

# コンテナ内で実行する読み取り専用クエリ。会話の本体は chat JSON の
# history.messages（トップレベルのmessagesは表示中の枝しか持たないことがある）
_EXTRACT_SCRIPT = r"""
import sqlite3, json, sys
since = int(sys.argv[1])
con = sqlite3.connect("file:/app/backend/data/webui.db?mode=ro", uri=True)
out = []
rows = con.execute(
    "SELECT id, title, created_at, updated_at, chat FROM chat "
    "WHERE archived = 0 AND updated_at >= ? ORDER BY updated_at", (since,))
for cid, title, created, updated, chat_json in rows:
    history = json.loads(chat_json).get("history", {}).get("messages", {})
    messages = [
        {"role": m.get("role", ""), "content": (m.get("content") or "").strip()}
        for m in sorted(history.values(), key=lambda m: m.get("timestamp", 0))
        if (m.get("content") or "").strip()
    ]
    out.append({"id": cid, "title": title, "created_at": created,
                "updated_at": updated, "messages": messages})
print(json.dumps(out, ensure_ascii=False))
"""

SUMMARY_PROMPT = """あなたはユーザーの会話記録係です。以下のAIアシスタントとの会話を読み、
後から読み返す価値のある内容だけをJSONで抽出してください。

判断基準:
- 決定・方針・アイデア・学び・宿題が含まれる会話 → worth_recording: true
- 挨拶・動作テスト・単発の日付/天気/予定の確認だけの会話 → worth_recording: false

必ずこの形式のJSONだけを返すこと:
{"worth_recording": true/false,
 "summary": "会話の要点を1〜2文で",
 "decisions": ["決めたこと"],
 "learnings": ["学んだこと・わかったこと"],
 "todos": ["宿題・次にやること"]}
該当がない項目は空配列にする。会話に出てこないことを創作しない。

会話（タイトル: {title}）:
{conversation}
"""


def ollama_alive() -> bool:
    try:
        return requests.get(f"{OLLAMA_URL}/api/tags", timeout=5).ok
    except requests.RequestException:
        return False


def fetch_recent_chats(since: int) -> list[dict]:
    docker = shutil.which("docker") or "/usr/local/bin/docker"
    res = subprocess.run(
        [docker, "exec", WEBUI_CONTAINER, "python3", "-c", _EXTRACT_SCRIPT, str(since)],
        capture_output=True, text=True, timeout=60,
    )
    if res.returncode != 0:
        raise RuntimeError(f"webui.dbの読み取りに失敗: {res.stderr.strip()[:300]}")
    return json.loads(res.stdout)


def summarize(chat: dict) -> dict:
    conversation = "\n".join(
        f"{'ユーザー' if m['role'] == 'user' else 'AI'}: {m['content']}"
        for m in chat["messages"]
    )
    prompt = SUMMARY_PROMPT.replace("{title}", chat["title"]).replace(
        "{conversation}", conversation[:8000]  # 長すぎる会話はコンテキスト保護で切る
    )
    res = requests.post(
        f"{OLLAMA_URL}/api/chat",
        json={
            "model": LLM_MODEL,
            "messages": [{"role": "user", "content": prompt}],
            "format": "json",
            "stream": False,
            "options": {"temperature": 0},
        },
        timeout=300,
    )
    res.raise_for_status()
    return json.loads(res.json()["message"]["content"])


def _as_items(value) -> list[str]:
    # LLMは指示どおりの形で返すとは限らない（入れ子リスト・文字列単体など）ので平らな文字列リストに正規化
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return [item for v in value for item in _as_items(v)]
    text = str(value).strip()
    return [text] if text else []


def render_note(chat: dict, summary: dict) -> str:
    created = datetime.fromtimestamp(chat["created_at"])
    lines = [
        "---",
        f"chat_id: {chat['id']}",
        f"date: {created:%Y-%m-%d}",
        "source: open-webui",
        "---",
        "",
        f"# {chat['title']}",
        "",
        " ".join(_as_items(summary.get("summary"))),
    ]
    for key, heading in [("decisions", "📌 決めたこと"),
                         ("learnings", "💡 学んだこと"),
                         ("todos", "📝 宿題・次にやること")]:
        items = _as_items(summary.get(key))
        if items:
            lines += ["", f"## {heading}"]
            lines += [f"- {item}" for item in items]
    return "\n".join(lines) + "\n"


def note_filename(chat: dict) -> str:
    created = datetime.fromtimestamp(chat["created_at"])
    # Finder/Obsidianで問題になる文字を除去し、長すぎるタイトルは切る
    title = re.sub(r'[/\\:*?"<>|#^\[\]]', " ", chat["title"]).strip() or "無題"
    return f"{created:%Y-%m-%d} {title[:40]}.md"


def load_state() -> dict:
    if STATE_PATH.exists():
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    return {}


def main() -> int:
    if not ollama_alive():
        print("Ollamaが起動していないためスキップします（次回に持ち越し）")
        return 0
    if not VAULT_DIR.is_dir():
        print(f"vaultが見つかりません: {VAULT_DIR}")
        return 1

    state = load_state()
    chats = fetch_recent_chats(int(time.time()) - WINDOW_SEC)

    collected = skipped = unchanged = 0
    for chat in chats:
        prev = state.get(chat["id"])
        if prev and prev["updated_at"] >= chat["updated_at"]:
            unchanged += 1
            continue
        # ユーザー発言が2回未満の会話は要約するまでもなく除外
        user_turns = sum(1 for m in chat["messages"] if m["role"] == "user")
        if user_turns < 2:
            skipped += 1
            continue

        summary = summarize(chat)
        if not summary.get("worth_recording"):
            print(f"[記録価値なし] {chat['title'][:40]}")
            state[chat["id"]] = {"updated_at": chat["updated_at"], "file": None}
            skipped += 1
            continue

        # 再要約時は前回と同じファイルに上書き（タイトル変更でファイルが増えないように）
        filename = (prev or {}).get("file") or note_filename(chat)
        CONV_DIR.mkdir(parents=True, exist_ok=True)
        (CONV_DIR / filename).write_text(render_note(chat, summary), encoding="utf-8")
        print(f"[{'再要約' if prev else '記録'}] conversations/{filename}")
        state[chat["id"]] = {"updated_at": chat["updated_at"], "file": filename}
        collected += 1

    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"完了: {collected} 件記録 / {skipped} 件スキップ / {unchanged} 件変更なし")
    return 0


if __name__ == "__main__":
    sys.exit(main())
