/**
 * ffmpeg-compress.js  v2.0
 * FFmpeg.wasm v0.11.x を使ったブラウザ側動画圧縮モジュール。
 *
 * ── バージョン選択の理由 ──
 *   v0.11: SharedArrayBuffer が必須でないシングルスレッド版。
 *          Cross-Origin-Isolated ヘッダーなしでも動作するため互換性が高い。
 *          ただし COOP/COEP ヘッダーが付与されていれば自動的にマルチスレッドに昇格する。
 *
 * ── 画質モード ──
 *   'speed'    : ultrafast preset / ビットレート制御のみ。最速・最軽量。
 *   'balanced' : fast preset / CRF+maxrate ハイブリッド。速度と画質の中間。
 *   'quality'  : medium preset / 目標MBに応じて解像度を自動調整。最高画質。
 */

const FFMPEG_CDN = 'https://unpkg.com/@ffmpeg/ffmpeg@0.11.6/dist/ffmpeg.min.js';

// ── ビットレートが低すぎる際に自動ダウンスケールするしきい値 ──
// 解像度ごとの「最低推奨ビットレート（kbps）」。
// ビットレートがこれを下回ったら次の解像度に下げてピクセル密度を上げる。
const MIN_BITRATE_FOR_RESOLUTION = {
  original: 2000,  // 解像度不明なので高め
  '1080':   1800,
  '720':     900,
  '480':     400,
  '360':     200,
};

// CRF の推奨値（低いほど高画質・ファイルサイズ大）
const CRF_BY_MODE = {
  speed:    28,
  balanced: 24,
  quality:  22,
};

// preset（低いほど高画質・低速・CPU/メモリ使用量大）
// wasm では 'medium' 以上は極端に遅くなるため 'fast' が現実的な上限
const PRESET_BY_MODE = {
  speed:    'ultrafast',
  balanced: 'fast',
  quality:  'fast',   // wasm で 'medium' は非現実的なため fast を上限とする
};

class BrowserVideoCompressor {
  constructor() {
    this._ffmpeg     = null;
    this._loaded     = false;
    this._loading    = false;
    this._onProgress = null;
    this._fetchFile  = null;
  }

  /**
   * FFmpeg.wasm をロードする（初回のみネットワーク取得、以降はキャッシュ）
   */
  async load(onProgress = () => {}) {
    if (this._loaded) return;
    if (this._loading) {
      await new Promise(r => {
        const t = setInterval(() => { if (!this._loading) { clearInterval(t); r(); } }, 100);
      });
      return;
    }
    this._loading    = true;
    this._onProgress = onProgress;

    try {
      if (typeof window.FFmpeg === 'undefined') {
        await loadScript(FFMPEG_CDN);
      }
      const { createFFmpeg, fetchFile } = window.FFmpeg;
      this._fetchFile = fetchFile;
      this._ffmpeg = createFFmpeg({
        log: false,
        progress: ({ ratio, time }) => {
          if (this._onProgress) this._onProgress({ ratio: Math.min(ratio, 1), time });
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
   * @param {File}   file  - 入力ファイル
   * @param {object} opts
   *   opts.targetMB     {number}   目標ファイルサイズ（MB）
   *   opts.resolution   {string}   'original' | '1080' | '720' | '480'（ユーザー指定）
   *   opts.qualityMode  {string}   'speed' | 'balanced' | 'quality'
   *   opts.trimStart    {number}   切り出し開始秒（0 = 先頭から）
   *   opts.trimDuration {number}   切り出し長さ秒（0 = 全体）
   *   opts.onProgress   {function} ({ ratio, time, phase, info }) コールバック
   * @returns {Promise<{blob, originalSize, resultSize, filename, encodeInfo}>}
   */
  async compress(file, opts = {}) {
    if (!this._loaded) await this.load(opts.onProgress || (() => {}));

    const {
      targetMB     = 25,
      resolution   = 'original',
      qualityMode  = 'balanced',
      trimStart    = 0,
      trimDuration = 0,
      onProgress   = () => {},
    } = opts;

    this._onProgress = ({ ratio, time }) => onProgress({ ratio, time, phase: 'encode' });

    const ffmpeg    = this._ffmpeg;
    const inputName  = 'input' + getExt(file.name);
    const outputName = 'output.mp4';

    // ── 仮想FSへ書き込み ──
    onProgress({ ratio: 0, time: 0, phase: 'loading', info: 'ファイルを読み込み中...' });
    ffmpeg.FS('writeFile', inputName, await this._fetchFile(file));

    // ── 動画の長さを取得 ──
    const totalDuration  = await getVideoDuration(file);
    const encodeDuration = trimDuration > 0
      ? trimDuration
      : Math.max(0.1, totalDuration - trimStart);

    // ── ビットレート・解像度を計算 ──
    const { videoBitrateKbps, audioBitrateKbps, finalResolution, scaleFilter } =
      calcEncodeParams({ targetMB, encodeDuration, resolution, qualityMode });

    onProgress({
      ratio: 0.01, time: 0, phase: 'encode',
      info: `解像度: ${finalResolution}  映像: ${videoBitrateKbps}kbps  音声: ${audioBitrateKbps}kbps`,
    });

    // ── FFmpeg コマンド構築 ──
    const args = buildFFmpegArgs({
      inputName, outputName,
      trimStart, trimDuration,
      videoBitrateKbps, audioBitrateKbps,
      scaleFilter, qualityMode,
    });

    // ── エンコード実行 ──
    await ffmpeg.run(...args);

    // ── 出力読み出し ──
    const data = ffmpeg.FS('readFile', outputName);
    const blob = new Blob([data.buffer], { type: 'video/mp4' });

    // ── 仮想FS クリーンアップ ──
    try { ffmpeg.FS('unlink', inputName); }  catch (_) {}
    try { ffmpeg.FS('unlink', outputName); } catch (_) {}

    const baseName = file.name.replace(/\.[^.]+$/, '');
    return {
      blob,
      originalSize: file.size,
      resultSize:   blob.size,
      filename:     `${baseName}_compressed.mp4`,
      encodeInfo: {
        resolution:   finalResolution,
        videoBitrateKbps,
        audioBitrateKbps,
        qualityMode,
        encodeDurationSec: Math.round(encodeDuration),
      },
    };
  }

  unload() {
    if (this._ffmpeg && this._loaded) {
      try { this._ffmpeg.exit(); } catch (_) {}
    }
    this._ffmpeg  = null;
    this._loaded  = false;
    this._loading = false;
  }
}

// ===========================================================================
// エンコードパラメータ計算
// ===========================================================================

/**
 * 目標MB・長さ・ユーザー指定解像度・画質モードから
 * 最適なビットレートと解像度を決定する。
 *
 * 【自動解像度ダウンスケールのロジック】
 * 1. ユーザー指定解像度でビットレートを計算
 * 2. そのビットレートが最低推奨値を下回る場合、1段階解像度を下げて再計算
 * 3. qualityMode='quality' の場合はより積極的にダウンスケールする
 */
function calcEncodeParams({ targetMB, encodeDuration, resolution, qualityMode }) {
  const audioBitrateKbps = qualityMode === 'quality' ? 160 : 128;

  // 目標ビットレート計算（0.96 = コンテナオーバーヘッドマージン）
  const targetBits    = targetMB * 1024 * 1024 * 8;
  const totalKbps     = (targetBits / encodeDuration / 1000) * 0.96;
  let   videoBitrateKbps = Math.max(80, totalKbps - audioBitrateKbps);

  // 解像度候補リスト（ユーザー指定を上限として、それ以下のみ許容）
  const ALL_RESOLUTIONS = ['original', '1080', '720', '480', '360'];
  const startIdx = ALL_RESOLUTIONS.indexOf(resolution);
  const candidates = ALL_RESOLUTIONS.slice(Math.max(0, startIdx));

  // quality モードは自動ダウンスケールを積極的に使う
  // balanced / speed モードではユーザー指定を最大限尊重
  const autoScaleThreshold = qualityMode === 'quality'    ? 1.0  // 積極的ダウンスケール
                           : qualityMode === 'balanced'   ? 0.75 // 軽度
                           :                               0.5;  // speed: 半分を下回るまで維持

  let finalResolution = candidates[0];
  for (const res of candidates) {
    const minBr = MIN_BITRATE_FOR_RESOLUTION[res] ?? 200;
    if (videoBitrateKbps >= minBr * autoScaleThreshold) {
      finalResolution = res;
      break;
    }
    finalResolution = res; // 最後まで達したら最小解像度を使う
  }

  // quality モードでは CRF を使うため、ビットレートは maxrate として機能させる
  // maxrate は計算値の 1.5 倍まで許容して瞬間的な高ビットレートを確保
  if (qualityMode === 'quality') {
    videoBitrateKbps = Math.max(150, videoBitrateKbps);
  }

  const scaleFilter = finalResolution !== 'original' ? `scale=-2:${finalResolution}` : null;

  return { videoBitrateKbps: Math.round(videoBitrateKbps), audioBitrateKbps, finalResolution, scaleFilter };
}

/**
 * FFmpeg コマンド引数を組み立てる。
 *
 * 【エンコード方式の違い】
 * speed    : -b:v のみ（CBR 相当）。速いが複雑なシーンでブロックノイズが出やすい。
 * balanced : -crf + -maxrate + -bufsize のハイブリッド（VBR）。品質が安定。
 * quality  : balanced と同じVBR。preset が少し良いためブロックノイズが減る。
 */
function buildFFmpegArgs({
  inputName, outputName,
  trimStart, trimDuration,
  videoBitrateKbps, audioBitrateKbps,
  scaleFilter, qualityMode,
}) {
  const args = [];

  // トリミング（-ss を -i の前に置いて高速シーク）
  if (trimStart > 0.01)    args.push('-ss', String(trimStart));
  if (trimDuration > 0.01) args.push('-t', String(trimDuration));

  args.push('-i', inputName);

  // ビデオフィルター（解像度変換）
  if (scaleFilter) {
    args.push('-vf', `${scaleFilter},format=yuv420p`);
  } else {
    args.push('-vf', 'format=yuv420p');
  }

  // エンコーダ共通
  args.push('-c:v', 'libx264');

  if (qualityMode === 'speed') {
    // ── 速度優先：固定ビットレートのみ ──
    args.push(
      '-preset', PRESET_BY_MODE.speed,
      '-b:v', `${videoBitrateKbps}k`,
      '-tune', 'fastdecode',
    );
  } else {
    // ── balanced / quality：CRF + maxrate ハイブリッド ──
    const crf     = CRF_BY_MODE[qualityMode];
    const maxrate = Math.round(videoBitrateKbps * 1.5);  // 瞬間的な高ビットレートを許容
    const bufsize = Math.round(videoBitrateKbps * 2.0);  // バッファサイズ（maxrate の約1.5倍）
    args.push(
      '-preset', PRESET_BY_MODE[qualityMode],
      '-crf', String(crf),
      '-maxrate', `${maxrate}k`,
      '-bufsize', `${bufsize}k`,
      '-b:v', `${videoBitrateKbps}k`,    // 目標ビットレート（CRFと併用でサイズ制御）
    );
  }

  args.push(
    '-c:a', 'aac',
    '-b:a', `${audioBitrateKbps}k`,
    '-movflags', '+faststart',
    outputName,
  );

  return args;
}

// ===========================================================================
// ユーティリティ
// ===========================================================================

function getExt(filename) {
  const i = filename.lastIndexOf('.');
  return i >= 0 ? filename.slice(i).toLowerCase() : '.mp4';
}

function loadScript(src) {
  return new Promise((resolve, reject) => {
    const s = document.createElement('script');
    s.src = src;
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

/**
 * 現在のビットレートと解像度の組み合わせから
 * ユーザーへの説明テキストを生成する（UI の補足表示に使用）
 */
window.getEncodeHint = function(targetMB, durationSec, resolution, qualityMode) {
  if (!durationSec || !targetMB) return '';
  const { videoBitrateKbps, finalResolution } = calcEncodeParams({
    targetMB, encodeDuration: durationSec, resolution, qualityMode,
  });
  const autoScaled = finalResolution !== resolution && resolution !== 'original';
  let hint = `映像: 約${videoBitrateKbps}kbps`;
  if (finalResolution !== 'original') hint += ` / ${finalResolution}p`;
  if (autoScaled) hint += ' （画質維持のため自動リサイズ）';
  return hint;
};

// グローバルに公開
window.BrowserVideoCompressor = BrowserVideoCompressor;
