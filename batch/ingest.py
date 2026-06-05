"""
data/ 以下のMarkdownファイルをChromaDBに取り込むスクリプト（upsert対応）
"""
from pathlib import Path
import chromadb
from llama_index.core import SimpleDirectoryReader, Settings, StorageContext
from llama_index.core.ingestion import IngestionPipeline, DocstoreStrategy
from llama_index.core.node_parser import SentenceSplitter
from llama_index.core.storage.docstore import SimpleDocumentStore
from llama_index.vector_stores.chroma import ChromaVectorStore
from llama_index.embeddings.ollama import OllamaEmbedding

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = str(BASE_DIR / "data")
CHROMA_DIR = str(BASE_DIR / "chroma_db")
PIPELINE_DIR = str(BASE_DIR / "chroma_db" / "pipeline")
COLLECTION_NAME = "personal_rag"


def main():
    Settings.embed_model = OllamaEmbedding(
        model_name="nomic-embed-text",
        base_url="http://localhost:11434",
    )
    Settings.llm = None

    chroma_client = chromadb.PersistentClient(path=CHROMA_DIR)
    collection = chroma_client.get_or_create_collection(COLLECTION_NAME)
    vector_store = ChromaVectorStore(chroma_collection=collection)

    pipeline_dir = Path(PIPELINE_DIR)
    pipeline_dir.mkdir(exist_ok=True)

    pipeline = IngestionPipeline(
        transformations=[SentenceSplitter(), Settings.embed_model],
        vector_store=vector_store,
        docstore=SimpleDocumentStore(),
        docstore_strategy=DocstoreStrategy.UPSERTS,
    )

    if (pipeline_dir / "docstore.json").exists():
        pipeline.load(str(pipeline_dir))
        print("既存のdocstoreを読み込みました")

    documents = SimpleDirectoryReader(
        DATA_DIR, recursive=True, required_exts=[".md"], filename_as_id=True
    ).load_data()
    print(f"{len(documents)} 件のMarkdownを読み込みました")

    nodes = pipeline.run(documents=documents)
    pipeline.persist(str(pipeline_dir))
    print(f"ChromaDB取り込み完了（{len(nodes)} ノードを処理）")


if __name__ == "__main__":
    main()
