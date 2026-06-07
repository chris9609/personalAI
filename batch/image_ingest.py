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
OLLAMA_URL = "http://localhost:11434"
MODEL = "gemma4:e4b"
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".heic", ".heif"}

CONSOLIDATE_PROMPT = (
    "これらは同じトピックに関連する複数の画像です。"
    "画像群全体から読み取れる情報を、トピックごとにまとめて日本語で詳しく書き出してください。"
    "重複情報は統合し、テキスト・数字・場所・連絡先・日付など読み取れる情報をすべて含めてください。"
    "構造化されたMarkdown形式（見出し・箇条書き）で出力してください。"
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
    imgs_b64 = [load_image_as_png_b64(p) for p in image_paths]
    return generate(CONSOLIDATE_PROMPT, images_b64=imgs_b64)


def describe_image(image_path: Path) -> str:
    img_b64 = load_image_as_png_b64(image_path)
    return generate(DESCRIBE_PROMPT, images_b64=[img_b64]).strip()


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
            if not isinstance(idx, int) or not (0 <= idx < len(images)) or idx in seen_indices:
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

    images = sorted([p for p in SCREENSHOTS_DIR.iterdir() if p.is_file() and p.suffix.lower() in IMAGE_EXTENSIONS])
    if not images:
        print(f"画像が見つかりません。{SCREENSHOTS_DIR} に画像を入れてください。")
        return

    groups = group_images(images)

    for topic, imgs in groups.items():
        out_md = OUTPUT_DIR / f"{topic}.md"
        if out_md.exists():
            print(f"[SKIP] {topic} (既に生成済み: {out_md.name})")
            continue

        print(f"[処理中] {topic} ({len(imgs)} 枚) ...")
        text = images_to_text(imgs)
        out_md.write_text(f"# {topic}\n\n{text}\n", encoding="utf-8")
        print(f"[完了] → {out_md.name}")

    print("\n完了！次に ingest.py を実行してChromaDBに取り込んでください。")
    print("  .venv/bin/python -m batch.ingest")


if __name__ == "__main__":
    main()
