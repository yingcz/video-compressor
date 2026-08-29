#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
app.py — 動画ツールキット（静的ファイル配信 + 音声抽出/GIF/無音化 API）

FFmpeg.wasm に移行したため、動画圧縮処理はブラウザ側で完結する。
Flask はHTML配信と、wasm未対応の補助機能（音声抽出・GIF変換・無音化）のみ担う。

起動:
  gunicorn app:app --bind 0.0.0.0:${PORT:-5000} --workers 1 \
    --worker-class gevent --timeout 300
"""

import json
import os
import subprocess
import threading
import time
import uuid

from flask import (
    Flask,
    jsonify,
    render_template,
    request,
    send_file,
    send_from_directory,
)

BASE_DIR   = os.path.dirname(os.path.abspath(__file__))
UPLOAD_DIR = os.path.join(BASE_DIR, "uploads")
OUTPUT_DIR = os.path.join(BASE_DIR, "outputs")
os.makedirs(UPLOAD_DIR, exist_ok=True)
os.makedirs(OUTPUT_DIR, exist_ok=True)

ALLOWED_EXTENSIONS = {
    ".mp4", ".mov", ".avi", ".mkv", ".webm",
    ".wmv", ".flv", ".m4v",
    ".ts", ".mts", ".m2ts",
    ".3gp", ".ogv",
}

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 512 * 1024 * 1024  # 512MB

# ---------------------------------------------------------------------------
# セキュリティヘッダー（FFmpeg.wasm の SharedArrayBuffer に必要）
# ---------------------------------------------------------------------------
# SharedArrayBuffer は Cross-Origin-Isolated 環境でのみ使用可能。
# Cross-Origin-Isolated を有効にするには COOP + COEP の両方が必要。
#
# COEP の値は "require-corp" と "credentialless" の2択。
#   require-corp  … 外部リソースに CORP/CORS ヘッダーが必須 → Google Fonts や
#                   unpkg などのヘッダーなし CDN がブロックされる。
#   credentialless … 外部リソースをブロックせず Cross-Origin-Isolated を実現。
#                   外部 CDN を併用するアプリに適している。
#                   （Chrome 96+, Firefox 119+, Safari 17+ 対応）
#
# Render 無料プランを含め、すべての環境で無条件に付与する。


@app.after_request
def add_coep_coop_headers(response):
    """
    Cross-Origin-Isolated 環境を有効化して SharedArrayBuffer を解放する。
    FFmpeg.wasm はマルチスレッド処理にこの環境を要求する。
    """
    response.headers["Cross-Origin-Opener-Policy"]   = "same-origin"
    response.headers["Cross-Origin-Embedder-Policy"] = "credentialless"
    return response


# ---------------------------------------------------------------------------
# 一時ファイルストア管理
# ---------------------------------------------------------------------------

_result_store: dict = {}
_store_lock = threading.Lock()
RESULT_TTL_SEC = 180  # 3分でクリーンアップ


def store_result(path: str, filename: str, mimetype: str) -> str:
    result_id = uuid.uuid4().hex
    with _store_lock:
        _result_store[result_id] = {
            "path":       path,
            "filename":   filename,
            "mimetype":   mimetype,
            "size":       os.path.getsize(path),
            "created_at": time.time(),
        }
    return result_id


def cleanup_expired():
    while True:
        time.sleep(60)
        now = time.time()
        with _store_lock:
            expired = [k for k, v in _result_store.items()
                       if now - v["created_at"] > RESULT_TTL_SEC]
            for rid in expired:
                entry = _result_store.pop(rid)
                _safe_remove(entry["path"])


threading.Thread(target=cleanup_expired, daemon=True).start()


def _safe_remove(path: str):
    if path and os.path.exists(path):
        try:
            os.remove(path)
        except OSError:
            pass


def cleanup_paths(*paths):
    for path in paths:
        _safe_remove(path)


def _purge_temp_dirs():
    """起動時に残留一時ファイルを削除する"""
    for d in (UPLOAD_DIR, OUTPUT_DIR):
        if not os.path.isdir(d):
            continue
        for fname in os.listdir(d):
            _safe_remove(os.path.join(d, fname))


_purge_temp_dirs()


# ---------------------------------------------------------------------------
# 共通ヘルパー
# ---------------------------------------------------------------------------

def save_uploaded_video(file):
    ext = os.path.splitext(file.filename)[1].lower()
    if ext not in ALLOWED_EXTENSIONS:
        raise ValueError(
            f"対応していないファイル形式です（{ext}）。"
            f"対応形式: {', '.join(sorted(ALLOWED_EXTENSIONS))}"
        )
    job_id     = uuid.uuid4().hex
    input_path = os.path.join(UPLOAD_DIR, f"{job_id}{ext}")
    file.save(input_path)
    return job_id, input_path


def run_cmd(cmd, error_label):
    """
    Popen + communicate() で実行。
    subprocess.run(capture_output=True) は gunicorn の SIGALRM と干渉するため
    Popen に変更して回避する。
    """
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    stdout_bytes, stderr_bytes = proc.communicate()
    if proc.returncode != 0:
        stderr_text = stderr_bytes.decode("utf-8", errors="replace")[-2000:]
        raise RuntimeError(f"{error_label}に失敗しました:\n{stderr_text}")

    class _Result:
        returncode = proc.returncode
        stderr = stderr_bytes.decode("utf-8", errors="replace")
        stdout = stdout_bytes.decode("utf-8", errors="replace")

    return _Result()


FFMPEG_NOT_FOUND_MSG = (
    "ffmpeg / ffprobe が見つかりません。インストールしてPATHを通してください。"
)

GIF_FPS = 15
GIF_QUALITY_PRESETS = {
    "low":    {"width": 320},
    "medium": {"width": 480},
    "high":   {"width": 640},
}


# ---------------------------------------------------------------------------
# ルーティング
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/privacy")
def privacy():
    return render_template("privacy.html")


@app.route("/terms")
def terms():
    return render_template("terms.html")


@app.route("/contact")
def contact():
    return render_template("contact.html")


@app.route("/static/<path:filename>")
def static_files(filename):
    return send_from_directory(os.path.join(BASE_DIR, "static"), filename)


# ---------------------------------------------------------------------------
# 処理済みファイル取得
# ---------------------------------------------------------------------------

@app.route("/result/<result_id>")
def get_result(result_id: str):
    with _store_lock:
        entry = _result_store.get(result_id)
    if not entry or not os.path.exists(entry["path"]):
        return jsonify({"error": "ファイルが見つかりません（期限切れの可能性があります）。"}), 404
    return send_file(entry["path"], as_attachment=True,
                     download_name=entry["filename"], mimetype=entry["mimetype"])


@app.route("/preview/<result_id>")
def preview_result(result_id: str):
    with _store_lock:
        entry = _result_store.get(result_id)
    if not entry or not os.path.exists(entry["path"]):
        return jsonify({"error": "ファイルが見つかりません。"}), 404
    return send_file(entry["path"], as_attachment=False, mimetype=entry["mimetype"])


# ---------------------------------------------------------------------------
# ① 音声抽出（任意形式 → MP3）  ※ サーバー側で処理継続
# ---------------------------------------------------------------------------

@app.route("/extract-audio", methods=["POST"])
def extract_audio():
    video       = request.files.get("video")
    bitrate_raw = request.form.get("bitrate", "192")

    if video is None or video.filename == "":
        return jsonify({"error": "動画ファイルが選択されていません。"}), 400

    try:
        bitrate_kbps = int(bitrate_raw)
        if bitrate_kbps not in (128, 192, 320):
            bitrate_kbps = 192
    except (TypeError, ValueError):
        bitrate_kbps = 192

    try:
        job_id, input_path = save_uploaded_video(video)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

    original_size = os.path.getsize(input_path)
    output_path   = os.path.join(OUTPUT_DIR, f"{job_id}_audio.mp3")
    cmd = [
        "ffmpeg", "-y", "-i", input_path,
        "-vn", "-c:a", "libmp3lame", "-b:a", f"{bitrate_kbps}k",
        "-threads", "1",
        output_path,
    ]

    try:
        run_cmd(cmd, "音声抽出")
    except FileNotFoundError:
        cleanup_paths(input_path)
        return jsonify({"error": FFMPEG_NOT_FOUND_MSG}), 500
    except Exception as e:
        cleanup_paths(input_path)
        return jsonify({"error": str(e)}), 500

    cleanup_paths(input_path)

    if not os.path.exists(output_path):
        return jsonify({"error": "音声抽出に失敗しました。"}), 500

    result_name = os.path.splitext(video.filename)[0]
    dl_filename = f"{result_name}.mp3"
    result_id   = store_result(output_path, dl_filename, "audio/mpeg")

    return jsonify({
        "result_id":     result_id,
        "filename":      dl_filename,
        "original_size": original_size,
        "result_size":   os.path.getsize(output_path),
        "mimetype":      "audio/mpeg",
        "preview_type":  "audio",
    })


# ---------------------------------------------------------------------------
# ② GIF 変換（任意形式 → GIF）  ※ サーバー側で処理継続
# ---------------------------------------------------------------------------

@app.route("/to-gif", methods=["POST"])
def to_gif():
    video        = request.files.get("video")
    quality      = request.form.get("quality", "medium")
    start_raw    = request.form.get("start", "0")
    duration_raw = request.form.get("duration", "10")

    if video is None or video.filename == "":
        return jsonify({"error": "動画ファイルが選択されていません。"}), 400

    preset = GIF_QUALITY_PRESETS.get(quality, GIF_QUALITY_PRESETS["medium"])

    try:
        start_sec = max(0.0, min(float(start_raw), 3600.0))
    except (TypeError, ValueError):
        start_sec = 0.0

    try:
        duration_sec = max(1.0, min(float(duration_raw), 30.0))
    except (TypeError, ValueError):
        duration_sec = 10.0

    try:
        job_id, input_path = save_uploaded_video(video)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

    original_size = os.path.getsize(input_path)
    palette_path  = os.path.join(UPLOAD_DIR, f"{job_id}_palette.png")
    output_path   = os.path.join(OUTPUT_DIR, f"{job_id}.gif")

    vf_palette = f"fps={GIF_FPS},scale={preset['width']}:-1:flags=lanczos,palettegen"
    vf_use     = f"fps={GIF_FPS},scale={preset['width']}:-1:flags=lanczos[x];[x][1:v]paletteuse"

    palette_cmd = [
        "ffmpeg", "-y",
        "-ss", str(start_sec), "-t", str(duration_sec),
        "-i", input_path, "-vf", vf_palette, palette_path,
    ]
    gif_cmd = [
        "ffmpeg", "-y",
        "-ss", str(start_sec), "-t", str(duration_sec),
        "-i", input_path, "-i", palette_path,
        "-filter_complex", vf_use, output_path,
    ]

    try:
        run_cmd(palette_cmd, "GIF変換（パレット生成）")
        run_cmd(gif_cmd, "GIF変換")
    except FileNotFoundError:
        cleanup_paths(input_path, palette_path)
        return jsonify({"error": FFMPEG_NOT_FOUND_MSG}), 500
    except Exception as e:
        cleanup_paths(input_path, palette_path)
        return jsonify({"error": str(e)}), 500

    cleanup_paths(input_path, palette_path)

    if not os.path.exists(output_path):
        return jsonify({"error": "GIF変換に失敗しました。"}), 500

    result_name = os.path.splitext(video.filename)[0]
    dl_filename = f"{result_name}.gif"
    result_id   = store_result(output_path, dl_filename, "image/gif")

    return jsonify({
        "result_id":     result_id,
        "filename":      dl_filename,
        "original_size": original_size,
        "result_size":   os.path.getsize(output_path),
        "mimetype":      "image/gif",
        "preview_type":  "image",
    })


# ---------------------------------------------------------------------------
# ③ 無音化（任意形式 → MP4）  ※ サーバー側で処理継続
# ---------------------------------------------------------------------------

@app.route("/mute", methods=["POST"])
def mute_video():
    video = request.files.get("video")

    if video is None or video.filename == "":
        return jsonify({"error": "動画ファイルが選択されていません。"}), 400

    try:
        job_id, input_path = save_uploaded_video(video)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

    original_size = os.path.getsize(input_path)
    output_path   = os.path.join(OUTPUT_DIR, f"{job_id}_muted.mp4")
    cmd = [
        "ffmpeg", "-y", "-i", input_path,
        "-c:v", "libx264", "-crf", "18",
        "-preset", "ultrafast",
        "-threads", "1",
        "-an", "-movflags", "+faststart",
        output_path,
    ]

    try:
        run_cmd(cmd, "無音化")
    except FileNotFoundError:
        cleanup_paths(input_path)
        return jsonify({"error": FFMPEG_NOT_FOUND_MSG}), 500
    except Exception as e:
        cleanup_paths(input_path)
        return jsonify({"error": str(e)}), 500

    cleanup_paths(input_path)

    if not os.path.exists(output_path):
        return jsonify({"error": "無音化に失敗しました。"}), 500

    result_name = os.path.splitext(video.filename)[0]
    dl_filename = f"{result_name}_muted.mp4"
    result_id   = store_result(output_path, dl_filename, "video/mp4")

    return jsonify({
        "result_id":     result_id,
        "filename":      dl_filename,
        "original_size": original_size,
        "result_size":   os.path.getsize(output_path),
        "mimetype":      "video/mp4",
        "preview_type":  "video",
    })


if __name__ == "__main__":
    print("=" * 60)
    print(" 動画ツールキットWebアプリを起動します")
    print(" ブラウザで http://127.0.0.1:5000 を開いてください")
    print("=" * 60)
    app.run(debug=True, host="127.0.0.1", port=5000)
