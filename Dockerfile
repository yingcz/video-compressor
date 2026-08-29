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

# ── 起動コマンド（Render 無料プラン 512MB 向け最小構成）────
#   --workers 1  : プロセス数を最小化（FFmpeg はすでにマルチスレッドで動く）
#   --threads 1  : スレッド数も 1 に抑えてメモリを節約
#   --timeout 120: 無料プランのタイムアウト上限に合わせる
#   --worker-class sync: デフォルトの同期ワーカーで最も軽量
CMD gunicorn app:app \
      --bind 0.0.0.0:${PORT:-5000} \
      --workers 1 \
      --threads 1 \
      --timeout 120 \
      --worker-class sync \
      --log-level info
