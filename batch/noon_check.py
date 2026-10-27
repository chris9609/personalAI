"""
午前チェック: 12:00に今日の時間割ノート（daily/YYYY-MM-DD.md）を読み、
午前（12:00より前に始まる行）のチェック状況を #秘書室 に厳しめに投稿する。

設計:
  - 数えるだけなのでLLMは使わない（daily_plan.py が行の形式を固定しているため、正規表現で足りる）。
  - カレンダー由来の行（補足が「予定」）は自分でチェックするタスクではないので数えない。
  - その日のノートが無いのは daily_plan.py が失敗したとき。そちらで #エラー に通知済みなので、
    ここではログに残して exit 1 するだけにする（同じ失敗を二重に通知しない）。

使い方:
  .venv/bin/python -m batch.noon_check            # 集計して投稿
  .venv/bin/python -m batch.noon_check --dry-run  # 集計して表示だけ
"""
import re
import sys
from datetime import datetime

from batch.daily_plan import DAILY_DIR
from batch.morning_briefing import JST, post_to_slack

# 例: - [x] 09:35〜09:57 Anki 英語（復習157枚・約22分）
TASK_LINE = re.compile(r"^- \[( |x|X)\] (\d{2}):(\d{2})〜\d{2}:\d{2} (.+)$")
NOON = 12


def read_morning_tasks(text: str) -> list[tuple[bool, str]]:
    tasks = []
    for line in text.splitlines():
        m = TASK_LINE.match(line.strip())
        if not m:
            continue
        done, hour, minute, title = m.group(1) != " ", int(m.group(2)), m.group(3), m.group(4)
        if hour >= NOON or title.endswith("予定）"):
            continue
        tasks.append((done, f"{hour:02d}:{minute} {title}"))
    return tasks


def build_message(tasks: list[tuple[bool, str]]) -> str:
    total = len(tasks)
    done = sum(1 for d, _ in tasks if d)
    left = [title for d, title in tasks if not d]
    if not left:
        return f"✅ *午前チェック*：{total}個すべて完了。この調子で午後も崩さないこと。"
    left_text = "\n".join(f"• {title}" for title in left)
    if done == 0:
        head = f"🚨 *午前チェック*：{total}個中 *0個*。午前中、1つも手をつけていません。"
    else:
        head = f"⚠️ *午前チェック*：{total}個中 *{done}個*。終わっていないものがあります。"
    return f"{head}\n\n{left_text}\n\nお昼のあと、最初にこれを片付けてください。後回しにしないこと。"


def main() -> int:
    dry_run = "--dry-run" in sys.argv
    now = datetime.now(JST)
    note_path = DAILY_DIR / f"{now:%Y-%m-%d}.md"
    if not note_path.exists():
        print(f"今日の時間割ノートがありません（daily_plan の失敗を確認）: {note_path}")
        return 1

    tasks = read_morning_tasks(note_path.read_text(encoding="utf-8"))
    if not tasks:
        print("午前のタスクが見つかりませんでした（ノートの形式を確認）")
        return 1

    message = build_message(tasks)
    if dry_run:
        print(message)
        return 0
    post_to_slack(message)
    print(f"午前チェックを投稿しました（{sum(1 for d, _ in tasks if d)}/{len(tasks)}）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
