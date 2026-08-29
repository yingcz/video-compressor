# ── ベースイメージ ──────────────────────────────────────────
FROM python:3.10-slim

# ── システム依存パッケージ ──────────────────────────────────
# ffmpeg と libx264（libmp3lame は ffmpeg に同梱）をインストール
# キャッシュを残さず最小サイズを維持
RUN apt-get update && \
    apt-get install -y --no-install-recommends ffmpeg && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

# ── 作業ディレクトリ ────────────────────────────────────────
WORKDIR /app

# ── Python 依存パッケージ ───────────────────────────────────
# requirements.txt だけ先にコピーしてレイヤーキャッシュを有効活用
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# ── アプリケーションコードをコピー ──────────────────────────
COPY . .

# ── 一時ファイル用ディレクトリを作成 ───────────────────────
# Render の一時ファイルシステムはコンテナ内に書き込み可能
RUN mkdir -p uploads outputs

# ── ポート公開 ──────────────────────────────────────────────
# Render は PORT 環境変数でポートを渡す（デフォルト 5000）
EXPOSE 5000

# ── 起動コマンド ────────────────────────────────────────────
# gunicorn でマルチワーカー起動
# --timeout 300: FFmpeg の長時間処理でタイムアウトしないよう余裕を持たせる
# --workers 2  : 同時リクエストを 2 本処理（メモリに合わせて調整可）
CMD gunicorn app:app \
      --bind 0.0.0.0:${PORT:-5000} \
      --timeout 300 \
      --workers 2 \
      --log-level info
