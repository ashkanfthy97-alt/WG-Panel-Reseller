(() => {
  "use strict";
  const $ = (selector) => document.querySelector(selector);
  const ownerView = location.pathname.replace(/\/$/, "") === "/resellers";
  const text = (fa, en) => language === "fa" ? fa : en;
  let language = "fa", csrf = "", nativeCSRF = "", account = null, adminData = null, peerData = null, childrenData = null, botData = null, busy = false;
  const pending = new Map();
  let provisionDefaultsLoaded = false, provisionDefaultsDirty = false, provisionDefaultsData = null;
  let securityData = null, securityPending = false, securityLoading = false, activationData = null;
  let creditData = null, creditRequest = 0, botStatusTimer = null, botStatusBusy = false, loggingOut = false;
  const requestsInFlight = new Set();
  const sections = {
    overview: ["نمای کلی", "Overview", "وضعیت حساب، اعتبار و دسترسی نمایندگی خود را ببینید.", "Review your reseller account, validity and access."],
    services: ["سرویس‌ها و کانفیگ‌ها", "Services and configs", "سرویس بسازید و کانفیگ، لینک کاربر و تمدید مشتری‌ها را از همین بخش مدیریت کنید.", "Create services and manage customer configs, user links and renewals in one place."],
    children: ["نماینده‌های من", "My subresellers", "زیرنماینده اضافه کنید و اینترفیس اختصاصی آن را راه‌اندازی کنید.", "Add subresellers and provision their dedicated interfaces."],
    security: ["امنیت و ورود دومرحله‌ای", "Security and two-factor login", "ورود دومرحله‌ای و کدهای بازیابی حساب خود را مدیریت کنید.", "Manage your account’s two-factor login and recovery codes."],
    bot: ["بات تلگرام", "Telegram bot", "بات اختصاصی نمایندگی خود را متصل و مدیریت کنید.", "Connect and manage your dedicated reseller bot."]
  };
  function sectionFromHash() {
    const value = location.hash.slice(1);
    if (value === "create-peer" || value === "peers") return "services";
    return value === "children-workspace" ? "children" : Object.hasOwn(sections, value) ? value : "overview";
  }
  let selectedSection = sectionFromHash();
  try { if (localStorage.getItem("wgportal-language") === "en") language = "en"; } catch { /* Storage is optional. */ }

  function renderSection({focus = false} = {}) {
    if (ownerView) return;
    const enabled = Boolean(account?.active);
    if (account && !enabled && selectedSection !== "security") {
      selectedSection = "overview";
    }
    if (account && location.hash !== "#" + selectedSection) history.replaceState(null, "", "#" + selectedSection);
    $("#reseller-navigation").hidden = !account;
    document.querySelectorAll("[data-reseller-section]").forEach(link => {
      const key = link.dataset.resellerSection;
      link.hidden = !enabled && !["overview", "security"].includes(key);
      const active = key === selectedSection;
      link.classList.toggle("active", active);
      if (active) link.setAttribute("aria-current", "page");
      else link.removeAttribute("aria-current");
    });
    document.querySelectorAll("[data-reseller-panel]").forEach(panel => {
      const key = panel.dataset.resellerPanel;
      panel.hidden = !account || key !== selectedSection || (!enabled && !["overview", "security"].includes(key));
    });
    const values = sections[selectedSection];
    $("#page-title").textContent = text(values[0], values[1]);
    $("#breadcrumb-title").textContent = text(values[0], values[1]);
    $("#page-copy").textContent = text(values[2], values[3]);
    document.title = text(values[0], values[1]) + " · WG Portal";
    if (focus) $("#page-title").focus({preventScroll: true});
    scheduleBotStatus();
    if (selectedSection === "security" && account && !securityData) loadSecurity();
  }
  function changeSection() {
    if (ownerView) return;
    const previous = selectedSection;
    selectedSection = sectionFromHash();
    if (previous !== selectedSection) {
      if (previous === "security") clearSecuritySecrets();
      if (previous === "bot") $("#bot-token").value = "";
      if (previous === "services") {
        for (const id of ["#peer-edit-dialog", "#user-link-dialog"]) if ($(id).open) $(id).close();
        closeQR();
      }
      if (previous === "children") {
        if ($("#child-dialog").open) $("#child-dialog").close();
        $("#signin-delivery").hidden = true;
      }
      notice("");
    }
    renderSection({focus: true});
  }
  window.addEventListener("hashchange", changeSection);

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
  async function api(path, { method = "GET", payload, key, bulkResult = false } = {}) {
    if (loggingOut && path !== "/reseller/api/logout") throw new Error(text("در حال خروج…", "Signing out…"));
    const headers = new Headers({ Accept: "application/json" });
    if (method !== "GET") {
      if (!csrf) throw new Error(text("وضعیت نشست را تازه کنید.", "Refresh the session first."));
      headers.set("X-Reseller-CSRF", csrf);
      if (path === "/reseller/api/bot") {
        if (!nativeCSRF) throw new Error(text("ابتدا وضعیت بات را تازه کنید.", "Refresh bot status first."));
        headers.set("X-CSRFToken", nativeCSRF);
      }
    }
    if (payload !== undefined) headers.set("Content-Type", "application/json");
    if (key) headers.set("Idempotency-Key", key);
    const controller = new AbortController();
    requestsInFlight.add(controller);
    const timer = setTimeout(() => controller.abort(), 90000);
    try {
      const response = await fetch(path, { method, headers, ...(path === "/reseller/api/bot" ? {referrerPolicy:"same-origin"} : {}), credentials: "same-origin", cache: "no-store", redirect: "error", signal: controller.signal, ...(payload !== undefined ? { body: JSON.stringify(payload) } : {}) });
      let data = {};
      const jsonResponse = (response.headers.get("Content-Type") || "").includes("application/json");
      if (jsonResponse) data = await response.json();
      else if (path === "/reseller/api/bot" && response.status === 400) data.error = "bot_csrf_rejected";
      if (data.csrf) csrf = data.csrf;
      if (data.native_csrf && path === "/reseller/api/bot") nativeCSRF = data.native_csrf;
      if (bulkResult && Array.isArray(data.created) && Array.isArray(data.failed)) return data;
      if (!response.ok || data.error || data.ok === false) {
        const known = {
          bot_invalid_token: text("توکن بات معتبر نیست؛ توکن کامل BotFather را دوباره وارد کنید.", "Invalid bot token. Enter the complete token from BotFather."),
          invalid_bot_token: text("توکن بات معتبر نیست؛ توکن کامل BotFather را دوباره وارد کنید.", "Invalid bot token. Enter the complete token from BotFather."),
          invalid_owner_id: text("شناسهٔ عددی حساب تلگرام خودتان را وارد کنید؛ نام کاربری یا شمارهٔ بات کافی نیست.", "Enter your personal numeric Telegram ID, not a username or bot ID."),
          telegram_invalid_token: text("تلگرام توکن را نپذیرفت؛ توکن بات را بررسی کنید.", "Telegram rejected the token. Check your bot credentials."),
          telegram_connection_failed: text("سرور به تلگرام وصل نشد؛ دسترسی خروجی VPS به تلگرام را بررسی کنید.", "The server could not reach Telegram. Check outbound access from your VPS."),
          telegram_unavailable: text("ارتباط سرور با تلگرام کامل نشد؛ کمی بعد دوباره تلاش کنید.", "The Telegram request did not complete. Try again shortly."),
          telegram_response_invalid: text("پاسخ معتبر از تلگرام دریافت نشد؛ دسترسی شبکهٔ سرور را بررسی کنید.", "Telegram returned an invalid response; check the server network."),
          invalid_csrf: text("نشست یا مبدأ درخواست معتبر نیست؛ صفحه را تازه کنید و از نشانی اصلی پنل استفاده کنید.", "The session or request origin is invalid. Reload and use the panel’s original address."),
          invalid_bot_settings: text("توکن و شناسهٔ عددی مالک بات را کامل وارد کنید.", "Enter both the bot token and its owner’s numeric ID."),
          invalid_panel_url: text("نشانی عمومی پنل معتبر نیست؛ مدیر باید نشانی پنل را بررسی کند.", "The public panel address is invalid; ask the owner to check it."),
          telegram_conflict: text("این بات در جای دیگری در حال اجراست؛ اجرای قبلی همین بات را متوقف کنید.", "This bot is already running elsewhere. Stop its previous runner."),
          telegram_rate_limited: text("تلگرام موقتاً درخواست‌ها را محدود کرده؛ کمی بعد دوباره تلاش کنید.", "Telegram is temporarily limiting requests. Try again shortly."),
          native_bot_conflict: text("این توکن به بات اصلی پنل وصل است؛ برای نماینده یک بات مستقل در BotFather بسازید.", "This token belongs to the panel’s main bot. Create a separate BotFather bot for the reseller."),
          bot_webhook_in_use: text("این بات به سرویس دیگری با Webhook متصل است؛ ابتدا اتصال قبلی آن را بردارید.", "This bot uses another service’s webhook. Disconnect that integration first."),
          bot_already_assigned: text("این بات قبلاً به نمایندهٔ دیگری متصل شده است؛ هر نماینده باید بات خودش را داشته باشد.", "This bot is assigned to another reseller. Each reseller needs its own bot."),
          bot_csrf_rejected: text("نشست امنیتی بات معتبر نیست؛ صفحه را تازه کنید و دوباره مشخصات بات را ذخیره کنید.", "The bot security session is invalid. Reload this page and save the bot credentials again."),
          bot_configuration_failed: text("ذخیرهٔ بات در سرور کامل نشد؛ تنظیمات کلید رمزگذاری و دسترسی ذخیره‌سازی را بررسی کنید.", "The server could not save the bot. Check its encryption key and storage access."),
          invalid_credentials: text("نام کاربری یا رمز درست نیست.", "The username or password is incorrect."),
          panel_paused: text("اعتبار پنل تمام شده یا دسترسی متوقف است.", "The panel has expired or access is suspended."),
          rate_limited: text("تعداد تلاش‌ها زیاد است؛ کمی بعد دوباره تلاش کنید.", "Too many attempts. Try again later."),
          admin_required: text("با حساب مدیر اصلی وارد شوید.", "Sign in with the native panel owner account."),
          login_required: text("نشست پایان یافته؛ دوباره وارد شوید.", "Your session ended. Sign in again."),
          interface_required: text("پیش از تأیید پرداخت، یک اینترفیس واقعی برای همین نماینده تعیین کنید.", "Assign a real interface to this reseller before approving payment."),
          interface_review_required: text("نتیجهٔ ساخت اینترفیس این نماینده نامشخص است. اینترفیس واقعی را بررسی و با گزینهٔ تعیین اینترفیس ثبت کنید؛ ساخت را تکرار نکنید.", "This reseller’s interface creation outcome is uncertain. Inspect the native interface and assign it explicitly; do not repeat creation."),
          provision_defaults_required: text("مدیر اصلی باید ابتدا پیش‌فرض ساخت اینترفیس نمایندگان را ذخیره کند.", "The owner must save the reseller interface defaults first."),
          interface_defaults_required: text("مدیر اصلی باید ابتدا پیش‌فرض ساخت اینترفیس نمایندگان را ذخیره کند.", "The owner must save the reseller interface defaults first."),
          no_available_ports: text("همهٔ پورت‌های ۵۱۸۲۵ تا ۵۲۰۰۰ استفاده یا رزرو شده‌اند؛ با مدیر تماس بگیرید.", "All ports from 51825 to 52000 are occupied or reserved; contact the owner."),
          port_scan_failed: text("بررسی پورت‌های آزاد کامل نشد؛ حساب تازه نسازید و وضعیت را با مدیر بررسی کنید.", "The free-port check did not complete. Check the status with the owner before creating another account."),
          interface_create_uncertain: text("نتیجهٔ ساخت اینترفیس قطعی نیست؛ حساب را دوباره نسازید و از مدیر بخواهید وضعیت را بررسی کند.", "The interface creation outcome is uncertain. Do not recreate the account; ask the owner to inspect it."),
          create_uncertain: text("نتیجهٔ ساخت نامشخص است؛ درخواست تازه نسازید و به مدیر اطلاع دهید.", "The creation outcome is uncertain. Do not submit a new request; contact the owner."),
          native_create_failed: text("ساخت تأیید نشد؛ پیش از تلاش تازه مدیر باید وضعیت هسته را بررسی کند.", "Creation was not confirmed. The owner must inspect the native panel before another attempt."),
          operation_not_repeatable: text("این درخواست نیازمند بررسی مدیر است و قابل تکرار نیست.", "This operation requires owner review and cannot be repeated.")
        };
        const error = new Error(known[data.error] || data.message || text("عملیات انجام نشد. اطلاعات و وضعیت سرویس را بررسی کنید.", "The operation failed. Check the details and service status."));
        error.code = data.error; error.status = response.status; error.data = data; throw error;
      }
      return data;
    } catch (error) {
      if (error.name === "AbortError") {
        const uncertain = new Error(text("پاسخ دریافت نشد؛ نتیجه ممکن است ثبت شده باشد. پیش از تلاش تازه وضعیت را بررسی کنید.", "No response was received; the operation may have completed. Check its status before trying again."));
        uncertain.code = "timeout"; throw uncertain;
      }
      throw error;
    } finally { clearTimeout(timer); requestsInFlight.delete(controller); }
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
      if (button.dataset.uncertain) button.disabled = true;
      if (button.closest("#peer-form") && $("#peer-form").dataset.locked) button.disabled = true;
      if (button.closest("#account-form") && $("#account-form").dataset.locked) button.disabled = true;
      if (button.closest("#child-form") && $("#child-form").dataset.locked) button.disabled = true;
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
    $("#page-copy").textContent = ownerView ? text("حساب، اعتبار ماهانه و دسترسی نمایندگان را از همین‌جا کنترل کنید.", "Manage reseller accounts, prepaid validity and access here.") : text("فقط سرویس‌ها و اینترفیسهای اختصاص‌یافته به حساب شما در این بخش نمایش داده می‌شوند.", "This workspace only shows services and interfaces assigned to your account.");
    $("#workspace-label").textContent = ownerView ? text("مدیریت مادر", "Owner workspace") : text("پنل نمایندگی", "Reseller panel");
    $("#breadcrumb-title").textContent = ownerView ? text("نماینده‌ها", "Resellers") : text("سرویس‌های من", "My services");
    document.title = $("#page-title").textContent + " · WG Portal";
    try { localStorage.setItem("wgportal-language", language); } catch { /* The current page still changes language. */ }
    if (adminData) renderAdmin(adminData);
    if (account) renderAccount();
    if (peerData && account?.active) renderPeers(peerData);
    if (childrenData && account?.active) renderChildren(childrenData);
    if (botData && account?.active) renderBot(botData);
    if (creditData && $("#credit-dialog").open) renderCredit(creditData);
    renderProvisionStatus();
    if (securityData) renderSecurity();
    renderSection();
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
      $("#audit-list").replaceChildren();
      $("#operations-list").replaceChildren();
      $("#account-password").value = "";
      if ($("#account-dialog").open) $("#account-dialog").close();
    }
  }
  function renderAccount() {
    $("#reseller-view").hidden = !account;
    $("#reseller-add-tab").hidden = ownerView || !account?.active;
    if (!account) { if (!ownerView) location.replace("/login"); return; }
    $("#account-name").textContent = account.username;
    $("#account-expiry").textContent = text("اعتبار تا: ", "Valid until: ") + date(account.expires_at);
    const active = Boolean(account.active);
    $("#account-state").textContent = account.approval_status !== "approved" ? text("در انتظار تأیید", "Awaiting approval") : active ? text("فعال", "Active") : text("متوقف", "Paused");
    $("#account-state").className = "pill" + (active ? "" : " paused");
    const awaiting = account.approval_status !== "approved";
    const paymentFee = Number.isSafeInteger(account.creation_fee_toman) ? account.creation_fee_toman : activationData?.amount;
    $("#payment-fee").textContent = Number.isSafeInteger(paymentFee) ? new Intl.NumberFormat(language === "fa" ? "fa-IR" : "en-GB").format(paymentFee) + text(" تومان", " toman") : text("از مدیر اصلی بپرسید.", "Ask the owner.");
    $("#payment-pending-panel").hidden = !awaiting;
    $("#paused-view").hidden = active || awaiting;
    $("#active-view").hidden = !active;
    if (!active) { stopBotStatus(); peerData = null; childrenData = null; botData = null; nativeCSRF = ""; $("#bot-token").value = ""; $("#peer-rows").replaceChildren(); $("#child-rows").replaceChildren(); $("#add-child").disabled = true; $("#child-password").value = ""; if ($("#child-dialog").open) $("#child-dialog").close(); if ($("#user-link-dialog").open) $("#user-link-dialog").close(); if ($("#peer-edit-dialog").open) $("#peer-edit-dialog").close(); closeQR(); }
    renderSection();
  }
  function renderAdmin(data) {
    adminData = data;
    ownerVisibility(true);
    const rows = $("#account-rows"); rows.replaceChildren();
    const accounts = data.accounts || [];
    const number = new Intl.NumberFormat(language === "fa" ? "fa-IR" : "en-GB");
    $("#account-count").textContent = number.format(accounts.length);
    $("#summary-total").textContent = number.format(accounts.length);
    $("#summary-active").textContent = number.format(accounts.filter((item) => item.active).length);
    $("#summary-expired").textContent = number.format(accounts.filter((item) => item.enabled && !item.active).length);
    $("#summary-suspended").textContent = number.format(accounts.filter((item) => !item.enabled).length);
    $("#summary-subresellers").textContent = Number.isSafeInteger(data.summary?.subresellers) ? number.format(data.summary.subresellers) : "—";
    $("#summary-creation-fees").textContent = Number.isSafeInteger(data.summary?.total_creation_fee_toman) ? number.format(data.summary.total_creation_fee_toman) : "—";
    $("#summary-pending").textContent = number.format(accounts.filter(item => item.approval_status !== "approved").length);
    for (const item of accounts) {
      const row = rows.insertRow(); row.dataset.accountId = String(item.id);
      const identity = document.createElement("div"); identity.className = "account-identity";
      const avatar = document.createElement("span"); avatar.className = "account-avatar"; avatar.setAttribute("aria-hidden", "true"); avatar.textContent = Array.from(String(item.username || "?"))[0];
      const name = document.createElement("strong"); name.textContent = item.username;
      identity.append(avatar, name); row.insertCell().append(identity);
      if (item.twofa_enabled) {const badge=document.createElement("small"); badge.textContent=text("🔐 ورود دومرحله‌ای فعال", "🔐 Two-factor login enabled"); identity.append(badge);}
      const created = document.createElement("small"); created.textContent = "#" + item.id + " · " + date(item.created_at); name.append(created);
      const state = document.createElement("span"); state.className = "pill " + (item.active ? "active" : item.enabled ? "expired" : "suspended");
      state.textContent = item.deleted || item.deleted_at ? text("دسترسی حذف‌شده", "Archived access") : item.approval_status !== "approved" ? text("در انتظار تأیید پرداخت", "Awaiting payment approval") : item.active ? text("فعال", "Active") : item.enabled ? text("اعتبار پایان‌یافته", "Expired") : text("متوقف توسط مدیر", "Suspended by owner");
      row.insertCell().append(state);
      cell(row, date(item.expires_at)); cell(row, item.max_peers);
      cell(row, item.parent_username || text("مدیر اصلی", "Owner"));
      cell(row, number.format(item.direct_child_count || 0) + " / " + number.format(item.descendant_count || 0));
      cell(row, number.format(item.creation_fee_toman || 0));
      cell(row, interfaceText(item, data.interfaces || []));
      cell(row, item.approval_status === "approved" ? text("تأییدشده", "Approved") + " · " + date(item.approved_at) + " · " + String(item.approved_by || "—") : text("در انتظار تأیید مدیر اصلی", "Awaiting owner approval"));
      if (item.deleted || item.deleted_at) {
        cell(row, text("حساب برای سوابق نگه داشته شده؛ پنل و بات غیرفعال‌اند. Peerها و زیرمجموعه‌ها حذف نشده‌اند.", "Retained for records. Panel and bot are disabled; peers and descendants are preserved."));
        continue;
      }
      const actions = document.createElement("div"); actions.className = "actions account-actions";
      const approval = document.createElement("button"); approval.type = "button"; approval.className = "account-approval secondary";
      approval.textContent = item.approval_status === "approved" ? text("لغو تأیید", "Revoke approval") : text("تأیید پرداخت و فعال‌سازی", "Approve payment and activate");
      approval.setAttribute("aria-label", approval.textContent + " · " + item.username);
      approval.addEventListener("click", () => action(approval, null, async () => {
        approval.textContent = text("در حال بررسی و آماده‌سازی اینترفیس…", "Checking and preparing interface…");
        try { await api(`/resellers/api/accounts/${Number(item.id)}/approval`, {method:"POST",payload:{approved:item.approval_status !== "approved",days:30}}); }
        catch (error) { try { await loadAdmin(); } catch { /* Keep the original failure. */ } throw error; }
        await loadAdmin(); notice(text("وضعیت تأیید ذخیره شد.", "Approval status saved."), "success");
      })); actions.append(approval);
      const assigned = document.createElement("select"); assigned.setAttribute("aria-label", text("اینترفیس اختصاصی نماینده", "Assigned reseller interface"));
      const missing = document.createElement("option"); missing.value = ""; missing.textContent = text("اینترفیس را تعیین کنید", "Assign an interface"); assigned.append(missing);
      for (const iface of data.interfaces || []) {const option = document.createElement("option"); option.value = iface.id; option.textContent = iface.name; option.selected = (item.interfaces || []).length === 1 && Number(item.interfaces[0]) === Number(iface.id); assigned.append(option);}
      const assign = document.createElement("button"); assign.type = "button"; assign.className = "secondary"; assign.textContent = text("تعیین اینترفیس", "Assign interface");
      assign.addEventListener("click", () => action(assign, null, async () => {if (!assigned.value) throw new Error(text("یک اینترفیس انتخاب کنید.", "Choose one interface.")); await api(`/resellers/api/accounts/${Number(item.id)}/interface`, {method:"POST",payload:{iface_id:Number(assigned.value)}}); await loadAdmin();}));
      actions.append(assigned, assign);
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
      const signin = document.createElement("button"); signin.type = "button"; signin.className = "secondary";
      signin.textContent = text("لینک ورود نماینده", "Reseller sign-in link");
      signin.addEventListener("click", () => showSignin("/reseller"));
      actions.append(days, renew, access, signin, deleteControl("account", item, true)); row.insertCell().append(actions);
    }
    if (!accounts.length) empty(rows, text("هنوز نماینده‌ای ساخته نشده است.", "No reseller accounts yet."), 10);
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
  function showSignin() {
    if (!ownerView && (!account?.active || selectedSection !== "children")) return;
    const url = new URL("/login", location.origin);
    $("#signin-url").value = url.href;
    $("#open-signin-url").href = url.href;
    $("#signin-copy-result").textContent = "";
    $("#signin-delivery").hidden = false;
  }
  function interfaceText(item, available = []) {
    const assigned = item.assigned_interfaces || available.filter(iface => (item.interfaces || []).includes(iface.id));
    const names = assigned.map(iface => String(iface.name || "#" + iface.id) + (iface.listen_port ? " : " + iface.listen_port : "")).join(" · ");
    const job = item.interface_provision;
    return (names || text("تعیین نشده", "Not assigned")) + (job ? " · " + String(job.state) + " : " + job.port : "");
  }
  function renderChildren(data) {
    childrenData = data;
    const number = new Intl.NumberFormat(language === "fa" ? "fa-IR" : "en-GB");
    const rows = $("#child-rows"); rows.replaceChildren();
    const accounts = data.accounts || [];
    $("#child-count").textContent = number.format(accounts.length) + text(" زیرنمایندهٔ مستقیم", " direct subresellers");
    const fee = data.creation_fee?.amount;
    $("#child-fee-amount").textContent = Number.isSafeInteger(fee) ? number.format(fee) + text(" تومان", " toman") : text("هزینه را از مدیر اصلی بپرسید.", "Ask the owner for the creation fee.");
    if (Number.isSafeInteger(fee)) {
      const copy = $("#child-dialog-copy");
      copy.dataset.fa = `ثبت هر زیرنماینده ${new Intl.NumberFormat("fa-IR").format(fee)} تومان هزینهٔ یک‌باره دارد. پس از ورود، برای دریافت شماره کارت و تأیید پرداخت به @AshkanfathiOfficial پیام بدهد.`;
      copy.dataset.en = `Each subreseller has a one-time creation fee of ${new Intl.NumberFormat("en-GB").format(fee)} toman. After signing in, contact @AshkanfathiOfficial for card details and payment approval.`;
      copy.textContent = copy.dataset[language];
    }
    for (const item of accounts) {
      const row = rows.insertRow(); cell(row, item.username);
      cell(row, item.deleted || item.deleted_at ? text("دسترسی حذف‌شده", "Archived access") : item.approval_status !== "approved" ? text("در انتظار تأیید پرداخت", "Awaiting payment approval") : item.active ? text("فعال", "Active") : !item.enabled ? text("متوقف", "Suspended") : text("اعتبار پایان یافته", "Expired"));
      cell(row, date(item.expires_at));
      cell(row, number.format(item.direct_child_count || 0) + " / " + number.format(item.descendant_count || 0));
      cell(row, number.format(item.creation_fee_toman || 0));
      cell(row, interfaceText(item));
      if (item.deleted || item.deleted_at) {cell(row, text("اطلاعات برای سوابق حفظ شده؛ پنل و بات غیرفعال‌اند.", "Retained for records; the panel and bot are disabled.")); continue;}
      const controls = document.createElement("div"); controls.className = "actions child-provision-actions";
      const link = document.createElement("button"); link.type = "button"; link.className = "secondary"; link.textContent = text("لینک ورود", "Sign-in link");
      link.addEventListener("click", () => showSignin()); controls.append(link, deleteControl("account", item, false));
      if (item.interface_provision?.state === "uncertain" || item.interface_provision?.state === "pending") {
        const state = document.createElement("span"); state.className = "muted";
        state.textContent = text("نیازمند بررسی مدیر؛ ساخت را تکرار نکنید.", "Owner review required; do not repeat creation."); controls.append(state);
      }
      row.insertCell().append(controls);
    }
    if (!accounts.length) empty(rows, text("هنوز زیرنماینده‌ای ثبت نکرده‌اید.", "You have not created a subreseller yet."), 7);
    $("#add-child").disabled = (account.interfaces || []).length !== 1;
    $("#child-load-state").textContent = text("فقط نام کاربری و رمز را وارد کنید؛ اینترفیس و پورت آزاد ۵۱۸۲۵ تا ۵۲۰۰۰ خودکار تعیین می‌شوند. همهٔ زیرمجموعه‌ها و هزینهٔ ثبت در پنل مدیر اصلی دیده می‌شوند و تأیید پرداخت با مدیر است.", "Enter only a username and password. The interface and an available port from 51825 to 52000 are assigned automatically. The owner sees every subreseller and approves payment.");
    $("#child-load-state").removeAttribute("data-fa"); $("#child-load-state").removeAttribute("data-en");
  }
  function closeQR() {
    $("#qr-image").removeAttribute("src");
    if ($("#qr-dialog").open) $("#qr-dialog").close();
  }
  let deletion = null, deletionTrigger = null;
  const uncertainDeletions = new Set();
  function deleteControl(kind, item, owner) {
    const button = document.createElement("button"); button.type = "button"; button.className = "secondary danger";
    const identity = String(item.username || item.name || "#" + item.id);
    button.textContent = text("حذف", "Delete");
    button.setAttribute("aria-label", text("حذف ", "Delete ") + identity);
    button.setAttribute("aria-haspopup", "dialog"); button.setAttribute("aria-controls", "delete-dialog");
    button.disabled = uncertainDeletions.has(kind + ":" + item.id);
    button.addEventListener("click", () => {
      deletion = {kind, id:Number(item.id), owner}; deletionTrigger = button;
      $("#delete-target").textContent = identity;
      $("#delete-copy").textContent = kind === "peer"
        ? text("این Peer و اتصال آن حذف می‌شوند. پیش از ادامه از انتخاب سرویس درست مطمئن شوید.", "This peer and its connection will be deleted. Check the selected service before continuing.")
        : text("پنل نماینده حذف و بات آن متوقف می‌شود. دسترسی زیرمجموعه‌ها به علت توقف والد متوقف می‌شود؛ اطلاعات Peerها، کانفیگ‌ها و حساب‌های زیرمجموعه خودکار حذف نمی‌شوند.", "The reseller panel will be deleted and its bot stopped. Descendant access pauses because their parent is paused; peer data, configurations and child accounts are not automatically deleted.");
      $("#delete-result").textContent = ""; delete $("#delete-confirm").dataset.uncertain; $("#delete-confirm").disabled = false;
      $("#delete-dialog").showModal(); $("#delete-cancel").focus();
    });
    return button;
  }
  $("#delete-cancel").addEventListener("click", () => $("#delete-dialog").close());
  $("#delete-dialog").addEventListener("close", () => { if (deletionTrigger?.isConnected) deletionTrigger.focus(); deletion = null; });
  $("#delete-confirm").addEventListener("click", event => {
    if (!deletion) return;
    const target = {...deletion};
    action(event.currentTarget, "#delete-result", async () => {
      const path = target.kind === "peer" ? `/reseller/api/peers/${target.id}` : `${target.owner ? "/resellers" : "/reseller"}/api/accounts/${target.id}`;
      let deletionResult;
      try {
        deletionResult = await api(path, {method:"DELETE", payload:{confirm:true}});
      } catch (error) {
        if (!error.status || error.status >= 500 || /uncertain|timeout/.test(error.code || "")) {
          uncertainDeletions.add(target.kind + ":" + target.id);
          $("#delete-confirm").dataset.uncertain = "true";
          formResult("#delete-result", error.message + " " + text("نتیجه نامشخص است؛ حذف را تکرار نکنید و وضعیت را با مدیر بررسی کنید.", "The outcome is uncertain. Do not repeat deletion; ask the owner to check the status."), true);
          try { if (target.kind === "peer") await loadPeers(); else if (target.owner) await loadAdmin(); else await loadChildren(); } catch { /* Retain the original outcome. */ }
          return;
        }
        throw error;
      }
      $("#delete-dialog").close();
      try { if (target.kind === "peer") { await loadPeers(); if ($("#credit-dialog").open) await loadCredit(); } else if (target.owner) await loadAdmin(); else await loadChildren(); }
      catch { notice(text("حذف ثبت شد؛ فهرست تازه دریافت نشد. فقط صفحه را تازه کنید.", "Deletion was recorded, but the list could not refresh. Refresh the page only.")); return; }
      notice(deletionResult.warning ? text("دسترسی حساب بسته شد؛ پاک‌سازی اطلاعات بات نیازمند بررسی مدیر است. مشتری‌ها و زیرمجموعه‌ها حفظ شدند.", "Account access is closed; the owner must review bot credential cleanup. Customers and descendants were preserved.") : text("حذف ثبت شد.", "Deletion recorded."), deletionResult.warning ? "warning" : "success");
    }).finally(() => { if ($("#delete-confirm").dataset.uncertain) $("#delete-confirm").disabled = true; });
  });
  function renderPeers(data) {
    peerData = data;
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
      const edit = document.createElement("button"); edit.type = "button"; edit.className = "secondary"; edit.textContent = text("نام / تمدید", "Name / renew");
      edit.addEventListener("click", () => {
        $("#peer-edit-form").dataset.peerId = String(peer.id);
        $("#peer-edit-name").value = peer.name || "";
        $("#peer-edit-result").textContent = "";
        $("#peer-edit-dialog").showModal();
      });
      const userLink = document.createElement("button"); userLink.type = "button"; userLink.className = "secondary peer-user-link"; userLink.textContent = text("لینک کاربر", "User link");
      userLink.addEventListener("click", () => action(userLink, null, async () => {
        const result = await api(`/reseller/api/peers/${Number(peer.id)}/user-link`);
        if (!account?.active || selectedSection !== "services") return;
        if (typeof result.url !== "string" || !result.url.trim()) throw new Error(text("لینک دریافت‌شده معتبر نیست.", "The returned link is invalid."));
        const url = new URL(result.url, location.origin);
        if (!["http:", "https:"].includes(url.protocol) || url.username || url.password) throw new Error(text("لینک دریافت‌شده معتبر نیست.", "The returned link is invalid."));
        $("#user-link-url").value = url.href; $("#user-link-open").href = url.href; $("#user-link-result").textContent = ""; $("#user-link-dialog").showModal();
      }));
      actions.append(config, qr, userLink, edit, deleteControl("peer", peer, false)); row.insertCell().append(actions);
    }
    if (!(data.peers || []).length) empty(rows, text("هنوز سرویسی برای این حساب وجود ندارد.", "No services belong to this account yet."), 5);
  }
  const provisionInputs = {
    endpoint_host:"#provision-endpoint-host", dns:"#provision-dns", mtu:"#provision-mtu",
    allowed_ips:"#provision-allowed-ips", persistent_keepalive:"#provision-keepalive",
    network_pool:"#provision-network-pool", subnet_prefix:"#provision-subnet-prefix",
    child_max_peers:"#provision-child-max-peers", auto_firewall:"#provision-auto-firewall"
  };
  function renderProvisionStatus() {
    if (!provisionDefaultsData) return;
    const configured = provisionDefaultsData.configured === true;
    $("#provision-defaults-state").dataset.configured = String(configured);
    $("#provision-defaults-state").textContent = configured ? text("الگوی ساخت ذخیره شده است؛ هر نمایندهٔ جدید اینترفیس خودکار می‌گیرد.", "Creation defaults are saved. Every new reseller receives an interface automatically.") : text("هنوز الگو ذخیره نشده است؛ پیش از ثبت نماینده، پیش‌فرض‌ها را تنظیم و ذخیره کنید.", "No template has been saved. Configure and save defaults before registering a reseller.");
  }
  function fillProvisionDefaults(data) {
    if (!data || !data.defaults || typeof data.defaults !== "object") throw new Error(text("الگوی ساخت معتبر دریافت نشد.", "The creation template response is invalid."));
    provisionDefaultsData = data;
    for (const [key, selector] of Object.entries(provisionInputs)) {
      const input = $(selector), value = data.defaults[key];
      if (key === "auto_firewall") input.checked = value === true;
      else input.value = value ?? "";
    }
    if (!data.configured && !$("#provision-endpoint-host").value) $("#provision-endpoint-host").value = location.hostname;
    provisionDefaultsLoaded = true; provisionDefaultsDirty = false;
    $("#provision-defaults-save").disabled = false;
    renderProvisionStatus();
  }
  async function loadProvisionDefaults({force = false} = {}) {
    if (!ownerView || !adminData || (provisionDefaultsLoaded && !force)) return;
    if (provisionDefaultsDirty) {
      if (force) formResult("#provision-defaults-result", text("تغییرات ذخیره‌نشده حفظ شدند؛ برای اعمال آن‌ها ذخیره را بزنید.", "Your unsaved edits were preserved. Save them to apply the changes."));
      return;
    }
    try { fillProvisionDefaults(await api("/resellers/api/interface-defaults")); }
    catch (error) {
      if (!provisionDefaultsLoaded) $("#provision-defaults-save").disabled = true;
      formResult("#provision-defaults-result", error.message || text("دریافت الگو ناموفق بود؛ دوباره دریافت کنید.", "The template could not be loaded. Reload it and try again."), true);
    }
  }
  async function loadAdmin() {
    try { renderAdmin(await api("/resellers/api/accounts")); await loadProvisionDefaults(); }
    catch (error) { ownerVisibility(false); throw error; }
  }
  async function loadPeers() { if (account?.active) renderPeers(await api("/reseller/api/peers")); }
  function renderCredit(value) {
    const numbers = ["server_time", "expires_at", "remaining_seconds", "max_peers", "used_peers", "reserved_peers", "remaining_peers"];
    if (!value || numbers.some(key => !Number.isSafeInteger(value[key]) || value[key] < 0) || typeof value.active !== "boolean") {
      throw new Error(text("پاسخ اعتبار معتبر نیست؛ دوباره بررسی کنید.", "The credit response is invalid; check again."));
    }
    creditData = value;
    const number = new Intl.NumberFormat(language === "fa" ? "fa-IR" : "en-GB");
    const seconds = value.remaining_seconds;
    const days = Math.floor(seconds / 86400), hours = Math.floor(seconds % 86400 / 3600), minutes = Math.floor(seconds % 3600 / 60);
    $("#credit-remaining-time").textContent = seconds === 0 ? text("اعتبار زمانی پایان یافته", "No validity remaining") : seconds < 60 ? text("کمتر از یک دقیقه", "Less than one minute") :
      `${number.format(days)} ${text("روز", "days")} · ${number.format(hours)} ${text("ساعت", "hours")} · ${number.format(minutes)} ${text("دقیقه", "minutes")}`;
    for (const [id, key] of [["remaining-peers", "remaining_peers"], ["max-peers", "max_peers"], ["used-peers", "used_peers"], ["reserved-peers", "reserved_peers"]]) {
      $("#credit-" + id).textContent = number.format(value[key]);
    }
    $("#credit-expiry").textContent = date(value.expires_at);
    $("#credit-state").textContent = value.approval_status !== "approved" ? text("حساب در انتظار تأیید مدیر است.", "The account awaits owner approval.") : !value.active ? text("دسترسی حساب یا والد متوقف است؛ با مدیر تماس بگیرید.", "This account or its parent is paused; contact the owner.") : value.remaining_peers === 0 ? text("ظرفیت Peer تکمیل شده است.", "Peer capacity is full.") : text("حساب فعال است؛ اعتبار و ظرفیت فعلی در زیر آمده است.", "The account is active; current validity and capacity are shown below.");
    $("#credit-values").hidden = false;
  }
  async function loadCredit() {
    const serial = ++creditRequest, identity = account?.id;
    $("#credit-values").hidden = true;
    $("#credit-state").textContent = text("در حال بررسی اعتبار…", "Checking credit…");
    $("#credit-result").textContent = "";
    try {
      const result = await api("/reseller/api/credit");
      if (serial !== creditRequest || !$("#credit-dialog").open || identity !== account?.id) return;
      renderCredit(result.credit);
      account = {...account, active:result.credit.active, enabled:result.credit.enabled,
        approval_status:result.credit.approval_status, expires_at:result.credit.expires_at};
      renderAccount();
    } catch (error) {
      if (serial !== creditRequest || !$("#credit-dialog").open) return;
      creditData = null;
      $("#credit-state").textContent = text("اعتبار دریافت نشد؛ دوباره تلاش کنید.", "Credit could not be loaded; try again.");
      throw error;
    }
  }
  function renderBot(data, {updateOwner = false} = {}) {
    botData = data;
    const bot = data.bot || {};
    const states = {
      running:text("فعال", "Running"), enabled:text("فعال", "Enabled"), ready:text("آماده", "Ready"),
      starting:text("در حال راه‌اندازی", "Starting"), configured:text("ذخیره‌شده؛ در انتظار راه‌اندازی", "Saved; waiting to start"),
      paused:text("متوقف به دلیل وضعیت حساب", "Paused by account status"), disabled:text("غیرفعال", "Disabled"),
      error:text("خطا؛ مشخصات بات و دسترسی شبکه را بررسی کنید.", "Error; check bot credentials and network access."),
      connection_error:text("اتصال به تلگرام ناموفق است؛ دسترسی شبکهٔ سرور را بررسی کنید.", "Telegram connection failed; check the server network."),
      credential_error:text("توکن قابل بازیابی نیست؛ مشخصات بات را دوباره ذخیره کنید.", "The token could not be recovered; save your bot credentials again."),
      panel_paused:text("بات به دلیل اعتبار یا وضعیت تأیید پنل متوقف است.", "The bot is paused by panel expiry or approval status."),
      conflict:text("بات در جای دیگری اجرا می‌شود؛ اجرای قبلی را متوقف کنید.", "The bot is running elsewhere; stop the previous runner."),
      telegram_conflict:text("این بات در جای دیگری در حال اجراست؛ اجرای قبلی را متوقف کنید.", "This bot is running elsewhere; stop its previous runner."),
      telegram_invalid_token:text("توکن از طرف تلگرام رد شده؛ مشخصات بات را دوباره ذخیره کنید.", "Telegram rejected the token; save your bot credentials again."),
      telegram_rate_limited:text("تلگرام موقتاً درخواست‌ها را محدود کرده؛ اتصال خودکار دوباره تلاش می‌کند.", "Telegram is temporarily limiting requests; the runner will retry."),
      telegram_connection_failed:text("ارتباط سرور با تلگرام برقرار نیست؛ اتصال خودکار دوباره تلاش می‌کند.", "The server cannot reach Telegram; the runner will retry.")
    };
    $("#bot-status").textContent = bot.configured ? (bot.username ? "@" + String(bot.username) + " · " : "") + (states[bot.status] || (bot.enabled ? text("در حال راه‌اندازی", "Starting") : text("غیرفعال", "Disabled"))) : text("هنوز باتی متصل نشده است.", "No bot connected yet.");
    $("#bot-disable").hidden = !bot.configured;
    if (bot.owner_id && (updateOwner || !$("#bot-owner-id").value)) $("#bot-owner-id").value = String(bot.owner_id);
  }
  function stopBotStatus() {
    if (botStatusTimer !== null) clearTimeout(botStatusTimer);
    botStatusTimer = null;
  }
  function scheduleBotStatus() {
    stopBotStatus();
    if (loggingOut || ownerView || !account?.active || selectedSection !== "bot" || document.hidden || botStatusBusy) return;
    botStatusTimer = setTimeout(() => {botStatusTimer = null; loadBot();}, 10000);
  }
  async function loadBot() {
    if (loggingOut || !account?.active || ownerView || botStatusBusy) return;
    botStatusBusy = true;
    try {renderBot(await api("/reseller/api/bot"));}
    catch (error) {$("#bot-status").textContent = error.message || text("دریافت وضعیت بات ناموفق بود؛ کمی بعد دوباره بررسی می‌شود.", "Bot status could not be loaded; it will be checked again shortly.");}
    finally {botStatusBusy = false; scheduleBotStatus();}
  }
  document.addEventListener("visibilitychange", scheduleBotStatus);
  window.addEventListener("pagehide", stopBotStatus);
  window.addEventListener("pageshow", scheduleBotStatus);
  async function loadChildren() {
    if (!account?.active) return;
    try { renderChildren(await api("/reseller/api/accounts")); }
    catch (error) {
      $("#add-child").disabled = true;
      $("#child-load-state").textContent = text("دریافت زیرنمایندگان ناموفق بود؛ وضعیت را تازه کنید.", "Subresellers could not be loaded. Refresh status.");
      $("#child-load-state").removeAttribute("data-fa"); $("#child-load-state").removeAttribute("data-en");
      throw error;
    }
  }
  async function load() {
    if (loggingOut) return;
    $("#loading").hidden = false;
    try {
      const result = await api("/reseller/api/session"); account = result.account || null; activationData = result.activation || null;
      if (ownerView) await loadAdmin();
      else { renderAccount(); if (account?.active) await Promise.all([loadPeers(), loadChildren(), loadBot()]); }
    } catch (error) {
      if (ownerView) ownerVisibility(false);
      else if (error.status === 401 || error.status === 403) { peerData = null; $("#peer-rows").replaceChildren(); $("#active-view").hidden = true; }
      notice(error.message, "error");
    } finally { $("#loading").hidden = true; }
  }
  function clearSecuritySecrets() {
    securityPending = false;
    for (const id of ["#security-password", "#security-otp", "#security-secret", "#security-recovery-codes"]) $(id).value = "";
    $("#security-qr").removeAttribute("src"); $("#security-enrollment").hidden = true; $("#security-recovery").hidden = true;
    $("#security-enable").disabled = true;
  }
  function renderSecurity() {
    if (!securityData) return;
    $("#security-status").textContent = securityData.enabled ? text("ورود دومرحله‌ای فعال است؛ کد بازیابی باقی‌مانده: ", "Two-factor login is enabled; recovery codes remaining: ") + String(securityData.recovery_codes_remaining ?? 0) : text("ورود دومرحله‌ای غیرفعال است.", "Two-factor login is disabled.");
    $("#security-setup").hidden = Boolean(securityData.enabled);
    $("#security-disable").hidden = !securityData.enabled;
    $("#security-enable").hidden = Boolean(securityData.enabled);
  }
  async function loadSecurity() {
    if (!account || ownerView || loggingOut || securityLoading) return;
    securityLoading = true;
    try { securityData = await api("/reseller/api/security"); renderSecurity(); }
    catch (error) { formResult("#security-result", error.message, true); }
    finally { securityLoading = false; }
  }
  function securityPayload() {
    return {password:$("#security-password").value, otp:$("#security-otp").value.trim().replace(/[۰-۹٠-٩]/g, value => String("۰۱۲۳۴۵۶۷۸۹".includes(value) ? "۰۱۲۳۴۵۶۷۸۹".indexOf(value) : "٠١٢٣٤٥٦٧٨٩".indexOf(value)))};
  }
  $("#security-refresh").addEventListener("click", () => loadSecurity());
  $("#security-setup").addEventListener("click", event => action(event.currentTarget, "#security-result", async () => {
    const password = $("#security-password").value;
    if (!password) throw new Error(text("رمز فعلی حساب را وارد کنید.", "Enter your current password."));
    const result = await api("/reseller/api/security/setup", {method:"POST", payload:{password}});
    clearSecuritySecrets(); $("#security-password").value = password;
    $("#security-secret").value = String(result.secret || "");
    const image = String(result.qr_data_uri || "");
    if (image.startsWith("data:image/png;base64,")) $("#security-qr").src = image;
    $("#security-qr").hidden = !image.startsWith("data:image/png;base64,");
    securityPending = true; $("#security-enrollment").hidden = false; $("#security-enable").disabled = false;
    formResult("#security-result", text("کلید را در برنامهٔ احراز هویت ثبت و با کد همان برنامه تأیید کنید.", "Add the key to your authenticator and confirm with its code."));
  }));
  $("#security-form").addEventListener("submit", event => {
    event.preventDefault(); if (!securityPending) return;
    action(event.submitter || $("#security-enable"), "#security-result", async () => {
      const result = await api("/reseller/api/security/enable", {method:"POST", payload:securityPayload()});
      clearSecuritySecrets();
      securityData = {enabled:true, recovery_codes_remaining:(result.recovery_codes || []).length}; renderSecurity();
      $("#security-recovery-codes").value = (result.recovery_codes || []).join("\n"); $("#security-recovery").hidden = false;
      formResult("#security-result", text("ورود دومرحله‌ای فعال شد. کدهای بازیابی را ذخیره کنید؛ برای ورود بعدی کد تازهٔ برنامه را بگیرید.", "Two-factor login enabled. Save recovery codes now; use a fresh authenticator code for your next sign-in."));
    });
  });
  $("#security-disable").addEventListener("click", event => action(event.currentTarget, "#security-result", async () => {
    await api("/reseller/api/security/disable", {method:"POST", payload:securityPayload()});
    clearSecuritySecrets(); securityData = {enabled:false,recovery_codes_remaining:0}; renderSecurity();
    formResult("#security-result", text("ورود دومرحله‌ای غیرفعال شد.", "Two-factor login disabled."));
  }));
  $("#security-recovery-hide").addEventListener("click", () => {$("#security-recovery-codes").value = ""; $("#security-recovery").hidden = true;});
  $("#provision-defaults-form").addEventListener("input", () => {provisionDefaultsDirty = true;});
  $("#provision-defaults-refresh").addEventListener("click", () => action($("#provision-defaults-refresh"), "#provision-defaults-result", () => loadProvisionDefaults({force:true})));
  $("#provision-defaults-form").addEventListener("submit", event => {
    event.preventDefault();
    if (!adminData || !ownerView) return;
    const payload = {};
    for (const [key, selector] of Object.entries(provisionInputs)) {
      const input = $(selector);
      payload[key] = key === "auto_firewall" ? input.checked : ["mtu", "persistent_keepalive", "subnet_prefix", "child_max_peers"].includes(key) ? Number(input.value) : input.value.trim();
    }
    if (["mtu", "persistent_keepalive", "subnet_prefix", "child_max_peers"].some(key => !Number.isSafeInteger(payload[key]))) return formResult("#provision-defaults-result", text("مقدارهای عددی را بررسی کنید.", "Check the numeric values."), true);
    action(event.submitter || $("#provision-defaults-save"), "#provision-defaults-result", async () => {
      fillProvisionDefaults(await api("/resellers/api/interface-defaults", {method:"PUT",payload}));
      formResult("#provision-defaults-result", text("پیش‌فرض‌ها ذخیره شدند؛ اینترفیس همهٔ نمایندگان جدید، از جمله حساب‌های ساخته‌شده توسط مدیر، از این الگو ساخته می‌شود.", "Defaults saved. All new reseller interfaces, including owner-created accounts, use this template."));
    });
  });
  $("#add-reseller").addEventListener("click", () => {
    if (!adminData || busy) return;
    if (!$("#account-form").dataset.locked) $("#account-result").textContent = "";
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
  $("#reseller-refresh").addEventListener("click", () => {
    if (!account || busy) return;
    $("#credit-dialog").showModal();
    action($("#reseller-refresh"), "#credit-result", loadCredit);
  });
  $("#credit-refresh").addEventListener("click", () => action($("#credit-refresh"), "#credit-result", loadCredit));
  $("#credit-close").addEventListener("click", () => $("#credit-dialog").close());
  $("#credit-dialog").addEventListener("close", () => {creditRequest += 1; $("#reseller-refresh").focus();});
  $("#account-refresh").addEventListener("click", () => action($("#account-refresh"), "#credit-result", async () => {await load(); await loadCredit();}));
  $("#logout").addEventListener("click", async () => {
    // Logout must remain available while another panel request is in flight.
    if (loggingOut) return;
    loggingOut = true;
    const button = $("#logout");
    button.disabled = true; button.setAttribute("aria-busy", "true");
    stopBotStatus(); creditRequest += 1;
    for (const controller of requestsInFlight) controller.abort();
    try {
      await api("/reseller/api/logout", {method:"POST", payload:{}});
      csrf = ""; nativeCSRF = ""; account = null; adminData = null;
      peerData = null; childrenData = null; botData = null; creditData = null;
      pending.clear(); clearSecuritySecrets(); securityData = null; closeQR();
      for (const id of ["#peer-rows", "#child-rows", "#account-rows"]) $(id).replaceChildren();
      for (const input of document.querySelectorAll('input[type="password"]')) input.value = "";
      for (const dialog of document.querySelectorAll("dialog[open]")) dialog.close();
      $("#active-view").hidden = true; $("#reseller-navigation").hidden = true;
      location.replace("/login");
    } catch (error) {
      loggingOut = false;
      notice(text("خروج انجام نشد؛ اتصال را بررسی کنید و دوباره خروج را بزنید.", "Sign-out failed. Check your connection and try again."), "error");
      scheduleBotStatus();
      button.disabled = false; button.removeAttribute("aria-busy");
    }
  });
  $("#account-form").addEventListener("submit", (event) => {
    event.preventDefault();
    if (event.currentTarget.dataset.locked) return formResult("#account-result", text("ساخت قبلی نیازمند بررسی است؛ فهرست نمایندگان را بررسی کنید و حساب را دوباره نسازید.", "The previous creation needs review. Check the reseller list and do not recreate the account."), true);
    const payload = {username:$("#account-username").value.trim(), password:$("#account-password").value, max_peers:Number($("#account-limit").value)};
    if (!Number.isSafeInteger(payload.max_peers) || payload.max_peers < 1 || payload.max_peers > 10000) return formResult("#account-result", text("سقف Peer باید عدد صحیح بین ۱ تا ۱۰۰۰۰ باشد.", "The Peer limit must be an integer from 1 to 10000."), true);
    action(event.submitter || event.currentTarget.querySelector('[type="submit"]'), "#account-result", async () => {
      formResult("#account-result", text("در حال ساخت خودکار حساب و اینترفیس اختصاصی…", "Creating the account and dedicated interface automatically…"));
      let created;
      try { created = await api("/resellers/api/accounts", {method:"POST", payload}); }
      catch (error) {
        if (!error.status || error.data?.account || ["interface_create_uncertain", "create_uncertain", "timeout"].includes(error.code)) {
          $("#account-form").dataset.locked = "true";
          $("#account-password").value = "";
          error.message = text("نتیجهٔ ساخت نیازمند بررسی است؛ فهرست نمایندگان و اینترفیس حساب را بررسی کنید. حساب را دوباره نسازید؛ از عملیات همان ردیف برای بررسی یا بازیابی استفاده کنید.", "Creation needs review. Inspect the reseller list and its interface. Do not recreate the account; use that account’s row to inspect or recover it.");
        }
        try { await loadAdmin(); } catch { /* Preserve the original creation outcome. */ }
        throw error;
      }
      $("#account-password").value = "";
      $("#account-dialog").close();
      $("#account-form").reset();
      showSignin(created.signin_url || "/reseller");
      notice(text("حساب و اینترفیس اختصاصی خودکار ساخته شدند. فعال‌سازی به پرداخت ۵٬۰۰۰٬۰۰۰ تومان و تأیید شما نیاز دارد.", "The account and dedicated interface were created automatically. Activation requires payment of 5,000,000 toman and your approval."), "success");
      try { await loadAdmin(); } catch { notice(text("حساب ساخته شد، اما فهرست تازه دریافت نشد. وضعیت را تازه کنید.", "The account was created, but the list could not be refreshed. Refresh status.")); }
    });
  });
  $("#copy-signin-url").addEventListener("click", async () => {
    const input = $("#signin-url"); input.focus(); input.select();
    try {
      if (navigator.clipboard?.writeText) await navigator.clipboard.writeText(input.value);
      else if (!document.execCommand("copy")) throw new Error("manual copy required");
      formResult("#signin-copy-result", text("لینک کپی شد.", "Link copied."));
    } catch { formResult("#signin-copy-result", text("لینک انتخاب شد؛ آن را با Ctrl+C یا منوی کپی دستگاه کپی کنید.", "The link is selected. Copy it with Ctrl+C or your device’s copy menu.")); }
  });
  $("#add-child").addEventListener("click", () => {
    if (!account?.active || !childrenData || busy) return;
    $("#child-result").textContent = ""; $("#child-dialog").showModal(); $("#child-username").focus();
  });
  $("#close-child-dialog").addEventListener("click", () => $("#child-dialog").close());
  $("#child-dialog").addEventListener("close", () => { $("#child-password").value = ""; $("#add-child").focus(); });
  $("#child-form").addEventListener("submit", (event) => {
    event.preventDefault();
    if (event.currentTarget.dataset.locked) return formResult("#child-result", text("درخواست قبلی نیازمند بررسی مدیر است؛ حساب را دوباره نسازید.", "The previous request needs owner review. Do not recreate the account."), true);
    const payload = {username:$("#child-username").value.trim(),password:$("#child-password").value};
    action(event.submitter || event.currentTarget.querySelector('[type="submit"]'), "#child-result", async () => {
      formResult("#child-result", text("در حال ساخت خودکار حساب و اینترفیس اختصاصی…", "Creating the account and dedicated interface automatically…"));
      let created;
      try { created = await api("/reseller/api/accounts", {method:"POST", payload}); }
      catch (error) {
        if (!error.status || ["interface_create_uncertain", "create_uncertain", "timeout"].includes(error.code)) {
          $("#child-form").dataset.locked = "true";
          error.message = text("نتیجهٔ ساخت قطعی نیست؛ پیش از درخواست تازه، فهرست را تازه کنید و از مدیر بخواهید وضعیت را بررسی کند.", "The creation outcome is uncertain. Refresh the list and ask the owner to verify it before a new request.");
        }
        try {await loadChildren();} catch { /* Keep the original creation error. */ }
        throw error;
      }
      $("#child-password").value = ""; $("#child-dialog").close(); $("#child-form").reset();
      showSignin(created.signin_url || "/reseller");
      notice(text("زیرنماینده و اینترفیس اختصاصی آن خودکار ساخته شدند و در پنل مدیر دیده می‌شوند. لینک ورود را برای پرداخت و تأیید مدیر تحویل دهید.", "The subreseller and dedicated interface were created automatically and are visible to the owner. Share the sign-in link for payment and approval."), "success");
      try { await loadChildren(); } catch { notice(text("حساب ثبت شد، اما فهرست تازه دریافت نشد. فقط وضعیت را تازه کنید؛ حساب را دوباره نسازید.", "The account was created, but the list could not be refreshed. Refresh status only; do not create it again.")); }
    });
  });
  let bulkOperation = null, bulkPayload = null;
  const bulkNew = document.createElement("button"); bulkNew.type = "button"; bulkNew.className = "secondary"; bulkNew.hidden = true;
  bulkNew.textContent = text("شروع دستهٔ جدید", "Start a new batch");
  bulkNew.dataset.fa = "شروع دستهٔ جدید"; bulkNew.dataset.en = "Start a new batch";
  $("#bulk-peer-form").append(bulkNew);
  bulkNew.addEventListener("click", () => {
    if (busy || !bulkOperation) return;
    finishReceipt(bulkOperation); bulkOperation = null; bulkPayload = null;
    $("#bulk-peer-form").querySelectorAll("input").forEach(input => input.readOnly = false);
    $("#bulk-peer-form").querySelector('[type="submit"]').disabled = false;
    const submit = $("#bulk-peer-form").querySelector('[type="submit"]');
    submit.dataset.fa = "ساخت انبوه"; submit.dataset.en = "Create batch"; submit.textContent = submit.dataset[language];
    $("#bulk-result").textContent = ""; $("#bulk-outcomes").replaceChildren(); bulkNew.hidden = true;
    $("#bulk-name-prefix").focus();
  });
  $("#bulk-peer-form").addEventListener("submit", event => {
    event.preventDefault();
    const button = event.submitter || event.currentTarget.querySelector('[type="submit"]');
    action(button, "#bulk-result", async () => {
      if (!bulkPayload) {
        bulkPayload = {name_prefix:$("#bulk-name-prefix").value.trim(),count:Number($("#bulk-count").value),days:Number($("#bulk-days").value),volume_gb:Number($("#bulk-volume").value)};
        bulkOperation = await receipt("bulk-peer:" + account.id, bulkPayload);
        $("#bulk-peer-form").querySelectorAll("input").forEach(input => input.readOnly = true);
      }
      button.dataset.fa = "بررسی نتیجهٔ همین دسته"; button.dataset.en = "Check this batch result";
      button.textContent = button.dataset[language];
      formResult("#bulk-result", text("ساخت دسته در حال بررسی است…", "Processing the batch…"));
      let result;
      try { result = await api("/reseller/api/peers/bulk", {method:"POST",payload:bulkPayload,key:bulkOperation.key,bulkResult:true}); }
      catch (error) {
        formResult("#bulk-result", error.message + " " + text("اطلاعات و شناسهٔ همین دسته حفظ شد؛ نتیجه را بررسی کنید. ارسال مجدد همین درخواست، دستهٔ تازه نمی‌سازد.", "This batch and its request key are preserved. Check the outcome; resubmitting uses the same batch."), true);
        try { await loadPeers(); } catch { /* Do not replace the batch error. */ }
        return;
      }
      const created = result.created || [], failed = result.failed || [];
      const uncertain = Boolean(result.uncertain);
      const summary = text(`از ${result.requested || bulkPayload.count} سرویس، ${created.length} ساخته شد و ${failed.length} مورد ناموفق بود.`, `${created.length} of ${result.requested || bulkPayload.count} services created; ${failed.length} failed.`);
      formResult("#bulk-result", summary + " " + (uncertain ? text("نتیجهٔ برخی موارد نامشخص است؛ دستهٔ جدید نسازید و از مدیر بخواهید وضعیت را بررسی کند.", "Some outcomes are uncertain. Do not create another batch; ask the owner to inspect them.") : text("کانفیگ‌ها در فهرست سرویس‌ها قابل تحویل‌اند.", "Deliver created configs from the service list.")), uncertain || failed.length > 0);
      $("#bulk-outcomes").replaceChildren(...created.map(item => {const li = document.createElement("li"); li.textContent = "✓ " + String(item.name || "#" + item.id); return li;}), ...failed.map(item => {const li = document.createElement("li"); li.textContent = text("مورد ", "Item ") + String(item.index) + ": " + String(item.message || item.error || "—"); return li;}));
      bulkNew.hidden = !(result.complete && !uncertain && failed.length === 0 && created.length === bulkPayload.count);
      try { await loadPeers(); if ($("#credit-dialog").open) await loadCredit(); }
      catch { formResult("#bulk-result", summary + " " + text("دریافت فهرست تازه ممکن نشد؛ فقط صفحه را تازه کنید، دسته را دوباره نسازید.", "The list could not refresh. Refresh the page; do not create the batch again."), true); }
    });
  });
  $("#peer-form").addEventListener("submit", (event) => {
    event.preventDefault();
    if (event.currentTarget.dataset.locked) return;
    const payload = Object.fromEntries(new FormData(event.currentTarget));
    ["days", "volume_gb"].forEach((key) => { payload[key] = Number(payload[key]); });
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
  $("#peer-edit-close").addEventListener("click", () => $("#peer-edit-dialog").close());
  $("#peer-edit-form").addEventListener("submit", event => {
    event.preventDefault();
    const peerId = Number(event.currentTarget.dataset.peerId);
    action(event.submitter, "#peer-edit-result", async () => {
      await api(`/reseller/api/peers/${peerId}/edit`, {method:"POST",payload:{name:$("#peer-edit-name").value.trim()}});
      formResult("#peer-edit-result", text("نام ذخیره شد.", "Name saved.")); await loadPeers();
    });
  });
  $("#peer-renew").addEventListener("click", () => action($("#peer-renew"), "#peer-edit-result", async () => {
    const peerId = Number($("#peer-edit-form").dataset.peerId);
    const payload = {days:Number($("#peer-renew-days").value),volume_gb:Number($("#peer-renew-volume").value)};
    if (!Number.isInteger(payload.days) || payload.days < 1 || payload.days > 366 || !Number.isInteger(payload.volume_gb) || payload.volume_gb < 1 || payload.volume_gb > 100000) throw new Error(text("مدت و حجم تمدید را بررسی کنید.", "Check renewal duration and volume."));
    const operation = await receipt("renew-peer:" + peerId, payload);
    await api(`/reseller/api/peers/${peerId}/renew`, {method:"POST",payload,key:operation.key});
    finishReceipt(operation);
    formResult("#peer-edit-result", text("زمان و حجم اضافه شد؛ مصرف و تنظیمات فنی قبلی حفظ شدند.", "Time and volume added; usage and technical settings were preserved.")); await loadPeers();
  }));
  $("#qr-dialog").addEventListener("close", () => $("#qr-image").removeAttribute("src"));
  $("#user-link-close").addEventListener("click", () => $("#user-link-dialog").close());
  $("#user-link-dialog").addEventListener("close", () => {$("#user-link-url").value = ""; $("#user-link-open").removeAttribute("href");});
  $("#user-link-copy").addEventListener("click", async () => {
    const input = $("#user-link-url"); input.focus(); input.select();
    try {
      if (navigator.clipboard?.writeText) await navigator.clipboard.writeText(input.value);
      else if (!document.execCommand("copy")) throw new Error("manual copy required");
      formResult("#user-link-result", text("لینک کاربر کپی شد.", "User link copied."));
    } catch {formResult("#user-link-result", text("لینک انتخاب شد؛ با Ctrl+C یا منوی دستگاه کپی کنید.", "The link is selected; copy with Ctrl+C or your device’s menu."));}
  });
  $("#bot-form").addEventListener("submit", event => {
    event.preventDefault();
    const payload = {owner_id:$("#bot-owner-id").value.trim(),token:$("#bot-token").value.trim()};
    action(event.submitter || event.currentTarget.querySelector('[type="submit"]'), "#bot-result", async () => {
      try {
        renderBot(await api("/reseller/api/bot", {method:"POST",payload}), {updateOwner:true});
        scheduleBotStatus();
        formResult("#bot-result", text("بات ذخیره شد و راه‌اندازی خودکار آغاز شد. در تلگرام Start بزنید.", "Bot saved and automatic startup requested. Press Start in Telegram."));
      } finally {$("#bot-token").value = ""; payload.token = "";}
    });
  });
  $("#bot-disable").addEventListener("click", () => action($("#bot-disable"), "#bot-result", async () => {
    const result = await api("/reseller/api/bot", {method:"DELETE"}); renderBot(result); scheduleBotStatus();
    $("#bot-token").value = ""; formResult("#bot-result", text("بات غیرفعال شد.", "Bot disabled."));
  }));
  $("#qr-image").addEventListener("error", () => { closeQR(); notice(text("تصویر QR دریافت نشد؛ وضعیت حساب و سرویس را تازه کنید.", "The QR image could not be loaded. Refresh your account and service status."), "error"); });
  translate(language); load();
})();
