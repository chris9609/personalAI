"""
起床時刻の計算モジュール（iPhoneショートカットの目覚まし連携用）

決定ルール（優先順）:
  1. 手動オーバーライド（「明日は7時に起きる」とAI秘書に言った分）
  2. その日の最初の予定（終日予定は除く）の PREP_MINUTES 分前
  3. 予定がなければ DEFAULT_WAKE
  ※ 計算結果が DEFAULT_WAKE より遅くなることはない（昼から予定でも8:30起床）

オーバーライドは data/wake_override.json に {"YYYY-MM-DD": "HH:MM"} 形式で保存。
コンテナ内の secrets/ は読み取り専用のため、token.json のリフレッシュは
メモリ上だけで行い書き戻さない（永続化は夜間バッチのcalendar_fetchが担う）。
"""
import json
import logging
from datetime import datetime, time, timedelta, timezone
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

JST = timezone(timedelta(hours=9))
BASE_DIR = Path(__file__).resolve().parent.parent
TOKEN_FILE = BASE_DIR / "secrets" / "token.json"
OVERRIDE_FILE = BASE_DIR / "data" / "wake_override.json"

SCOPES = ["https://www.googleapis.com/auth/calendar.readonly"]
PREP_MINUTES = 60          # 最初の予定の何分前に起きるか
DEFAULT_WAKE = time(8, 30)  # 予定がない日の起床時刻（＝これより遅くはしない上限）


# ---------------------------------------------------------------------------
# オーバーライドの読み書き
# ---------------------------------------------------------------------------

def _load_overrides() -> dict[str, str]:
    if not OVERRIDE_FILE.exists():
        return {}
    try:
        return json.loads(OVERRIDE_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def _save_overrides(overrides: dict[str, str]) -> None:
    # 過去の日付は掃除してから保存（ファイルが無限に育たないように）
    today = datetime.now(JST).date().isoformat()
    overrides = {d: t for d, t in overrides.items() if d >= today}
    OVERRIDE_FILE.parent.mkdir(parents=True, exist_ok=True)
    OVERRIDE_FILE.write_text(json.dumps(overrides, ensure_ascii=False, indent=2))


def set_override(date_str: str, time_str: str) -> str:
    """指定日の起床時刻を手動設定する。time_strに'auto'を渡すと解除してカレンダー基準に戻す。"""
    datetime.strptime(date_str, "%Y-%m-%d")  # 形式チェック（不正ならValueError）
    overrides = _load_overrides()
    if time_str.strip().lower() == "auto":
        overrides.pop(date_str, None)
        _save_overrides(overrides)
        return f"{date_str} の起床時刻の手動設定を解除しました（カレンダーの予定から自動計算に戻ります）"
    datetime.strptime(time_str, "%H:%M")
    overrides[date_str] = time_str
    _save_overrides(overrides)
    return f"{date_str} の起床時刻を {time_str} に設定しました"


# ---------------------------------------------------------------------------
# カレンダー照会
# ---------------------------------------------------------------------------

def _get_calendar_service():
    creds = Credentials.from_authorized_user_file(str(TOKEN_FILE), SCOPES)
    if creds.expired and creds.refresh_token:
        # secrets/ は読み取り専用マウントなので書き戻さず、メモリ上でだけ更新する
        creds.refresh(Request())
    return build("calendar", "v3", credentials=creds)


def _first_event(target_date: datetime.date) -> dict | None:
    """その日の最初の「時刻付き」予定を返す。終日予定は起床時刻に影響させない。"""
    day_start = datetime.combine(target_date, time(0, 0), tzinfo=JST)
    day_end = day_start + timedelta(days=1)
    service = _get_calendar_service()
    result = service.events().list(
        calendarId="primary",
        timeMin=day_start.isoformat(),
        timeMax=day_end.isoformat(),
        singleEvents=True,
        orderBy="startTime",
    ).execute()
    for event in result.get("items", []):
        start = event.get("start", {})
        if "dateTime" not in start:  # 終日予定はスキップ
            continue
        start_dt = datetime.fromisoformat(start["dateTime"]).astimezone(JST)
        if start_dt.date() != target_date:  # 前日から跨ぐ予定など
            continue
        return {"summary": event.get("summary", "（無題）"), "start": start_dt}
    return None


# ---------------------------------------------------------------------------
# 起床時刻の決定
# ---------------------------------------------------------------------------

def compute_wake_time(target_date: datetime.date) -> dict:
    """起床時刻を決定して {date, wake_time, hour, minute, reason} を返す。
    カレンダーに届かないときもデフォルトで返す（アラームが無設定になる事故を防ぐ）。"""
    date_str = target_date.isoformat()

    override = _load_overrides().get(date_str)
    if override:
        wake = datetime.strptime(override, "%H:%M").time()
        reason = f"手動設定（{override}）"
    else:
        try:
            event = _first_event(target_date)
        except Exception as e:
            logging.error(f"[wake_time] カレンダー取得失敗: {e}")
            event = None
            reason = "カレンダー取得に失敗したためデフォルト"
            wake = DEFAULT_WAKE
        else:
            if event is None:
                wake = DEFAULT_WAKE
                reason = "予定なし（デフォルト）"
            else:
                wake_dt = event["start"] - timedelta(minutes=PREP_MINUTES)
                wake = min(wake_dt.time(), DEFAULT_WAKE)  # デフォルトより遅くはしない
                reason = (
                    f"最初の予定「{event['summary']}」({event['start'].strftime('%H:%M')}開始)の"
                    f"{PREP_MINUTES}分前"
                    + ("" if wake_dt.time() <= DEFAULT_WAKE else f" → 上限{DEFAULT_WAKE.strftime('%H:%M')}に丸め")
                )

    return {
        "date": date_str,
        "wake_time": wake.strftime("%H:%M"),
        # iPhoneショートカットの「アラームを作成」が時刻として確実に解釈できるISO形式
        "wake_datetime": datetime.combine(target_date, wake, tzinfo=JST).isoformat(),
        "hour": wake.hour,
        "minute": wake.minute,
        "reason": reason,
    }


def default_target_date() -> datetime.date:
    """ショートカットが夜(正午以降)に叩いたら「明日」、午前中なら「今日」を対象にする。"""
    now = datetime.now(JST)
    if now.hour >= 12:
        return (now + timedelta(days=1)).date()
    return now.date()
