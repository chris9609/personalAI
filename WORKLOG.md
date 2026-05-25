# パーソナルRAGシステム 作業ログ

## 2026-05-25 — フェーズ1：RAG基盤構築スタート

### 決めたこと

| 項目 | 選択 | 理由 |
|------|------|------|
| Python環境管理 | venv | 標準機能、追加インストール不要 |
| 埋め込みモデル | nomic-embed-text | Ollamaで `ollama pull` するだけ。追加サービス不要でシンプル |
| コード配置 | `src/` 以下 | 後でファイルが増えても見通しがいい |
| LLM | gemma4:e4b | 既存のOllamaに入っていたモデル |

### フェーズ1のゴール（最小成功条件）
- `data/profile.md`（プロフィール情報）を作る
- LlamaIndex + ChromaDB に取り込む
- 「私の名前は？」などの質問に Ollama 経由で正しく答える

### ディレクトリ構成
```
parsonalAI/
├── personalAI.md       # 設計ドキュメント
├── WORKLOG.md          # このファイル（作業ログ）
├── data/
│   └── profile.md      # RAGに取り込むプロフィール情報
└── src/
    ├── ingest.py       # ドキュメントをChromaDBに取り込むスクリプト
    └── query.py        # 質問して回答を得るスクリプト
```

### インストールしたもの

```bash
# 埋め込みモデル（Ollama）
ollama pull nomic-embed-text

# Python仮想環境
python3 -m venv .venv
source .venv/bin/activate

# Pythonパッケージ
pip install llama-index \
            llama-index-vector-stores-chroma \
            llama-index-embeddings-ollama \
            llama-index-llms-ollama \
            chromadb
```

### 実行したコマンド（順番通り）
1. `mkdir src data`
2. `ollama pull nomic-embed-text`
3. `python3 -m venv .venv`
4. `pip install ...`（上記）
5. `data/profile.md` を作成
6. `python src/ingest.py` でChromaDBに取り込み
7. `python src/query.py` で動作確認

### 完了した作業
- [x] `src/` `data/` ディレクトリ作成
- [x] `ollama pull nomic-embed-text` 完了
- [x] `python3 -m venv .venv` 完了
- [x] pip install 完了（llama-index / chromadb など）
- [x] `src/ingest.py` 作成（data/以下をChromaDBに取り込む）
- [x] `src/query.py` 作成（質問→Ollama経由で回答）
- [x] `data/profile.md` に実際の情報を記入（名前・住所・職業・年齢）
- [x] `python src/ingest.py` でChromaDBに取り込み完了
- [x] `python src/query.py "質問"` で動作確認完了

### 動作確認結果
```
質問: 私の名前は？
回答: クリスです。

質問: 私の職業と住んでいる場所は？
回答: 職業はエンジニアで、住んでいる場所は大阪です（引っ越し予定があります）。
```
→ フェーズ1 完了 ✅

### 次回やること（フェーズ2）
→ 下記フェーズ2セクション参照

### ハマった点・メモ
- profile.mdの住所は引っ越し後に更新すること（`python src/ingest.py` 再実行で反映）

---

## 2026-05-25 — フェーズ2：Googleカレンダー連携

### 決めたこと

| 項目 | 選択 | 理由 |
|------|------|------|
| ライブラリ | google-api-python-client + google-auth-oauthlib | 公式。OAuthエラーの原因が追いやすい |
| 認証情報の置き場 | `secrets/` ディレクトリ | .gitignoreで除外済み。絶対にGitHubにあげない |
| 取得期間 | デフォルト7日（`--days` で変更可） | 日常用途として十分 |

### インストールしたもの

```bash
.venv/bin/pip install google-api-python-client google-auth-oauthlib
```

### クリスが手動でやること（Google Cloud Console）
- [ ] https://console.cloud.google.com にアクセス
- [ ] 新規プロジェクト作成（名前は何でもOK）
- [ ] 「APIとサービス」→「ライブラリ」→ Google Calendar API を有効化
- [ ] 「APIとサービス」→「認証情報」→「OAuthクライアントID」を作成
  - アプリケーションの種類：**デスクトップアプリ**
- [ ] `credentials.json` をダウンロードして `secrets/credentials.json` に置く
- [ ] 初回 `python src/calendar_fetch.py` を実行 → ブラウザでGoogle認証 → `secrets/token.json` が自動生成

### 完了した作業
- [x] `src/calendar_fetch.py` 作成（カレンダー取得 + ChromaDB取り込み）
- [x] `secrets/` ディレクトリ作成（.gitignoreで除外済み）
- [x] `requirements.txt` 更新

### 完了した作業
- [x] クリスがGoogle Cloud ConsoleでOAuth設定
- [x] `secrets/credentials.json` を配置
- [x] テストユーザーに自分のGmailを追加（ハマったポイント）
- [x] `python src/calendar_fetch.py` 実行 → 認証成功・`token.json` 生成

### 完了（続き）
- [x] カレンダーに「仕事（明日9:30〜10:30）」を入れて再実行
- [x] `python src/query.py "明日の予定は？"` → 正しく回答 ✅

→ フェーズ2 完了 ✅

---

## 次回やること — LangServe でOpen WebUIと繋ぐ

### ゴール
Open WebUIのチャット画面からRAG（ChromaDB）を経由して質問できるようにする

### 構成イメージ
```
Open WebUI
    ↓（OpenAI互換API）
LangServe サーバー（src/server.py）
    ↓
LangChain + LlamaIndex（RAG）
    ↓
ChromaDB → Ollama(gemma4:e4b)
```

### やること
1. `pip install langserve langchain langchain-community` をインストール
2. `src/server.py` を作成（LangServeでOpenAI互換エンドポイントを立てる）
3. Open WebUIの接続先を `http://localhost:8000` に変更
4. Open WebUIから「明日の予定は？」と聞いてRAGが効いてるか確認

### ← 次のセッションはここから
上記の1〜4をやるだけ。既存の ingest.py / query.py / calendar_fetch.py はそのまま使える。

### フェーズ3（その後）
- Obsidianのmdファイルをバッチでdataフォルダに同期して `ingest.py` で取り込む

### ← 次のセッションはここから
認証は完了済み（token.json生成済み）。カレンダーに予定を入れたら：
```bash
cd src
python calendar_fetch.py   # 取り込み
python query.py "今週の予定は？"  # 確認
```

### ハマった点
- Google OAuth: アプリが未審査だと「エラー詳細」しか出ない → **OAuth同意画面でテストユーザーに自分のGmailを追加**することで解決
- ファイル名：ダウンロードした credentials.json は長い名前になってる → `credentials.json` にリネームが必要
