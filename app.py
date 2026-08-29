#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
app.py — 動画ツールキットWebアプリ（Flask + FFmpeg）

機能:
  - 動画圧縮       : /compress       指定範囲・解像度・目標サイズで圧縮
  - 音声抽出(MP3)  : /extract-audio  音声トラックだけをMP3で抽出
  - GIF変換        : /to-gif         指定範囲をアニメーションGIFに変換
  - 無音化         : /mute           音声だけを取り除いたMP4を出力
  - ファイル取得   : /result/<id>    処理済みファイルをダウンロード
  - プレビュー     : /preview/<id>   処理済みファイルをインライン表示

対応入力形式:
  mp4, mov, avi, mkv, webm, wmv, flv, m4v, ts, mts, m2ts, 3gp, ogv

必要なもの:
  - Python 3.x
  - Flask  (pip install flask)
  - FFmpeg / FFprobe がインストールされ、PATHが通っていること

起動方法:
  1) pip install flask
  2) python app.py
  3) ブラウザで http://127.0.0.1:5000 を開く
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
)

BASE_DIR   = os.path.dirname(os.path.abspath(__file__))
UPLOAD_DIR = os.path.join(BASE_DIR, "uploads")
OUTPUT_DIR = os.path.join(BASE_DIR, "outputs")
os.makedirs(UPLOAD_DIR, exist_ok=True)
os.makedirs(OUTPUT_DIR, exist_ok=True)

# 受け付ける入力拡張子（すべて小文字で定義）
ALLOWED_EXTENSIONS = {
    ".mp4", ".mov", ".avi", ".mkv", ".webm",
    ".wmv", ".flv", ".m4v",
    ".ts", ".mts", ".m2ts",
    ".3gp",
    ".ogv",
}

# 処理済み一時ファイルの管理
# { result_id: { "path": str, "filename": str, "mimetype": str,
#                "size": int, "created_at": float } }
_result_store: dict = {}
_store_lock = threading.Lock()
RESULT_TTL_SEC = 180  # 3分でクリーンアップ（Render 無料プランのストレージ節約）

app = Flask(__name__)
# Render 無料プラン（メモリ 512MB）に合わせてアップロード上限を 512MB に設定。
# それ以上の大きなファイルはアップロード段階で 413 エラーを返し OOM を防ぐ。
app.config["MAX_CONTENT_LENGTH"] = 512 * 1024 * 1024  # 512MB


# ---------------------------------------------------------------------------
# 一時ファイルストア管理
# ---------------------------------------------------------------------------

def store_result(path: str, filename: str, mimetype: str) -> str:
    """処理済みファイルをストアに登録して result_id を返す"""
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
    """TTL を超えた一時ファイルを削除する（バックグラウンドスレッドから呼ぶ）"""
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


def cleanup_paths(*paths):
    for path in paths:
        _safe_remove(path)


def _purge_temp_dirs():
    """
    起動時にアップロード・出力ディレクトリの残留ファイルをすべて削除する。
    Render では再デプロイ後も同じコンテナが再利用される場合があり、
    前回の処理ファイルが残ってストレージを圧迫するのを防ぐ。
    """
    for d in (UPLOAD_DIR, OUTPUT_DIR):
        if not os.path.isdir(d):
            continue
        for fname in os.listdir(d):
            _safe_remove(os.path.join(d, fname))


_purge_temp_dirs()


def run_cmd(cmd, error_label):
    """
    FFmpeg コマンドを実行する。
    subprocess.run(capture_output=True) は gunicorn の SIGALRM と干渉して
    selectors.select() 内で SystemExit が発生することがある。
    Popen + communicate() に変更することで gunicorn のシグナルハンドラーから
    独立した形でプロセス完了を待機し、この問題を回避する。
    """
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    stdout_bytes, stderr_bytes = proc.communicate()
    if proc.returncode != 0:
        stderr_text = stderr_bytes.decode("utf-8", errors="replace")[-2000:]
        raise RuntimeError(f"{error_label}に失敗しました:\n{stderr_text}")
    # 呼び出し元が result.stderr を参照しないため戻り値は簡易オブジェクトで代替
    class _Result:
        returncode = proc.returncode
        stderr = stderr_bytes.decode("utf-8", errors="replace")
        stdout = stdout_bytes.decode("utf-8", errors="replace")
    return _Result()


def parse_trim_params(form):
    """(start_sec, duration_sec | None) を返す"""
    try:
        start_sec = max(0.0, float(form.get("trim_start", 0)))
    except (TypeError, ValueError):
        start_sec = 0.0
    try:
        dur = float(form.get("trim_duration", 0))
        duration_sec = dur if dur > 0 else None
    except (TypeError, ValueError):
        duration_sec = None
    return start_sec, duration_sec


def parse_resolution(form) -> str | None:
    """
    フォームの resolution フィールドを解析して FFmpeg の scale フィルタ文字列を返す。
    'original' または未指定の場合は None。
    戻り値例: "scale=-2:720"
    """
    res = form.get("resolution", "original")
    mapping = {
        "1080": "scale=-2:1080",
        "720":  "scale=-2:720",
        "480":  "scale=-2:480",
    }
    return mapping.get(res)


FFMPEG_NOT_FOUND_MSG = (
    "ffmpeg / ffprobe が見つかりません。インストールしてPATHを通してください。"
)


# ---------------------------------------------------------------------------
# 動画情報取得
# ---------------------------------------------------------------------------

def get_duration_seconds(input_path: str) -> float:
    cmd = [
        "ffprobe", "-v", "error",
        "-show_entries", "format=duration",
        "-of", "json",
        input_path,
    ]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    stdout_bytes, stderr_bytes = proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(f"動画情報の取得に失敗しました: {stderr_bytes.decode('utf-8', errors='replace')}")
    data = json.loads(stdout_bytes.decode("utf-8", errors="replace"))
    return float(data["format"]["duration"])


# ---------------------------------------------------------------------------
# 動画圧縮ロジック（2-pass エンコード）
# ---------------------------------------------------------------------------

def calc_bitrates(target_mb: float, duration_sec: float,
                  audio_bitrate_kbps: int = 128,
                  min_video_bitrate_kbps: int = 100):
    target_bits        = target_mb * 1024 * 1024 * 8
    total_kbps         = target_bits / duration_sec / 1000 * 0.98
    video_bitrate_kbps = max(min_video_bitrate_kbps, total_kbps - audio_bitrate_kbps)
    return total_kbps, video_bitrate_kbps


def run_ffmpeg_2pass(input_path: str, output_path: str,
                     video_bitrate_kbps: float, audio_bitrate_kbps: int,
                     passlog_prefix: str,
                     trim_start: float = 0.0,
                     trim_duration: float | None = None,
                     scale_filter: str | None = None):
    """
    2-pass エンコード。
    scale_filter: 例 "scale=-2:720"。None の場合はリサイズなし。
    """
    vbr      = f"{video_bitrate_kbps:.0f}k"
    abr      = f"{audio_bitrate_kbps}k"
    null_dev = "NUL" if os.name == "nt" else "/dev/null"

    trim_opts = []
    if trim_start > 0:
        trim_opts += ["-ss", str(trim_start)]
    if trim_duration is not None:
        trim_opts += ["-t", str(trim_duration)]

    # vf オプション（スケール指定がある場合のみ付与）
    vf_opts = ["-vf", scale_filter] if scale_filter else []

    pass1_cmd = (
        ["ffmpeg", "-y"]
        + trim_opts
        + ["-i", input_path]
        + vf_opts
        + ["-c:v", "libx264", "-b:v", vbr,
           "-preset", "ultrafast",   # CPU/メモリ負荷を最小化（Render 無料プラン対応）
           "-threads", "1",          # FFmpeg のスレッド数を 1 に制限して RSS を抑制
           "-pass", "1", "-passlogfile", passlog_prefix,
           "-an", "-f", "mp4", null_dev]
    )
    pass2_cmd = (
        ["ffmpeg", "-y"]
        + trim_opts
        + ["-i", input_path]
        + vf_opts
        + ["-c:v", "libx264", "-b:v", vbr,
           "-preset", "ultrafast",   # 1pass と同じプリセットで一貫性を保つ
           "-threads", "1",
           "-pass", "2", "-passlogfile", passlog_prefix,
           "-c:a", "aac", "-b:a", abr,
           "-movflags", "+faststart",
           output_path]
    )

    run_cmd(pass1_cmd, "1passエンコード")
    run_cmd(pass2_cmd, "2passエンコード")
    cleanup_paths(passlog_prefix + "-0.log", passlog_prefix + "-0.log.mbtree")


# ---------------------------------------------------------------------------
# GIF 変換の画質プリセット
# ---------------------------------------------------------------------------

GIF_FPS = 15

GIF_QUALITY_PRESETS = {
    "low":    {"width": 320},
    "medium": {"width": 480},
    "high":   {"width": 640},
}


# ---------------------------------------------------------------------------
# ルーティング（画面）
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    return render_template("index.html")


# ---------------------------------------------------------------------------
# 処理済みファイルの取得 / プレビュー
# ---------------------------------------------------------------------------

@app.route("/result/<result_id>")
def get_result(result_id: str):
    """添付ダウンロード"""
    with _store_lock:
        entry = _result_store.get(result_id)
    if not entry or not os.path.exists(entry["path"]):
        return jsonify({"error": "ファイルが見つかりません（期限切れの可能性があります）。"}), 404
    return send_file(
        entry["path"],
        as_attachment=True,
        download_name=entry["filename"],
        mimetype=entry["mimetype"],
    )


@app.route("/preview/<result_id>")
def preview_result(result_id: str):
    """インライン表示（プレビュープレイヤー用）"""
    with _store_lock:
        entry = _result_store.get(result_id)
    if not entry or not os.path.exists(entry["path"]):
        return jsonify({"error": "ファイルが見つかりません。"}), 404
    return send_file(
        entry["path"],
        as_attachment=False,
        mimetype=entry["mimetype"],
    )


# ---------------------------------------------------------------------------
# ① 動画圧縮（トリミング・解像度変換・任意形式 → MP4）
# ---------------------------------------------------------------------------

@app.route("/compress", methods=["POST"])
def compress():
    video         = request.files.get("video")
    target_mb_raw = request.form.get("target_mb")

    if video is None or video.filename == "":
        return jsonify({"error": "動画ファイルが選択されていません。"}), 400

    try:
        target_mb = float(target_mb_raw)
        if target_mb <= 0:
            raise ValueError
    except (TypeError, ValueError):
        return jsonify({"error": "目標サイズが正しくありません。"}), 400

    trim_start, trim_duration = parse_trim_params(request.form)
    scale_filter              = parse_resolution(request.form)

    original_size = video.stream.seek(0, 2)  # ストリーム末尾でサイズ取得
    video.stream.seek(0)

    try:
        job_id, input_path = save_uploaded_video(video)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

    # save後に実ファイルサイズを取得
    original_size = os.path.getsize(input_path)

    output_path    = os.path.join(OUTPUT_DIR, f"{job_id}_compressed.mp4")
    passlog_prefix = os.path.join(UPLOAD_DIR,  f"{job_id}_2pass")

    try:
        total_duration = get_duration_seconds(input_path)
        if total_duration <= 0:
            raise RuntimeError("動画の長さを取得できませんでした。")

        encode_duration = (
            trim_duration if trim_duration is not None
            else max(0.1, total_duration - trim_start)
        )
        _, video_bitrate_kbps = calc_bitrates(target_mb, encode_duration)

        run_ffmpeg_2pass(
            input_path=input_path,
            output_path=output_path,
            video_bitrate_kbps=video_bitrate_kbps,
            audio_bitrate_kbps=128,
            passlog_prefix=passlog_prefix,
            trim_start=trim_start,
            trim_duration=trim_duration,
            scale_filter=scale_filter,
        )
    except FileNotFoundError:
        cleanup_paths(input_path)
        return jsonify({"error": FFMPEG_NOT_FOUND_MSG}), 500
    except Exception as e:
        cleanup_paths(input_path)
        return jsonify({"error": str(e)}), 500

    cleanup_paths(input_path)

    if not os.path.exists(output_path):
        return jsonify({"error": "圧縮に失敗しました。出力ファイルが生成されませんでした。"}), 500

    result_name = os.path.splitext(video.filename)[0]
    dl_filename = f"{result_name}_compressed.mp4"
    result_id   = store_result(output_path, dl_filename, "video/mp4")

    return jsonify({
        "result_id":     result_id,
        "filename":      dl_filename,
        "original_size": original_size,
        "result_size":   os.path.getsize(output_path),
        "mimetype":      "video/mp4",
        "preview_type":  "video",
    })


# ---------------------------------------------------------------------------
# ② 音声抽出（任意形式 → MP3）
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
# ③ GIF 変換（任意形式 → GIF）
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
# ④ 無音化（任意形式 → MP4）
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
        "-preset", "ultrafast",  # Render 無料プラン対応：CPU/メモリ負荷を最小化
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
