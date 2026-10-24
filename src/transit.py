"""
乗換案内モジュール（NAVITIME API via RapidAPI）

流れ:
  1. 行き先の駅名 → 駅名検索API(transport_node)で候補を取り、自宅に一番近い駅を選ぶ
  2. 自宅の座標 → その駅の座標 で経路検索API(route_transit)
  3. 上位 MAX_ROUTES 件を短い日本語テキストにまとめて返す

行き先は駅IDではなく駅の「座標」で渡す。難波のように駅が固まっている場所でも、
どの駅で降りて歩くのが早いかをNAVITIME側が選んでくれるため。

RapidAPIのBasicプランは各API月500回（ハードリミット）なので回数を節約する:
  - 調べた駅は data/navitime_stations.json に保存し、2回目からはAPIを使わない
    （secrets/ はコンテナ内で読み取り専用のため data/ に置く）
  - 見つからなかった駅名も保存する
  - 失敗時は「再試行しないで」とエージェントに伝える文を返す

キーと自宅座標は secrets/navitime.json に {"rapidapi_key": ..., "home": "緯度,経度"}。
出発地は origin（iPhoneの現在地 "緯度,経度"）があればそれ、無ければ自宅。
自宅・現在地の座標がURLに入るので、リクエストURLはログに出さない（httpxのINFOログも止める）。
"""
import json
import logging
import math
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

# httpxはINFOでリクエストURL（＝自宅・現在地の座標入り）を出すので止める
logging.getLogger("httpx").setLevel(logging.WARNING)

JST = timezone(timedelta(hours=9))
BASE_DIR = Path(__file__).resolve().parent.parent
SECRETS_FILE = BASE_DIR / "secrets" / "navitime.json"
STATION_CACHE_FILE = BASE_DIR / "data" / "navitime_stations.json"

ROUTE_HOST = "navitime-route-totalnavi.p.rapidapi.com"
NODE_HOST = "navitime-transport.p.rapidapi.com"
MAX_ROUTES = 2       # スマホで読むので候補は2件まで
NODE_LIMIT = 10      # 駅名検索の取得件数（何件取っても1リクエスト）
TIMEOUT = 20

NO_RETRY = "同じ条件で道具を再度呼ばずに、この内容をユーザーに伝えてください。"


class TransitError(Exception):
    """ユーザーに伝えて終わりにすべき失敗"""


# ---------------------------------------------------------------------------
# 設定・キャッシュ
# ---------------------------------------------------------------------------

def _load_secrets() -> tuple[str, str]:
    try:
        conf = json.loads(SECRETS_FILE.read_text())
        return conf["rapidapi_key"], conf["home"].replace(" ", "")
    except (OSError, json.JSONDecodeError, KeyError) as e:
        raise TransitError(f"乗換案内の設定（secrets/navitime.json）が読めません: {type(e).__name__}")


def _load_cache() -> dict:
    if not STATION_CACHE_FILE.exists():
        return {}
    try:
        return json.loads(STATION_CACHE_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def _save_cache(cache: dict) -> None:
    STATION_CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATION_CACHE_FILE.write_text(json.dumps(cache, ensure_ascii=False, indent=2))


# ---------------------------------------------------------------------------
# API呼び出し
# ---------------------------------------------------------------------------

def _get(host: str, path: str, params: dict, key: str) -> dict:
    headers = {"X-RapidAPI-Key": key, "X-RapidAPI-Host": host}
    try:
        r = httpx.get(f"https://{host}/{path}", params=params, headers=headers, timeout=TIMEOUT)
    except httpx.HTTPError as e:
        raise TransitError(f"NAVITIMEに接続できませんでした（{type(e).__name__}）")
    if r.status_code == 429:
        raise TransitError("今月のNAVITIME APIの利用回数（月500回）を使い切りました")
    if r.status_code != 200:
        logging.error(f"[transit] {path} HTTP {r.status_code}: {r.text[:200]}")
        raise TransitError(f"NAVITIMEがエラーを返しました（HTTP {r.status_code}）")
    return r.json()


def _distance(a: str, b: dict) -> float:
    """座標間のおおよその距離（大小比較用。経度は緯度で縮める）"""
    lat1, lon1 = map(float, a.split(","))
    dlat = b["lat"] - lat1
    dlon = (b["lon"] - lon1) * math.cos(math.radians(lat1))
    return dlat * dlat + dlon * dlon


def find_station(word: str, key: str, home: str) -> dict:
    """駅名から {"name", "coord"} を返す。候補が複数あれば自宅に一番近い駅を選ぶ。"""
    word = word.strip().removesuffix("駅")
    cache = _load_cache()
    if word in cache:
        if cache[word] is None:
            raise TransitError(f"「{word}」という駅は見つかりませんでした")
        return cache[word]

    data = _get(NODE_HOST, "transport_node", {"word": word, "type": "station", "limit": NODE_LIMIT}, key)
    items = data.get("items", [])
    if not items:
        cache[word] = None
        _save_cache(cache)
        raise TransitError(f"「{word}」という駅は見つかりませんでした")

    nearest = min(items, key=lambda it: _distance(home, it["coord"]))
    station = {
        "name": nearest["name"],
        "coord": f"{nearest['coord']['lat']},{nearest['coord']['lon']}",
    }
    cache[word] = station
    _save_cache(cache)
    logging.info(f"[transit] 駅を登録: {word} → {station['name']}（候補{len(items)}件）")
    return station


# ---------------------------------------------------------------------------
# 経路検索
# ---------------------------------------------------------------------------

def _normalize_time(when: str) -> str:
    """'YYYY-MM-DD HH:MM' / 'HH:MM' / 空（=今）を NAVITIME の YYYY-MM-DDThh:mm:ss にする"""
    now = datetime.now(JST)
    when = (when or "").strip().replace("T", " ")
    if not when:
        return now.strftime("%Y-%m-%dT%H:%M:%S")
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%H:%M"):
        try:
            t = datetime.strptime(when, fmt)
        except ValueError:
            continue
        if fmt == "%H:%M":
            t = t.replace(year=now.year, month=now.month, day=now.day)
        return t.strftime("%Y-%m-%dT%H:%M:%S")
    raise TransitError(f"時刻「{when}」の形式が不正です（YYYY-MM-DD HH:MM で指定）")


def _hhmm(iso: str) -> str:
    return iso[11:16]


def _format_route(no: int, item: dict) -> str:
    move = item["summary"]["move"]
    fare = move.get("fare", {}).get("unit_0")
    fare_text = f"{int(fare)}円" if fare is not None else "運賃不明"
    lines = [
        f"【候補{no}】{_hhmm(move['from_time'])}発 → {_hhmm(move['to_time'])}着"
        f"（{move['time']}分・乗換{move['transit_count']}回・{fare_text}）"
    ]
    # sections は 地点→移動→地点→移動… の順に並ぶ。電車区間は前後の地点名で乗車・降車駅がわかる
    sections = item["sections"]
    for i, s in enumerate(sections):
        if s["type"] != "move":
            continue
        if s["move"] == "walk":
            lines.append(f"  徒歩{s['time']}分")
            continue
        board = sections[i - 1].get("name", "?")
        alight = sections[i + 1].get("name", "?")
        lines.append(f"  {s['line_name']}：{board} {_hhmm(s['from_time'])} → {alight} {_hhmm(s['to_time'])}")
    return "\n".join(lines)


def search(destination: str, when: str = "", mode: str = "depart", origin: str = "") -> str:
    """出発地から destination（駅名）までの経路を検索して、上位候補をテキストで返す。
    origin（"緯度,経度"）が空なら自宅から。
    mode='arrive' なら when は到着時刻、それ以外は出発時刻として扱う。"""
    origin = origin.replace(" ", "")
    start_label = "現在地" if origin else "自宅"
    try:
        key, home = _load_secrets()
        # 駅の候補選びは自宅基準のまま（キャッシュが駅名単位なので、出発地ごとに変えない）
        station = find_station(destination, key, home)
        time_key = "goal_time" if mode == "arrive" else "start_time"
        params = {"start": origin or home, "goal": station["coord"], time_key: _normalize_time(when)}
        data = _get(ROUTE_HOST, "route_transit", params, key)
    except TransitError as e:
        return f"{e}。{NO_RETRY}"

    items = data.get("items", [])
    if not items:
        return f"{start_label}から{station['name']}までの経路が見つかりませんでした。{NO_RETRY}"
    label = "到着" if mode == "arrive" else "出発"
    header = (
        f"出発地: {start_label}（{'iPhone等の位置情報' if origin else '現在地は取得できていないため自宅から'}）\n"
        f"{start_label} → {station['name']}（{params[time_key][5:16].replace('T', ' ')} {label}で検索）"
    )
    routes = [_format_route(i, it) for i, it in enumerate(items[:MAX_ROUTES], 1)]
    return "\n".join([header, *routes])
