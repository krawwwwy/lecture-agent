const $ = (id) => document.getElementById(id);
const DEFAULT_PORT = 8756;
let timer = null;

const pad = (n) => String(n).padStart(2, "0");
const hms = (sec) => `${pad(Math.floor(sec / 3600))}:${pad(Math.floor(sec / 60) % 60)}:${pad(sec % 60)}`;

function say(text, bad = false) {
  $("status").textContent = text || "";
  $("status").classList.toggle("bad", !!bad);
}

let autoPair = null; // пара, подставленная из расписания агента

async function pingAgent(port) {
  try {
    const res = await fetch(`http://127.0.0.1:${port}/ping`, { signal: AbortSignal.timeout(900) });
    return res.ok;
  } catch (e) {
    return false;
  }
}

async function currentPair(port) {
  try {
    const res = await fetch(`http://127.0.0.1:${port}/now`, { signal: AbortSignal.timeout(900) });
    const data = await res.json();
    return data.pair || null;
  } catch (e) {
    return null;
  }
}

function showPair(pair) {
  autoPair = pair;
  if (!pair) {
    $("pair").classList.add("hidden");
    return;
  }
  const where = pair.room ? `${pair.format}, ауд. ${pair.room}` : pair.format;
  $("pair").innerHTML = "";
  const name = document.createElement("b");
  name.textContent = pair.subject;
  const rest = document.createTextNode(`${pair.title} · ${pair.start}-${pair.end} · ${where}`);
  $("pair").append(name, rest);
  $("pair").classList.remove("hidden");
  $("subject").value = pair.subject;
}

function streamIdFor(tabId) {
  return new Promise((resolve, reject) => {
    chrome.tabCapture.getMediaStreamId({ targetTabId: tabId }, (id) => {
      const err = chrome.runtime.lastError;
      if (err || !id) reject(new Error(err ? err.message : "нет доступа к вкладке"));
      else resolve(id);
    });
  });
}

async function fillTabs() {
  const [active] = await chrome.tabs.query({ active: true, currentWindow: true });
  const tabs = (await chrome.tabs.query({})).filter((t) => /^https?:/.test(t.url || ""));
  const select = $("tab");
  select.innerHTML = "";
  tabs
    .sort((a, b) => (b.id === active?.id) - (a.id === active?.id) || b.audible - a.audible)
    .forEach((t) => {
      const option = document.createElement("option");
      option.value = t.id;
      const mark = t.id === active?.id ? "● " : t.audible ? "♪ " : "";
      option.textContent = mark + (t.title || t.url).slice(0, 60);
      select.appendChild(option);
    });
  if (!tabs.length) {
    say("Нет подходящих вкладок. Открой страницу с лекцией.", true);
    $("go").disabled = true;
  }
}

function showRecording(state) {
  $("idle").classList.add("hidden");
  $("active").classList.remove("hidden");
  $("settings").classList.add("hidden");
  $("go").textContent = "Остановить и сделать конспект";
  $("go").classList.add("rec");
  $("where").textContent =
    (state.subject || "Пара") +
    (state.mode === "server" ? " → агент сделает конспект сам" : " → файл в Загрузки");
  const tick = () => ($("timer").textContent = hms(Math.max(0, Math.round((Date.now() - state.started) / 1000))));
  tick();
  timer = setInterval(tick, 1000);
  $("go").onclick = stop;
}

async function start() {
  const subject = $("subject").value.trim() || "Онлайн-пара";
  const auto = !!autoPair && subject === autoPair.subject;
  const tabId = Number($("tab").value);
  const port = Number($("port").value) || DEFAULT_PORT;
  await chrome.storage.local.set({ subject, port });

  $("go").disabled = true;
  say("Подключаюсь к вкладке...");

  let streamId;
  try {
    streamId = await streamIdFor(tabId);
  } catch (e) {
    $("go").disabled = false;
    say("Chrome не дал доступ к этой вкладке. Перейди на неё и нажми «Начать запись» снова.", true);
    return;
  }

  const online = await pingAgent(port);
  const tab = await chrome.tabs.get(tabId).catch(() => null);
  const answer = await chrome.runtime.sendMessage({
    target: "background",
    type: "begin",
    streamId,
    subject,
    tabId,
    tabTitle: tab ? tab.title : "",
    serverUrl: online ? `http://127.0.0.1:${port}` : null,
    auto: auto && online,
  });

  $("go").disabled = false;
  if (!answer || answer.error) {
    say("Не получилось начать запись: " + ((answer && answer.error) || "нет ответа"), true);
    return;
  }
  showRecording(answer.state);
  say(
    online
      ? (auto
          ? `Пишу: ${autoPair.subject}, ${autoPair.title.toLowerCase()}. Конспект сохранится сам. ` +
            "Можно переключаться на другие вкладки и приложения."
          : "Можно переключаться на другие вкладки и приложения: пишется звук именно этой вкладки.")
      : `Агент на порту ${port} не отвечает, поэтому запись сохранится файлом в Загрузки. ` +
        `Потом обработай её командой process.`
  );
}

async function stop() {
  $("go").disabled = true;
  say("Завершаю запись...");
  const result = await chrome.runtime.sendMessage({ target: "background", type: "halt" });
  clearInterval(timer);

  if (!result || result.error) {
    say("Запись остановлена с ошибкой: " + ((result && result.error) || "нет ответа"), true);
  } else if (result.mode === "server") {
    say(`Готово, записано ${hms(result.seconds)}. Агент расшифровывает пару, конспект появится в папке lectures.`);
  } else {
    say(
      `Готово, записано ${hms(result.seconds)}. Файл сохранён в Загрузки: ${result.file || "lecture-agent"}. ` +
      `Обработай его командой: python agent.py process <файл> -s "${result.subject}"`,
      !!result.note
    );
  }
  setTimeout(() => window.close(), 9000);
  $("go").disabled = false;
  $("go").textContent = "Начать запись";
  $("go").classList.remove("rec");
  $("go").onclick = start;
  $("idle").classList.remove("hidden");
  $("active").classList.add("hidden");
  $("settings").classList.remove("hidden");
  await fillTabs();
  showPair(await currentPair(Number($("port").value) || DEFAULT_PORT));
}

(async () => {
  const saved = await chrome.storage.local.get(["recording", "subject", "port"]);
  $("subject").value = saved.subject || "";
  $("port").value = saved.port || DEFAULT_PORT;

  if (saved.recording) {
    showRecording(saved.recording);
    return;
  }

  await fillTabs();
  $("go").onclick = start;
  const port = Number($("port").value) || DEFAULT_PORT;
  if (!(await pingAgent(port))) {
    say("Агент не запущен. Запись сохранится файлом в Загрузки. " +
        "Чтобы конспект делался сам, запусти python agent.py serve");
    return;
  }
  const pair = await currentPair(port);
  showPair(pair);
  say(pair
    ? "Пара определена по расписанию, название и номер занятия подставятся сами."
    : "Сейчас по расписанию пар нет. Впиши предмет сам, если это внеплановая запись.");
})();
