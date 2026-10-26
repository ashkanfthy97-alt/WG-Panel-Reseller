(() => {
  "use strict";
  const $ = (selector) => document.querySelector(selector);
  const ownerView = location.pathname.replace(/\/$/, "") === "/resellers";
  const text = (fa, en) => language === "fa" ? fa : en;
  let language = "fa", csrf = "", account = null, adminData = null, peerData = null, busy = false;
  const pending = new Map();
  try { if (localStorage.getItem("wgportal-language") === "en") language = "en"; } catch { /* Storage is optional. */ }

  function notice(message, kind = "") {
    $("#notice").textContent = message || "";
    $("#notice").className = "notice " + kind;
    $("#notice").hidden = !message;
  }
  function formResult(id, message, error = false) {
    if (!id) return notice(message, error ? "error" : "success");
    $(id).textContent = message;
    $(id).className = "form-result" + (error ? " error" : "");
  }
  function date(value) {
    if (!value) return "—";
    const parsed = new Date(typeof value === "number" ? value * 1000 : value);
    return Number.isNaN(parsed.getTime()) ? "—" : new Intl.DateTimeFormat(language === "fa" ? "fa-IR" : "en-GB", { dateStyle: "medium", timeStyle: "short" }).format(parsed);
  }
  function cell(row, value) {
    const td = row.insertCell(); td.textContent = String(value ?? "—"); return td;
  }
  function empty(table, message, columns) {
    const row = document.createElement("tr"), td = cell(row, message); td.colSpan = columns; table.replaceChildren(row);
  }
  async function api(path, { method = "GET", payload, key } = {}) {
    const headers = new Headers({ Accept: "application/json" });
    if (method !== "GET") {
      if (!csrf) throw new Error(text("وضعیت نشست را تازه کنید.", "Refresh the session first."));
      headers.set("X-Reseller-CSRF", csrf);
    }
    if (payload !== undefined) headers.set("Content-Type", "application/json");
    if (key) headers.set("Idempotency-Key", key);
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 90000);
    try {
      const response = await fetch(path, { method, headers, credentials: "same-origin", cache: "no-store", redirect: "error", signal: controller.signal, ...(payload !== undefined ? { body: JSON.stringify(payload) } : {}) });
      let data = {};
      if ((response.headers.get("Content-Type") || "").includes("application/json")) data = await response.json();
      if (data.csrf) csrf = data.csrf;
      if (!response.ok || data.error || data.ok === false) {
        const known = {
          invalid_credentials: text("نام کاربری یا رمز درست نیست.", "The username or password is incorrect."),
          panel_paused: text("اعتبار پنل تمام شده یا دسترسی متوقف است.", "The panel has expired or access is suspended."),
          rate_limited: text("تعداد تلاش‌ها زیاد است؛ کمی بعد دوباره تلاش کنید.", "Too many attempts. Try again later."),
          admin_required: text("با حساب مدیر اصلی وارد شوید.", "Sign in with the native panel owner account."),
          login_required: text("نشست پایان یافته؛ دوباره وارد شوید.", "Your session ended. Sign in again."),
          create_uncertain: text("نتیجهٔ ساخت نامشخص است؛ درخواست تازه نسازید و به مدیر اطلاع دهید.", "The creation outcome is uncertain. Do not submit a new request; contact the owner."),
          native_create_failed: text("ساخت تأیید نشد؛ پیش از تلاش تازه مدیر باید وضعیت هسته را بررسی کند.", "Creation was not confirmed. The owner must inspect the native panel before another attempt."),
          operation_not_repeatable: text("این درخواست نیازمند بررسی مدیر است و قابل تکرار نیست.", "This operation requires owner review and cannot be repeated.")
        };
        const error = new Error(known[data.error] || data.message || text("عملیات انجام نشد. اطلاعات و وضعیت سرویس را بررسی کنید.", "The operation failed. Check the details and service status."));
        error.code = data.error; error.status = response.status; throw error;
      }
      return data;
    } catch (error) {
      if (error.name === "AbortError") {
        const uncertain = new Error(text("پاسخ دریافت نشد؛ نتیجه ممکن است ثبت شده باشد. پیش از تلاش تازه وضعیت را بررسی کنید.", "No response was received; the operation may have completed. Check its status before trying again."));
        uncertain.code = "timeout"; throw uncertain;
      }
      throw error;
    } finally { clearTimeout(timer); }
  }
  async function receipt(operation, payload) {
    const signature = operation + ":" + JSON.stringify(payload);
    if (pending.has(signature)) return pending.get(signature);
    const digest = crypto.subtle
      ? Array.from(new Uint8Array(await crypto.subtle.digest("SHA-256", new TextEncoder().encode(signature)))).map((value) => value.toString(16).padStart(2, "0")).join("")
      : encodeURIComponent(signature);
    const storage = "wgportal-operation:" + digest;
    let key;
    try { key = sessionStorage.getItem(storage); } catch { /* In-memory retry still works. */ }
    if (!key || !/^[A-Za-z0-9_-]{16,80}$/.test(key)) key = Array.from(crypto.getRandomValues(new Uint8Array(16)), (value) => value.toString(16).padStart(2, "0")).join("");
    const value = { key, storage, signature };
    pending.set(signature, value);
    try { sessionStorage.setItem(storage, key); } catch { /* Keep the same key in memory. */ }
    return value;
  }
  function finishReceipt(value) {
    pending.delete(value.signature);
    try { sessionStorage.removeItem(value.storage); } catch { /* Storage is optional. */ }
  }
  async function action(button, result, operation) {
    if (busy) return;
    busy = true;
    button.disabled = true;
    button.setAttribute("aria-busy", "true");
    try { await operation(); }
    catch (error) {
      formResult(result, error.message, true);
      if (error.status === 401 || error.code === "panel_paused") await load();
    } finally {
      busy = false; button.disabled = false; button.removeAttribute("aria-busy");
      if (button.closest("#peer-form") && $("#peer-form").dataset.locked) button.disabled = true;
    }
  }
  function translate(next) {
    language = next === "en" ? "en" : "fa";
    document.documentElement.lang = language;
    document.documentElement.dir = language === "fa" ? "rtl" : "ltr";
    document.querySelectorAll("[data-fa][data-en]").forEach((node) => { node.textContent = node.dataset[language]; });
    document.querySelectorAll("[data-fa-label][data-en-label]").forEach((node) => { node.setAttribute("aria-label", node.dataset[language + "Label"]); });
    $("#language-toggle").textContent = language === "fa" ? "EN" : "فارسی";
    $("#language-toggle").setAttribute("aria-label", text("Switch to English", "تغییر زبان به فارسی"));
    $("#page-title").textContent = ownerView ? text("مدیریت نمایندگی‌ها", "Manage resellers") : text("مدیریت سرویس‌های شما", "Manage your services");
    $("#page-copy").textContent = ownerView ? text("حساب، اعتبار ماهانه و دسترسی نمایندگان را از همین‌جا کنترل کنید.", "Manage reseller accounts, prepaid validity and access here.") : text("فقط سرویس‌ها و Interfaceهای اختصاص‌یافته به حساب شما در این بخش نمایش داده می‌شوند.", "This workspace only shows services and interfaces assigned to your account.");
    $("#workspace-label").textContent = ownerView ? text("مدیریت مادر", "Owner workspace") : text("پنل نمایندگی", "Reseller panel");
    $("#breadcrumb-title").textContent = ownerView ? text("نماینده‌ها", "Resellers") : text("سرویس‌های من", "My services");
    document.title = $("#page-title").textContent + " · WG Portal";
    try { localStorage.setItem("wgportal-language", language); } catch { /* The current page still changes language. */ }
    if (adminData) renderAdmin(adminData);
    if (account) renderAccount();
    if (peerData && account?.active) renderPeers(peerData);
  }
  function ownerVisibility(authorized) {
    $("#admin-view").hidden = !authorized;
    $("#admin-login").hidden = authorized || !ownerView;
    $("#owner-navigation").hidden = !authorized;
    $("#reseller-navigation").hidden = ownerView;
    $("#add-reseller").hidden = !authorized;
    if (!authorized) {
      adminData = null;
      $("#account-rows").replaceChildren();
      $("#interface-options").replaceChildren();
      $("#audit-list").replaceChildren();
      $("#operations-list").replaceChildren();
      $("#account-password").value = "";
      if ($("#account-dialog").open) $("#account-dialog").close();
    }
  }
  function renderAccount() {
    $("#login-view").hidden = Boolean(account);
    $("#reseller-view").hidden = !account;
    if (!account) return;
    $("#account-name").textContent = account.username;
    $("#account-expiry").textContent = text("اعتبار تا: ", "Valid until: ") + date(account.expires_at);
    const active = Boolean(account.active);
    $("#account-state").textContent = active ? text("فعال", "Active") : text("متوقف", "Paused");
    $("#account-state").className = "pill" + (active ? "" : " paused");
    $("#paused-view").hidden = active;
    $("#active-view").hidden = !active;
    if (!active) { peerData = null; $("#peer-rows").replaceChildren(); closeQR(); }
  }
  function renderAdmin(data) {
    adminData = data;
    ownerVisibility(true);
    const selected = new Set(Array.from(document.querySelectorAll("#interface-options input:checked"), (node) => Number(node.value)));
    $("#interface-options").replaceChildren(...(data.interfaces || []).map((iface) => {
      const label = document.createElement("label"), input = document.createElement("input"), caption = document.createElement("span");
      input.type = "checkbox"; input.value = iface.id; input.checked = selected.has(iface.id); caption.textContent = iface.name;
      label.append(input, caption); return label;
    }));
    if (!(data.interfaces || []).length) $("#interface-options").textContent = text("ابتدا یک Interface از پنل اصلی بسازید.", "Create an interface in the native panel first.");
    const rows = $("#account-rows"); rows.replaceChildren();
    const accounts = data.accounts || [];
    const number = new Intl.NumberFormat(language === "fa" ? "fa-IR" : "en-GB");
    $("#account-count").textContent = number.format(accounts.length);
    $("#summary-total").textContent = number.format(accounts.length);
    $("#summary-active").textContent = number.format(accounts.filter((item) => item.active).length);
    $("#summary-expired").textContent = number.format(accounts.filter((item) => item.enabled && !item.active).length);
    $("#summary-suspended").textContent = number.format(accounts.filter((item) => !item.enabled).length);
    for (const item of accounts) {
      const row = rows.insertRow();
      const identity = document.createElement("div"); identity.className = "account-identity";
      const avatar = document.createElement("span"); avatar.className = "account-avatar"; avatar.setAttribute("aria-hidden", "true"); avatar.textContent = Array.from(String(item.username || "?"))[0];
      const name = document.createElement("strong"); name.textContent = item.username;
      identity.append(avatar, name); row.insertCell().append(identity);
      const state = document.createElement("span"); state.className = "pill " + (item.active ? "active" : item.enabled ? "expired" : "suspended");
      state.textContent = item.active ? text("فعال", "Active") : item.enabled ? text("اعتبار پایان‌یافته", "Expired") : text("متوقف توسط مدیر", "Suspended by owner");
      row.insertCell().append(state);
      cell(row, date(item.expires_at)); cell(row, item.max_peers);
      const actions = document.createElement("div"); actions.className = "actions";
      const days = document.createElement("input"); days.type = "number"; days.min = "1"; days.max = "366"; days.value = "30"; days.setAttribute("aria-label", text("روزهای تمدید برای ", "Renewal days for ") + item.username);
      const renew = document.createElement("button"); renew.type = "button"; renew.textContent = text("تمدید", "Renew");
      renew.addEventListener("click", () => action(renew, null, async () => {
        if (!days.reportValidity()) return;
        const payload = { days: Number(days.value) };
        const operation = await receipt("renew:" + item.id, payload);
        await api("/resellers/api/accounts/" + item.id + "/renew", { method: "POST", payload, key: operation.key });
        finishReceipt(operation);
        notice(text("اعتبار نماینده تمدید شد.", "The reseller validity was renewed."), "success");
        try { await loadAdmin(); } catch { notice(text("تمدید انجام شد، اما فهرست تازه دریافت نشد. فقط وضعیت را تازه کنید؛ تمدید را دوباره ثبت نکنید.", "Renewal succeeded, but the refreshed list could not be loaded. Refresh status only; do not submit renewal again.")); }
      }));
      const access = document.createElement("button"); access.type = "button"; access.className = "secondary"; access.textContent = item.enabled ? text("توقف دسترسی", "Suspend access") : text("اجازهٔ دسترسی", "Allow access");
      access.addEventListener("click", () => action(access, null, async () => {
        await api("/resellers/api/accounts/" + item.id + "/access", { method: "POST", payload: { enabled: !item.enabled } });
        notice(text("وضعیت دسترسی ذخیره شد؛ تاریخ اعتبار مستقل است.", "Access was updated; the validity date is independent."), "success"); await loadAdmin();
      }));
      actions.append(days, renew, access); row.insertCell().append(actions);
    }
    if (!accounts.length) empty(rows, text("هنوز نماینده‌ای ساخته نشده است.", "No reseller accounts yet."), 5);
    $("#audit-list").replaceChildren(...(data.audit || []).slice(0, 30).map((event) => {
      const li = document.createElement("li"); li.textContent = date(event.created_at) + " · " + String(event.action || "—") + " · " + String(event.actor || "—"); return li;
    }));
    if (!(data.audit || []).length) $("#audit-list").textContent = text("رویدادی ثبت نشده است.", "No recorded events.");
    const operations = data.operations || [];
    $("#operations-card").hidden = !operations.length;
    $("#operations-list").replaceChildren(...operations.map((item) => {
      const li = document.createElement("li"); li.textContent = "#" + item.id + " · " + text("نماینده ", "Reseller ") + item.reseller_id + " · " + String(item.state || "pending") + " · " + date(item.created_at); return li;
    }));
  }
  function closeQR() {
    $("#qr-image").removeAttribute("src");
    if ($("#qr-dialog").open) $("#qr-dialog").close();
  }
  function renderPeers(data) {
    peerData = data;
    const select = $("#peer-interface"), previous = select.value;
    select.replaceChildren(...(data.interfaces || []).map((item) => {
      const option = document.createElement("option"); option.value = item.id; option.textContent = item.name; return option;
    }));
    if (Array.from(select.options).some((option) => option.value === previous)) select.value = previous;
    const rows = $("#peer-rows"); rows.replaceChildren();
    for (const peer of data.peers || []) {
      const row = rows.insertRow(); cell(row, peer.name);
      const state = String(peer.panel_status || peer.status || "unknown");
      cell(row, String(peer.conn_status || "—") + " / " + state);
      const used = Math.max(0, Number(peer.used_bytes) || 0) / 1073741824;
      cell(row, used.toFixed(2) + " GiB / " + String(peer.data_limit ?? "—") + " " + String(peer.limit_unit || ""));
      cell(row, peer.ttl_seconds == null ? "—" : Math.max(0, Math.ceil(Number(peer.ttl_seconds) / 86400)) + text(" روز", " days"));
      const actions = document.createElement("div"); actions.className = "actions";
      const config = document.createElement("a"); config.className = "button secondary"; config.href = "/reseller/api/peers/" + Number(peer.id) + "/config"; config.textContent = text("کانفیگ", "Config"); config.setAttribute("download", "");
      const qr = document.createElement("button"); qr.type = "button"; qr.className = "secondary"; qr.textContent = "QR";
      qr.addEventListener("click", () => { $("#qr-image").src = "/reseller/api/peers/" + Number(peer.id) + "/qr"; $("#qr-dialog").showModal(); });
      const toggle = document.createElement("button"); toggle.type = "button"; toggle.className = "secondary";
      const enabled = ["online", "enabled", "active"].includes(state.toLowerCase());
      toggle.textContent = enabled ? text("غیرفعال", "Disable") : text("فعال", "Enable");
      toggle.addEventListener("click", () => action(toggle, null, async () => {
        await api("/reseller/api/peers/" + Number(peer.id) + "/" + (enabled ? "disable" : "enable"), { method: "POST", payload: {} });
        notice(text("وضعیت سرویس تغییر کرد.", "The service access was updated."), "success"); await loadPeers();
      }));
      actions.append(config, qr, toggle); row.insertCell().append(actions);
    }
    if (!(data.peers || []).length) empty(rows, text("هنوز سرویسی برای این حساب وجود ندارد.", "No services belong to this account yet."), 5);
  }
  async function loadAdmin() {
    try { renderAdmin(await api("/resellers/api/accounts")); }
    catch (error) { ownerVisibility(false); throw error; }
  }
  async function loadPeers() { if (account?.active) renderPeers(await api("/reseller/api/peers")); }
  async function load() {
    $("#loading").hidden = false;
    try {
      const result = await api("/reseller/api/session"); account = result.account || null;
      if (ownerView) await loadAdmin();
      else { renderAccount(); if (account?.active) await loadPeers(); }
    } catch (error) {
      if (ownerView) ownerVisibility(false);
      else if (error.status === 401 || error.status === 403) { peerData = null; $("#peer-rows").replaceChildren(); $("#active-view").hidden = true; }
      notice(error.message, "error");
    } finally { $("#loading").hidden = true; }
  }
  $("#add-reseller").addEventListener("click", () => {
    if (!adminData || busy) return;
    $("#account-result").textContent = "";
    $("#account-dialog").showModal();
    $("#account-username").focus();
  });
  $("#close-account-dialog").addEventListener("click", () => $("#account-dialog").close());
  $("#account-dialog").addEventListener("close", () => {
    $("#account-password").value = "";
    if (adminData) $("#add-reseller").focus();
  });
  $("#language-toggle").addEventListener("click", () => translate(language === "fa" ? "en" : "fa"));
  $("#admin-refresh").addEventListener("click", () => action($("#admin-refresh"), null, load));
  $("#reseller-refresh").addEventListener("click", () => action($("#reseller-refresh"), null, load));
  $("#login-form").addEventListener("submit", (event) => {
    event.preventDefault(); const payload = Object.fromEntries(new FormData(event.currentTarget));
    action(event.submitter || event.currentTarget.querySelector('[type="submit"]'), "#login-result", async () => {
      await api("/reseller/api/login", { method: "POST", payload });
      $("#login-password").value = ""; await load();
    });
  });
  $("#logout").addEventListener("click", () => action($("#logout"), null, async () => {
    await api("/reseller/api/logout", { method: "POST", payload: {} });
    csrf = ""; account = null; peerData = null; closeQR(); $("#peer-rows").replaceChildren(); await load();
  }));
  $("#account-form").addEventListener("submit", (event) => {
    event.preventDefault(); const payload = Object.fromEntries(new FormData(event.currentTarget));
    payload.interfaces = Array.from(document.querySelectorAll("#interface-options input:checked"), (node) => Number(node.value));
    payload.max_peers = Number(payload.max_peers);
    if (!payload.interfaces.length) return formResult("#account-result", text("حداقل یک Interface انتخاب کنید.", "Select at least one interface."), true);
    action(event.submitter || event.currentTarget.querySelector('[type="submit"]'), "#account-result", async () => {
      await api("/resellers/api/accounts", { method: "POST", payload });
      $("#account-password").value = "";
      $("#account-dialog").close();
      $("#account-form").reset();
      notice(text("حساب ساخته شد. برای فعال‌شدن، اعتبار آن را از فهرست تمدید کنید.", "Account created. Renew its validity in the list to activate it."), "success");
      try { await loadAdmin(); } catch { notice(text("حساب ساخته شد، اما فهرست تازه دریافت نشد. وضعیت را تازه کنید.", "The account was created, but the list could not be refreshed. Refresh status.")); }
    });
  });
  $("#peer-form").addEventListener("submit", (event) => {
    event.preventDefault();
    if (event.currentTarget.dataset.locked) return;
    const payload = Object.fromEntries(new FormData(event.currentTarget));
    ["iface_id", "days", "volume_gb"].forEach((key) => { payload[key] = Number(payload[key]); });
    action(event.submitter || event.currentTarget.querySelector('[type="submit"]'), "#peer-result", async () => {
      const operation = await receipt("create-peer:" + account.id, payload);
      try {
        await api("/reseller/api/peers", { method: "POST", payload, key: operation.key });
        finishReceipt(operation);
        formResult("#peer-result", text("سرویس ساخته شد و در فهرست پایین قابل تحویل است.", "The service was created and can be delivered from the list below."));
        try { await loadPeers(); } catch { formResult("#peer-result", text("ساخت سرویس موفق بود، اما فهرست تازه دریافت نشد. فقط وضعیت را تازه کنید؛ سرویس را دوباره نسازید.", "Creation succeeded, but the list could not be refreshed. Refresh status only; do not create the service again.")); }
      } catch (error) {
        if (["create_uncertain", "native_create_failed", "operation_not_repeatable", "timeout"].includes(error.code)) $("#peer-form").dataset.locked = "true";
        throw error;
      }
    });
  });
  $("#qr-dialog").addEventListener("close", () => $("#qr-image").removeAttribute("src"));
  $("#qr-image").addEventListener("error", () => { closeQR(); notice(text("تصویر QR دریافت نشد؛ وضعیت حساب و سرویس را تازه کنید.", "The QR image could not be loaded. Refresh your account and service status."), "error"); });
  translate(language); load();
})();
