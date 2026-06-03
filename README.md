# personalAI

自分専用のローカルAIアシスタント。自分の情報（予定・ドキュメント）をナレッジベースに取り込み、プライベートな環境でRAG検索とLLMを組み合わせて動かすことを目的としたプロジェクトです。

## 概要

外部サービスにデータを渡さず、すべてローカルで完結するパーソナルAIを目指しています。日常的に使うツール（Googleカレンダーなど）と連携し、自分のコンテキストを理解したAIと会話できる環境を構築しています。

## 主な機能

- **RAG検索**：LlamaIndexのRetrieverを使い、ベクトルDB（Chroma）に蓄積した情報を検索してLLMへ渡す
- **ローカルLLM**：Ollamaを使ってLLMをローカルで実行。ストリーミング応答に対応
- **天気情報の取得**：現在の天気情報をプロンプトに自動で組み込み、状況に応じた回答が可能
- **Googleカレンダー連携**（実装中）：カレンダーの予定を取得しChromaに格納。スケジュールを踏まえた応答を実現予定

## 技術スタック

| カテゴリ | 使用技術 |
|------|------|
| LLM実行環境 | Ollama |
| RAGフレームワーク | LlamaIndex |
| ベクトルDB | Chroma |
| コンテナ管理 | Docker / docker-compose |
| 言語 | Python |
| 外部連携 | Google Calendar API |

## アーキテクチャ

```
[Google Calendar / 外部データ]
        ↓
[データ取得・前処理]
        ↓
[Chroma（ベクトルDB）]
        ↓
[LlamaIndex Retriever（RAG）]
        ↓
[Ollama（ローカルLLM）]
        ↓
[ストリーミング応答]
```

## セットアップ

### 前提条件

- Docker / docker-compose
- Ollama（インストール済みであること）

### 起動方法

```bash
git clone https://github.com/chris9609/personalAI.git
cd personalAI
docker-compose up
```

## 開発の背景

「自分のデータをクラウドに渡さずに、自分専用のAIを持ちたい」という動機でスタートしました。Googleカレンダーの予定や個人のドキュメントをローカルのベクトルDBに蓄積し、日常的に使えるパーソナルAIを目指して継続開発しています。
