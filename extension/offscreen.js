// Здесь происходит сама запись. Страница невидимая и живёт, пока идёт запись,
// поэтому переключение вкладок и сворачивание браузера ей не мешают.

let session = null;

function reset() {
  session = null;
}

async function start({ streamId, sessionId, subject, serverUrl, started, auto }) {
  if (session) return { error: "запись уже идёт" };

  const stream = await navigator.mediaDevices.getUserMedia({
    audio: { mandatory: { chromeMediaSource: "tab", chromeMediaSourceId: streamId } },
  });

  const ctx = new AudioContext();
  if (ctx.state === "suspended") await ctx.resume();
  const source = ctx.createMediaStreamSource(stream);

  // Захват забирает звук вкладки себе, поэтому возвращаем его обратно в колонки,
  // иначе во время записи лекцию не будет слышно.
  source.connect(ctx.destination);

  // Пишем в моно: файл вдвое меньше, а распознаванию речи стерео не нужно.
  let target;
  try {
    target = new MediaStreamAudioDestinationNode(ctx, {
      channelCount: 1,
      channelCountMode: "explicit",
      channelInterpretation: "speakers",
    });
  } catch (e) {
    target = ctx.createMediaStreamDestination();
  }
  source.connect(target);

  const recorder = new MediaRecorder(target.stream, {
    mimeType: "audio/webm;codecs=opus",
    audioBitsPerSecond: 48000,
  });

  session = {
    id: sessionId,
    subject,
    started,
    serverUrl,
    auto: auto ? 1 : 0,
    mode: serverUrl ? "server" : "memory",
    stream,
    ctx,
    recorder,
    chunks: [],      // весь файл держим в памяти на случай, если агент отвалится
    pending: [],     // куски, ещё не отправленные агенту
    uploads: Promise.resolve(),
    bytes: 0,
    stopped: null,
  };

  recorder.ondataavailable = (e) => {
    if (!session || !e.data || !e.data.size) return;
    session.chunks.push(e.data);
    session.bytes += e.data.size;
    if (session.mode === "server") {
      session.pending.push(e.data);
      session.uploads = session.uploads.then(flush);
    }
  };

  const done = new Promise((resolve) => recorder.addEventListener("stop", resolve, { once: true }));
  session.stopped = done;

  // если вкладку закрыли, звук кончается сам: доводим запись до конца и сохраняем
  stream.getAudioTracks().forEach((track) => {
    track.addEventListener("ended", async () => {
      if (!session || session.finishing) return;
      const result = await stop();
      chrome.runtime.sendMessage({ target: "background", type: "ended", result }).catch(() => {});
    });
  });

  recorder.start(15000); // кусок раз в 15 секунд
  return { ok: true, mode: session.mode };
}

async function flush() {
  while (session && session.mode === "server" && session.pending.length) {
    const blob = session.pending[0];
    const url =
      session.serverUrl +
      "/chunk?session=" + encodeURIComponent(session.id) +
      "&subject=" + encodeURIComponent(session.subject) +
      "&started=" + session.started +
      "&auto=" + session.auto;
    try {
      const res = await fetch(url, { method: "POST", body: blob });
      if (!res.ok) throw new Error("HTTP " + res.status);
      session.pending.shift();
    } catch (e) {
      // агент недоступен: дописываем запись в память и сохраним файл в Загрузки
      session.mode = "memory";
      session.pending.length = 0;
      session.note = "агент перестал отвечать, запись сохранится файлом";
      return;
    }
  }
}

function fileName(subject, started) {
  const d = new Date(started);
  const p = (n) => String(n).padStart(2, "0");
  const stamp = `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}_${p(d.getHours())}-${p(d.getMinutes())}`;
  const safe = (subject || "Пара").replace(/[<>:"/\\|?*\x00-\x1f]/g, "_").trim() || "Пара";
  return `lecture-agent/${safe}_${stamp}.webm`;
}

function saveToDownloads() {
  const blob = new Blob(session.chunks, { type: "audio/webm" });
  const url = URL.createObjectURL(blob);
  const name = fileName(session.subject, session.started);
  return new Promise((resolve) => {
    chrome.downloads.download({ url, filename: name, saveAs: false }, (id) => {
      const err = chrome.runtime.lastError;
      setTimeout(() => URL.revokeObjectURL(url), 60000);
      resolve(err ? { error: err.message } : { file: name, id });
    });
  });
}

async function stop() {
  if (!session) return { error: "запись не идёт" };
  session.finishing = true;
  const s = session;

  if (s.recorder.state !== "inactive") {
    s.recorder.stop();
    await s.stopped;
  }
  s.stream.getTracks().forEach((t) => t.stop());
  try { await s.ctx.close(); } catch (e) { /* уже закрыт */ }

  const result = {
    ok: true,
    subject: s.subject,
    seconds: Math.round((Date.now() - s.started) / 1000),
    bytes: s.bytes,
    note: s.note || null,
  };

  if (s.mode === "server") {
    await s.uploads.then(flush);
  }

  if (s.mode === "server" && !s.pending.length) {
    try {
      const res = await fetch(s.serverUrl + "/finish?session=" + encodeURIComponent(s.id), { method: "POST" });
      if (!res.ok) throw new Error("HTTP " + res.status);
      const data = await res.json();
      result.mode = "server";
      result.folder = data.folder || null;
    } catch (e) {
      s.mode = "memory";
    }
  }

  if (s.mode !== "server" || result.mode !== "server") {
    const saved = await saveToDownloads();
    result.mode = "download";
    Object.assign(result, saved);
  }

  reset();
  return result;
}

chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  if (!msg || msg.target !== "offscreen") return;
  (async () => {
    try {
      if (msg.type === "start") sendResponse(await start(msg));
      else if (msg.type === "stop") sendResponse(await stop());
      else sendResponse({ error: "неизвестная команда" });
    } catch (e) {
      reset();
      sendResponse({ error: String((e && e.message) || e) });
    }
  })();
  return true;
});
