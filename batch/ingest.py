"""
data/ 以下のMarkdownファイルをChromaDBに取り込むスクリプト（upsert対応）
"""
import re
from pathlib import Path
import chromadb
from llama_index.core import SimpleDirectoryReader, Settings, StorageContext
from llama_index.core.ingestion import IngestionPipeline, DocstoreStrategy
from llama_index.core.node_parser import MarkdownNodeParser, SentenceSplitter
from llama_index.core.schema import TransformComponent
from llama_index.core.storage.docstore import SimpleDocumentStore
from llama_index.vector_stores.chroma import ChromaVectorStore
from llama_index.embeddings.ollama import OllamaEmbedding

from batch.auto_link import BLOCK_RE as AUTO_LINK_BLOCK_RE

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = str(BASE_DIR / "data")
CHROMA_DIR = str(BASE_DIR / "chroma_db")
PIPELINE_DIR = str(BASE_DIR / "chroma_db" / "pipeline")
COLLECTION_NAME = "personal_rag"
HEADING_LINE_RE = re.compile(r"^#+ .*$", re.MULTILINE)


class DropHeadingOnlyNodes(TransformComponent):
    """見出しだけで本文がないチャンクを捨てる。

    `# デプロイと運用` のようなチャンクは話題の言葉だけなので検索上位に来やすいが、
    答えになる中身がない。見出し自体は配下チャンクの header_path に残るので失われない
    """

    def __call__(self, nodes, **kwargs):
        return [n for n in nodes if HEADING_LINE_RE.sub("", n.text).strip()]


def main():
    Settings.embed_model = OllamaEmbedding(
        model_name="bge-m3",
        base_url="http://localhost:11434",
    )
    Settings.llm = None

    chroma_client = chromadb.PersistentClient(path=CHROMA_DIR)
    collection = chroma_client.get_or_create_collection(COLLECTION_NAME)
    vector_store = ChromaVectorStore(chroma_collection=collection)

    pipeline_dir = Path(PIPELINE_DIR)
    pipeline_dir.mkdir(exist_ok=True)

    pipeline = IngestionPipeline(
        # 話題が混ざらないようMarkdownの見出しで分け、長すぎる節だけさらに切る。
        # 物差し25問（答えの記述を上位4件に含むか）: 18問 → 21問（2026-11-05）
        transformations=[
            MarkdownNodeParser(),
            SentenceSplitter(),
            DropHeadingOnlyNodes(),
            Settings.embed_model,
        ],
        vector_store=vector_store,
        docstore=SimpleDocumentStore(),
        # UPSERTS_AND_DELETE: 入力に存在しなくなったファイル（vaultで削除・移動・除外
        # されたもの）をChromaDBからも削除する。カレンダーは別ルートで取り込んでおり
        # このdocstoreに載っていないため影響を受けない
        docstore_strategy=DocstoreStrategy.UPSERTS_AND_DELETE,
    )

    if (pipeline_dir / "docstore.json").exists():
        pipeline.load(str(pipeline_dir))
        print("既存のdocstoreを読み込みました")

    documents = SimpleDirectoryReader(
        DATA_DIR, recursive=True, required_exts=[".md"], filename_as_id=True,
        # 説明書・契約書の写真から作ったMD(data/screenshots/)はRAGに入れない。
        # 一度も質問されず、全体の4割を占めてノート検索の邪魔になっていたため（ファイル自体は残す）
        exclude=["screenshots"],
    ).load_data()
    # auto_link が書き込む関連リンクは自分で書いた内容ではないのでRAGに入れない
    for doc in documents:
        doc.set_content(AUTO_LINK_BLOCK_RE.sub("", doc.text))
    print(f"{len(documents)} 件のMarkdownを読み込みました")

    nodes = pipeline.run(documents=documents)
    pipeline.persist(str(pipeline_dir))
    print(f"ChromaDB取り込み完了（{len(nodes)} ノードを処理）")


if __name__ == "__main__":
    main()
