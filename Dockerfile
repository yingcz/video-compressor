# ── ベースイメージ ──────────────────────────────────────────
FROM python:3.10-slim

# ── システム依存パッケージ ──────────────────────────────────
RUN apt-get update && \
    apt-get install -y --no-install-recommends ffmpeg && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

# ── 作業ディレクトリ ────────────────────────────────────────
WORKDIR /app

# ── Python 依存パッケージ ───────────────────────────────────
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# ── アプリケーションコードをコピー ──────────────────────────
COPY . .

# ── 一時ファイル用ディレクトリを作成 ───────────────────────
RUN mkdir -p uploads outputs

# ── ポート公開 ──────────────────────────────────────────────
EXPOSE 5000

# ── 起動コマンド ────────────────────────────────────────────
#
# [なぜ gevent ワーカーが必要か]
# sync ワーカーは gunicorn が SIGALRM でタイムアウトを実装するが、
# subprocess.run() が I/O 待ち中に SIGALRM を受け取ると
# selectors.select() の中で SystemExit が投げられ処理が中断される。
# gevent ワーカーは SIGALRM を使わず greenlet の協調スケジューリングで
# タイムアウト管理するため、FFmpeg などの長時間 subprocess と相性が良い。
#
# [各オプションの意図]
#   --workers 1    : Render 無料プラン 512MB に合わせてプロセス数を最小化
#   --worker-class gevent : 上記理由で gevent を使用
#   --worker-connections 4 : gevent の同時接続数（軽量に制限）
#   --timeout 300  : FFmpeg 2pass エンコードは数分かかるため余裕を持たせる
CMD gunicorn app:app \
      --bind 0.0.0.0:${PORT:-5000} \
      --workers 1 \
      --worker-class gevent \
      --worker-connections 4 \
      --timeout 300 \
      --log-level info
