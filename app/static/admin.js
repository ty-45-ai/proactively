const $ = (id) => document.getElementById(id);

// Build DOM nodes without innerHTML so participant data is never parsed as HTML.
function h(tag, attrs = {}, ...children) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else if (k === "class") el.className = v;
    else if (v !== false && v != null) el.setAttribute(k, v === true ? "" : v);
  }
  for (const c of children.flat()) {
    if (c == null || c === false) continue;
    el.append(c instanceof Node ? c : String(c));
  }
  return el;
}

const STATUS_LABEL = { done: "完了", queued: "待機中", processing: "生成中", failed: "失敗" };
const LANG_LABEL = { ja: "日本語", en: "英語" };

async function api(path, opts = {}) {
  const init = { credentials: "same-origin", ...opts };
  if (opts.json !== undefined) {
    init.method = init.method || "POST";
    init.headers = { "Content-Type": "application/json" };
    init.body = JSON.stringify(opts.json);
  }
  const res = await fetch(path, init);
  if (res.status === 401) {
    showLogin();
    throw new Error("login");
  }
  if (!res.ok) {
    let msg = `エラー (${res.status})`;
    try {
      const d = (await res.json()).detail;
      msg = typeof d === "string" ? d : JSON.stringify(d);
    } catch (_) {
      /* keep default */
    }
    alert(msg);
    throw new Error(msg);
  }
  return res.json();
}

function showLogin() {
  $("login").classList.remove("hidden");
  $("app").classList.add("hidden");
}

$("login-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  $("login-error").textContent = "";
  const res = await fetch("/api/admin/login", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ password: $("password").value }),
  });
  if (!res.ok) {
    $("login-error").textContent = (await res.json()).detail || "ログインできませんでした";
    return;
  }
  $("password").value = "";
  start();
});

// ---- tabs ----

let activeTab = "participants";
$("tabs").addEventListener("click", (e) => {
  const tab = e.target.dataset.tab;
  if (!tab) return;
  activeTab = tab;
  document.querySelectorAll("#tabs button").forEach((b) => b.classList.toggle("on", b.dataset.tab === tab));
  document.querySelectorAll(".tab").forEach((t) => t.classList.toggle("hidden", t.id !== `tab-${tab}`));
  refreshTab();
});

function refreshTab() {
  if (activeTab === "participants") loadParticipants();
  if (activeTab === "sentences") loadSentences();
  if (activeTab === "clips") loadClips();
  if (activeTab === "settings") loadSettings();
}

// ---- status bar ----

let status = null;
async function loadStatus() {
  status = await api("/api/admin/status");
  const q = status.queue;
  $("stats").replaceChildren(
    h("div", { class: "stat" }, "完了", h("b", {}, q.done)),
    h("div", { class: "stat" }, "待機中", h("b", {}, q.queued)),
    h("div", { class: "stat" }, "生成中", h("b", {}, q.processing)),
    h("div", { class: "stat" }, "失敗", h("b", {}, q.failed)),
  );
  let state = `エンジン: ${status.engine}`;
  if (status.engine_loading) state += "（モデル読み込み中…）";
  if (status.engine_error) state += `（エラー: ${status.engine_error}）`;
  $("engine-state").textContent = state;
  $("engine-state").style.color = status.engine_error ? "var(--bad)" : "";
}

// ---- participants ----

let lastCreated = [];

$("create-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  lastCreated = await api("/api/admin/participants", {
    json: {
      count: Number($("create-count").value),
      prefix: $("create-prefix").value.trim(),
      group_name: $("create-group").value.trim(),
    },
  });
  $("created").classList.remove("hidden");
  $("created-table").replaceChildren(
    h("tr", {}, h("th", {}, "ID"), h("th", {}, "あいことば"), h("th", {}, "グループ")),
    ...lastCreated.map((p) =>
      h("tr", {}, h("td", { class: "mono" }, p.id), h("td", { class: "mono" }, p.passcode), h("td", {}, p.group_name)),
    ),
  );
  loadParticipants();
});

function downloadCsv(name, rows) {
  const csv = "﻿" + rows.map((r) => r.map((v) => `"${String(v).replaceAll('"', '""')}"`).join(",")).join("\r\n");
  const a = h("a", { href: URL.createObjectURL(new Blob([csv], { type: "text/csv" })), download: name });
  document.body.append(a);
  a.click();
  a.remove();
}

$("created-csv").addEventListener("click", () => {
  downloadCsv("participants-passcodes.csv", [
    ["id", "passcode", "group", "url"],
    ...lastCreated.map((p) => [p.id, p.passcode, p.group_name, location.origin + "/"]),
  ]);
});

const adminAudio = $("admin-audio");
function preview(url) {
  adminAudio.classList.remove("hidden");
  adminAudio.src = url;
  adminAudio.play();
}

async function loadParticipants() {
  const rows = await api("/api/admin/participants");
  const sel = $("clip-participant");
  const keep = sel.value;
  sel.replaceChildren(h("option", { value: "" }, "すべて"), ...rows.map((p) => h("option", { value: p.id }, p.id)));
  sel.value = keep;

  $("participants-table").replaceChildren(
    h("tr", {},
      ["ID", "グループ", "状態", "声の登録", "生成（完了/全体）", "最終利用", "操作"].map((t) => h("th", {}, t))),
    ...rows.map((p) =>
      h("tr", {},
        h("td", { class: "mono" }, p.id),
        h("td", {}, h("button", { class: "link", onclick: () => editGroup(p) }, p.group_name || "（なし）")),
        h("td", {}, p.active ? "有効" : "停止中"),
        h("td", {},
          p.has_reference
            ? [p.ref_uploaded_at, " ", h("button", { onclick: () => preview(`/api/admin/participants/${p.id}/reference`) }, "試聴")]
            : h("span", { class: "small" }, "未登録")),
        h("td", { class: "num" }, `${p.clip_done} / ${p.clip_total}`, p.clip_failed ? h("span", { class: "badge failed" }, ` 失敗 ${p.clip_failed}`) : ""),
        h("td", { class: "small" }, p.last_activity || "—"),
        h("td", {},
          h("div", { class: "actions" },
            h("button", { onclick: () => toggleActive(p) }, p.active ? "停止" : "再開"),
            h("button", { onclick: () => resetPass(p) }, "あいことば再発行"),
            h("button", { class: "danger", onclick: () => removeParticipant(p) }, "データ削除"),
          ),
        ),
      ),
    ),
  );
  if (!rows.length) $("participants-table").append(h("tr", {}, h("td", { colspan: 7 }, "まだ参加者がいません。")));
}

async function editGroup(p) {
  const g = prompt(`${p.id} のグループ（空欄でなし）`, p.group_name);
  if (g === null) return;
  await api(`/api/admin/participants/${p.id}`, { method: "PATCH", json: { group_name: g } });
  loadParticipants();
}

async function toggleActive(p) {
  await api(`/api/admin/participants/${p.id}`, { method: "PATCH", json: { active: !p.active } });
  loadParticipants();
}

async function resetPass(p) {
  if (!confirm(`${p.id} のあいことばを再発行しますか？（古いあいことばは使えなくなります）`)) return;
  const r = await api(`/api/admin/participants/${p.id}/passcode`, { method: "POST" });
  prompt(`${r.id} の新しいあいことば（この画面でしか表示されません）`, r.passcode);
}

async function removeParticipant(p) {
  const typed = prompt(
    `${p.id} の声の録音・生成音声・利用ログをすべて削除します。元に戻せません。\n確認のため ID を入力してください。`,
  );
  if (typed === null) return;
  if (typed.trim().toUpperCase() !== p.id) {
    alert("ID が一致しないため削除しませんでした。");
    return;
  }
  await api(`/api/admin/participants/${p.id}`, { method: "DELETE" });
  loadParticipants();
  loadStatus();
}

// ---- sentences ----

$("sentence-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const lines = $("sentence-text").value.split("\n").map((s) => s.trim()).filter(Boolean);
  for (const text of lines) {
    await api("/api/admin/sentences", {
      json: { text, lang: $("sentence-lang").value, group_name: $("sentence-group").value.trim() },
    });
  }
  $("sentence-text").value = "";
  loadSentences();
  loadStatus();
});

async function loadSentences() {
  const rows = await api("/api/admin/sentences");
  $("sentences-table").replaceChildren(
    h("tr", {}, ["順番", "文章", "言語", "対象", "公開", "操作"].map((t) => h("th", {}, t))),
    ...rows.map((s) =>
      h("tr", {},
        h("td", { class: "num" }, s.sort_order),
        h("td", { lang: s.lang }, s.text),
        h("td", {}, LANG_LABEL[s.lang] || s.lang),
        h("td", {}, s.group_name || "全員"),
        h("td", {}, s.active ? "公開" : "非公開"),
        h("td", {},
          h("div", { class: "actions" },
            h("button", { onclick: () => editSentence(s) }, "編集"),
            h("button", { onclick: () => moveSentence(s) }, "順番"),
            h("button", { onclick: () => patchSentence(s.id, { active: !s.active }) }, s.active ? "非公開にする" : "公開する"),
            h("button", { class: "danger", onclick: () => deleteSentence(s) }, "削除"),
          ),
        ),
      ),
    ),
  );
  if (!rows.length) $("sentences-table").append(h("tr", {}, h("td", { colspan: 6 }, "まだ課題文がありません。")));
}

async function patchSentence(id, body) {
  await api(`/api/admin/sentences/${id}`, { method: "PATCH", json: body });
  loadSentences();
  loadStatus();
}

async function editSentence(s) {
  const text = prompt("文章を編集（変更すると全員分の音声をつくり直します）", s.text);
  if (text === null || !text.trim() || text.trim() === s.text) return;
  const group = prompt("対象グループ（空欄 = 全員）", s.group_name);
  if (group === null) return;
  patchSentence(s.id, { text: text.trim(), lang: "auto", group_name: group });
}

async function moveSentence(s) {
  const n = prompt("順番の数字（小さいほど上に表示）", s.sort_order);
  if (n === null || Number.isNaN(Number(n))) return;
  patchSentence(s.id, { sort_order: Number(n) });
}

async function deleteSentence(s) {
  if (!confirm(`「${s.text}」と、その生成音声をすべて削除しますか？`)) return;
  await api(`/api/admin/sentences/${s.id}`, { method: "DELETE" });
  loadSentences();
  loadStatus();
}

// ---- clips ----

$("clip-reload").addEventListener("click", loadClips);
$("clip-participant").addEventListener("change", loadClips);
$("clip-status").addEventListener("change", loadClips);
$("clip-retry-failed").addEventListener("click", async () => {
  const r = await api("/api/admin/clips/regenerate-failed", { method: "POST" });
  alert(`${r.count} 件を再生成キューに入れました。`);
  loadClips();
  loadStatus();
});

async function loadClips() {
  const params = new URLSearchParams();
  if ($("clip-participant").value) params.set("participant_id", $("clip-participant").value);
  if ($("clip-status").value) params.set("status", $("clip-status").value);
  const rows = await api(`/api/admin/clips?${params}`);
  const needApproval = status && status.require_approval;
  $("clips-table").replaceChildren(
    h("tr", {}, ["参加者", "文章", "状態", "長さ", "更新", needApproval ? "承認" : null, "操作"].filter(Boolean).map((t) => h("th", {}, t))),
    ...rows.map((c) =>
      h("tr", {},
        h("td", { class: "mono" }, c.participant_id),
        h("td", { lang: c.lang }, c.text, c.error ? h("div", { class: "err-text" }, c.error.split("\n")[0]) : ""),
        h("td", {}, h("span", { class: `badge ${c.status}` }, STATUS_LABEL[c.status] || c.status)),
        h("td", { class: "num" }, c.seconds ? `${c.seconds.toFixed(1)}秒` : ""),
        h("td", { class: "small" }, c.updated_at),
        needApproval ? h("td", {}, c.status === "done" ? (c.approved ? "承認済" : "未承認") : "") : null,
        h("td", {},
          h("div", { class: "actions" },
            c.status === "done" ? h("button", { onclick: () => preview(`/api/admin/clips/${c.id}/audio`) }, "試聴") : null,
            needApproval && c.status === "done"
              ? h("button", { onclick: () => approve(c, !c.approved) }, c.approved ? "承認取消" : "承認")
              : null,
            h("button", { onclick: () => regenerate(c) }, "再生成"),
          ),
        ),
      ),
    ),
  );
  if (!rows.length) $("clips-table").append(h("tr", {}, h("td", { colspan: 7 }, "該当する音声はありません。")));
}

async function approve(c, approved) {
  await api(`/api/admin/clips/${c.id}/approve`, { json: { approved } });
  loadClips();
}

async function regenerate(c) {
  await api(`/api/admin/clips/${c.id}/regenerate`, { method: "POST" });
  loadClips();
  loadStatus();
}

// ---- settings ----

async function loadSettings() {
  await loadStatus();
  $("ref-script").value = status.reference_script;
  $("require-approval").checked = status.require_approval;
}

$("save-settings").addEventListener("click", async () => {
  await api("/api/admin/settings", {
    method: "PUT",
    json: { reference_script: $("ref-script").value, require_approval: $("require-approval").checked },
  });
  await loadStatus();
  $("settings-saved").textContent = "保存しました";
  setTimeout(() => ($("settings-saved").textContent = ""), 2500);
});

// ---- start ----

let statusTimer = null;
async function start() {
  try {
    await loadStatus();
  } catch (_) {
    return;
  }
  $("login").classList.add("hidden");
  $("app").classList.remove("hidden");
  refreshTab();
  if (!statusTimer) statusTimer = setInterval(() => loadStatus().catch(() => {}), 5000);
}

start();
