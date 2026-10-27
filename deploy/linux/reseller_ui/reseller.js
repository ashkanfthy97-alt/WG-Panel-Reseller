(() => {
  "use strict";
  const $ = (selector) => document.querySelector(selector);
  const ownerView = location.pathname.replace(/\/$/, "") === "/resellers";
  const text = (fa, en) => language === "fa" ? fa : en;
  let language = "fa", csrf = "", nativeCSRF = "", account = null, adminData = null, peerData = null, childrenData = null, botData = null, busy = false;
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
      if (path === "/reseller/api/bot") {
        if (!nativeCSRF) throw new Error(text("ابتدا وضعیت بات را تازه کنید.", "Refresh bot status first."));
        headers.set("X-CSRFToken", nativeCSRF);
      }
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
      if (data.native_csrf && path === "/reseller/api/bot") nativeCSRF = data.native_csrf;
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
      if (button.dataset.uncertain) button.disabled = true;
      if (button.closest("#peer-form") && $("#peer-form").dataset.locked) button.disabled = true;
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
    $("#page-copy").textContent = ownerView ? text("حساب، اعتبار ماهانه و دسترسی نمایندگان را از همین‌جا کنترل کنید.", "Manage reseller accounts, prepaid validity and access here.") : text("فقط سرویس‌ها و Interfaceهای اختصاص‌یافته به حساب شما در این بخش نمایش داده می‌شوند.", "This workspace only shows services and interfaces assigned to your account.");
    $("#workspace-label").textContent = ownerView ? text("مدیریت مادر", "Owner workspace") : text("پنل نمایندگی", "Reseller panel");
    $("#breadcrumb-title").textContent = ownerView ? text("نماینده‌ها", "Resellers") : text("سرویس‌های من", "My services");
    document.title = $("#page-title").textContent + " · WG Portal";
    try { localStorage.setItem("wgportal-language", language); } catch { /* The current page still changes language. */ }
    if (adminData) renderAdmin(adminData);
    if (account) renderAccount();
    if (peerData && account?.active) renderPeers(peerData);
    if (childrenData && account?.active) renderChildren(childrenData);
    if (botData && account?.active) renderBot(botData);
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
    $("#reseller-view").hidden = !account;
    $("#reseller-add-tab").hidden = ownerView || !account?.active;
    if (!account) { if (!ownerView) location.replace("/login"); return; }
    $("#account-name").textContent = account.username;
    $("#account-expiry").textContent = text("اعتبار تا: ", "Valid until: ") + date(account.expires_at);
    const active = Boolean(account.active);
    $("#account-state").textContent = account.approval_status !== "approved" ? text("در انتظار تأیید", "Awaiting approval") : active ? text("فعال", "Active") : text("متوقف", "Paused");
    $("#account-state").className = "pill" + (active ? "" : " paused");
    const awaiting = account.approval_status !== "approved";
    $("#payment-pending-panel").hidden = !awaiting;
    $("#paused-view").hidden = active || awaiting;
    $("#active-view").hidden = !active;
    if (!active) { peerData = null; childrenData = null; botData = null; nativeCSRF = ""; $("#bot-token").value = ""; $("#peer-rows").replaceChildren(); $("#child-rows").replaceChildren(); $("#add-child").disabled = true; $("#child-password").value = ""; if ($("#child-dialog").open) $("#child-dialog").close(); if ($("#user-link-dialog").open) $("#user-link-dialog").close(); if ($("#peer-edit-dialog").open) $("#peer-edit-dialog").close(); closeQR(); }
  }
  function renderAdmin(data) {
    adminData = data;
    ownerVisibility(true);
    const selected = new Set(Array.from(document.querySelectorAll("#interface-options input:checked"), (node) => Number(node.value)));
    $("#interface-options").replaceChildren(...(data.interfaces || []).map((iface) => {
      const label = document.createElement("label"), input = document.createElement("input"), caption = document.createElement("span");
      input.type = "radio"; input.name = "assigned-interface"; input.value = iface.id; input.checked = selected.has(iface.id); caption.textContent = iface.name;
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
    $("#summary-subresellers").textContent = Number.isSafeInteger(data.summary?.subresellers) ? number.format(data.summary.subresellers) : "—";
    $("#summary-creation-fees").textContent = Number.isSafeInteger(data.summary?.total_creation_fee_toman) ? number.format(data.summary.total_creation_fee_toman) : "—";
    $("#summary-pending").textContent = number.format(accounts.filter(item => item.approval_status !== "approved").length);
    for (const item of accounts) {
      const row = rows.insertRow();
      const identity = document.createElement("div"); identity.className = "account-identity";
      const avatar = document.createElement("span"); avatar.className = "account-avatar"; avatar.setAttribute("aria-hidden", "true"); avatar.textContent = Array.from(String(item.username || "?"))[0];
      const name = document.createElement("strong"); name.textContent = item.username;
      identity.append(avatar, name); row.insertCell().append(identity);
      const created = document.createElement("small"); created.textContent = "#" + item.id + " · " + date(item.created_at); name.append(created);
      const state = document.createElement("span"); state.className = "pill " + (item.active ? "active" : item.enabled ? "expired" : "suspended");
      state.textContent = item.approval_status !== "approved" ? text("در انتظار تأیید پرداخت", "Awaiting payment approval") : item.active ? text("فعال", "Active") : item.enabled ? text("اعتبار پایان‌یافته", "Expired") : text("متوقف توسط مدیر", "Suspended by owner");
      row.insertCell().append(state);
      cell(row, date(item.expires_at)); cell(row, item.max_peers);
      cell(row, item.parent_username || text("مدیر اصلی", "Owner"));
      cell(row, number.format(item.direct_child_count || 0) + " / " + number.format(item.descendant_count || 0));
      cell(row, number.format(item.creation_fee_toman || 0));
      cell(row, interfaceText(item, data.interfaces || []));
      cell(row, item.approval_status === "approved" ? text("تأییدشده", "Approved") + " · " + date(item.approved_at) + " · " + String(item.approved_by || "—") : text("در انتظار تأیید مدیر اصلی", "Awaiting owner approval"));
      const actions = document.createElement("div"); actions.className = "actions account-actions";
      const approval = document.createElement("button"); approval.type = "button"; approval.className = "account-approval secondary";
      approval.textContent = item.approval_status === "approved" ? text("لغو تأیید", "Revoke approval") : text("تأیید پرداخت و فعال‌سازی", "Approve payment and activate");
      approval.addEventListener("click", () => action(approval, null, async () => {
        await api(`/resellers/api/accounts/${Number(item.id)}/approval`, {method:"POST",payload:{approved:item.approval_status !== "approved",days:30}});
        await loadAdmin(); notice(text("وضعیت تأیید ذخیره شد.", "Approval status saved."), "success");
      })); actions.append(approval);
      const assigned = document.createElement("select"); assigned.setAttribute("aria-label", text("Interface اختصاصی نماینده", "Assigned reseller interface"));
      const missing = document.createElement("option"); missing.value = ""; missing.textContent = text("Interface را تعیین کنید", "Assign an interface"); assigned.append(missing);
      for (const iface of data.interfaces || []) {const option = document.createElement("option"); option.value = iface.id; option.textContent = iface.name; option.selected = (item.interfaces || []).length === 1 && Number(item.interfaces[0]) === Number(iface.id); assigned.append(option);}
      const assign = document.createElement("button"); assign.type = "button"; assign.className = "secondary"; assign.textContent = text("تعیین Interface", "Assign interface");
      assign.addEventListener("click", () => action(assign, null, async () => {if (!assigned.value) throw new Error(text("یک Interface انتخاب کنید.", "Choose one interface.")); await api(`/resellers/api/accounts/${Number(item.id)}/interface`, {method:"POST",payload:{iface_id:Number(assigned.value)}}); await loadAdmin();}));
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
      actions.append(days, renew, access, signin); row.insertCell().append(actions);
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
      cell(row, item.approval_status !== "approved" ? text("در انتظار تأیید پرداخت", "Awaiting payment approval") : item.active ? text("فعال", "Active") : !item.enabled ? text("متوقف", "Suspended") : text("اعتبار پایان یافته", "Expired"));
      cell(row, date(item.expires_at));
      cell(row, number.format(item.direct_child_count || 0) + " / " + number.format(item.descendant_count || 0));
      cell(row, number.format(item.creation_fee_toman || 0));
      cell(row, interfaceText(item));
      const controls = document.createElement("div"); controls.className = "actions child-provision-actions";
      const link = document.createElement("button"); link.type = "button"; link.className = "secondary"; link.textContent = text("لینک ورود", "Sign-in link");
      link.addEventListener("click", () => showSignin()); controls.append(link);
      if (!(item.interfaces || []).length && !["pending", "complete", "uncertain"].includes(item.interface_provision?.state)) {
        const port = document.createElement("input"); port.className = "child-port"; port.type = "number"; port.min = "20000"; port.max = "65535"; port.required = true;
        port.setAttribute("aria-label", text("پورت Interface برای ", "Interface port for ") + item.username); port.dataset.childId = String(item.id);
        const propose = document.createElement("button"); propose.className = "child-port-propose secondary"; propose.type = "button"; propose.textContent = text("پیشنهاد پورت آزاد", "Suggest available port"); propose.dataset.childId = String(item.id);
        propose.addEventListener("click", () => action(propose, null, async () => {const result = await api(`/reseller/api/accounts/${Number(item.id)}/interface-port`); port.value = String(result.port); port.focus();}));
        const create = document.createElement("button"); create.className = "child-interface-create"; create.type = "button"; create.textContent = text("ساخت Interface", "Create interface"); create.dataset.childId = String(item.id);
        create.addEventListener("click", () => action(create, null, async () => {
          if (!port.reportValidity()) return;
          try {
            const result = await api(`/reseller/api/accounts/${Number(item.id)}/interface`, {method:"POST",payload:{port:Number(port.value)}});
            await loadChildren(); notice(result.message || text("Interface ساخته شد؛ فعال‌سازی حساب به تأیید مدیر اصلی نیاز دارد.", "Interface created; the owner must approve account activation."), "success");
          } catch (error) {
            if (!error.status || error.status >= 500) {create.dataset.uncertain = "true"; propose.disabled = true; port.disabled = true;}
            try {await loadChildren();} catch { /* Preserve the original error. */ }
            throw error;
          }
        })); controls.append(port, propose, create);
      }
      row.insertCell().append(controls);
    }
    if (!accounts.length) empty(rows, text("هنوز زیرنماینده‌ای ثبت نکرده‌اید.", "You have not created a subreseller yet."), 7);
    $("#child-limit").max = String(account.max_peers);
    if (Number($("#child-limit").value) > account.max_peers) $("#child-limit").value = String(account.max_peers);
    $("#add-child").disabled = (account.interfaces || []).length !== 1;
    $("#child-load-state").textContent = text("برای زیرنمایندهٔ مستقیم خود Interface اختصاصی بسازید. همهٔ زیرمجموعه‌ها و هزینهٔ ثبت در پنل مدیر اصلی دیده می‌شوند؛ تأیید پرداخت فقط با مدیر اصلی است.", "Provision a dedicated interface for your direct child. The owner sees all descendants and registration fees and approves payment.");
    $("#child-load-state").removeAttribute("data-fa"); $("#child-load-state").removeAttribute("data-en");
  }
  function closeQR() {
    $("#qr-image").removeAttribute("src");
    if ($("#qr-dialog").open) $("#qr-dialog").close();
  }
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
        if (typeof result.url !== "string" || !result.url.trim()) throw new Error(text("لینک دریافت‌شده معتبر نیست.", "The returned link is invalid."));
        const url = new URL(result.url, location.origin);
        if (!["http:", "https:"].includes(url.protocol) || url.username || url.password) throw new Error(text("لینک دریافت‌شده معتبر نیست.", "The returned link is invalid."));
        $("#user-link-url").value = url.href; $("#user-link-open").href = url.href; $("#user-link-result").textContent = ""; $("#user-link-dialog").showModal();
      }));
      actions.append(config, qr, userLink, edit); row.insertCell().append(actions);
    }
    if (!(data.peers || []).length) empty(rows, text("هنوز سرویسی برای این حساب وجود ندارد.", "No services belong to this account yet."), 5);
  }
  async function loadAdmin() {
    try { renderAdmin(await api("/resellers/api/accounts")); }
    catch (error) { ownerVisibility(false); throw error; }
  }
  async function loadPeers() { if (account?.active) renderPeers(await api("/reseller/api/peers")); }
  function renderBot(data) {
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
      conflict:text("بات در جای دیگری اجرا می‌شود؛ اجرای قبلی را متوقف کنید.", "The bot is running elsewhere; stop the previous runner.")
    };
    $("#bot-status").textContent = bot.configured ? (bot.username ? "@" + String(bot.username) + " · " : "") + (states[bot.status] || (bot.enabled ? text("در حال راه‌اندازی", "Starting") : text("غیرفعال", "Disabled"))) : text("هنوز باتی متصل نشده است.", "No bot connected yet.");
    $("#bot-disable").hidden = !bot.configured;
    if (bot.owner_id) $("#bot-owner-id").value = String(bot.owner_id);
  }
  async function loadBot() {
    if (!account?.active || ownerView) return;
    try {renderBot(await api("/reseller/api/bot"));}
    catch (error) {$("#bot-status").textContent = text("دریافت وضعیت بات ناموفق بود. وضعیت حساب را تازه کنید.", "Bot status could not be loaded. Refresh account status.");}
  }
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
    $("#loading").hidden = false;
    try {
      const result = await api("/reseller/api/session"); account = result.account || null;
      if (ownerView) await loadAdmin();
      else { renderAccount(); if (account?.active) await Promise.all([loadPeers(), loadChildren(), loadBot()]); }
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
  $("#logout").addEventListener("click", () => action($("#logout"), null, async () => {
    await api("/reseller/api/logout", { method: "POST", payload: {} });
    csrf = ""; account = null; peerData = null; childrenData = null; closeQR(); $("#peer-rows").replaceChildren(); $("#child-rows").replaceChildren(); await load();
  }));
  $("#account-form").addEventListener("submit", (event) => {
    event.preventDefault(); const payload = Object.fromEntries(new FormData(event.currentTarget));
    payload.interfaces = Array.from(document.querySelectorAll("#interface-options input:checked"), (node) => Number(node.value));
    payload.max_peers = Number(payload.max_peers);
    if (!payload.interfaces.length) return formResult("#account-result", text("حداقل یک Interface انتخاب کنید.", "Select at least one interface."), true);
    action(event.submitter || event.currentTarget.querySelector('[type="submit"]'), "#account-result", async () => {
      const created = await api("/resellers/api/accounts", { method: "POST", payload });
      $("#account-password").value = "";
      $("#account-dialog").close();
      $("#account-form").reset();
      showSignin(created.signin_url || "/reseller");
      notice(text("حساب ساخته شد. فعال‌سازی به پرداخت ۳٬۰۰۰٬۰۰۰ تومان و تأیید شما نیاز دارد.", "Account created. Activation requires payment of 3,000,000 toman and your approval."), "success");
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
    if (event.currentTarget.dataset.locked) return;
    const payload = Object.fromEntries(new FormData(event.currentTarget));
    payload.max_peers = Number(payload.max_peers);
    action(event.submitter || event.currentTarget.querySelector('[type="submit"]'), "#child-result", async () => {
      let created;
      try { created = await api("/reseller/api/accounts", {method:"POST", payload}); }
      catch (error) {
        if (!error.status || error.status >= 500) {
          $("#child-form").dataset.locked = "true";
          error.message = text("نتیجهٔ ساخت قطعی نیست؛ پیش از درخواست تازه، فهرست را تازه کنید و از مدیر بخواهید وضعیت را بررسی کند.", "The creation outcome is uncertain. Refresh the list and ask the owner to verify it before a new request.");
        }
        throw error;
      }
      $("#child-password").value = ""; $("#child-dialog").close(); $("#child-form").reset();
      showSignin(created.signin_url || "/reseller");
      notice(text("زیرنماینده ثبت شد و در پنل مدیر اصلی دیده می‌شود. Interface اختصاصی بسازید و لینک ورود را بدهید تا برای پرداخت و تأیید به مدیر پیام بدهد.", "Subreseller created and visible to the owner. Provision its dedicated interface and share the sign-in link for payment and approval."), "success");
      try { await loadChildren(); } catch { notice(text("حساب ثبت شد، اما فهرست تازه دریافت نشد. فقط وضعیت را تازه کنید؛ حساب را دوباره نسازید.", "The account was created, but the list could not be refreshed. Refresh status only; do not create it again.")); }
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
        renderBot(await api("/reseller/api/bot", {method:"POST",payload}));
        formResult("#bot-result", text("بات ذخیره شد و راه‌اندازی خودکار آغاز شد. در تلگرام Start بزنید.", "Bot saved and automatic startup requested. Press Start in Telegram."));
      } finally {$("#bot-token").value = ""; payload.token = "";}
    });
  });
  $("#bot-disable").addEventListener("click", () => action($("#bot-disable"), "#bot-result", async () => {
    const result = await api("/reseller/api/bot", {method:"DELETE"}); renderBot(result);
    $("#bot-token").value = ""; formResult("#bot-result", text("بات غیرفعال شد.", "Bot disabled."));
  }));
  $("#qr-image").addEventListener("error", () => { closeQR(); notice(text("تصویر QR دریافت نشد؛ وضعیت حساب و سرویس را تازه کنید.", "The QR image could not be loaded. Refresh your account and service status."), "error"); });
  translate(language); load();
})();
