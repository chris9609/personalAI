"""
Claude Code 受付役: gemma（Docker内のserver.py）から開発の依頼を受け取り、
Mac側で claude -p に作業させて、GitHubにPRを出し、結果をSlackに知らせる。

流れ:
  1. server.py の道具 claude_code_request が POST /request {repo, request} を送る
  2. すぐ「受け付けました」と返し、裏のスレッドで作業する（数分かかるため）
  3. origin/main から別フォルダ(worktree)に claude/日時 ブランチを切る
     （普段の作業フォルダ・mainには一切触らない。auto_push もそのまま動く）
  4. その中で claude -p に変更させる（道具は読み書きだけ。git・シェル・MCPは渡さない）
  5. commit・push・gh pr create はこのスクリプトが決め打ちで行う（mainへのpushはしない）
  6. PRのリンクを #秘書室 へ、失敗は #エラー へ。worktreeは片付ける

設計:
  - claude・gh・gitの認証がキーチェーンにあるので、cronではなくlaunchdで常駐させる。
  - 127.0.0.1 だけで待ち受け、共有トークン（secrets/claude_bridge_token）で認証する。
    0.0.0.0 にするとTailscale経由で外から叩けてしまうため。
  - 対象リポジトリは ~/claude/application/ 配下で origin があるものだけ。名前の完全一致で選ぶ。
  - 同時に走らせるのは1件だけ。実行中に来た依頼は断る。

使い方:
  .venv/bin/python -m batch.claude_bridge                          # 受付役として常駐
  .venv/bin/python -m batch.claude_bridge --run <repo> "<依頼>"     # 受付を通さず1件だけ実行（動作確認用）
"""
import json
import logging
import os
import shutil
import subprocess
import sys
import threading
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from batch.morning_briefing import BASE_DIR, JST, post_to_slack

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(message)s")

HOST = "127.0.0.1"
PORT = 8611
TOKEN_FILE = BASE_DIR / "secrets" / "claude_bridge_token"
REPOS_DIR = Path.home() / "claude" / "application"
WORKTREE_DIR = Path.home() / ".cache" / "claude-bridge"
ERROR_CHANNEL_ID = "C0BEQ1A52HX"  # #エラー
# launchdはPATHが細いので絶対パスで呼ぶ（ghは中でgitを呼ぶのでPATHも足す）
CLAUDE_BIN = str(Path.home() / ".local" / "bin" / "claude")
GH_BIN = "/opt/homebrew/bin/gh"
# 認証の入力待ちで固まらないように（入力する人がいない。固まるとロックが外れず以後の依頼を全部断ってしまう）
ENV = {
    **os.environ,
    "PATH": f"/opt/homebrew/bin:/usr/bin:/bin:{os.environ.get('PATH', '')}",
    "GIT_TERMINAL_PROMPT": "0",
    "GH_PROMPT_DISABLED": "1",
}
COMMAND_TIMEOUT_SEC = 120
CLAUDE_TIMEOUT_SEC = 30 * 60
# claude には読み書きだけ渡す。commit・pushはこちらでやるので Bash は渡さない
CLAUDE_TOOLS = "Read,Edit,Write,Glob,Grep"

SYSTEM_PROMPT_APPEND = """あなたはユーザーの依頼をもとに、このリポジトリのコードを変更します。
ユーザーは外出先からスマホ経由で依頼しているので、質問はできません。依頼を読み取って最善の変更をしてください。
git の操作（commit・push・ブランチ作成）はしないでください。変更後の作業は別の仕組みが行います。
公開リポジトリのこともあり、PRは作った瞬間から誰でも見られます。依頼文に予定の中身・住所・本名・トークンなどの個人情報が含まれていても、
それをコードにもPRの説明にも書かないでください。書かないと依頼を満たせない場合は、変更せずに理由を説明してください。

最後の返答は次の形式にしてください（これがそのままPRのタイトルと本文になります）:
1行目: PRのタイトル（日本語・50文字以内）
2行目: 空行
3行目以降: 何をなぜ変えたかの説明（箇条書き）。変更しなかった場合はその理由
"""

_job_lock = threading.Lock()


def load_token() -> str:
    return TOKEN_FILE.read_text(encoding="utf-8").strip()


def allowed_repos() -> dict[str, Path]:
    repos = {}
    for d in sorted(REPOS_DIR.iterdir()):
        if (d / ".git").exists() and run(["git", "-C", str(d), "remote", "get-url", "origin"], check=False).returncode == 0:
            repos[d.name] = d
    return repos


def run(cmd: list[str], cwd: Path | None = None, check: bool = True, timeout: int = COMMAND_TIMEOUT_SEC, **kw) -> subprocess.CompletedProcess:
    result = subprocess.run(cmd, cwd=cwd, env=ENV, capture_output=True, text=True, timeout=timeout, **kw)
    if check and result.returncode != 0:
        raise RuntimeError(f"{' '.join(cmd[:3])} が失敗 (exit {result.returncode}): {(result.stderr or result.stdout).strip()[:500]}")
    return result


def ask_claude(worktree: Path, request: str) -> str:
    result = run(
        [
            CLAUDE_BIN, "-p",
            "--permission-mode", "acceptEdits",
            "--tools", CLAUDE_TOOLS,
            "--strict-mcp-config",
            "--no-session-persistence",
            "--append-system-prompt", SYSTEM_PROMPT_APPEND,
        ],
        cwd=worktree,
        input=request,
        timeout=CLAUDE_TIMEOUT_SEC,
    )
    return result.stdout.strip()


def split_title_body(answer: str, request: str) -> tuple[str, str]:
    lines = answer.splitlines()
    title = lines[0].strip().lstrip("#").strip() if lines else ""
    if not title or len(title) > 80:
        title = f"Claude: {request[:40]}"
    body = "\n".join(lines[1:]).strip()
    return title, body


def do_job(repo_name: str, request: str) -> str:
    """1件の依頼を処理して、PRのURL（変更なしならその旨）を返す。失敗時は例外"""
    repo = allowed_repos()[repo_name]
    branch = f"claude/{datetime.now(JST):%Y%m%d-%H%M%S}"
    worktree = WORKTREE_DIR / f"{repo_name}-{branch.split('/')[1]}"
    WORKTREE_DIR.mkdir(parents=True, exist_ok=True)

    run(["git", "-C", str(repo), "fetch", "--quiet", "origin", "main"])
    run(["git", "-C", str(repo), "worktree", "add", "--quiet", "-b", branch, str(worktree), "origin/main"])
    try:
        logging.info(f"[job] {repo_name} {branch}: claude 実行開始")
        answer = ask_claude(worktree, request)
        title, body = split_title_body(answer, request)

        if not run(["git", "status", "--porcelain"], cwd=worktree).stdout.strip():
            return f"変更はありませんでした（{repo_name}）\n> {request}\n\n{answer[:1500]}"

        run(["git", "add", "-A"], cwd=worktree)
        run(["git", "commit", "--quiet", "-m", f"{title}\n\n依頼: {request}"], cwd=worktree)
        run(["git", "push", "--quiet", "-u", "origin", branch], cwd=worktree)
        pr_body = f"## 依頼\n> {request}\n\n## 変更内容\n{body}\n\n🤖 Claude Code（スマホからの依頼）で作成"
        pr_url = run(
            [GH_BIN, "pr", "create", "--base", "main", "--head", branch, "--title", title, "--body", pr_body],
            cwd=worktree,
        ).stdout.strip().splitlines()[-1]
        visibility = run([GH_BIN, "repo", "view", "--json", "visibility", "-q", ".visibility"], cwd=worktree, check=False).stdout.strip()
        label = {"PUBLIC": "公開リポジトリ", "PRIVATE": "非公開リポジトリ"}.get(visibility, "公開/非公開は不明")
        return f":white_check_mark: PRを作りました（{repo_name}・{label}）\n*{title}*\n{pr_url}\n> {request}"
    finally:
        # worktreeとローカルのブランチは片付ける（PRはGitHub側のブランチで残る）
        run(["git", "-C", str(repo), "worktree", "remove", "--force", str(worktree)], check=False)
        run(["git", "-C", str(repo), "branch", "-D", branch], check=False)


def job_thread(repo_name: str, request: str):
    try:
        message = do_job(repo_name, request)
        logging.info(f"[job] 完了: {message.splitlines()[0]}")
        post_to_slack(message)
    except Exception as e:
        logging.exception("[job] 失敗")
        try:
            post_to_slack(f":rotating_light: Claude Codeへの依頼が失敗しました（{repo_name}）\n> {request}\n{e}", channel=ERROR_CHANNEL_ID)
        except Exception:
            logging.exception("[job] Slack通知にも失敗")
    finally:
        _job_lock.release()


class Handler(BaseHTTPRequestHandler):
    def _reply(self, status: int, message: str):
        data = json.dumps({"message": message}, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        if self.path != "/request":
            return self._reply(404, "not found")
        if self.headers.get("Authorization") != f"Bearer {load_token()}":
            return self._reply(401, "認証に失敗しました")
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
            repo_name, request = str(body["repo"]).strip(), str(body["request"]).strip()
        except (ValueError, KeyError):
            return self._reply(400, "repo と request を指定してください")

        repos = allowed_repos()
        if repo_name not in repos:
            return self._reply(400, f"リポジトリ「{repo_name}」はありません。指定できるのは: {', '.join(repos)}")
        if not request:
            return self._reply(400, "依頼内容が空です")
        if not _job_lock.acquire(blocking=False):
            return self._reply(409, "前の依頼をまだ作業中です。終わってからもう一度頼んでください")

        logging.info(f"[受付] {repo_name}: {request[:80]}")
        threading.Thread(target=job_thread, args=(repo_name, request), daemon=True).start()
        return self._reply(202, f"{repo_name} への依頼を受け付けました。Claude Codeが作業して、終わったらSlackの#秘書室にPRのリンクを送ります（数分かかります）")

    def log_message(self, format, *args):
        pass  # アクセスログは[受付]の行で足りる


def main() -> int:
    if len(sys.argv) >= 4 and sys.argv[1] == "--run":
        repo_name, request = sys.argv[2], sys.argv[3]
        if repo_name not in allowed_repos():
            print(f"リポジトリ「{repo_name}」はありません: {', '.join(allowed_repos())}")
            return 1
        print(do_job(repo_name, request))
        return 0

    if not shutil.which(CLAUDE_BIN) or not TOKEN_FILE.exists():
        logging.error(f"claude({CLAUDE_BIN}) か トークン({TOKEN_FILE}) がありません")
        return 1
    logging.info(f"[起動] http://{HOST}:{PORT} で待ち受け")
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
