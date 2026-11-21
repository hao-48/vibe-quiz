"use strict";

const el = id => document.getElementById(id);
let session = null;
let events = null;
let state = null;
let pending = null;
let busy = false;

async function request(action, body) {
  const headers = {"Content-Type": "application/json"};
  if (session) headers.Authorization = "Bearer " + session.session;
  const response = await fetch("/api/" + action, {method: "POST", headers, body: JSON.stringify(body)});
  const data = await response.json();
  if (!response.ok) {
    const error = new Error(data.error);
    error.status = response.status;
    throw error;
  }
  return data;
}

async function run(callback) {
  if (busy) return;
  busy = true;
  render();
  el("notice").textContent = "";
  try {
    await callback();
  } catch (error) {
    el("notice").textContent = error.status ? error.message : "連線中斷，請重試。";
  } finally {
    busy = false;
    render();
  }
}

function enter(data) {
  session = data;
  sessionStorage.setItem("quiz-session", JSON.stringify(data));
  const roleLabel = data.role === "teacher" ? "老師" : data.role + " 組";
  el("role").textContent = roleLabel;
  const tabLabel = data.number ? roleLabel + " " + data.number + " 號" : roleLabel;
  document.title = tabLabel + "｜課堂搶答";
  el("lobby").hidden = true;
  const teacher = data.role === "teacher";
  el("import-panel").hidden = !teacher;
  el("controls").hidden = !teacher;
  el("fixture-field").hidden = data.mode !== "offline";
  for (const name of ["book", "sheet"]) {
    el(name + "-field").hidden = data.mode === "offline";
    el(name).disabled = data.mode === "offline";
  }
  el("round-panel").hidden = false;
  events?.close();
  events = new EventSource("/events?session=" + encodeURIComponent(data.session));
  events.onmessage = event => {
    const update = JSON.parse(event.data);
    if (state?.round !== update.round) {
      el("answer").value = "";
      pending = null;
    }
    state = update;
    render();
  };
  events.onerror = () => {
    el("notice").textContent = "即時連線已中斷，正在嘗試重新連線。";
    for (const button of document.querySelectorAll("#round-panel button")) button.disabled = true;
  };
  events.onopen = () => { el("notice").textContent = ""; };
}

function fillList(node, entries) {
  node.replaceChildren(...entries.map(text => {
    const item = document.createElement("li");
    item.textContent = text;
    return item;
  }));
}

function render() {
  const phase = state?.phase ?? "等待中";
  el("question").textContent = state?.question ? "題號 " + state.question : "等待老師匯入題單";
  el("prompt").textContent = state?.prompt ?? "";
  el("phase").textContent = phase;
  el("phase").dataset.open = phase === "搶答中";
  const ready = events?.readyState === EventSource.OPEN && !busy;
  el("start").disabled = !ready || !state?.round || phase !== "等待中";
  el("end").disabled = !ready || phase !== "搶答中";
  el("next").disabled = !ready || !state?.can_next;
  el("write").disabled = !ready || !state?.can_write;
  el("import-form").querySelector("button").disabled = busy || phase === "搶答中";
  el("answer-form").hidden = !session || session.role === "teacher" || phase !== "搶答中" || Boolean(state?.receipt);
  el("answer-form").querySelector("button").disabled = !ready;
  el("receipt").textContent = state?.receipt ? "本組已送達：" + state.receipt : "";
  const result = state?.result;
  el("result").textContent = phase !== "已結束" ? "" :
    result ? "勝出：" + result.group + " 組 · " + result.answer : "本題無人勝出";
  fillList(el("answers"), (state?.answers ?? []).map(item => item.group + " 組：" + item.answer));
  el("write-status").textContent = state?.write_status ?? "";
  const questions = state?.questions ?? [];
  el("count").textContent = questions.length ? "共 " + questions.length + " 題" : "";
  fillList(el("preview"), questions.map(q =>
    "題號 " + q.key + " · 填空格 " + q.cell + (q.prompt ? "\n" + q.prompt : "")));
}

for (const button of document.querySelectorAll("[data-group]")) {
  button.addEventListener("click", () => run(async () => enter(await request("session", {role: button.dataset.group}))));
}
el("import-form").addEventListener("submit", event => {
  event.preventDefault();
  const offline = session.mode === "offline";
  run(() => request("import", {
    book: offline ? el("fixture").value : el("book").value.trim(),
    sheet: offline ? "課堂筆記" : el("sheet").value,
  }));
});
for (const action of ["start", "end", "next", "write"]) {
  el(action).addEventListener("click", () => {
    if (action === "write" && !confirm("將已結束題目的勝出答案寫回原 Guided Notes？")) return;
    run(() => request(action, {round: state.round}));
  });
}
el("answer-form").addEventListener("submit", event => {
  event.preventDefault();
  const answer = el("answer").value;
  if (!pending || pending.answer !== answer) pending = {answer, submission: crypto.randomUUID(), round: state.round};
  run(async () => {
    await request("submit", pending);
    pending = null;
  });
});

const teacherEntry = new URLSearchParams(location.hash.slice(1)).get("teacher");
if (teacherEntry) {
  history.replaceState(null, "", "/");
  run(async () => enter(await request("session", {role: "teacher", entry: teacherEntry})));
} else {
  const saved = sessionStorage.getItem("quiz-session");
  if (saved) {
    const candidate = JSON.parse(saved);
    session = candidate;
    run(async () => {
      try {
        await request("state", {});
        enter(candidate);
      } catch (error) {
        sessionStorage.removeItem("quiz-session");
        session = null;
        throw error;
      }
    });
  }
}
