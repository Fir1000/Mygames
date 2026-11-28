// Phone helper for converter.html / subtitles.html.
// A phone can't reach the helper at 127.0.0.1, so it has to open the PC's Wi-Fi
// address instead. This adds a "scan QR" button (camera inside the page) and a
// one-tap button for the PC used last time. Shown only where the page shows its
// phone steps (#phoneSteps not hidden).
(function () {
  const steps = document.getElementById("phoneSteps");
  if (!steps || steps.hidden || location.port === "8765") return;

  const KEY = "myfir-pc";
  const page = /subtitles\.html$/.test(location.pathname) ? "/subtitles.html" : "/";

  const css = document.createElement("style");
  css.textContent = `
    .pc-connect { display: flex; gap: 10px; flex-wrap: wrap; margin: 12px 0 4px; }
    .pc-connect a, .pc-connect button { flex: 1 1 200px; text-align: center; text-decoration: none; font-weight: 700; }
    .pc-connect a { padding: 12px 16px; border-radius: 10px; background: var(--card2); color: var(--text); }
    .pc-connect .note2 { flex-basis: 100%; font-size: 13px; color: var(--err); min-height: 0; margin: 0; }
    .scan { position: fixed; inset: 0; z-index: 1000; background: #000; display: flex; flex-direction: column; }
    .scan video { flex: 1; width: 100%; min-height: 0; object-fit: cover; }
    .scan .aim { position: absolute; left: 50%; top: 45%; width: min(64vw, 300px); aspect-ratio: 1; transform: translate(-50%, -50%);
      border: 3px solid var(--accent); border-radius: 18px; box-shadow: 0 0 0 100vmax rgba(0,0,0,.45); pointer-events: none; }
    .scan .bar { position: absolute; left: 0; right: 0; bottom: 0; padding: 18px 16px calc(18px + env(safe-area-inset-bottom));
      text-align: center; color: #fff; background: linear-gradient(transparent, rgba(0,0,0,.85)); font-size: 15px; line-height: 1.6; }
    .scan .bar b { color: var(--accent); }
    .scan .close { position: absolute; top: calc(12px + env(safe-area-inset-top)); right: 12px; width: 46px; height: 46px;
      border-radius: 50%; background: rgba(0,0,0,.6); color: #fff; font-size: 22px; padding: 0; }`;
  document.head.appendChild(css);

  const box = document.createElement("div");
  box.className = "pc-connect";
  box.innerHTML = `<button class="primary" type="button">📷 สแกน QR จากจอคอม</button><p class="note2"></p>`;
  steps.after(box);
  const scanBtn = box.querySelector("button"), note = box.querySelector(".note2");

  const last = localStorage.getItem(KEY);
  if (last) {
    const a = document.createElement("a");
    a.href = last + page;
    a.textContent = "เปิดคอมเครื่องเดิม (" + new URL(last).hostname + ")";
    box.insertBefore(a, note);
  }

  // Only accept the helper's own address: http://<home network IP>:8765
  function pcAddress(text) {
    try {
      const u = new URL(text.trim());
      const privateIp = /^(10\.\d+|192\.168|172\.(1[6-9]|2\d|3[01]))\.\d+\.\d+$/;
      if (u.protocol === "http:" && u.port === "8765" && privateIp.test(u.hostname)) return u.origin;
    } catch {}
    return null;
  }

  let stream = null, overlay = null, timer = null;

  function stop() {
    clearTimeout(timer);
    if (stream) stream.getTracks().forEach(t => t.stop());
    stream = null;
    overlay?.remove();
    overlay = null;
  }

  function loadJsQR() {
    if (window.jsQR) return Promise.resolve();
    return new Promise((res, rej) => {
      const s = document.createElement("script");
      s.src = "https://cdn.jsdelivr.net/npm/jsqr@1.4.0/dist/jsQR.js";
      s.onload = res; s.onerror = () => rej(new Error("load"));
      document.head.appendChild(s);
    });
  }

  async function makeReader() {
    if ("BarcodeDetector" in window) {
      try {
        if ((await BarcodeDetector.getSupportedFormats()).includes("qr_code")) {
          const det = new BarcodeDetector({ formats: ["qr_code"] });
          return async video => (await det.detect(video)).map(c => c.rawValue);
        }
      } catch {}
    }
    await loadJsQR(); // iPhone Safari has no BarcodeDetector
    const canvas = document.createElement("canvas");
    const ctx = canvas.getContext("2d", { willReadFrequently: true });
    return async video => {
      const w = video.videoWidth, h = video.videoHeight;
      if (!w) return [];
      const k = Math.min(1, 720 / Math.max(w, h));
      canvas.width = Math.round(w * k); canvas.height = Math.round(h * k);
      ctx.drawImage(video, 0, 0, canvas.width, canvas.height);
      const img = ctx.getImageData(0, 0, canvas.width, canvas.height);
      const r = jsQR(img.data, img.width, img.height, { inversionAttempts: "dontInvert" });
      return r ? [r.data] : [];
    };
  }

  scanBtn.onclick = async () => {
    note.textContent = "";
    if (!navigator.mediaDevices?.getUserMedia) {
      note.textContent = "เบราว์เซอร์นี้เปิดกล้องในหน้าเว็บไม่ได้ ใช้แอปกล้องของมือถือสแกนแทน";
      return;
    }
    overlay = document.createElement("div");
    overlay.className = "scan";
    overlay.innerHTML = `<video playsinline muted></video><div class="aim"></div>
      <button class="close" type="button" aria-label="ปิด">✕</button>
      <div class="bar">เล็งกล้องไปที่ <b>QR code บนจอคอม</b><br><span class="hint">ในหน้าเดียวกันนี้บนคอม (ต้องเปิดตัวช่วยไว้)</span></div>`;
    document.body.appendChild(overlay);
    overlay.querySelector(".close").onclick = stop;
    const video = overlay.querySelector("video"), hint = overlay.querySelector(".hint");
    try {
      stream = await navigator.mediaDevices.getUserMedia({ video: { facingMode: "environment" }, audio: false });
      video.srcObject = stream;
      await video.play();
    } catch (e) {
      stop();
      note.textContent = e && e.name === "NotAllowedError"
        ? "ต้องกดอนุญาตให้ใช้กล้องก่อน (ถ้าเคยกดไม่อนุญาต ให้เปิดในการตั้งค่าเว็บไซต์ของเบราว์เซอร์)"
        : "เปิดกล้องไม่ได้ ใช้แอปกล้องของมือถือสแกนแทน";
      return;
    }
    let read;
    try { read = await makeReader(); }
    catch { stop(); note.textContent = "โหลดตัวอ่าน QR ไม่ได้ ตรวจสอบอินเทอร์เน็ต หรือใช้แอปกล้องสแกนแทน"; return; }

    const loop = async () => {
      if (!overlay) return;
      let codes = [];
      try { codes = await read(video); } catch {}
      for (const text of codes) {
        const origin = pcAddress(text);
        if (origin) {
          localStorage.setItem(KEY, origin);
          hint.textContent = "เจอแล้ว! กำลังเชื่อมต่อ...";
          stop();
          location.href = origin + page;
          return;
        }
        hint.textContent = "QR นี้ไม่ใช่ของตัวช่วย MyFiR ลองสแกน QR ในกล่อง \"ดูบนมือถือ\" บนจอคอม";
      }
      timer = setTimeout(loop, 150);
    };
    loop();
  };

  window.addEventListener("pagehide", stop);
})();
