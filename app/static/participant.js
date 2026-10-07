import { Recorder, decodeFile, encodeWav, micSupported, prepareVoice } from "./audio.js";

const $ = (id) => document.getElementById(id);
const views = ["view-login", "view-record", "view-practice"];
let me = null;
let pollTimer = null;
let recorder = null;
let recTimer = null;
let pendingBlob = null;
let currentAudio = null;

function show(view) {
  views.forEach((v) => $(v).classList.toggle("hidden", v !== view));
  $("logout").classList.toggle("hidden", view === "view-login");
}

async function api(path, opts = {}) {
  const res = await fetch(path, { credentials: "same-origin", ...opts });
  if (res.status === 401) {
    me = null;
    show("view-login");
    throw new Error("login");
  }
  if (!res.ok) {
    let msg = "うまく いきませんでした。もういちど ためしてね。";
    try {
      msg = (await res.json()).detail || msg;
    } catch (_) {
      /* keep default */
    }
    throw new Error(typeof msg === "string" ? msg : "エラー");
  }
  return res.json();
}

function logEvent(type, sentence_id = null, detail = "") {
  fetch("/api/events", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ type, sentence_id, detail }),
  }).catch(() => {});
}

// ---- login ----

$("login-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  $("login-error").textContent = "";
  try {
    await api("/api/login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ id: $("login-id").value, passcode: $("login-pass").value }),
    });
    $("login-pass").value = "";
    await load();
  } catch (err) {
    if (err.message !== "login") $("login-error").textContent = err.message;
    else $("login-error").textContent = "ID か あいことば が ちがいます";
  }
});

$("logout").addEventListener("click", async () => {
  stopAudio();
  await fetch("/api/logout", { method: "POST" });
  me = null;
  $("who").textContent = "";
  clearTimeout(pollTimer);
  show("view-login");
});

// ---- load & route ----

async function load() {
  try {
    me = await api("/api/me");
  } catch (err) {
    if (err.message !== "login") console.error(err);
    return;
  }
  $("who").textContent = me.id;
  if (!me.has_reference) {
    openRecorder(false);
  } else if ($("view-record").classList.contains("hidden")) {
    renderPractice();
  }
}

// ---- voice registration ----

function openRecorder(canCancel) {
  $("script").textContent = me.reference_script;
  resetRecorderUI();
  $("rec-cancel").classList.toggle("hidden", !canCancel);
  if (!micSupported()) {
    $("rec-start").disabled = true;
    $("rec-error").textContent =
      "この がめんでは マイクが つかえません。したの「ファイルを えらぶ」を つかってね。";
    document.querySelector("details.upload").open = true;
  }
  show("view-record");
}

function resetRecorderUI() {
  pendingBlob = null;
  $("rec-start").classList.remove("hidden");
  $("rec-start").disabled = false;
  $("rec-stop").classList.add("hidden");
  $("rec-review").classList.add("hidden");
  $("rec-error").textContent = "";
  $("rec-time").textContent = "0.0 びょう";
  $("meter-bar").style.width = "0%";
  $("file-input").value = "";
}

$("rec-cancel").addEventListener("click", () => renderPractice());

$("rec-start").addEventListener("click", async () => {
  $("rec-error").textContent = "";
  recorder = new Recorder((peak) => {
    $("meter-bar").style.width = `${Math.min(100, peak * 140)}%`;
  });
  try {
    await recorder.start();
  } catch (err) {
    console.error(err);
    $("rec-error").textContent =
      "マイクが つかえませんでした。ブラウザの マイクの きょかを たしかめてね。";
    return;
  }
  $("rec-start").classList.add("hidden");
  $("rec-stop").classList.remove("hidden");
  $("rec-review").classList.add("hidden");
  recTimer = setInterval(() => {
    const t = recorder.elapsed();
    $("rec-time").textContent = `${t.toFixed(1)} びょう`;
    if (t > 90) $("rec-stop").click();
  }, 100);
});

$("rec-stop").addEventListener("click", async () => {
  clearInterval(recTimer);
  $("rec-stop").classList.add("hidden");
  const raw = await recorder.stop();
  recorder = null;
  $("meter-bar").style.width = "0%";
  await review(raw);
});

$("file-input").addEventListener("change", async (e) => {
  const file = e.target.files[0];
  if (!file) return;
  $("rec-error").textContent = "";
  try {
    const raw = await decodeFile(file);
    $("rec-start").classList.add("hidden");
    await review(raw);
  } catch (err) {
    console.error(err);
    $("rec-error").textContent = "この ファイルは よめませんでした。WAV か MP3 を ためしてね。";
  }
});

async function review(raw) {
  const voice = await prepareVoice(raw);
  pendingBlob = voice.blob;
  $("rec-audio").src = URL.createObjectURL(voice.blob);
  $("rec-time").textContent = `${voice.duration.toFixed(1)} びょう`;
  const check = $("rec-check");
  check.className = "notice";
  $("rec-send").disabled = false;
  if (voice.duration < 3) {
    check.textContent = "みじかすぎるよ。ぶんを ぜんぶ よんでね。";
    check.className = "notice bad";
    $("rec-send").disabled = true;
  } else if (voice.peak < 0.05 || voice.voiced < 0.2) {
    check.textContent = "こえが ちいさい みたい。もうすこし おおきな こえで よむと いいよ。";
    check.className = "notice warn";
  } else if (voice.duration < 6) {
    check.textContent = "すこし みじかいよ。6びょう いじょう あると もっと よく なるよ。";
    check.className = "notice warn";
  } else {
    check.textContent = "じょうずに ろくおん できたよ！";
    check.className = "notice good";
  }
  $("rec-review").classList.remove("hidden");
}

$("rec-redo").addEventListener("click", resetRecorderUI);

$("rec-send").addEventListener("click", async () => {
  if (!pendingBlob) return;
  $("rec-send").disabled = true;
  $("rec-error").textContent = "";
  const form = new FormData();
  form.append("file", pendingBlob, "voice.wav");
  try {
    await api("/api/reference", { method: "POST", body: form });
    me = await api("/api/me");
    renderPractice();
  } catch (err) {
    $("rec-send").disabled = false;
    if (err.message !== "login") $("rec-error").textContent = err.message;
  }
});

$("re-record").addEventListener("click", () => {
  if (confirm("こえを とりなおすと、いまの おんせいは つくりなおしに なります。いい？")) {
    stopAudio();
    openRecorder(true);
  }
});

// ---- practice ----

const STATUS = {
  done: ["できたよ", "ok"],
  queued: ["つくって いるよ…", "wait"],
  processing: ["つくって いるよ…", "wait"],
  waiting: ["つくって いるよ…", "wait"],
  reviewing: ["じゅんび ちゅう", "wait"],
  failed: ["じゅんび ちゅう", "wait"],
  no_reference: ["", "wait"],
};

function stopAudio() {
  if (currentAudio) {
    currentAudio.pause();
    currentAudio = null;
  }
}

function renderPractice() {
  show("view-practice");
  const list = $("items");
  list.innerHTML = "";
  const tpl = $("item-tpl");
  const pending = me.items.filter((i) => i.status !== "done").length;
  const progress = $("progress");
  if (me.items.length === 0) {
    progress.textContent = "まだ れんしゅうする ぶんが ありません。せんせいの じゅんびを まってね。";
    progress.classList.remove("hidden");
  } else if (pending > 0) {
    const done = me.items.length - pending;
    progress.textContent = `AIの こえを つくって いるよ（${done} / ${me.items.length}）。できた ものから きけるよ。`;
    progress.classList.remove("hidden");
  } else {
    progress.classList.add("hidden");
  }

  for (const item of me.items) {
    const node = tpl.content.firstElementChild.cloneNode(true);
    node.querySelector(".lang").textContent = item.lang === "en" ? "English" : "にほんご";
    node.querySelector(".lang").classList.add(item.lang);
    const [label, cls] = STATUS[item.status] || ["", "wait"];
    const status = node.querySelector(".status");
    status.textContent = label;
    status.classList.add(cls);
    const text = node.querySelector(".text");
    text.textContent = item.text;
    text.lang = item.lang;

    let rate = 1;
    const play = node.querySelector(".play");
    play.disabled = !item.audio_url;
    node.querySelectorAll(".speed button").forEach((b) => {
      b.addEventListener("click", () => {
        rate = Number(b.dataset.rate);
        node.querySelectorAll(".speed button").forEach((x) => x.classList.toggle("on", x === b));
        if (currentAudio && currentAudio.dataset.sid === String(item.sentence_id)) currentAudio.playbackRate = rate;
      });
    });
    play.addEventListener("click", () => {
      stopAudio();
      const audio = new Audio(item.audio_url);
      audio.dataset.sid = String(item.sentence_id);
      audio.playbackRate = rate;
      audio.preservesPitch = true;
      currentAudio = audio;
      play.textContent = "♪ きいて いるよ";
      audio.addEventListener("ended", () => (play.textContent = "▶ もういちど きく"));
      audio.addEventListener("pause", () => (play.textContent = "▶ もういちど きく"));
      audio.play().catch(() => (play.textContent = "▶ きく"));
      logEvent("play", item.sentence_id, `rate=${rate}`);
    });

    setupSelfPractice(node, item);
    list.appendChild(node);
  }

  clearTimeout(pollTimer);
  if (pending > 0) pollTimer = setTimeout(refresh, 5000);
}

// The child's own practice reading stays in this browser tab; it is never uploaded.
function setupSelfPractice(node, item) {
  const recBtn = node.querySelector(".self-rec");
  const playBtn = node.querySelector(".self-play");
  const hint = node.querySelector(".self-hint");
  let rec = null;
  let url = null;
  if (!micSupported()) {
    node.querySelector(".practice").classList.add("hidden");
    return;
  }
  recBtn.addEventListener("click", async () => {
    if (!rec) {
      stopAudio();
      rec = new Recorder();
      try {
        await rec.start();
      } catch (_) {
        rec = null;
        hint.textContent = "マイクが つかえません";
        return;
      }
      recBtn.textContent = "■ とめる";
      recBtn.classList.add("recording");
      hint.textContent = "よんでね…";
    } else {
      const raw = await rec.stop();
      rec = null;
      recBtn.textContent = "🎤 もういちど よむ";
      recBtn.classList.remove("recording");
      if (url) URL.revokeObjectURL(url);
      url = URL.createObjectURL(encodeWav(raw.samples, raw.sampleRate));
      playBtn.classList.remove("hidden");
      hint.textContent = "AIの こえと くらべて みよう";
      logEvent("practice_record", item.sentence_id);
    }
  });
  playBtn.addEventListener("click", () => {
    stopAudio();
    const audio = new Audio(url);
    currentAudio = audio;
    audio.play();
    logEvent("practice_play", item.sentence_id);
  });
}

async function refresh() {
  try {
    const next = await api("/api/me");
    const changed = JSON.stringify(next.items.map((i) => i.status)) !== JSON.stringify(me.items.map((i) => i.status));
    me = next;
    const busy = currentAudio && !currentAudio.paused;
    const recording = document.querySelector(".self-rec.recording");
    if (!$("view-practice").classList.contains("hidden")) {
      if (changed && !busy && !recording) renderPractice();
      else pollTimer = setTimeout(refresh, 5000);
    }
  } catch (_) {
    pollTimer = setTimeout(refresh, 10000);
  }
}

load().then(() => {
  if (!me) show("view-login");
});
