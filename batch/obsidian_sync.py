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
import errno
import shutil
import sys
import time
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


def copy_note(src: Path, dest: Path, retries: int = 3) -> bool:
    """vaultのノートを1件コピーする。成功でTrue、諦めたらFalse。

    iCloudは中身をクラウドにだけ置く「dataless」状態のファイルを作ることがあり、
    macOSのshutil.copy2（内部でfcopyfile）はそれに当たると自動ダウンロードを
    待たずに OSError(EDEADLK) で即失敗する（2026-07-06の夜間バッチで発生）。
    その場合は通常のread/writeコピーにフォールバックする。openによる読み込みは
    iCloudのダウンロード完了を待ってくれる。
    """
    try:
        shutil.copy2(src, dest)
        return True
    except OSError as e:
        if e.errno != errno.EDEADLK:
            raise

    # 書き込み途中で失敗しても同期済みの旧ファイルを壊さないよう、一時ファイル経由で置き換える
    tmp = dest.with_name(dest.name + ".sync-tmp")
    for attempt in range(retries):
        try:
            with open(src, "rb") as fsrc, open(tmp, "wb") as fdst:
                shutil.copyfileobj(fsrc, fdst)
            shutil.copystat(src, tmp)
            tmp.replace(dest)
            return True
        except OSError as e:
            tmp.unlink(missing_ok=True)
            if e.errno != errno.EDEADLK:
                raise
            time.sleep(2 * (attempt + 1))
    return False


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

    copied = skipped = failed = 0
    seen: set[Path] = set()
    for src, rel in iter_markdown(VAULT_DIR):
        seen.add(rel)
        dest = DEST_DIR / rel
        if needs_copy(src, dest):
            dest.parent.mkdir(parents=True, exist_ok=True)
            if copy_note(src, dest):
                print(f"[同期] {rel}")
                copied += 1
            else:
                # iCloudのダウンロードが間に合わない場合はこの1件だけ諦めて続行。
                # destは更新されないので、次回実行時にneeds_copyが再度拾う
                print(f"[保留] {rel}（iCloud未ダウンロード。次回に再試行）")
                failed += 1
        else:
            skipped += 1

    print(f"\n完了: {copied} 件コピー / {skipped} 件スキップ（変更なし）"
          + (f" / {failed} 件保留" if failed else ""))

    # 0件コピー+0件スキップ = vaultの中身が1つも見えていない。
    # iCloudのアクセス拒否（TCC）や空vaultの可能性が高く、このまま後続の
    # ingest を走らせるとRAGが古いまま静かに固定されるので失敗として止める。
    # ※ この判定はミラー削除より前に行うこと。vaultが見えていない状態で
    #   削除を実行すると、同期先を全消ししてしまう。
    if copied == 0 and skipped == 0 and failed == 0:
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
