/**
 * ffmpeg-compress.js
 * FFmpeg.wasm v0.11.x を使ったブラウザ側動画圧縮モジュール。
 *
 * v0.11 を選んだ理由:
 *   - v0.12+ は SharedArrayBuffer が必須（Cross-Origin-Isolated ヘッダー必要）
 *   - Render 無料プランはカスタムレスポンスヘッダー非対応
 *   - v0.11 は SharedArrayBuffer なしで動作し、Render 無料プランと相性が良い
 *
 * 使い方:
 *   const compressor = new BrowserVideoCompressor();
 *   await compressor.load(onProgress);   // FFmpeg.wasm をロード
 *   const result = await compressor.compress(file, options);
 *   // result: { blob, originalSize, resultSize, filename }
 */

const FFMPEG_CDN = 'https://unpkg.com/@ffmpeg/ffmpeg@0.11.6/dist/ffmpeg.min.js';

class BrowserVideoCompressor {
  constructor() {
    this._ffmpeg    = null;
    this._loaded    = false;
    this._loading   = false;
    this._onProgress = null;
  }

  /**
   * FFmpeg.wasm をロードする（初回のみネットワーク取得、以降はキャッシュ）
   * @param {function} onProgress - ({ratio, time}) を受け取る進捗コールバック
   */
  async load(onProgress = () => {}) {
    if (this._loaded) return;
    if (this._loading) {
      // 並行呼び出し時は完了を待つ
      await new Promise(r => { const t = setInterval(() => { if (!this._loading) { clearInterval(t); r(); } }, 100); });
      return;
    }
    this._loading = true;
    this._onProgress = onProgress;

    try {
      // CDN から ffmpeg.js を動的ロード（まだ読み込まれていない場合のみ）
      if (typeof window.FFmpeg === 'undefined') {
        await loadScript(FFMPEG_CDN);
      }

      const { createFFmpeg, fetchFile } = window.FFmpeg;
      this._fetchFile = fetchFile;

      this._ffmpeg = createFFmpeg({
        log: false,
        progress: ({ ratio, time }) => {
          this._onProgress({ ratio: Math.min(ratio, 1), time });
        },
      });

      await this._ffmpeg.load();
      this._loaded  = true;
      this._loading = false;
    } catch (err) {
      this._loading = false;
      throw new Error(`FFmpeg.wasm のロードに失敗しました: ${err.message}`);
    }
  }

  /**
   * 動画を圧縮する
   *
   * @param {File}   file        - 入力ファイル
   * @param {object} opts
   *   @param {number}  opts.targetMB     - 目標ファイルサイズ（MB）
   *   @param {string}  opts.resolution   - 'original' | '1080' | '720' | '480'
   *   @param {number}  opts.trimStart    - 切り出し開始秒（0 = 先頭から）
   *   @param {number}  opts.trimDuration - 切り出し長さ秒（0 = 全体）
   *   @param {function} opts.onProgress  - ({ ratio, time, phase }) 進捗コールバック
   * @returns {Promise<{blob, originalSize, resultSize, filename}>}
   */
  async compress(file, opts = {}) {
    if (!this._loaded) await this.load(opts.onProgress || (() => {}));

    const {
      targetMB     = 25,
      resolution   = 'original',
      trimStart    = 0,
      trimDuration = 0,
      onProgress   = () => {},
    } = opts;

    // 進捗コールバックを更新
    this._onProgress = ({ ratio, time }) => onProgress({ ratio, time, phase: 'encode' });

    const ffmpeg = this._ffmpeg;
    const inputName  = 'input' + getExt(file.name);
    const outputName = 'output.mp4';

    // ── ファイルを wasm の仮想FS に書き込む ──
    onProgress({ ratio: 0, time: 0, phase: 'loading' });
    ffmpeg.FS('writeFile', inputName, await this._fetchFile(file));

    // ── 動画の長さを取得（ffprobe 相当を JS で代替）──
    // v0.11 に ffprobe は含まれないため、<video> タグ経由で取得する
    const totalDuration = await getVideoDuration(file);
    const encodeDuration = trimDuration > 0
      ? trimDuration
      : Math.max(0.1, totalDuration - trimStart);

    // ── ビットレート計算 ──
    // 目標サイズ(bit) = targetMB * 1024 * 1024 * 8
    // 全体ビットレート(kbps) = 目標サイズ / 秒数 / 1000 * 0.98（マージン）
    const audioBitrateKbps = 128;
    const targetBits       = targetMB * 1024 * 1024 * 8;
    const totalKbps        = (targetBits / encodeDuration / 1000) * 0.98;
    const videoBitrateKbps = Math.max(100, totalKbps - audioBitrateKbps);

    // ── FFmpeg コマンド構築 ──
    const args = [];

    // トリミング（-ss を -i の前に置いて高速シーク）
    if (trimStart > 0.01)    args.push('-ss', String(trimStart));
    if (trimDuration > 0.01) args.push('-t', String(trimDuration));

    args.push('-i', inputName);

    // 解像度
    const scaleMap = { '1080': 'scale=-2:1080', '720': 'scale=-2:720', '480': 'scale=-2:480' };
    if (scaleMap[resolution]) args.push('-vf', scaleMap[resolution]);

    args.push(
      '-c:v', 'libx264',
      '-b:v', `${Math.round(videoBitrateKbps)}k`,
      '-preset', 'ultrafast',   // wasm では速度優先が現実的
      '-c:a', 'aac',
      '-b:a', `${audioBitrateKbps}k`,
      '-movflags', '+faststart',
      outputName,
    );

    // ── エンコード実行 ──
    onProgress({ ratio: 0.01, time: 0, phase: 'encode' });
    await ffmpeg.run(...args);

    // ── 出力ファイルを読み出し ──
    const data = ffmpeg.FS('readFile', outputName);
    const blob = new Blob([data.buffer], { type: 'video/mp4' });

    // ── 仮想FS をクリーンアップ（メモリ解放）──
    try { ffmpeg.FS('unlink', inputName); }  catch (_) {}
    try { ffmpeg.FS('unlink', outputName); } catch (_) {}

    const baseName = file.name.replace(/\.[^.]+$/, '');
    return {
      blob,
      originalSize: file.size,
      resultSize:   blob.size,
      filename:     `${baseName}_compressed.mp4`,
    };
  }

  /** FFmpeg インスタンスをアンロードしてメモリを解放する */
  unload() {
    if (this._ffmpeg && this._loaded) {
      try { this._ffmpeg.exit(); } catch (_) {}
    }
    this._ffmpeg  = null;
    this._loaded  = false;
    this._loading = false;
  }
}

// ---------------------------------------------------------------------------
// ユーティリティ
// ---------------------------------------------------------------------------

function getExt(filename) {
  const i = filename.lastIndexOf('.');
  return i >= 0 ? filename.slice(i).toLowerCase() : '.mp4';
}

function loadScript(src) {
  return new Promise((resolve, reject) => {
    const s = document.createElement('script');
    s.src = src;
    // COEP: credentialless 環境では crossOrigin を設定することで
    // ブラウザが匿名リクエストとして扱い、ブロックを回避できる
    s.crossOrigin = 'anonymous';
    s.onload  = resolve;
    s.onerror = () => reject(new Error(`スクリプトの読み込みに失敗しました: ${src}`));
    document.head.appendChild(s);
  });
}

function getVideoDuration(file) {
  return new Promise(resolve => {
    const url = URL.createObjectURL(file);
    const v   = document.createElement('video');
    v.preload = 'metadata';
    v.onloadedmetadata = () => {
      URL.revokeObjectURL(url);
      resolve(isFinite(v.duration) && v.duration > 0 ? v.duration : 60);
    };
    v.onerror = () => { URL.revokeObjectURL(url); resolve(60); };
    v.src = url;
  });
}

// グローバルに公開
window.BrowserVideoCompressor = BrowserVideoCompressor;
