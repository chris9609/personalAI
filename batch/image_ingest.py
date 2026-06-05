"""
data/screenshots/HEIC/<topic>/ 以下の画像群をOllamaのビジョンAPIでまとめてテキスト化し、
data/screenshots/MD/<topic>.md として保存するスクリプト。
その後 ingest.py を実行すれば ChromaDB に取り込まれる。

使い方:
  HEIC/new_home/ に画像をまとめて置く → MD/new_home.md が生成される
"""
import base64
import io
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

PROMPT = (
    "これらは同じトピックに関連する複数の画像です。"
    "画像群全体から読み取れる情報を、トピックごとにまとめて日本語で詳しく書き出してください。"
    "重複情報は統合し、テキスト・数字・場所・連絡先・日付など読み取れる情報をすべて含めてください。"
    "構造化されたMarkdown形式（見出し・箇条書き）で出力してください。"
)


def load_image_as_png_b64(image_path: Path) -> str:
    img = Image.open(image_path).convert("RGB")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode()


def images_to_text(image_paths: list[Path]) -> str:
    imgs_b64 = [load_image_as_png_b64(p) for p in image_paths]
    resp = httpx.post(
        f"{OLLAMA_URL}/api/generate",
        json={"model": MODEL, "prompt": PROMPT, "images": imgs_b64, "stream": False},
        timeout=180,
    )
    resp.raise_for_status()
    return resp.json()["response"]


def main():
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    # ルート直下のバラ画像は警告して無視
    loose_files = [p for p in SCREENSHOTS_DIR.iterdir() if p.is_file() and p.suffix.lower() in IMAGE_EXTENSIONS]
    if loose_files:
        print(f"[警告] HEIC/ 直下のファイルはサブディレクトリに移動してください:")
        for f in loose_files:
            print(f"  {f.name}")

    subdirs = sorted([d for d in SCREENSHOTS_DIR.iterdir() if d.is_dir()])
    if not subdirs:
        print("サブディレクトリが見つかりません。例: data/screenshots/HEIC/new_home/ に画像を入れてください。")
        return

    for subdir in subdirs:
        out_md = OUTPUT_DIR / (subdir.name + ".md")
        if out_md.exists():
            print(f"[SKIP] {subdir.name}/ (既に生成済み: {out_md.name})")
            continue

        images = sorted([p for p in subdir.iterdir() if p.suffix.lower() in IMAGE_EXTENSIONS])
        if not images:
            print(f"[SKIP] {subdir.name}/ (画像なし)")
            continue

        print(f"[処理中] {subdir.name}/ ({len(images)} 枚) ...")
        text = images_to_text(images)
        out_md.write_text(f"# {subdir.name}\n\n{text}\n", encoding="utf-8")
        print(f"[完了] → {out_md.name}")

    print("\n完了！次に ingest.py を実行してChromaDBに取り込んでください。")
    print("  .venv/bin/python -m batch.ingest")


if __name__ == "__main__":
    main()
