"""
自動リンク: vaultのノート同士を意味の近さで [[リンク]] する

やること:
  1. ChromaDB（personal_rag）からvault由来ノートの埋め込みを読み出す
  2. ノート同士のコサイン類似度を計算し、各ノートの「関連ノート」を選ぶ
  3. vaultの各ノート末尾に「## 関連」セクションとして [[リンク]] を書き込む

設計:
  - 埋め込みは毎晩の ingest がChromaに保存済みのものを再利用する。
    新しいLLM/埋め込み計算はゼロ（Ollama不要）
  - 書き込みは %% auto-link:start/end %% マーカーで囲んだ末尾ブロックだけ。
    本文には一切触らず、ブロックごと消せばいつでも完全に元に戻せる
  - 内容が変わらないファイルには書き込まない（mtimeを汚すと翌晩の
    obsidian_sync が全ファイル再コピーになるため）
  - 夜間バッチでは ingest の後に実行する（その晩の新ノートまで反映）

使い方:
  .venv/bin/python -m batch.auto_link            # 実際に書き込む
  .venv/bin/python -m batch.auto_link --dry-run  # 類似度と予定だけ表示
"""
import re
import sys
from collections import defaultdict
from pathlib import Path

import chromadb
import numpy as np

BASE_DIR = Path(__file__).resolve().parent.parent
CHROMA_DIR = str(BASE_DIR / "chroma_db")
COLLECTION_NAME = "personal_rag"
OBSIDIAN_PREFIX = str(BASE_DIR / "data" / "obsidian") + "/"
VAULT_DIR = Path.home() / "Library" / "Mobile Documents" / "iCloud~md~obsidian" / "Documents" / "Obsidian Vault"

MAX_LINKS = 3          # 1ノートに張るリンクの上限
MIN_SIMILARITY = 0.40  # これ未満の関連は「無関係」とみなして張らない
# ※ 類似度は「全チャンクの重心を引いてから」のコサイン（センタリング）。
#   nomic-embed-text は素のコサインだと無関係な文書同士でも0.8前後まで出て
#   関連(0.81-0.97)と分離できないため。実測: センタリング後は
#   無関係=最大0.22 / 関連トップ3=0.49-0.89 で、0.40はその間の値（2026-07-07）

BLOCK_START = "%% auto-link:start %%"
BLOCK_END = "%% auto-link:end %%"
BLOCK_RE = re.compile(
    re.escape(BLOCK_START) + r".*?" + re.escape(BLOCK_END) + r"\n?", re.DOTALL
)


def load_note_chunks() -> tuple[dict[str, np.ndarray], np.ndarray]:
    """Chromaからvault由来ノートの埋め込みを読み出す。

    返り値: ({ vault相対パス: チャンク行列 }, 全チャンクの重心)
    重心はセンタリング用。vault以外（カレンダー・画像分析）も含めた
    コレクション全体から取り、多様な内容で「平均的な埋め込み」を作る。
    """
    collection = chromadb.PersistentClient(path=CHROMA_DIR).get_collection(COLLECTION_NAME)
    res = collection.get(include=["embeddings", "metadatas"])
    chunks: dict[str, list] = defaultdict(list)
    for meta, emb in zip(res["metadatas"], res["embeddings"]):
        file_path = (meta or {}).get("file_path", "")
        if file_path.startswith(OBSIDIAN_PREFIX):
            chunks[file_path[len(OBSIDIAN_PREFIX):]].append(emb)
    center = np.array(res["embeddings"], dtype=float).mean(axis=0)
    return {rel: np.array(embs, dtype=float) for rel, embs in chunks.items()}, center


def related_notes(notes: dict[str, np.ndarray], center: np.ndarray) -> dict[str, list[tuple[str, float]]]:
    """各ノートの関連ノートを { 相対パス: [(相手の相対パス, 類似度), ...] } で返す。

    ノート間の類似度 = チャンク同士の（センタリング後）コサイン類似度の最大値。
    「どこか一部でも同じ話題に触れていれば関連」という基準（平均だと
    長いノートほど薄まって、部分的に強い関連を取りこぼすため）。
    """
    rels = list(notes)
    normed = {}
    for rel in rels:
        m = notes[rel] - center
        normed[rel] = m / np.linalg.norm(m, axis=1, keepdims=True)

    result: dict[str, list[tuple[str, float]]] = {}
    for a in rels:
        scored = []
        for b in rels:
            if b == a:
                continue
            sim = float((normed[a] @ normed[b].T).max())
            if sim >= MIN_SIMILARITY:
                scored.append((b, sim))
        scored.sort(key=lambda x: -x[1])
        result[a] = scored[:MAX_LINKS]
    return result


def wiki_link(rel: str) -> str:
    """vault相対パス → Obsidianリンク。サブフォルダ内は同名衝突を避けてパス指定にする"""
    target = rel.removesuffix(".md")
    name = Path(target).name
    return f"[[{target}|{name}]]" if target != name else f"[[{name}]]"


def build_block(links: list[tuple[str, float]]) -> str:
    lines = "\n".join(f"- {wiki_link(rel)}" for rel, _ in links)
    return f"{BLOCK_START}\n## 関連\n{lines}\n{BLOCK_END}"


def update_note(vault_path: Path, links: list[tuple[str, float]]) -> bool:
    """末尾の関連ブロックを最新に置き換える。ファイルを変更したら True"""
    original = vault_path.read_text(encoding="utf-8")
    body = BLOCK_RE.sub("", original).rstrip()
    updated = f"{body}\n\n{build_block(links)}\n" if links else f"{body}\n"
    if updated == original:
        return False
    vault_path.write_text(updated, encoding="utf-8")
    return True


def main() -> int:
    dry_run = "--dry-run" in sys.argv
    if not VAULT_DIR.is_dir():
        print(f"vaultが見つかりません: {VAULT_DIR}")
        return 1

    notes, center = load_note_chunks()
    if not notes:
        print("エラー: Chromaにvault由来のノートが1件もありません（ingest未実行の疑い）")
        return 1
    print(f"対象: {len(notes)} ノート")

    updated = unchanged = missing = 0
    for rel, links in related_notes(notes, center).items():
        vault_path = VAULT_DIR / rel
        if not vault_path.is_file():
            # 今日vault側で消された等。翌晩のsync→ingestでChromaからも消える
            print(f"[スキップ] vaultに見つかりません: {rel}")
            missing += 1
            continue
        if dry_run:
            shown = ", ".join(f"{Path(b).stem}({sim:.2f})" for b, sim in links) or "（関連なし）"
            print(f"[dry-run] {rel} → {shown}")
            continue
        if update_note(vault_path, links):
            print(f"[更新] {rel} → {len(links)} 件")
            updated += 1
        else:
            unchanged += 1

    if not dry_run:
        print(f"完了: {updated} 件更新 / {unchanged} 件変更なし / {missing} 件スキップ")
    return 0


if __name__ == "__main__":
    sys.exit(main())
