"""
iCloud上のObsidian vaultの .md を data/obsidian/ に同期するスクリプト。
画像RAG（image_ingest.py）と同じ「data/ に集約して ingest」構成に揃える。

- ソース: ~/Library/Mobile Documents/iCloud~md~obsidian/Documents/Obsidian Vault（読み取りのみ）
- 同期先: data/obsidian/（サブフォルダ構造を保持。.gitignoreでGit管理外）
- 対象:  .md のみ。.obsidian / .trash などの隠しフォルダと private/ は除外
- 差分:  mtime + サイズが変わったものだけコピー
- 削除:  vault側に存在しなくなったファイルは同期先からも削除する（ミラーリング）。
        ChromaDB側の削除は ingest.py（UPSERTS_AND_DELETE）が追従する。

実行後:
  .venv/bin/python -m batch.ingest
でChromaDBに取り込む。
"""
import shutil
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
VAULT_DIR = Path.home() / "Library" / "Mobile Documents" / "iCloud~md~obsidian" / "Documents" / "Obsidian Vault"
DEST_DIR = BASE_DIR / "data" / "obsidian"

# RAGに取り込みたくないフォルダ（vault直下のフォルダ名で指定）
# private/ にはID・パスワード類を置く運用のため、同期対象から外す
EXCLUDE_DIRS = {"private"}


def iter_markdown(root: Path):
    """隠しフォルダ（.obsidian / .trash など）と除外フォルダを除いた .md を相対パス付きで列挙する。"""
    for path in root.rglob("*.md"):
        rel = path.relative_to(root)
        if any(part.startswith(".") for part in rel.parts):
            continue
        if rel.parts[0] in EXCLUDE_DIRS:
            continue
        yield path, rel


def needs_copy(src: Path, dest: Path) -> bool:
    if not dest.exists():
        return True
    src_stat, dest_stat = src.stat(), dest.stat()
    return src_stat.st_size != dest_stat.st_size or src_stat.st_mtime > dest_stat.st_mtime


def main():
    if not VAULT_DIR.is_dir():
        print(f"vaultが見つかりません: {VAULT_DIR}")
        sys.exit(1)

    DEST_DIR.mkdir(parents=True, exist_ok=True)

    copied = skipped = 0
    seen: set[Path] = set()
    for src, rel in iter_markdown(VAULT_DIR):
        seen.add(rel)
        dest = DEST_DIR / rel
        if needs_copy(src, dest):
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dest)
            print(f"[同期] {rel}")
            copied += 1
        else:
            skipped += 1

    print(f"\n完了: {copied} 件コピー / {skipped} 件スキップ（変更なし）")

    # 0件コピー+0件スキップ = vaultの中身が1つも見えていない。
    # iCloudのアクセス拒否（TCC）や空vaultの可能性が高く、このまま後続の
    # ingest を走らせるとRAGが古いまま静かに固定されるので失敗として止める。
    # ※ この判定はミラー削除より前に行うこと。vaultが見えていない状態で
    #   削除を実行すると、同期先を全消ししてしまう。
    if copied == 0 and skipped == 0:
        print("エラー: ソースの .md が1件も見つかりませんでした（アクセス拒否 or 空vault の疑い）")
        sys.exit(1)

    # ミラーリング: vault側に存在しない（移動・削除・除外された）ファイルを同期先から消す
    deleted = 0
    for dest in DEST_DIR.rglob("*.md"):
        rel = dest.relative_to(DEST_DIR)
        if rel not in seen:
            dest.unlink()
            print(f"[削除] {rel}")
            deleted += 1
    # 空になったフォルダを深い階層から順に片付ける
    for d in sorted((p for p in DEST_DIR.rglob("*") if p.is_dir()), reverse=True):
        if not any(d.iterdir()):
            d.rmdir()
    if deleted:
        print(f"削除: {deleted} 件（vault側に存在しないため）")

    if copied:
        print("次に ingest.py を実行してChromaDBに取り込んでください。")
        print("  .venv/bin/python -m batch.ingest")


if __name__ == "__main__":
    main()
