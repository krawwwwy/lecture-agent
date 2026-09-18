// Связывает попап с невидимой страницей, которая делает саму запись,
// и хранит состояние, чтобы попап можно было закрывать и открывать заново.

const OFFSCREEN_URL = "offscreen.html";

async function ensureOffscreen() {
  try {
    const contexts = await chrome.runtime.getContexts({ contextTypes: ["OFFSCREEN_DOCUMENT"] });
    if (contexts.length) return;
  } catch (e) {
    // getContexts есть не во всех версиях Chrome, тогда полагаемся на ошибку ниже
  }
  try {
    await chrome.offscreen.createDocument({
      url: OFFSCREEN_URL,
      reasons: ["USER_MEDIA", "AUDIO_PLAYBACK"],
      justification: "Запись звука выбранной вкладки в фоне",
    });
  } catch (e) {
    if (!String(e).includes("Only a single offscreen")) throw e;
  }
}

function setBadge(recording) {
  chrome.action.setBadgeText({ text: recording ? "REC" : "" });
  if (recording) chrome.action.setBadgeBackgroundColor({ color: "#d93025" });
}

async function begin(msg) {
  await ensureOffscreen();
  const sessionId = Date.now().toString(36) + "-" + Math.random().toString(36).slice(2, 8);
  const started = Date.now();
  const answer = await chrome.runtime.sendMessage({
    target: "offscreen",
    type: "start",
    streamId: msg.streamId,
    subject: msg.subject,
    serverUrl: msg.serverUrl || null,
    auto: !!msg.auto,
    sessionId,
    started,
  });
  if (!answer || answer.error) return { error: (answer && answer.error) || "запись не запустилась" };

  const state = {
    sessionId,
    started,
    subject: msg.subject,
    tabId: msg.tabId,
    tabTitle: msg.tabTitle,
    mode: answer.mode,
  };
  await chrome.storage.local.set({ recording: state });
  setBadge(true);
  return { ok: true, state };
}

async function halt() {
  const result = await chrome.runtime.sendMessage({ target: "offscreen", type: "stop" });
  await chrome.storage.local.remove("recording");
  setBadge(false);
  return result || { error: "запись не отвечает" };
}

chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  if (!msg || msg.target !== "background") return;
  (async () => {
    try {
      if (msg.type === "begin") sendResponse(await begin(msg));
      else if (msg.type === "halt") sendResponse(await halt());
      else if (msg.type === "ended") {
        // вкладку закрыли: запись остановилась сама
        await chrome.storage.local.remove("recording");
        await chrome.storage.local.set({ lastResult: msg.result || null });
        setBadge(false);
        sendResponse({ ok: true });
      } else sendResponse({ error: "неизвестная команда" });
    } catch (e) {
      sendResponse({ error: String((e && e.message) || e) });
    }
  })();
  return true;
});

// после перезапуска браузера запись не переживает, значок не должен врать
chrome.runtime.onStartup.addListener(async () => {
  await chrome.storage.local.remove("recording");
  setBadge(false);
});
