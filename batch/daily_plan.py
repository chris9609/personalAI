"""
今日の時間割: vaultの「毎日やること」ノートと今日のカレンダー・Ankiの枚数から、
Claude Code（claude -p）に時間割を組ませ、vaultの daily/YYYY-MM-DD.md にチェックリストとして書き出す。
全文は #秘書室 に流さず、朝ブリーフィングにノートへのリンクだけ載せる（情報が多くなりすぎるため）。
ブリーフィング（8:00）の時点でノートができているよう、こちらは7:50に動かす。

設計:
  - 入力集め（ノート・カレンダー・Anki）と書き出し（vault）はPythonが行い、
    claude -p には「時間割を組む」ことだけをさせる（道具もMCPも渡さない）。
    launchd下だとMCPの起動やツール許可の確認が沈黙失敗になりやすいため。
  - やることの中身はvault側のノートにだけ書く（このリポジトリはpublicなので、ここには仕組みだけ置く）。
  - 朝はAnkiが起動していないのが普通なので、自分で起動して AnkiConnect が応答するまで待つ。
    それでも繋がらなければ枚数不明のまま組ませる（時間割自体は止めない）。
  - 時間割が作れなかった場合は #エラー に通知して exit 1 する。
  - その日のノートが既にあれば上書きしない（チェック済みの状態を消さないため）。

使い方:
  .venv/bin/python -m batch.daily_plan            # 作成してvaultに書き出す
  .venv/bin/python -m batch.daily_plan --dry-run  # 作成して表示だけ（書き出さない）
"""
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

import requests

from batch.morning_briefing import (
    ANKI_CONNECT_URL,
    JST,
    DAILY_DIR,
    WEEKDAYS_JA,
    fetch_today_events,
    format_events,
    post_to_slack,
)
from batch.obsidian_sync import VAULT_DIR

ROUTINE_NOTE = VAULT_DIR / "life" / "毎日やること.md"
ERROR_CHANNEL_ID = "C0BEQ1A52HX"  # #エラー
# launchdはPATHが細いので絶対パスで呼ぶ
CLAUDE_BIN = str(Path.home() / ".local" / "bin" / "claude")
CLAUDE_MODEL = "sonnet"
# 枚数を数えるデッキ（中身の数字はノート側に書く。ここは数える対象だけ）
# キーはノートの表と同じ名前、値は実際に数えるデッキ。
# 英語は単語の2デッキだけ数える（リスニング・文法・長文は他で勉強しているのでやらない）。
ENGLISH_WORD_DECKS = [
    "外国語::英語::単語::4000 Essential English Words",
    "外国語::英語::単語::TOEIC L＆R TEST 出る単特急　金のフレーズ",
]
ANKI_DECKS = {
    "外国語::英語": ENGLISH_WORD_DECKS,
    "SVL12000": ["SVL12000"],
    "AWS": ["AWS"],
    "クイズ": ["クイズ"],
}
ANKI_WAIT_SEC = 90

SYSTEM_PROMPT = """あなたはユーザーの1日の時間割を組む秘書です。
渡される「毎日やること」ノートのルールに従い、今日のカレンダーの予定とAnkiの枚数を踏まえて時間割を組んでください。

出力のルール（厳守）:
- 出力するのは時間割だけ。前置き・説明・コードブロックは書かない。
- 1行目は「> 」で始まる今日のひとこと（予定の多さや注意点を1文で）。
- 2行目以降は、すべて次の形式のチェックリスト行にする:
  - [ ] HH:MM〜HH:MM やること（補足）
- 時間順に並べる。カレンダーの予定もチェックリスト行として入れ、補足に「予定」と書く。
- Ankiはデッキごとに1行にし、補足に枚数と見積もり時間を書く（例: 復習74枚＋新規20枚・約25分）。
- ノートで「時間割に入れない」とされているものは入れない。
"""


def ensure_anki_running() -> bool:
    def alive() -> bool:
        try:
            requests.post(ANKI_CONNECT_URL, json={"action": "version", "version": 6}, timeout=3)
            return True
        except requests.RequestException:
            return False

    if alive():
        return True
    # -g: 前面に出さずに起動する
    subprocess.run(["open", "-g", "-a", "Anki"], check=False)
    deadline = time.time() + ANKI_WAIT_SEC
    while time.time() < deadline:
        time.sleep(3)
        if alive():
            return True
    return False


def fetch_anki_due_counts() -> str:
    if not ensure_anki_running():
        return "（Ankiに接続できず、今日の枚数は不明。ノートの平均的な枚数で見積もること）"

    lines = []
    for name, decks in ANKI_DECKS.items():
        deck_query = " OR ".join(f'"deck:{d}"' for d in decks)
        res = requests.post(
            ANKI_CONNECT_URL,
            json={"action": "findCards", "version": 6, "params": {"query": f"({deck_query}) is:due"}},
            timeout=10,
        ).json()
        if res.get("error"):
            raise RuntimeError(res["error"])
        lines.append(f"• {name}: 今日の期限 {len(res['result'])}枚")
    return "\n".join(lines)


def build_prompt(now: datetime, routine: str, events_text: str, anki_text: str) -> str:
    date_str = f"{now:%Y年%m月%d日}（{WEEKDAYS_JA[now.weekday()]}曜日）"
    return (
        f"今日は {date_str} です。\n\n"
        f"## 毎日やることノート\n{routine}\n\n"
        f"## 今日のカレンダーの予定\n{events_text}\n\n"
        f"## Ankiの今日の枚数\n{anki_text}\n"
    )


def ask_claude(prompt: str) -> str:
    # 作業ディレクトリをリポジトリの外にして、プロジェクトの設定やメモリを読み込ませない
    with tempfile.TemporaryDirectory() as cwd:
        result = subprocess.run(
            [
                CLAUDE_BIN, "-p",
                "--model", CLAUDE_MODEL,
                "--tools", "",
                "--strict-mcp-config",
                "--no-session-persistence",
                "--system-prompt", SYSTEM_PROMPT,
            ],
            input=prompt,
            capture_output=True,
            text=True,
            timeout=300,
            cwd=cwd,
        )
    if result.returncode != 0:
        raise RuntimeError(f"claude -p が失敗 (exit {result.returncode}): {result.stderr.strip()}")
    plan = result.stdout.strip()
    # チェックリストになっていない出力（エラー文や説明文）を書き出さないための最低限の確認
    if sum(1 for line in plan.splitlines() if line.startswith("- [ ] ")) < 5:
        raise RuntimeError(f"時間割の形式になっていません:\n{plan[:500]}")
    return plan


def main() -> int:
    dry_run = "--dry-run" in sys.argv
    now = datetime.now(JST)
    note_path = DAILY_DIR / f"{now:%Y-%m-%d}.md"
    if note_path.exists() and not dry_run:
        print(f"今日のノートは作成済みのためスキップします: {note_path}")
        return 0

    try:
        routine = ROUTINE_NOTE.read_text(encoding="utf-8")
        try:
            events_text = format_events(fetch_today_events())
        except Exception as e:
            # 予定が分からないと組めないので、空きと決めつけずに不明と伝える
            events_text = f"（カレンダーの取得に失敗したため不明: {e}）"
        anki_text = fetch_anki_due_counts()
        plan = ask_claude(build_prompt(now, routine, events_text, anki_text))
    except Exception as e:
        print(f"時間割の作成に失敗: {e!r}")
        if not dry_run:
            post_to_slack(f":rotating_light: 今日の時間割の作成に失敗しました: {e}", channel=ERROR_CHANNEL_ID)
        return 1

    # ひとこと（引用）とチェックリストが1つのブロックに見えないよう空行で区切る
    # （Claudeが空行を入れてくる日もあるので、一度詰めてから1行だけ空ける）
    lines = [line for line in plan.splitlines() if line.strip()]
    plan = "\n\n".join([lines[0], "\n".join(lines[1:])]) if lines[0].startswith(">") else "\n".join(lines)
    title = f"# {now.month}月{now.day}日（{WEEKDAYS_JA[now.weekday()]}）の時間割"
    if dry_run:
        print(f"{title}\n\n{plan}")
        return 0

    DAILY_DIR.mkdir(exist_ok=True)
    note_path.write_text(f"{title}\n\n{plan}\n", encoding="utf-8")
    print(f"時間割を書き出しました: {note_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
