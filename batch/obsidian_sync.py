"""
iCloud上のObsidian vaultの .md を data/obsidian/ に同期するスクリプト。
画像RAG（image_ingest.py）と同じ「data/ に集約して ingest」構成に揃える。

- ソース: ~/Library/Mobile Documents/iCloud~md~obsidian/Documents/Obsidian Vault（読み取りのみ）
- 同期先: data/obsidian/（サブフォルダ構造を保持。.gitignoreでGit管理外）
- 対象:  .md のみ。.obsidian / .trash などの隠しフォルダは除外
- 差分:  mtime + サイズが変わったものだけコピー

実行後:
  .venv/bin/python -m batch.ingest
でChromaDBに取り込む。

※ vault側で削除した .md の扱い（同期先・ChromaDBからの削除）は今後対応。
"""
import shutil
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
VAULT_DIR = Path.home() / "Library" / "Mobile Documents" / "iCloud~md~obsidian" / "Documents" / "Obsidian Vault"
DEST_DIR = BASE_DIR / "data" / "obsidian"


def iter_markdown(root: Path):
    """隠しフォルダ（.obsidian / .trash など）を除いた .md を相対パス付きで列挙する。"""
    for path in root.rglob("*.md"):
        rel = path.relative_to(root)
        if any(part.startswith(".") for part in rel.parts):
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
        return

    DEST_DIR.mkdir(parents=True, exist_ok=True)

    copied = skipped = 0
    for src, rel in iter_markdown(VAULT_DIR):
        dest = DEST_DIR / rel
        if needs_copy(src, dest):
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dest)
            print(f"[同期] {rel}")
            copied += 1
        else:
            skipped += 1

    print(f"\n完了: {copied} 件コピー / {skipped} 件スキップ（変更なし）")
    if copied:
        print("次に ingest.py を実行してChromaDBに取り込んでください。")
        print("  .venv/bin/python -m batch.ingest")


if __name__ == "__main__":
    main()
