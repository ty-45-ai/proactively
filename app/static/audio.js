// Recording and WAV encoding in the browser, so the server never needs ffmpeg
// and every upload arrives as the same 24 kHz mono 16-bit WAV.

export const TARGET_RATE = 24000;

export function micSupported() {
  return !!(navigator.mediaDevices && navigator.mediaDevices.getUserMedia && window.isSecureContext);
}

export class Recorder {
  constructor(onLevel) {
    this.onLevel = onLevel;
  }

  async start() {
    this.stream = await navigator.mediaDevices.getUserMedia({
      audio: { echoCancellation: false, noiseSuppression: true, autoGainControl: true, channelCount: 1 },
    });
    const Ctx = window.AudioContext || window.webkitAudioContext;
    this.ctx = new Ctx();
    if (this.ctx.state === "suspended") await this.ctx.resume();
    this.source = this.ctx.createMediaStreamSource(this.stream);
    // ScriptProcessor is deprecated but is the one capture path that works on every
    // current Chrome, Edge and Safari without serving a separate worklet file.
    this.proc = this.ctx.createScriptProcessor(4096, 1, 1);
    this.chunks = [];
    this.proc.onaudioprocess = (e) => {
      const data = e.inputBuffer.getChannelData(0);
      this.chunks.push(new Float32Array(data));
      if (this.onLevel) {
        let peak = 0;
        for (let i = 0; i < data.length; i++) peak = Math.max(peak, Math.abs(data[i]));
        this.onLevel(peak);
      }
    };
    this.source.connect(this.proc);
    this.proc.connect(this.ctx.destination);
    this.startedAt = performance.now();
  }

  elapsed() {
    return this.startedAt ? (performance.now() - this.startedAt) / 1000 : 0;
  }

  async stop() {
    this.proc.disconnect();
    this.source.disconnect();
    this.stream.getTracks().forEach((t) => t.stop());
    const sampleRate = this.ctx.sampleRate;
    await this.ctx.close();
    const total = this.chunks.reduce((n, c) => n + c.length, 0);
    const samples = new Float32Array(total);
    let off = 0;
    for (const c of this.chunks) {
      samples.set(c, off);
      off += c.length;
    }
    this.chunks = [];
    this.startedAt = null;
    return { samples, sampleRate };
  }

  cancel() {
    try {
      this.proc && this.proc.disconnect();
      this.source && this.source.disconnect();
      this.stream && this.stream.getTracks().forEach((t) => t.stop());
      this.ctx && this.ctx.close();
    } catch (_) {
      /* already stopped */
    }
  }
}

export async function decodeFile(file) {
  const buf = await file.arrayBuffer();
  const Ctx = window.AudioContext || window.webkitAudioContext;
  const ctx = new Ctx();
  try {
    const audio = await new Promise((resolve, reject) => {
      const p = ctx.decodeAudioData(buf, resolve, reject);
      if (p && p.then) p.then(resolve, reject);
    });
    const n = audio.length;
    const samples = new Float32Array(n);
    for (let ch = 0; ch < audio.numberOfChannels; ch++) {
      const d = audio.getChannelData(ch);
      for (let i = 0; i < n; i++) samples[i] += d[i] / audio.numberOfChannels;
    }
    return { samples, sampleRate: audio.sampleRate };
  } finally {
    ctx.close();
  }
}

export async function resample(samples, fromRate, toRate = TARGET_RATE) {
  if (fromRate === toRate || samples.length === 0) return samples;
  const length = Math.max(1, Math.round((samples.length * toRate) / fromRate));
  const Offline = window.OfflineAudioContext || window.webkitOfflineAudioContext;
  const ctx = new Offline(1, length, toRate);
  const buffer = ctx.createBuffer(1, samples.length, fromRate);
  buffer.getChannelData(0).set(samples);
  const src = ctx.createBufferSource();
  src.buffer = buffer;
  src.connect(ctx.destination);
  src.start();
  const rendered = await ctx.startRendering();
  return rendered.getChannelData(0).slice();
}

// Drop leading/trailing silence so a slow start does not count as voice.
export function trimSilence(samples, sampleRate, threshold = 0.02) {
  const pad = Math.round(sampleRate * 0.2);
  let start = 0;
  let end = samples.length - 1;
  while (start < samples.length && Math.abs(samples[start]) < threshold) start++;
  while (end > start && Math.abs(samples[end]) < threshold) end--;
  if (start >= end) return new Float32Array(0);
  return samples.slice(Math.max(0, start - pad), Math.min(samples.length, end + pad));
}

export function analyze(samples, sampleRate) {
  let peak = 0;
  let loudFrames = 0;
  const frame = Math.round(sampleRate * 0.03);
  let frames = 0;
  for (let i = 0; i < samples.length; i += frame) {
    let sum = 0;
    const end = Math.min(samples.length, i + frame);
    for (let j = i; j < end; j++) {
      const v = Math.abs(samples[j]);
      if (v > peak) peak = v;
      sum += samples[j] * samples[j];
    }
    const rms = Math.sqrt(sum / Math.max(1, end - i));
    if (rms > 0.02) loudFrames++;
    frames++;
  }
  return {
    duration: samples.length / sampleRate,
    peak,
    voiced: frames ? loudFrames / frames : 0,
  };
}

export function encodeWav(samples, sampleRate) {
  const buffer = new ArrayBuffer(44 + samples.length * 2);
  const view = new DataView(buffer);
  const str = (o, s) => [...s].forEach((c, i) => view.setUint8(o + i, c.charCodeAt(0)));
  str(0, "RIFF");
  view.setUint32(4, 36 + samples.length * 2, true);
  str(8, "WAVE");
  str(12, "fmt ");
  view.setUint32(16, 16, true);
  view.setUint16(20, 1, true);
  view.setUint16(22, 1, true);
  view.setUint32(24, sampleRate, true);
  view.setUint32(28, sampleRate * 2, true);
  view.setUint16(32, 2, true);
  view.setUint16(34, 16, true);
  str(36, "data");
  view.setUint32(40, samples.length * 2, true);
  for (let i = 0; i < samples.length; i++) {
    const s = Math.max(-1, Math.min(1, samples[i]));
    view.setInt16(44 + i * 2, s < 0 ? s * 0x8000 : s * 0x7fff, true);
  }
  return new Blob([buffer], { type: "audio/wav" });
}

// Turn any capture or file into the WAV the server expects, and report on its quality.
export async function prepareVoice({ samples, sampleRate }, maxSeconds = 60) {
  let s = trimSilence(samples, sampleRate);
  s = await resample(s, sampleRate, TARGET_RATE);
  if (s.length > maxSeconds * TARGET_RATE) s = s.slice(0, maxSeconds * TARGET_RATE);
  const info = analyze(s, TARGET_RATE);
  return { blob: encodeWav(s, TARGET_RATE), ...info };
}
