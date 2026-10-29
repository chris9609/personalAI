"""
時間割チェック: 12:00・15:00・18:00に今日の時間割ノート（daily/YYYY-MM-DD.md）を読み、
その時刻より前に始まるタスクのチェック状況を #秘書室 に投稿する。

設計:
  - 数えるだけなのでLLMは使わない（daily_plan.py が行の形式を固定しているため、正規表現で足りる）。
  - カレンダー由来の行（補足が「予定」）は自分でチェックするタスクではないので数えない。
  - 12:00は厳しめに残りを全部突きつける。15:00・18:00は「残りを絞る」:
    終わっていないものから短い順に FOCUS_COUNT 個だけ選び、それ以外は今日は手放してよいと伝える。
    量が多すぎて手が止まるのを防ぐため、やることを小さくして見せる。
  - どのチェックかは実行時刻から決める（スリープで launchd が遅れて動いても、直前の枠として扱う）。
  - その日のノートが無いのは daily_plan.py が失敗したとき。そちらで #エラー に通知済みなので、
    ここではログに残して exit 1 するだけにする（同じ失敗を二重に通知しない）。

使い方:
  .venv/bin/python -m batch.noon_check                    # 集計して投稿
  .venv/bin/python -m batch.noon_check --dry-run          # 集計して表示だけ
  .venv/bin/python -m batch.noon_check --dry-run --at 15  # 15:00のチェックとして表示
"""
import re
import sys
from datetime import datetime

from batch.daily_plan import DAILY_DIR
from batch.morning_briefing import JST, post_to_slack

# 例: - [x] 09:35〜09:57 Anki 英語（復習157枚・約22分）
TASK_LINE = re.compile(r"^- \[( |x|X)\] (\d{2}):(\d{2})〜(\d{2}):(\d{2}) (.+)$")
CHECK_HOURS = (12, 15, 18)
FOCUS_COUNT = 2


def check_hour_for(now: datetime) -> int:
    passed = [h for h in CHECK_HOURS if now.hour >= h]
    return passed[-1] if passed else CHECK_HOURS[0]


def read_tasks(text: str, before_hour: int) -> list[tuple[bool, str, int]]:
    """(完了したか, 表示用の行, 所要分) を、before_hour より前に始まるタスクだけ返す。"""
    tasks = []
    for line in text.splitlines():
        m = TASK_LINE.match(line.strip())
        if not m:
            continue
        done, title = m.group(1) != " ", m.group(6)
        start_h, start_m, end_h, end_m = (int(m.group(i)) for i in range(2, 6))
        if start_h >= before_hour or title.endswith("予定）"):
            continue
        minutes = (end_h * 60 + end_m) - (start_h * 60 + start_m)
        tasks.append((done, f"{start_h:02d}:{start_m:02d} {title}", minutes))
    return tasks


def build_noon_message(tasks: list[tuple[bool, str, int]]) -> str:
    total = len(tasks)
    done = sum(1 for d, _, _ in tasks if d)
    left = [title for d, title, _ in tasks if not d]
    if not left:
        return f"✅ *午前チェック*：{total}個すべて完了。この調子で午後も崩さないこと。"
    left_text = "\n".join(f"• {title}" for title in left)
    if done == 0:
        head = f"🚨 *午前チェック*：{total}個中 *0個*。午前中、1つも手をつけていません。"
    else:
        head = f"⚠️ *午前チェック*：{total}個中 *{done}個*。終わっていないものがあります。"
    return f"{head}\n\n{left_text}\n\nお昼のあと、最初にこれを片付けてください。後回しにしないこと。"


def build_focus_message(tasks: list[tuple[bool, str, int]], hour: int) -> str:
    total = len(tasks)
    done = sum(1 for d, _, _ in tasks if d)
    left = [(title, minutes) for d, title, minutes in tasks if not d]
    if not left:
        return f"✅ *{hour}:00チェック*：ここまでの{total}個すべて完了。このまま続けましょう。"
    left.sort(key=lambda t: t[1])
    focus, rest = left[:FOCUS_COUNT], left[FOCUS_COUNT:]
    focus_text = "\n".join(f"• {title}" for title, _ in focus)
    focus_minutes = sum(minutes for _, minutes in focus)
    message = (
        f"📝 *{hour}:00チェック*：ここまで{total}個中 *{done}個* 完了。\n\n"
        f"今日はこれだけやりましょう（合わせて約{focus_minutes}分）:\n{focus_text}"
    )
    if rest:
        rest_text = "\n".join(f"• {title}" for title, _ in sorted(rest))
        message += f"\n\n残りの{len(rest)}個は今日は手放してOKです:\n{rest_text}"
    return message


def main() -> int:
    dry_run = "--dry-run" in sys.argv
    now = datetime.now(JST)
    hour = int(sys.argv[sys.argv.index("--at") + 1]) if "--at" in sys.argv else check_hour_for(now)
    note_path = DAILY_DIR / f"{now:%Y-%m-%d}.md"
    if not note_path.exists():
        print(f"今日の時間割ノートがありません（daily_plan の失敗を確認）: {note_path}")
        return 1

    tasks = read_tasks(note_path.read_text(encoding="utf-8"), hour)
    if not tasks:
        print(f"{hour}:00より前のタスクが見つかりませんでした（ノートの形式を確認）")
        return 1

    message = build_noon_message(tasks) if hour == CHECK_HOURS[0] else build_focus_message(tasks, hour)
    if dry_run:
        print(message)
        return 0
    post_to_slack(message)
    print(f"{hour}:00チェックを投稿しました（{sum(1 for d, _, _ in tasks if d)}/{len(tasks)}）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
