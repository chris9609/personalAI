"""
Slackメモ回収: #メモ チャンネルの投稿をObsidian vaultに取り込む

やること:
  1. #メモ への投稿（クリスの通常メッセージ）を取得する
  2. 1件ごとに vault の inbox/ へ .md として保存する
  3. 保存できたメッセージに ✅ リアクションを付ける（回収済みの印）

設計:
  - 「✅が付いていない投稿だけ処理する」リアクション方式。状態ファイルを持たず、
    Slack側を見れば回収済みかどうかが人間にも一目でわかる
  - 保存先は iCloud の vault 直下 inbox/。Obsidianアプリですぐ見える上に、
    後続の obsidian_sync → ingest が同じ晩にRAGへ取り込む
  - 夜間バッチ（personalAI.sh）から1日1回実行。失敗は exit 1 で
    バッチ側の #エラー 通知に任せる

使い方:
  .venv/bin/python -m batch.memo_collect
"""
import re
import sys
from datetime import datetime
from pathlib import Path

import requests

MCP_ENV_FILE = Path.home() / "claude" / "application" / "MCP" / ".env"
VAULT_DIR = Path.home() / "Library" / "Mobile Documents" / "iCloud~md~obsidian" / "Documents" / "Obsidian Vault"
INBOX_DIR = VAULT_DIR / "inbox"

MEMO_CHANNEL_ID = "C0BEJQ6CME1"  # #メモ
DONE_REACTION = "white_check_mark"  # ✅

FILENAME_MAX_CHARS = 25


def load_slack_token() -> str:
    # Slackトークンは personal-mcp の .env を共有（english_assistant と同じ方式）
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


def is_collected(message: dict, bot_user_id: str) -> bool:
    return any(
        r["name"] == DONE_REACTION and bot_user_id in r.get("users", [])
        for r in message.get("reactions", [])
    )


def clean_slack_text(text: str) -> str:
    """Slack記法を素のテキストに直す（<url|ラベル> → ラベル (url) など）"""
    text = re.sub(r"<(https?://[^>|]+)\|([^>]+)>", r"\2 (\1)", text)
    text = re.sub(r"<(https?://[^>]+)>", r"\1", text)
    return text.replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")


def memo_filename(text: str, posted_at: datetime) -> str:
    """先頭行から安全なファイル名を作る（例: 2026-07-07 0930 引っ越しの段取り.md）"""
    first_line = text.strip().splitlines()[0]
    slug = re.sub(r'[\\/:*?"<>|#^\[\]]', " ", first_line)
    slug = re.sub(r"\s+", " ", slug).strip()[:FILENAME_MAX_CHARS].strip() or "メモ"
    return f"{posted_at:%Y-%m-%d %H%M} {slug}.md"


def save_memo(text: str, posted_at: datetime) -> Path:
    path = INBOX_DIR / memo_filename(text, posted_at)
    # 同じ分に複数投稿しても上書きしない
    counter = 2
    while path.exists():
        path = path.with_name(f"{path.stem} ({counter}){path.suffix}")
        counter += 1
    body = f"{text.strip()}\n\n---\n出典: Slack #メモ（{posted_at:%Y-%m-%d %H:%M}）\n"
    path.write_text(body, encoding="utf-8")
    return path


def main() -> int:
    if not VAULT_DIR.is_dir():
        print(f"vaultが見つかりません: {VAULT_DIR}")
        return 1

    token = load_slack_token()
    bot_user_id = slack_api(token, "auth.test")["user_id"]
    history = slack_api(token, "conversations.history", channel=MEMO_CHANNEL_ID, limit=100)

    collected = 0
    for m in sorted(history.get("messages", []), key=lambda m: float(m["ts"])):
        # 通常の投稿のみ対象（参加ログなどのsubtype・bot投稿・スレッド返信は除外）
        if m.get("type") != "message" or m.get("subtype") or m.get("bot_id"):
            continue
        if m.get("thread_ts") and m["thread_ts"] != m["ts"]:
            continue
        if is_collected(m, bot_user_id):
            continue
        text = clean_slack_text((m.get("text") or "").strip())
        if not text:
            continue

        INBOX_DIR.mkdir(parents=True, exist_ok=True)
        path = save_memo(text, datetime.fromtimestamp(float(m["ts"])))
        print(f"[回収] {path.relative_to(VAULT_DIR)}")
        # ✅を付けて回収済みにする。ここで失敗したら次回同じメモを二重保存して
        # しまうので、リアクション失敗はエラーとして止める（already_reactedは除く）
        try:
            slack_api(token, "reactions.add", channel=MEMO_CHANNEL_ID, timestamp=m["ts"], name=DONE_REACTION)
        except RuntimeError as e:
            if "already_reacted" not in str(e):
                raise
        collected += 1

    print(f"完了: {collected} 件回収")
    return 0


if __name__ == "__main__":
    sys.exit(main())
