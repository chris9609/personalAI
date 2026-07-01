"""
ChromaDBに取り込んだ情報に対して質問するスクリプト
使い方: python src/query.py "質問文"
"""
import sys
import chromadb
from llama_index.core import VectorStoreIndex
from llama_index.vector_stores.chroma import ChromaVectorStore
from llama_index.embeddings.ollama import OllamaEmbedding
from llama_index.llms.ollama import Ollama
from llama_index.core import Settings

CHROMA_DIR = "../chroma_db"
COLLECTION_NAME = "personal_rag"
LLM_MODEL = "gemma4:e4b"

def main(question: str):
    Settings.embed_model = OllamaEmbedding(
        model_name="nomic-embed-text",
        base_url="http://localhost:11434",
    )
    Settings.llm = Ollama(
        model=LLM_MODEL,
        base_url="http://localhost:11434",
        request_timeout=120.0,
    )

    chroma_client = chromadb.PersistentClient(path=CHROMA_DIR)
    collection = chroma_client.get_or_create_collection(COLLECTION_NAME)

    vector_store = ChromaVectorStore(chroma_collection=collection)
    index = VectorStoreIndex.from_vector_store(vector_store)

    query_engine = index.as_query_engine(similarity_top_k=4)
    response = query_engine.query(question)
    print(f"\n質問: {question}")
    print(f"回答: {response}\n")

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("使い方: python src/query.py \"質問文\"")
        sys.exit(1)
    main(sys.argv[1])
