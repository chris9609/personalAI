"""
data/ 以下のMarkdownファイルをChromaDBに取り込むスクリプト
"""
import chromadb
from llama_index.core import SimpleDirectoryReader, VectorStoreIndex, StorageContext
from llama_index.vector_stores.chroma import ChromaVectorStore
from llama_index.embeddings.ollama import OllamaEmbedding
from llama_index.core import Settings

DATA_DIR = "../data"
CHROMA_DIR = "../chroma_db"
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
    storage_context = StorageContext.from_defaults(vector_store=vector_store)

    documents = SimpleDirectoryReader(DATA_DIR).load_data()
    print(f"{len(documents)} 件のドキュメントを読み込みました")

    VectorStoreIndex.from_documents(documents, storage_context=storage_context)
    print("ChromaDBへの取り込み完了")

if __name__ == "__main__":
    main()
