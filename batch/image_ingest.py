"""
data/screenshots/HEIC/ 直下の画像群をOllamaのビジョンAPIで内容ごとに自動グルーピングし、
トピックごとに data/screenshots/MD/<topic>.md として統合保存するスクリプト。
その後 ingest.py を実行すれば ChromaDB に取り込まれる。

使い方:
  HEIC/ に画像をまとめて置くだけでOK。
  例: テレビと冷蔵庫の説明書の写真を一緒に入れると、
      MD/tv_manual.md と MD/fridge_manual.md のように自動で分割生成される。
"""
import base64
import io
import json
import re
import httpx
from pathlib import Path
from PIL import Image
import pillow_heif

pillow_heif.register_heif_opener()

BASE_DIR = Path(__file__).resolve().parent.parent
SCREENSHOTS_DIR = BASE_DIR / "data" / "screenshots" / "HEIC"
OUTPUT_DIR = BASE_DIR / "data" / "screenshots" / "MD"
PROCESSED_MANIFEST = OUTPUT_DIR / ".processed.json"
OLLAMA_URL = "http://localhost:11434"
MODEL = "gemma4:e4b"
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".heic", ".heif"}

TRANSCRIBE_PROMPT = (
    "これは取扱説明書などの文書の1ページの画像です。"
    "このページに書かれているテキスト・数字・型番・連絡先・日付・表の内容を、"
    "省略や要約をせず、できるだけ忠実にすべて日本語で書き出してください。"
    "読み取れる情報のみを出力し、推測や補足は加えないでください。"
    "見出し・箇条書き・表など元の構造を保ったMarkdown形式で出力してください。"
)

DESCRIBE_PROMPT = (
    "この画像が何についての文書・写真かを、内容を1文で簡潔に説明してください。"
    "（例:「テレビの取扱説明書の操作パネルの説明」「冷蔵庫の型番が記載されたラベル」「賃貸借契約書の1ページ目」）"
    "説明文のみを出力してください。"
)

GROUPING_PROMPT_TEMPLATE = (
    "以下は複数の画像の内容説明です。同じトピック"
    "（同じ製品の説明書、同じ契約書など）に属する画像番号をグループ化してください。\n\n"
    "{descriptions}\n\n"
    "出力は次のJSON形式のみとし、説明文は付けないでください。\n"
    '{{"トピック名1": [画像番号, ...], "トピック名2": [...]}}\n'
    "トピック名は内容を簡潔に表す短い名前にしてください（日本語可。例: tv_manual, 賃貸契約書）。"
)


def load_image_as_png_b64(image_path: Path) -> str:
    img = Image.open(image_path).convert("RGB")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode()


def generate(prompt: str, images_b64: list[str] | None = None, json_format: bool = False) -> str:
    payload = {"model": MODEL, "prompt": prompt, "stream": False}
    if images_b64:
        payload["images"] = images_b64
    if json_format:
        payload["format"] = "json"
    resp = httpx.post(f"{OLLAMA_URL}/api/generate", json=payload, timeout=180)
    resp.raise_for_status()
    return resp.json()["response"]


def images_to_text(image_paths: list[Path]) -> str:
    # 連続した別ページなので、1枚ずつ忠実に文字起こししてファイル名順に連結する
    # （全画像を1回の呼び出しに渡すとコンテキスト溢れ・切り詰めで内容が欠落するため）
    ordered = sorted(image_paths)
    parts = []
    for i, p in enumerate(ordered, start=1):
        text = generate(TRANSCRIBE_PROMPT, images_b64=[load_image_as_png_b64(p)]).strip()
        parts.append(f"## ページ{i}（{p.name}）\n\n{text}")
        print(f"    ページ {i}/{len(ordered)} 文字起こし完了: {p.name}")
    return "\n\n".join(parts)


def describe_image(image_path: Path) -> str:
    img_b64 = load_image_as_png_b64(image_path)
    return generate(DESCRIBE_PROMPT, images_b64=[img_b64]).strip()


def load_processed() -> dict[str, str]:
    # 処理済み画像の記録（ファイル名 → 取り込み先トピック）。毎晩の全枚数LLM再分析を防ぐ
    if PROCESSED_MANIFEST.exists():
        return json.loads(PROCESSED_MANIFEST.read_text(encoding="utf-8"))
    return {}


def save_processed(processed: dict[str, str]) -> None:
    PROCESSED_MANIFEST.write_text(
        json.dumps(processed, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def sanitize_topic(name: str) -> str:
    # ファイル名として使えない文字・空白だけをアンダースコアに置換し、日本語はそのまま活かす
    name = re.sub(r'[\\/:*?"<>|\s]+', "_", name.strip()).strip("_")
    return name or "untitled"


def group_images(images: list[Path]) -> dict[str, list[Path]]:
    print(f"{len(images)} 枚の内容を分析中...")
    descriptions = []
    for i, p in enumerate(images):
        desc = describe_image(p)
        print(f"  [{i}] {p.name}: {desc}")
        descriptions.append(f"{i}: {desc}")

    raw = generate(GROUPING_PROMPT_TEMPLATE.format(descriptions="\n".join(descriptions)), json_format=True)
    try:
        grouping = json.loads(raw)
    except json.JSONDecodeError:
        print(f"[警告] グルーピング結果のJSON解析に失敗したため、すべて1つのトピックにまとめます。\n  応答: {raw}")
        return {"untitled": images}

    groups: dict[str, list[Path]] = {}
    seen_indices: set[int] = set()
    for topic, indices in grouping.items():
        if not isinstance(indices, list):
            continue
        topic = sanitize_topic(str(topic))
        valid = []
        for idx in indices:
            # モデルが番号を文字列（"0"等）で返すことがあるためintへキャスト許容
            try:
                idx = int(idx)
            except (TypeError, ValueError):
                continue
            if not (0 <= idx < len(images)) or idx in seen_indices:
                continue
            seen_indices.add(idx)
            valid.append(images[idx])
        if valid:
            groups.setdefault(topic, []).extend(valid)

    leftover = [p for i, p in enumerate(images) if i not in seen_indices]
    if leftover:
        print(f"[警告] グルーピングから漏れた画像を 'untitled' にまとめます: {[p.name for p in leftover]}")
        groups.setdefault("untitled", []).extend(leftover)

    return groups


def main():
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    all_images = sorted([p for p in SCREENSHOTS_DIR.iterdir() if p.is_file() and p.suffix.lower() in IMAGE_EXTENSIONS])
    if not all_images:
        print(f"画像が見つかりません。{SCREENSHOTS_DIR} に画像を入れてください。")
        return

    processed = load_processed()
    images = [p for p in all_images if p.name not in processed]
    if not images:
        print(f"新規画像はありません（処理済み {len(all_images)} 枚）。")
        return
    print(f"新規 {len(images)} 枚を処理します（処理済み {len(all_images) - len(images)} 枚はスキップ）。")

    groups = group_images(images)

    for topic, imgs in groups.items():
        out_md = OUTPUT_DIR / f"{topic}.md"

        print(f"[処理中] {topic} ({len(imgs)} 枚) ...")
        text = images_to_text(imgs)
        if out_md.exists():
            # 既存トピックに合流した新規画像は末尾に追記する（スキップすると文字起こしが失われるため）
            with out_md.open("a", encoding="utf-8") as f:
                f.write(f"\n\n{text}\n")
            print(f"[追記] → {out_md.name}")
        else:
            out_md.write_text(f"# {topic}\n\n{text}\n", encoding="utf-8")
            print(f"[完了] → {out_md.name}")

        for p in imgs:
            processed[p.name] = topic
        save_processed(processed)

    print("\n完了！次に ingest.py を実行してChromaDBに取り込んでください。")
    print("  .venv/bin/python -m batch.ingest")


if __name__ == "__main__":
    main()
