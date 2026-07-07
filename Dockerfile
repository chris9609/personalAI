FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# chroma_db はイメージに焼き込まず compose の volume でマウントする
# （焼き込むと夜間バッチの更新がコンテナに届かず、RAGがビルド時点で固まるため）
COPY src/ ./src/

CMD ["uvicorn", "src.server:app", "--host", "0.0.0.0", "--port", "8000"]
