"""
data/screenshots/ 以下の画像をOllamaのビジョンAPIでテキスト化し、
サイドカー.mdファイルとして保存するスクリプト。
その後 ingest.py を実行すれば ChromaDB に取り込まれる。
"""
import base64
import httpx
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
SCREENSHOTS_DIR = BASE_DIR / "data" / "screenshots"
OLLAMA_URL = "http://localhost:11434"
MODEL = "gemma4:e4b"
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp"}

PROMPT = (
    "この画像に含まれる情報をすべて日本語で詳しく書き出してください。"
    "テキスト、数字、場所、人物、物体など、読み取れる情報をすべて含めてください。"
)


def image_to_text(image_path: Path) -> str:
    with open(image_path, "rb") as f:
        img_b64 = base64.b64encode(f.read()).decode()

    resp = httpx.post(
        f"{OLLAMA_URL}/api/generate",
        json={"model": MODEL, "prompt": PROMPT, "images": [img_b64], "stream": False},
        timeout=120,
    )
    resp.raise_for_status()
    return resp.json()["response"]


def main():
    images = [p for p in SCREENSHOTS_DIR.iterdir() if p.suffix.lower() in IMAGE_EXTENSIONS]
    if not images:
        print("画像が見つかりません。data/screenshots/ に画像を入れてください。")
        return

    for img_path in images:
        sidecar = img_path.with_suffix(".md")
        if sidecar.exists():
            print(f"[SKIP] {img_path.name} (既にテキスト化済み)")
            continue

        print(f"[処理中] {img_path.name} ...")
        text = image_to_text(img_path)
        sidecar.write_text(f"# {img_path.stem}\n\n{text}\n", encoding="utf-8")
        print(f"[完了] → {sidecar.name}")

    print("\n完了！次に ingest.py を実行してChromaDBに取り込んでください。")
    print("  .venv/bin/python -m src.ingest")


if __name__ == "__main__":
    main()
