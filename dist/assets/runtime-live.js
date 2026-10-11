/* Activated only by the Linux installer. WG_Panel keeps authentication and host operations. */
(() => {
  if (window.WG_PORTAL_RUNTIME?.mode !== "wgpanel") return;

  const $ = (selector) => document.querySelector(selector);
  const fa = () => document.documentElement.lang !== "en";
  let peers = [];
  let connected = false;
  let connectionProblem = null;

  document.body.classList.add("runtime-live");
  $(".role-picker").hidden = true;
  $("#customerMetric").textContent = "—";
  $("#customerRows").replaceChildren();
  $("#view-customers .heading-actions").replaceChildren();
  const managePeers = document.createElement("a");
  managePeers.className = "button button-primary";
  managePeers.href = "/users";
  $("#view-customers .heading-actions").append(managePeers);
  const createPeer = document.createElement("a");
  createPeer.className = "button button-primary";
  createPeer.href = "/users";
  $(".welcome-row button[data-action='create-customer']").replaceWith(createPeer);

  for (const link of document.querySelectorAll(".nav-link[data-page]")) {
    if (!["overview", "customers"].includes(link.dataset.page)) link.hidden = true;
  }
  const panelLink = document.createElement("a");
  panelLink.className = "nav-link operational-link";
  panelLink.href = "/users";
  panelLink.innerHTML = '<i aria-hidden="true">▦</i><span></span>';
  $(".main-nav").append(panelLink);

  const liveCard = document.createElement("section");
  liveCard.className = "card live-peer-card";
  liveCard.innerHTML = '<div class="card-head"><div><div class="section-title"><span class="heading-icon blue" aria-hidden="true">▦</span><h2></h2></div><p></p></div><a class="button button-outline" href="/users"></a></div><div class="live-peer-list"></div>';
  $(".metric-grid").after(liveCard);
  const usageCard = document.createElement("section");
  usageCard.className = "card live-usage-card";
  usageCard.innerHTML = '<div class="card-head"><div><div class="section-title"><span class="heading-icon mint" aria-hidden="true">▥</span><h2></h2></div><p></p></div><strong class="live-usage-total"></strong></div><div class="live-usage-bars"></div>';
  liveCard.after(usageCard);

  function translate() {
    const isFa = fa();
    for (const link of document.querySelectorAll(".nav-link[data-page]")) {
      if (!["overview", "customers"].includes(link.dataset.page)) link.hidden = true;
    }
    managePeers.textContent = isFa ? "مدیریت Peer در پنل عملیاتی" : "Manage peers in control panel";
    createPeer.textContent = isFa ? "مدیریت Peerها" : "Manage peers";
    $(".welcome-row p").textContent = isFa ? "وضعیت WireGuard و Peerهای سرورت را یک‌جا ببین." : "See your WireGuard server and peers in one place.";
    $("#profileName").textContent = isFa ? "اپراتور WG_Panel" : "WG_Panel operator";
    $("#profileRole").textContent = isFa ? "پنل اجرایی" : "Control panel";
    $("#profileAvatar").textContent = isFa ? "و" : "W";
    $("#workspaceName").textContent = "WG_Panel";
    panelLink.querySelector("span").textContent = isFa ? "پنل عملیاتی WireGuard" : "WireGuard control panel";
    liveCard.querySelector("h2").textContent = isFa ? "Peerهای واقعی" : "Live peers";
    liveCard.querySelector("p").textContent = isFa ? "داده‌های خوانده‌شده از WG_Panel" : "Data from WG_Panel";
    liveCard.querySelector("a").textContent = isFa ? "مدیریت کامل ←" : "Full controls →";
    usageCard.querySelector("h2").textContent = isFa ? "مصرف Peerها" : "Peer usage";
    usageCard.querySelector("p").textContent = isFa ? "مصرف تجمعی گزارش‌شده از سرور" : "Cumulative usage reported by the server";
    $(".metric-card:nth-child(1) .metric-label").textContent = isFa ? "کل Peerها" : "Total peers";
    $("#view-overview .metric-card:nth-child(1) .metric-period").textContent = isFa ? "هم‌اکنون" : "Now";
    $(".metric-card:nth-child(2) .metric-label").textContent = isFa ? "Peer متصل" : "Connected peers";
    $(".demo-ribbon").textContent = connected
      ? (isFa ? "اتصال زنده به WG_Panel · عملیات فروش در حال آماده‌سازی است" : "Live WG_Panel connection · sales tools in development")
      : (isFa ? "برای دادهٔ زنده به پنل عملیاتی وارد شوید" : "Sign in to see live data");
    if (connected) {
      $(".status-banner b").textContent = isFa ? "WG_Panel متصل است" : "WG_Panel connected";
      $(".status-banner small").textContent = isFa ? "اطلاعات Peerها از سرور خوانده شد." : "Peers were read from the server.";
    } else if (connectionProblem) {
      $(".status-banner b").textContent = connectionProblem === "sign-in"
        ? (isFa ? "برای دیدن دادهٔ زنده وارد شوید" : "Sign in to see live data")
        : (isFa ? "ارتباط با WG_Panel برقرار نیست" : "WG_Panel is unavailable");
      $(".status-banner small").textContent = isFa ? "پس از ورود، اطلاعات واقعی نمایش داده می‌شود." : "Live data appears after sign-in.";
    }
    renderSummaries();
    renderPeers();
    renderUsage();
  }

  function renderSummaries() {
    const number = (value) => value.toLocaleString(fa() ? "fa-IR" : "en-US");
    const online = peers.filter((peer) => peerState(peer) === "online").length;
    const near = peers.filter((peer) => {
      const quota = quotaGb(peer);
      return quota !== null && usedGb(peer) / quota >= .9;
    }).length;
    const expiring = peers.filter((peer) => peer.ttl_seconds != null &&
      Number(peer.ttl_seconds) >= 0 && Number(peer.ttl_seconds) <= 3 * 86400).length;
    $("#customerMetric").textContent = connected ? number(peers.length) : "—";
    $(".nav-link[data-page='customers'] .nav-count").textContent = connected ? number(peers.length) : "—";
    $("#view-overview .metric-card:nth-child(2) strong").textContent = connected ? number(online) : "—";
    const metrics = $("#view-customers .compact-metrics").querySelectorAll(".metric-card strong");
    [peers.length, online, near, expiring].forEach((value, index) => {
      metrics[index].textContent = connected ? number(value) : "—";
    });
  }

  function peerState(peer) {
    if ([peer.status, peer.panel_status].some((state) => ["expired", "disabled", "blocked"].includes(state))) return "expired";
    return /^(online|connected)$/i.test(String(peer.conn_status || peer.connection_status || "").trim()) ? "online" : "offline";
  }

  function usedGb(peer) {
    return Math.max(0, Number(peer.used_bytes || 0) / 1073741824);
  }

  function quotaGb(peer) {
    if (peer.unlimited) return null;
    const value = Number(peer.data_limit || 0);
    const unit = String(peer.limit_unit || "GB").toLowerCase();
    if (!value) return null;
    if (["mi", "mb", "mib"].includes(unit)) return value / 1024;
    if (["ti", "tb", "tib"].includes(unit)) return value * 1024;
    return value;
  }

  function statusText(status) {
    return fa() ? ({ online: "آنلاین", offline: "آفلاین", expired: "غیرفعال" }[status])
      : ({ online: "Online", offline: "Offline", expired: "Disabled" }[status]);
  }

  function renderUsage() {
    const total = peers.reduce((sum, peer) => sum + usedGb(peer), 0);
    usageCard.querySelector(".live-usage-total").textContent = connected ? `${total.toFixed(1)} GB` : "—";
    const bars = usageCard.querySelector(".live-usage-bars");
    bars.replaceChildren();
    const topPeers = [...peers].sort((a, b) => usedGb(b) - usedGb(a)).slice(0, 5);
    for (const peer of topPeers) {
      const row = document.createElement("div");
      row.className = "live-usage-row";
      const label = document.createElement("strong");
      label.textContent = String(peer.name || peer.address || "Peer");
      const track = document.createElement("div");
      track.className = "live-usage-track";
      const fill = document.createElement("span");
      const quota = quotaGb(peer);
      fill.style.width = `${Math.min(100, Math.max(3, quota ? usedGb(peer) / quota * 100 : total ? usedGb(peer) / total * 100 : 3))}%`;
      track.append(fill);
      const value = document.createElement("span");
      value.textContent = `${usedGb(peer).toFixed(1)} / ${quota === null ? "∞" : quota.toFixed(1)} GB`;
      row.append(label, track, value);
      bars.append(row);
    }
    if (!topPeers.length) bars.textContent = fa() ? "مصرفی برای نمایش نیست" : "No usage to show";
  }

  function textCell(row, value) {
    const cell = row.insertCell();
    cell.textContent = value;
    return cell;
  }

  function renderPeers() {
    const needle = ($("#customerSearch")?.value || "").trim().toLocaleLowerCase();
    const filter = $("#customerFilter")?.value || "all";
    const visible = peers.filter((peer) => {
      const status = peerState(peer);
      const quota = quotaGb(peer);
      const matches = !needle || [peer.name, peer.phone_number, peer.iface]
        .some((part) => String(part || "").toLocaleLowerCase().includes(needle));
      return matches && (filter === "all" ||
        (filter === "active" && status !== "expired") ||
        (filter === "near" && quota && usedGb(peer) / quota >= .9) ||
        (filter === "expired" && status === "expired"));
    });
    const body = $("#customerRows");
    body.replaceChildren();
    for (const peer of visible) {
      const row = body.insertRow();
      const status = peerState(peer);
      const quota = quotaGb(peer);
      const name = String(peer.name || peer.address || "Peer");
      textCell(row, name);
      const stateCell = textCell(row, statusText(status));
      stateCell.className = status === "online" ? "live-online" : "live-muted";
      textCell(row, `${usedGb(peer).toFixed(1)} / ${quota === null ? "∞" : quota.toFixed(1)} GB`);
      const days = peer.ttl_seconds == null ? "—" : String(Math.max(0, Math.ceil(Number(peer.ttl_seconds) / 86400)));
      textCell(row, days === "—" ? days : `${days} ${fa() ? "روز" : "days"}`);
      textCell(row, String(peer.iface || "—"));
      const linkCell = row.insertCell();
      const link = document.createElement("a");
      link.href = "/users";
      link.className = "live-row-link";
      link.textContent = fa() ? "مدیریت" : "Manage";
      linkCell.append(link);
    }
    if (visible.length === 0) {
      const row = body.insertRow();
      const cell = row.insertCell();
      cell.colSpan = 6;
      cell.className = "empty-row";
      cell.textContent = fa() ? "Peerی برای نمایش نیست" : "No peers to show";
    }
    $("#customerCount").textContent = `${visible.length.toLocaleString(fa() ? "fa-IR" : "en-US")} / ${peers.length.toLocaleString(fa() ? "fa-IR" : "en-US")}`;
    const list = liveCard.querySelector(".live-peer-list");
    list.replaceChildren();
    for (const peer of peers.slice(0, 6)) {
      const item = document.createElement("div");
      item.className = "live-peer-item";
      const name = document.createElement("strong");
      name.textContent = String(peer.name || peer.address || "Peer");
      const detail = document.createElement("span");
      detail.textContent = String(peer.iface || "WireGuard");
      const state = document.createElement("em");
      state.textContent = statusText(peerState(peer));
      state.className = peerState(peer) === "online" ? "live-online" : "live-muted";
      item.append(name, detail, state);
      list.append(item);
    }
    if (!peers.length) list.textContent = fa() ? "هنوز Peerی در پنل ثبت نشده است." : "No peers yet.";
  }

  function showConnectionAction(message) {
    const box = $(".status-banner");
    box.querySelector("b").textContent = message;
    box.querySelector("small").textContent = fa() ? "پس از ورود، اطلاعات واقعی نمایش داده می‌شود." : "Live data appears after sign-in.";
    const login = document.createElement("a");
    login.href = "/login";
    login.className = "button button-outline";
    login.textContent = fa() ? "ورود به پنل" : "Sign in";
    box.append(login);
  }

  $("#customerSearch").addEventListener("input", renderPeers);
  $("#customerFilter").addEventListener("change", renderPeers);
  $("#clearSearch").addEventListener("click", renderPeers);
  $("#languageToggle").addEventListener("click", translate);
  translate();

  fetch("/api/peers", { credentials: "same-origin", headers: { Accept: "application/json" } })
    .then(async (response) => {
      if (response.status === 401 || response.redirected) throw new Error("sign-in");
      if (!response.ok || !(response.headers.get("content-type") || "").includes("application/json")) throw new Error("unavailable");
      const data = await response.json();
      if (!Array.isArray(data.peers)) throw new Error("unavailable");
      return data.peers;
    })
    .then((result) => {
      peers = result;
      connected = true;
      translate();
    })
    .catch((error) => {
      connectionProblem = error.message;
      const message = error.message === "sign-in"
        ? (fa() ? "برای دیدن دادهٔ زنده وارد شوید" : "Sign in to see live data")
        : (fa() ? "ارتباط با WG_Panel برقرار نیست" : "WG_Panel is unavailable");
      showConnectionAction(message);
      renderPeers();
    });
})();
