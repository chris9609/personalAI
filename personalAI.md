# パーソナルRAGシステム

## ゴール
iPhoneからローカルLLMに質問できる自分専用の秘書AIを構築する
- 「明日の予定は？」など日常的な質問に答えてくれる
- 自分の情報・予定・メモを一元管理する

## システム構成
- フロントエンド：（UIは後で決める）
- オーケストレーション：LangChain
- 検索・RAG：LlamaIndex
- ベクトルDB：ChromaDB
- LLM：Ollama（Gemma4）
- インフラ：Tailscale経由でiPhoneからアクセス

## 使用言語・技術
- Python（メイン言語）
- Markdown（ドキュメント管理）

## 作る順番
1. LlamaIndex + ChromaDB でRAG基盤構築
2. LangChainで繋ぐ
3. ~~UIを作る~~ → Open WebUI（完了✅）
4. ~~iPhoneからアクセスできるようにする~~ → Tailscale（完了✅）
5. Googleカレンダー連携
6. Obsidianの内容を自動でRAGに取り込むバッチ処理

## RAGに入れるドキュメント
### フェーズ1（手動）
- 自分のプロフィール（名前・住所・よく行く場所など）

### フェーズ2（自動同期）
- Googleカレンダー（予定・タスク）

### フェーズ3（バッチ処理）
- ObsidianのMarkdownファイル（iCloud同期→PCでバッチ→RAGに追加）

## GitHub
- 成果物はGitHubにポートフォリオとして公開
- READMEに構成図・使い方を記載