"""
時間割チェック: 12:00・15:00・18:00に今日の時間割ノート（daily/YYYY-MM-DD.md）を読み、
その時刻より前に始まるタスクのチェック状況を #秘書室 に投稿する。

設計:
  - 数えるだけなのでLLMは使わない（daily_plan.py が行の形式を固定しているため、正規表現で足りる）。
  - カレンダー由来の行（補足が「予定」）は自分でチェックするタスクではないので数えない。
  - Ankiの行は自分でチェックしない。AnkiWebと同期してから今日の残り枚数（新規＋学習中＋復習）を見て、
    0枚なら完了とし、ノートのチェックもその結果で書き換える（iPhoneで解いた分も同期で拾う）。
    Ankiに繋がらないときだけ、ノートのチェックで数える。
  - 12:00は厳しめに残りを全部突きつける。15:00・18:00は「残りを絞る」:
    終わっていないものから短い順に FOCUS_COUNT 個だけ選び、それ以外は今日は手放してよいと伝える。
    量が多すぎて手が止まるのを防ぐため、やることを小さくして見せる。
  - どのチェックかは実行時刻から決める（スリープで launchd が遅れて動いても、直前の枠として扱う）。
  - その日のノートが無いのは daily_plan.py が失敗したとき。そちらで #エラー に通知済みなので、
    ここではログに残して exit 1 するだけにする（同じ失敗を二重に通知しない）。

使い方:
  .venv/bin/python -m batch.noon_check                    # 集計して投稿
  .venv/bin/python -m batch.noon_check --dry-run          # 集計して表示だけ（ノートも書き換えない）
  .venv/bin/python -m batch.noon_check --dry-run --at 15  # 15:00のチェックとして表示
"""
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime

import requests

from batch.daily_plan import DAILY_DIR, ensure_anki_running
from batch.morning_briefing import ANKI_CONNECT_URL, JST, post_to_slack

# 例: - [x] 09:35〜09:57 Anki 英語（復習157枚・約22分）
TASK_LINE = re.compile(r"^- \[( |x|X)\] (\d{2}):(\d{2})〜(\d{2}):(\d{2}) (.+)$")
CHECK_HOURS = (12, 15, 18)
FOCUS_COUNT = 2
# 時間割の「Anki ◯◯」の行 → 残り枚数を見るデッキ。
# 英語は単語の2デッキだけ見る（リスニング・文法・長文は他で勉強しているので数えない）。
# SAPの新規は親「AWS」の上限0に隠れて親の枚数に出ないので、SAPデッキも見る。
ANKI_ROW_DECKS = {
    "英語": [
        "外国語::英語::単語::4000 Essential English Words",
        "外国語::英語::単語::TOEIC L＆R TEST 出る単特急　金のフレーズ",
    ],
    "SVL12000": ["SVL12000"],
    "AWS": ["AWS", "AWS::professional::Solutions Architect"],
    "クイズ": ["クイズ"],
}
ANKI_SYNC_WAIT_SEC = 60


@dataclass
class Task:
    line_no: int
    done: bool
    label: str
    minutes: int
    anki_left: int | None = None


def check_hour_for(now: datetime) -> int:
    passed = [h for h in CHECK_HOURS if now.hour >= h]
    return passed[-1] if passed else CHECK_HOURS[0]


def anki_row_name(label: str) -> str | None:
    m = re.match(r"\d{2}:\d{2} Anki (\S+?)(（|$)", label)
    return m.group(1) if m and m.group(1) in ANKI_ROW_DECKS else None


def read_tasks(lines: list[str], before_hour: int) -> list[Task]:
    """before_hour より前に始まるタスクだけ返す。"""
    tasks = []
    for i, line in enumerate(lines):
        m = TASK_LINE.match(line.strip())
        if not m:
            continue
        done, title = m.group(1) != " ", m.group(6)
        start_h, start_m, end_h, end_m = (int(m.group(j)) for j in range(2, 6))
        if start_h >= before_hour or title.endswith("予定）"):
            continue
        minutes = (end_h * 60 + end_m) - (start_h * 60 + start_m)
        tasks.append(Task(i, done, f"{start_h:02d}:{start_m:02d} {title}", minutes))
    return tasks


def anki(action: str, **params):
    res = requests.post(
        ANKI_CONNECT_URL, json={"action": action, "version": 6, "params": params}, timeout=60
    ).json()
    if res.get("error"):
        raise RuntimeError(res["error"])
    return res["result"]


def fetch_anki_left() -> dict[str, int] | None:
    """「Anki ◯◯」の行ごとに今日の残り枚数を返す。Ankiに繋がらなければ None。"""
    if not ensure_anki_running():
        return None
    try:
        anki("sync")
    except (requests.RequestException, RuntimeError) as e:
        # 同期できなくてもMac側の枚数で判断はできるので、ログだけ残して続ける
        print(f"AnkiWebとの同期に失敗（Mac側の枚数で判断）: {e}")
    # 同期中・直後はコレクションの読み直し中で、エラーや null が返るので、返ってくるまで待つ
    decks = sorted({d for ds in ANKI_ROW_DECKS.values() for d in ds})
    deadline = time.time() + ANKI_SYNC_WAIT_SEC
    while True:
        try:
            stats = anki("getDeckStats", decks=decks)
        except (requests.RequestException, RuntimeError):
            stats = None
        if stats:
            break
        if time.time() > deadline:
            return None
        time.sleep(3)
    left_by_deck = {
        s["name"]: s["new_count"] + s["learn_count"] + s["review_count"] for s in stats.values()
    }
    return {row: sum(left_by_deck.get(d, 0) for d in ds) for row, ds in ANKI_ROW_DECKS.items()}


def apply_anki(tasks: list[Task], lines: list[str], anki_left: dict[str, int]) -> bool:
    """Ankiの行の完了をAnkiの残り枚数で決め直し、ノートのチェックも書き換える。変わったら True。"""
    changed = False
    for task in tasks:
        row = anki_row_name(task.label)
        if row is None:
            continue
        task.anki_left = anki_left[row]
        task.done = task.anki_left == 0
        new_line = re.sub(r"^(\s*- \[)[ xX](\])", rf"\g<1>{'x' if task.done else ' '}\g<2>", lines[task.line_no])
        if new_line != lines[task.line_no]:
            lines[task.line_no] = new_line
            changed = True
    return changed


def left_label(task: Task) -> str:
    return task.label if task.anki_left is None else f"{task.label} → *残り{task.anki_left}枚*"


def build_noon_message(tasks: list[Task]) -> str:
    total = len(tasks)
    done = sum(1 for t in tasks if t.done)
    left = [t for t in tasks if not t.done]
    if not left:
        return f"✅ *午前チェック*：{total}個すべて完了。この調子で午後も崩さないこと。"
    left_text = "\n".join(f"• {left_label(t)}" for t in left)
    if done == 0:
        head = f"🚨 *午前チェック*：{total}個中 *0個*。午前中、1つも手をつけていません。"
    else:
        head = f"⚠️ *午前チェック*：{total}個中 *{done}個*。終わっていないものがあります。"
    return f"{head}\n\n{left_text}\n\nお昼のあと、最初にこれを片付けてください。後回しにしないこと。"


def build_focus_message(tasks: list[Task], hour: int) -> str:
    total = len(tasks)
    done = sum(1 for t in tasks if t.done)
    left = [t for t in tasks if not t.done]
    if not left:
        return f"✅ *{hour}:00チェック*：ここまでの{total}個すべて完了。このまま続けましょう。"
    left.sort(key=lambda t: t.minutes)
    focus, rest = left[:FOCUS_COUNT], left[FOCUS_COUNT:]
    focus_text = "\n".join(f"• {left_label(t)}" for t in focus)
    focus_minutes = sum(t.minutes for t in focus)
    message = (
        f"📝 *{hour}:00チェック*：ここまで{total}個中 *{done}個* 完了。\n\n"
        f"今日はこれだけやりましょう（合わせて約{focus_minutes}分）:\n{focus_text}"
    )
    if rest:
        rest_text = "\n".join(f"• {left_label(t)}" for t in sorted(rest, key=lambda t: t.label))
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

    lines = note_path.read_text(encoding="utf-8").split("\n")
    tasks = read_tasks(lines, hour)
    if not tasks:
        print(f"{hour}:00より前のタスクが見つかりませんでした（ノートの形式を確認）")
        return 1

    anki_left = fetch_anki_left()
    if anki_left is not None and apply_anki(tasks, lines, anki_left) and not dry_run:
        note_path.write_text("\n".join(lines), encoding="utf-8")

    message = build_noon_message(tasks) if hour == CHECK_HOURS[0] else build_focus_message(tasks, hour)
    if anki_left is None:
        message += "\n\n（Ankiに接続できなかったので、Ankiの行はノートのチェックで数えています）"
    if dry_run:
        print(message)
        return 0
    post_to_slack(message)
    print(f"{hour}:00チェックを投稿しました（{sum(1 for t in tasks if t.done)}/{len(tasks)}）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
