(() => {
  "use strict";
  const $ = (selector) => document.querySelector(selector);
  const management = location.pathname.replace(/\/$/, "") === "/manage";
  const notice = $("#notice");
  let language = "fa";
  let csrf = "";
  let status = null;
  let telegramSettings = null;
  let busy = false;
  let certificatePoll = null;
  const text = (fa, en) => language === "fa" ? fa : en;
  const savedLanguage = (() => { try { return localStorage.getItem("wgportal-language"); } catch { return null; } })();

  function say(message, kind = "") {
    notice.textContent = message || "";
    notice.className = "notice " + kind;
    notice.hidden = !message;
    if (message) notice.scrollIntoView({ behavior: matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth", block: "nearest" });
  }

  function translate(next) {
    language = next === "en" ? "en" : "fa";
    document.documentElement.lang = language;
    document.documentElement.dir = language === "fa" ? "rtl" : "ltr";
    document.querySelectorAll("[data-fa][data-en]").forEach((node) => { node.textContent = node.dataset[language]; });
    $("#language-toggle").textContent = language === "fa" ? "EN" : "فارسی";
    $("#language-toggle").setAttribute("aria-label", text("Switch to English", "تغییر زبان به فارسی"));
    $("#intro-eyebrow").textContent = management ? text("مدیریت اتصال و سرویس‌ها", "CONNECTION & SERVICE MANAGEMENT") : text("راه‌اندازی اولیهٔ پنل", "FIRST-RUN PANEL SETUP");
    $("#intro-title").textContent = management ? text("تنظیمات پنل، در دست شما.", "Your panel, under your control.") : text("پنل شما، آمادهٔ شروع.", "Your panel, ready to begin.");
    $("#intro-copy").textContent = text("حساب مدیر، اتصال امن و سرویس‌ها را از همین مرورگر مدیریت کنید.", "Manage administrator access, certificates and services in this browser.");
    document.title = text("راه‌اندازی و مدیریت · WG Portal", "Setup & management · WG Portal");
    try { localStorage.setItem("wgportal-language", language); } catch { /* Storage is optional. */ }
    if (status) render(status);
    if (telegramSettings) renderTelegramSettings(telegramSettings);
  }

  function cookie(name) {
    const prefix = name + "=";
    const entry = document.cookie.split(";").map((part) => part.trim()).find((part) => part.startsWith(prefix));
    if (!entry) return "";
    try { return decodeURIComponent(entry.slice(prefix.length)); } catch { return ""; }
  }

  async function request(path, { method = "GET", payload, native = false, timeout = 20000 } = {}) {
    const headers = new Headers({ Accept: "application/json" });
    if (payload !== undefined) headers.set("Content-Type", "application/json");
    if (method !== "GET") {
      if (native) {
        let token = cookie("csrf_token");
        if (!token) {
          await request("/api/telegram/settings", { native: true });
          token = cookie("csrf_token");
        }
        if (!token) throw new Error(text("توکن امنیتی نشست دریافت نشد؛ دوباره وارد پنل شوید.", "The session security token is unavailable. Sign in again."));
        headers.set("X-CSRFToken", token);
        headers.set("X-CSRF-Token", token);
      } else {
        if (!csrf) throw new Error(text("وضعیت صفحه را دوباره دریافت کنید.", "Refresh this page’s status first."));
        headers.set("X-Setup-CSRF", csrf);
      }
    }
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), timeout);
    try {
      const response = await fetch(path, {
        method, headers, credentials: "same-origin", cache: "no-store", redirect: "error", signal: controller.signal,
        ...(payload !== undefined ? { body: JSON.stringify(payload) } : {})
      });
      const contentType = response.headers.get("Content-Type") || "";
      const body = contentType.includes("application/json") ? await response.json() : {};
      if (body.csrf || body.csrf_token) csrf = body.csrf || body.csrf_token;
      if (response.status === 401 || response.status === 403 && !body.message && !body.error) {
        throw new Error(text("برای ادامه با حساب مدیر وارد شوید یا صفحه را تازه کنید.", "Sign in as administrator or refresh this page to continue."));
      }
      if (!response.ok || body.ok === false || body.error) {
        const detail = typeof body.message === "string" ? body.message : typeof body.detail === "string" ? body.detail : typeof body.error === "string" ? body.error : null;
        throw new Error(detail || text("درخواست انجام نشد؛ تنظیمات و وضعیت سرویس را بررسی کنید.", "The request failed. Check the settings and service status."));
      }
      if (!contentType.includes("application/json")) throw new Error(text("پاسخ معتبر دریافت نشد؛ دوباره با مدیر وارد شوید.", "No valid response was received. Sign in as administrator again."));
      return body;
    } catch (error) {
      if (error.name === "AbortError") throw new Error(text("پاسخ در زمان مقرر نرسید. نتیجه ممکن است روی سرور ثبت شده باشد؛ پیش از تکرار وضعیت را بررسی کنید.", "The request timed out. It may have completed on the server; check its status before retrying."));
      throw error;
    } finally { clearTimeout(timer); }
  }

  function pill(selector, label, ready) {
    const node = $(selector);
    node.textContent = label;
    node.className = "state-pill " + (ready ? "ready" : "wait");
  }

  function render(data) {
    status = data;
    $("#loading").hidden = true;
    $("#unavailable").hidden = true;
    $("#setup-content").hidden = false;
    const owner = Boolean(data.owner_claimed);
    const admin = Boolean(data.admin_authenticated);
    const tls = Boolean(data.tls_ready);
    const apply = data.tls_apply || {};
    const applying = apply.phase === "pending";
    const recovery = Boolean(apply.recovery_required);
    const trusted = Boolean(data.tls_trusted) && !applying && !recovery;
    const complete = Boolean(data.setup_complete || data.phase === "complete");
    if (complete && !management) { location.replace("/"); return; }
    $("#owner-card").hidden = owner || management;
    $("#login-card").hidden = !owner || admin;
    $("#finish-card").hidden = management || complete || !owner;
    $("#progress-card").hidden = management;
    $("#certificate-fields").disabled = !admin || busy || applying || recovery;
    $("#interface-fields").disabled = !admin || busy;
    $("#telegram-fields").disabled = !admin || busy;
    $("#ip-certificate").disabled = !admin || busy || applying || recovery;
    $("#finish-setup").disabled = !admin || !owner || !tls || !data.panel_ready || busy || applying || recovery;
    $("#trust-title").textContent = applying ? text("اعمال گواهی در انتظار تأیید", "Certificate application awaiting confirmation") : trusted ? text("گواهی عمومی نصب شده است", "Public certificate installed") : text("اتصال با گواهی آغازین یا تأییدنشده", "Bootstrap or unconfirmed certificate connection");
    $("#trust-copy").textContent = trusted
      ? text("اعتبار مرورگر به نام میزبان و گواهی فعال بستگی دارد. اگر پس از صدور هنوز هشدار می‌بینید، گواهی ارائه‌شده را بررسی کنید.", "Browser trust depends on the hostname and active certificate. If a warning remains after issuance, inspect the certificate being served.")
      : text("رمزگذاری فعال است؛ مورد اعتماد عمومی بودن گواهی هنوز تأیید نشده است.", "Encryption is enabled; public certificate trust has not been established.");
    $("#trust-card").classList.toggle("bootstrap", !trusted);
    $("#tls-apply-message").hidden = !["pending", "error"].includes(apply.phase);
    $("#tls-apply-message").textContent = applying
      ? text("صدور گواهی انجام شده، اما بارگذاری آن هنوز قطعی نیست. تا تأیید سرویس، وضعیت گواهی قبلی نمایش داده می‌شود.", "Issuance has completed, but application is not yet confirmed. The previous certificate state remains visible until the service confirms it.")
      : recovery
        ? text("اعمال گواهی شکست خورد و بازگشت امن تأیید نشد. پیش از صدور دوباره، بازیابی سرویس روی سرور لازم است.", "Certificate application failed and safe rollback was not confirmed. Recover the server service before another issuance.")
        : text("اعمال گواهی شکست خورد و گواهی قبلی بازگردانده شد. وضعیت سرویس را پیش از درخواست دوباره بررسی کنید.", "Certificate application failed and the previous certificate was restored. Check service status before requesting again.");
    $("#fingerprint-hint").hidden = !["bootstrap", "none"].includes(data.certificate_kind || "none");
    pill("#trust-state", applying ? text("در حال اعمال", "Applying") : recovery ? text("نیازمند بازیابی", "Recovery required") : trusted ? text("گواهی رسمی", "Public certificate") : text("تأییدنشده", "Unconfirmed"), trusted);
    $("#current-url").textContent = location.origin;
    $("#current-url").href = location.origin;
    pill("#owner-state", owner ? text("ساخته‌شده", "Created") : text("در انتظار", "Waiting"), owner);
    pill("#domain-state", applying ? text("در حال اعمال", "Applying") : trusted ? text("گواهی رسمی", "Public certificate") : text("تأییدنشده", "Unconfirmed"), trusted);
    pill("#panel-state", data.panel_ready ? text("هسته در دسترس", "Core available") : text("هسته در انتظار", "Core pending"), Boolean(data.panel_ready));
    if (data.active_domain && !$("#domain-name").value) $("#domain-name").value = data.active_domain;
    const labels = text(["اتصال رمزگذاری‌شده", "حساب مدیر", "سلامت هسته", "تکمیل"], ["Encrypted connection", "Administrator", "Panel health", "Complete"]);
    const flags = [tls, owner, Boolean(data.panel_ready), complete];
    const count = flags.filter(Boolean).length;
    $("#progress-count").textContent = count + " / " + flags.length;
    $("#progress-bar").style.width = count / flags.length * 100 + "%";
    $("#step-list").replaceChildren(...labels.map((label, index) => {
      const item = document.createElement("li");
      item.className = "step-item " + (flags[index] ? "done" : "");
      const bullet = document.createElement("span");
      bullet.className = "step-bullet";
      bullet.textContent = flags[index] ? "✓" : String(index + 1);
      const copy = document.createElement("span"); copy.textContent = label;
      item.append(bullet, copy);
      return item;
    }));
  }

  function renderTelegramSettings(data) {
    $("#telegram-token-state").textContent = data.has_token
      ? text("توکن ذخیره شده؛ برای حفظ آن این کادر را خالی بگذارید.", "A token is saved; leave this field blank to keep it.")
      : text("هنوز توکنی ذخیره نشده است.", "No bot token has been saved yet.");
    $("#telegram-enabled").checked = Boolean(data.enabled);
  }

  async function refreshServices() {
    if (!status?.admin_authenticated) return;
    const results = await Promise.allSettled([
      request("/api/telegram/settings", { native: true }), request("/api/telegram/status", { native: true })
    ]);
    if (results[0].status === "fulfilled") {
      telegramSettings = results[0].value;
      renderTelegramSettings(telegramSettings);
    }
    if (results[1].status === "fulfilled") {
      const online = Boolean(results[1].value.bot_online);
      pill("#telegram-state", online ? text("سرویس در دسترس", "Service available") : text("سرویس آفلاین", "Service offline"), online);
    } else pill("#telegram-state", text("وضعیت نامشخص", "Status unavailable"), false);
  }

  async function refresh() {
    try {
      const data = await request(management ? "/__manage/api/status" : "/__setup/api/status");
      render(data);
      await refreshServices();
      if (data.tls_apply?.phase === "pending") void pollCertificate();
    } catch (error) {
      $("#loading").hidden = true;
      $("#unavailable").hidden = false;
      $("#setup-content").hidden = true;
      say(error.message, "error");
    }
  }

  function pollCertificate() {
    if (certificatePoll) return certificatePoll;
    certificatePoll = (async () => {
      const delays = [800, 1200, 1800, 2500, 3500, 5000, 5000, 5000, 5000, 5000, 5000, 5000];
      for (const delay of delays) {
        await new Promise((resolve) => setTimeout(resolve, delay));
        try {
          const data = await request(management ? "/__manage/api/status" : "/__setup/api/status", { timeout: 5000 });
          render(data);
          if (data.tls_apply?.phase === "applied") {
            say(text("بارگذاری گواهی توسط سرویس تأیید شد. گواهی فعال مرورگر را هم بررسی کنید.", "The service confirmed certificate application. Check the active browser certificate too."), "success");
            return;
          }
          if (data.tls_apply?.phase === "error") {
            say($("#tls-apply-message").textContent, "error");
            return;
          }
          if (data.tls_apply?.phase !== "pending") return;
        } catch { /* A reload can briefly interrupt GET status. No mutation is retried. */ }
      }
      say(text("بارگذاری گواهی در این بازه تأیید نشد. وضعیت را بعداً تازه کنید؛ درخواست صدور دوباره خودکار انجام نمی‌شود.", "Certificate application was not confirmed within this window. Refresh later; issuance is not automatically repeated."));
    })().finally(() => { certificatePoll = null; });
    return certificatePoll;
  }

  async function action(button, operation) {
    if (busy) return;
    busy = true;
    button.disabled = true;
    button.setAttribute("aria-busy", "true");
    if (status) render(status);
    say(text("در حال انجام درخواست…", "Working on your request…"));
    try { await operation(); } catch (error) { say(error.message, "error"); }
    finally {
      busy = false;
      button.removeAttribute("aria-busy");
      button.disabled = false;
      if (status) render(status);
    }
  }

  $("#language-toggle").addEventListener("click", () => translate(language === "fa" ? "en" : "fa"));
  $("#retry-setup").addEventListener("click", refresh);
  $("#refresh-services").addEventListener("click", () => action($("#refresh-services"), refresh));
  $("#owner-form").addEventListener("submit", (event) => {
    event.preventDefault();
    const payload = Object.fromEntries(new FormData(event.currentTarget));
    if (payload.password !== payload.password2) {
      say(text("تکرار رمز یکسان نیست.", "The passwords do not match."), "error");
      return;
    }
    action($("#owner-submit"), async () => {
      await request("/__setup/api/owner", { method: "POST", payload });
      $("#owner-form").reset();
      say(text("حساب مدیر ساخته شد. برای ادامه وارد پنل شوید؛ کد نصب دوباره لازم نیست.", "The administrator was created. Sign in to continue; the setup code is no longer needed."), "success");
      await refresh();
    });
  });

  async function certificate(kind) {
    const payload = kind === "domain" ? { kind, domain: $("#domain-name").value.trim() } : { kind };
    const button = kind === "domain" ? $("#domain-submit") : $("#ip-certificate");
    await action(button, async () => {
      await request("/__manage/api/certificate", { method: "POST", payload, timeout: 240000 });
      say(text("درخواست صدور پذیرفته شد؛ تا تأیید بارگذاری، گواهی جدید فعال اعلام نمی‌شود.", "Issuance was accepted; the new certificate is not marked active until application is confirmed."));
      await refresh();
      if (status?.tls_apply?.phase === "pending") await pollCertificate();
    });
  }
  $("#domain-form").addEventListener("submit", (event) => { event.preventDefault(); certificate("domain"); });
  $("#ip-certificate").addEventListener("click", () => certificate("ip"));
  $("#interface-form").addEventListener("submit", (event) => {
    event.preventDefault();
    const payload = Object.fromEntries(new FormData(event.currentTarget));
    payload.listen_port = Number(payload.listen_port);
    payload.auto_up = $("#interface-up").checked;
    payload.auto_firewall = $("#interface-firewall").checked;
    action($("#interface-submit"), async () => {
      await request("/api/interfaces", { method: "POST", payload, native: true, timeout: 90000 });
      say(text("Interface در هستهٔ WG_Panel ساخته شد. برای ساخت Peer به مدیریت Interface و Peer بروید.", "The interface was created in WG_Panel. Open interface and peer management to create a peer."), "success");
    });
  });
  $("#telegram-form").addEventListener("submit", (event) => {
    event.preventDefault();
    action($("#telegram-save"), async () => {
      let writes = 0;
      try {
        const existing = await request("/api/telegram/settings", { native: true });
        const token = $("#telegram-token").value.trim();
        const admin = $("#telegram-admin").value.trim();
        if (!token && !existing.has_token) throw new Error(text("ابتدا توکن ربات را وارد کنید.", "Enter a bot token first."));
        if (token) {
          await request("/api/telegram/token", { method: "POST", payload: { bot_token: token }, native: true });
          writes++;
          $("#telegram-token").value = "";
        }
        if (admin) {
          await request("/api/telegram/admins", { method: "POST", payload: { tg_id: admin }, native: true });
          writes++;
        }
        await request("/api/telegram/settings", {
          method: "POST", payload: { enabled: $("#telegram-enabled").checked, notify: existing.notify || {} }, native: true
        });
        say(text("تنظیمات ربات ذخیره شد. اکنون سرویس را راه‌اندازی یا پیام آزمایشی ارسال کنید.", "Bot settings saved. You can now start the service or send a test message."), "success");
        await refreshServices();
      } catch (error) {
        if (writes) throw new Error(text("بخشی از تنظیمات ذخیره شد؛ عملیات کامل نشد. ", "Some settings were saved; the operation did not finish. ") + error.message);
        throw error;
      }
    });
  });
  $("#telegram-start").addEventListener("click", () => action($("#telegram-start"), async () => {
    await request("/__manage/api/telegram/start", { method: "POST", payload: {}, timeout: 45000 });
    say(text("درخواست راه‌اندازی سرویس ربات انجام شد؛ وضعیت heartbeat را دوباره بررسی کنید.", "The bot service start request completed. Refresh to check its heartbeat."), "success");
    await refreshServices();
  }));
  $("#telegram-test").addEventListener("click", () => action($("#telegram-test"), async () => {
    const result = await request("/api/telegram/test", { method: "POST", payload: {}, native: true, timeout: 90000 });
    const sent = Number(result.sent) || 0;
    say(text("ارسال آزمایشی برای " + sent + " گیرنده گزارش شد؛ تلگرام را بررسی کنید.", "Test delivery was reported for " + sent + " recipients. Check Telegram."), "success");
  }));
  $("#finish-setup").addEventListener("click", () => action($("#finish-setup"), async () => {
    await request("/__setup/api/complete", { method: "POST", payload: {} });
    location.assign("/");
  }));
  translate(savedLanguage === "en" ? "en" : "fa");
  refresh();
})();
