/* Translate the shared shell without replacing native elements or event bindings. */
(() => {
  "use strict";
  const dictionary = new Map(Object.entries({
    "Dashboard":"داشبورد","Overview":"نمای کلی","Peers":"Peerها","Subscriptions":"اشتراک‌ها","Nodes":"سرورها","Panel Center":"مرکز عملیات","Activity logs":"گزارش فعالیت","Backup":"پشتیبان‌گیری","Settings":"تنظیمات","API Docs":"مستندات API","Main workspace":"فضای اصلی","Manage nodes":"مدیریت سرورها","Network overview":"نمای کلی شبکه","Peer workspace":"مدیریت Peerها","Create peer":"ساخت Peer","Refresh":"تازه‌سازی","Connected peers":"Peerهای متصل","Active subscriptions":"اشتراک‌های فعال","Telegram hub":"ربات تلگرام","Telegram":"تلگرام","Sign in":"ورود","Sign In":"ورود","Log in":"ورود","Login":"ورود","Register":"ثبت حساب","Create account":"ساخت حساب","Username":"نام کاربری","Email":"ایمیل","Password":"رمز عبور","Confirm password":"تکرار رمز عبور","Remember me":"مرا به خاطر بسپار","Sign out":"خروج","Password & security":"رمز و امنیت","Save profile":"ذخیرهٔ پروفایل","Navigation":"دسترسی‌ها","Management":"مدیریت","Secure management":"مدیریت اتصال و سرویس‌ها","Resellers":"نمایندگی‌ها"
  }));
  dictionary.set("Welcome back","خوش آمدید");
  dictionary.set("Sign in to your WG Portal","وارد پنل خود شوید");
  Object.entries({"Create peer":"ساخت Peer","Create Peer":"ساخت Peer","Profile defaults":"پیش‌فرض‌های پروفایل","Apply or save reusable peer defaults.":"پیش‌فرض‌های قابل استفادهٔ مجدد را اعمال یا ذخیره کنید.","Use profile":"استفاده از پروفایل","Identity":"مشخصات","Name and client address.":"نام و آدرس کلاینت.","Friendly Name":"نام سرویس","IP Address":"آدرس IP","Limits":"محدودیت‌ها","Traffic, timer, first-use, and unlimited rules.":"حجم، زمان، شروع در اولین اتصال و دسترسی نامحدود.","Traffic Limit":"سقف حجم","Unit":"واحد","Days":"روز","Hours":"ساعت","Start timer on first connection":"شروع زمان در اولین اتصال","Unlimited":"نامحدود","Contact":"اطلاعات تماس","Optional details for search and management.":"اطلاعات اختیاری برای جستجو و مدیریت.","Phone Number":"شماره تلفن","Telegram ID":"شناسهٔ تلگرام","WireGuard config":"تنظیمات WireGuard","Routing, endpoints, DNS, keepalive, and MTU.":"مسیرها، Endpoint، DNS، Keepalive و MTU.","Server endpoint":"Endpoint سرور","Interface default":"پیش‌فرض Interface","Not configured":"تنظیم نشده","Saved endpoint presets":"Endpointهای ذخیره‌شده","Fixed client endpoint":"Endpoint ثابت کلاینت","Optional":"اختیاری","Allowed IPs":"مسیرهای مجاز","Include private networks":"شامل شبکه‌های خصوصی","Default":"پیش‌فرض","Add":"افزودن","Cancel":"انصراف","Create interface":"ساخت Interface","New interface":"Interface جدید","Interface name":"نام Interface","Server address":"آدرس سرور","Listen port":"پورت اتصال","Client DNS":"DNS کلاینت","Start after creation":"فعال‌سازی پس از ساخت","Bring the new interface online immediately.":"Interface جدید بلافاصله فعال شود.","Set the name, WireGuard defaults, limits, and contact details in one clean form.":"نام، تنظیمات وایرگارد، محدودیت‌ها و اطلاعات تماس سرویس را مشخص کنید.","Choose from the free addresses on the selected interface.":"یک آدرس آزاد از Interface انتخاب‌شده تعیین کنید.","Leave empty to use interface default":"برای استفاده از پیش‌فرض Interface خالی بگذارید","Leave empty for roaming clients":"برای کلاینت‌های سیار خالی بگذارید","e.g. azumi":"مثلاً سرویس مشتری","e.g. 10":"مثلاً ۱۰","optional":"اختیاری"}).forEach(([key,value]) => dictionary.set(key,value));
  dictionary.set("Create Admin Account","ساخت حساب مدیر");
  dictionary.set("Setup token","کد نصب");
  dictionary.set("Set up your administrator credentials.","نام کاربری و رمز مدیر اصلی را تنظیم کنید.");
  dictionary.set("A clear view of your network and connected clients.","وضعیت شبکه و مشتریان متصل را یک‌جا ببینید.");
  Object.entries({
    "Workspace":"فضای کاری","Infrastructure":"زیرساخت","Admin":"مدیریت","Activity":"فعالیت‌ها","Control panel":"پنل مدیریت","Search panel…":"جستجو در پنل…","Open profile":"باز کردن پروفایل",
    "Total transfer":"مجموع ترافیک","Panel response":"زمان پاسخ پنل","Server totals since boot":"مجموع سرور از زمان راه‌اندازی","Browser → panel API":"مرورگر ← API پنل",
    "Waiting for peer data":"در انتظار اطلاعات Peerها","Waiting for policies":"در انتظار اطلاعات دسترسی‌ها","Checking system health…":"در حال بررسی سلامت سیستم…","Checking nodes…":"در حال بررسی سرورها…","View health":"مشاهدهٔ سلامت",
    "Update center":"مرکز به‌روزرسانی","Traffic overview":"مصرف سرویس‌ها","Incoming and outgoing server bandwidth":"پهنای باند ورودی و خروجی سرور","Download":"دریافت","Upload":"ارسال","Overall system health":"وضعیت کلی سلامت سیستم",
    "Checking":"در حال بررسی","Healthy":"سالم","Review":"نیازمند بررسی","Attention":"نیازمند رسیدگی","Unknown":"نامشخص","Unavailable":"در دسترس نیست","Healthy checks / all checks":"بررسی‌های سالم / همهٔ بررسی‌ها",
    "Collecting live traffic samples…":"در حال جمع‌آوری نمونه‌های ترافیک…","Live samples · collected while this page is open":"نمونه‌های زنده · در مدت باز بودن این صفحه","Refresh failed · chart shows earlier samples":"تازه‌سازی ناموفق · نمودار نمونه‌های قبلی را نشان می‌دهد",
    "Chart library unavailable. Live values are still shown above.":"نمودار در دسترس نیست. مقادیر زنده در بالا نمایش داده می‌شوند.","Live metrics unavailable · retrying":"آمار زنده در دسترس نیست · تلاش دوباره",
    "Memory":"حافظه","Disk":"دیسک","System details":"جزئیات سیستم","Host":"میزبان","Uptime":"زمان کارکرد","Platform":"سیستم‌عامل","Not available":"در دسترس نیست",
    "Checking bot status…":"در حال بررسی وضعیت ربات…","Connected":"متصل","Offline":"آفلاین","Online":"آنلاین","Blocked":"مسدود","Bot service is running":"سرویس ربات در حال اجراست","Bot is offline or has no recent heartbeat":"ربات آفلاین است یا اخیراً پاسخی نداده است","Unable to read bot status":"دریافت وضعیت ربات ناموفق بود",
    "Bot logs":"گزارش ربات","Admin activity":"فعالیت مدیر","Needs attention":"نیازمند رسیدگی","Checking subscriptions…":"در حال بررسی اشتراک‌ها…","Expiry and access-policy summary.":"خلاصهٔ اعتبار و سیاست‌های دسترسی.","Review subscriptions":"بررسی اشتراک‌ها","No subscription alerts":"هشداری برای اشتراک‌ها وجود ندارد","No active subscriptions expire within the next 7 days.":"هیچ اشتراک فعالی در ۷ روز آینده منقضی نمی‌شود.","Review expired, exhausted or manually disabled access.":"دسترسی‌های منقضی، تمام‌شده یا غیرفعال را بررسی کنید.",
    "Recent connections":"اتصال‌های اخیر","Peers ordered by latest WireGuard handshake":"Peerها به ترتیب آخرین اتصال وایرگارد","View all":"مشاهدهٔ همه","Peer":"Peer","Node / interface":"سرور / Interface","Traffic":"ترافیک","Status":"وضعیت","Last handshake":"آخرین اتصال","Loading peers…":"در حال دریافت Peerها…","Waiting for live data":"در انتظار اطلاعات زنده","Manage peers":"مدیریت Peerها","WireGuard workspace":"فضای مدیریت وایرگارد","Waiting for first refresh":"در انتظار نخستین تازه‌سازی",
    "No peers yet. Create your first peer to get started.":"هنوز Peerی وجود ندارد. برای شروع اولین Peer را بسازید.","Unable to load peers. Use Refresh to try again.":"دریافت Peerها ناموفق بود. دوباره تازه‌سازی کنید.","Peer data unavailable":"اطلاعات Peerها در دسترس نیست","Policy data unavailable":"اطلاعات دسترسی‌ها در دسترس نیست","Subscription check unavailable":"بررسی اشتراک‌ها در دسترس نیست","Open subscriptions to inspect current access policies.":"برای بررسی دسترسی‌ها، صفحهٔ اشتراک‌ها را باز کنید.","Node status unavailable":"وضعیت سرورها در دسترس نیست","Local server":"سرور محلی","Health data unavailable":"اطلاعات سلامت در دسترس نیست","Health summary unavailable":"خلاصهٔ سلامت در دسترس نیست","Health check unavailable":"بررسی سلامت در دسترس نیست","Health details require administrator access":"جزئیات سلامت به دسترسی مدیر نیاز دارد",
    "Workspace tools":"ابزارهای مدیریت","Live traffic window":"بازهٔ ترافیک زنده","Open Panel Center":"باز کردن مرکز عملیات","Connected peers during this session":"Peerهای متصل در این نشست","Active subscriptions during this session":"اشتراک‌های فعال در این نشست","Server traffic during this session":"ترافیک سرور در این نشست","API response times during this session":"زمان پاسخ API در این نشست","Live download and upload bandwidth in megabits per second":"پهنای باند زندهٔ دریافت و ارسال بر حسب مگابیت بر ثانیه","CPU usage":"مصرف پردازنده","Memory usage":"مصرف حافظه","Disk usage":"مصرف دیسک"
  }).forEach(([key,value]) => dictionary.set(key,value));
  const originals = new WeakMap();
  const attributeOriginals = new WeakMap();
  let language = document.documentElement.lang === "en" ? "en" : "fa";
  try { if(localStorage.getItem("wgportal-language") === "en") language = "en"; } catch { /* Storage is optional. */ }
  const selector = ".sb2-text,.sb2-section,.nx-workspace-picker a,.nx-workspace-picker summary span,.nx-mobile-nav a,.nx-breadcrumb span,.nx-breadcrumb b,.nx-search-trigger span,.account-signout,.auth-header .muted,.auth-card .hint,label,button[type=submit],h1,h2,#nx-dashboard :is(p,span,small,strong,b,th,dt,dd,button,a),.peerx-form-modal :is(p,span,small,strong,b,legend,label,button),#iface-create-modal :is(p,span,small,strong,b,legend,label,button),.portal-interface-defaults :is(p,span,small,strong,b,label,button)";
  function localized(key) {
    if (dictionary.has(key)) return dictionary.get(key);
    const patterns = [
      [/^(\d+) peers in inventory$/,n=>`${n} Peer در فهرست`],
      [/^(\d+) total access policies$/,n=>`${n} سیاست دسترسی`],
      [/^(\d+) online · (\d+) offline or blocked$/,(a,b)=>`${a} آنلاین · ${b} آفلاین یا مسدود`],
      [/^(\d+) of (\d+) nodes online$/,(a,b)=>`${a} از ${b} سرور آنلاین`],
      [/^(\d+) of (\d+) checks healthy$/,(a,b)=>`${a} از ${b} بررسی سالم`],
      [/^(\d+) subscriptions? expire soon$/,n=>`${n} اشتراک به‌زودی منقضی می‌شود`],
      [/^(\d+) blocked or disabled polic(?:y|ies)$/,n=>`${n} دسترسی مسدود یا غیرفعال`],
      [/^Expiring within 7 days\.(?: (\d+) other policies are blocked or disabled\.)?$/,n=>`انقضا در ۷ روز آینده.${n ? ` ${n} دسترسی دیگر مسدود یا غیرفعال است.` : ""}`],
      [/^Updated (.+) · (.+)$/,(time,zone)=>`آخرین دریافت ${time} · ${zone}`]
    ];
    for (const [pattern, format] of patterns) {const match = key.match(pattern);if(match) return format(...match.slice(1));}
    return null;
  }
  function translateNode(node) {
    const previous = originals.get(node);
    // Native refreshes replace text nodes or overwrite textContent. Always retain the newest source.
    const original = previous && node.textContent === previous.rendered ? previous.source : node.textContent;
    const key = original.trim(), translated = localized(key);
    if (translated === null) return;
    const rendered = language === "fa" ? original.replace(key,translated) : original;
    originals.set(node,{source:original,rendered});
    if(node.textContent !== rendered) node.textContent = rendered;
  }
  function themeCharts() {
    if(!window.Chart?.getChart) return;
    const colors = ["#63cef3","#57d7b4"];
    const chart = window.Chart.getChart("nx-traffic-chart");
    if(chart){
      chart.data.datasets.forEach((dataset,index)=>{dataset.borderColor=colors[index];dataset.backgroundColor=index ? "#57d7b414" : "#63cef31a";dataset.label=language === "fa" ? ["دریافت","ارسال"][index] : ["Download","Upload"][index];});
      for(const axis of Object.values(chart.options.scales)){axis.ticks.color="#93b1c4";axis.grid.color="#8dbbd311";}
      chart.update("none");
    }
    ["peer","sub","transfer","response"].forEach((key,index)=>{
      const spark=window.Chart.getChart(`nx-${key}-spark`);if(!spark) return;
      const color=["#63cef3","#57d7b4","#d9b475","#63cef3"][index];
      spark.data.datasets[0].borderColor=color;spark.data.datasets[0].backgroundColor=color+"18";spark.update("none");
    });
  }
  function apply() {
    document.documentElement.lang = language;
    document.documentElement.dir = language === "fa" ? "rtl" : "ltr";
    document.querySelectorAll(selector).forEach((element) => {
      if(element.closest("[data-portal-fa]") || (element.closest("#nx-recent-peers") && !element.matches(".nx-status-pill,.nx-empty"))) return;
      element.childNodes.forEach((node) => {
        if(node.nodeType !== Node.TEXT_NODE) return;
        translateNode(node);
      });
    });
    document.querySelectorAll("[data-portal-fa][data-portal-en]").forEach((element)=>{const value=element.getAttribute(`data-portal-${language}`);if(element.textContent!==value)element.textContent=value;});
    document.querySelectorAll("#nx-dashboard [aria-label],.nx-topbar [aria-label]").forEach((element)=>{
      const source=attributeOriginals.get(element) || element.getAttribute("aria-label");
      if(!dictionary.has(source)) return;
      attributeOriginals.set(element,source);
      element.setAttribute("aria-label",language === "fa" ? dictionary.get(source) : source);
    });
    document.querySelectorAll("#portal-language-toggle,#portal-language-toggle-mobile").forEach((toggle)=>{toggle.textContent = language === "fa" ? "EN" : "فارسی";toggle.setAttribute("aria-label",language === "fa" ? "Switch to English" : "تغییر زبان به فارسی");});
    const management = document.getElementById("portal-management-link");
    if(management) management.textContent = language === "fa" ? "مدیریت اتصال و سرویس‌ها" : "Secure management";
    try {localStorage.setItem("wgportal-language",language);} catch { /* This page still uses the selected language. */ }
    document.cookie = "wgportal-language=" + language + "; Path=/; Max-Age=31536000; SameSite=Lax" + (location.protocol === "https:" ? "; Secure" : "");
    renderResellerSummary();
  }
  let resellerSummary = null;
  function renderResellerSummary() {
    if (!resellerSummary) return;
    const numbers = new Intl.NumberFormat(language === "fa" ? "fa-IR" : "en-GB");
    [["portal-total-resellers", "total_resellers"], ["portal-subresellers", "subresellers"], ["portal-reseller-fees", "total_creation_fee_toman"]].forEach(([id, key]) => {
      const node = document.getElementById(id), value = numbers.format(resellerSummary[key]);
      if (node && node.textContent !== value) node.textContent = value;
    });
  }
  async function loadResellerSummary() {
    const state = document.getElementById("portal-reseller-summary-state");
    if (!state) return;
    const controller = new AbortController(), timer = setTimeout(() => controller.abort(), 10000);
    try {
      const response = await fetch("/resellers/api/summary", {credentials:"same-origin", cache:"no-store", headers:{Accept:"application/json"}, signal:controller.signal});
      if (!response.ok) throw new Error("Reseller totals unavailable");
      const data = (await response.json()).summary;
      if (!data || ["total_resellers", "subresellers", "total_creation_fee_toman"].some((key) => !Number.isSafeInteger(data[key]) || data[key] < 0)) throw new Error("Invalid reseller totals");
      resellerSummary = data;
      state.dataset.portalFa = "هزینه‌ها فقط مبلغ ثبت‌شده هستند؛ پرداخت آنلاین هنوز فعال نیست.";
      state.dataset.portalEn = "Fees are declared amounts only; online payment is not enabled.";
    } catch (_) {
      state.dataset.portalFa = "دریافت آمار نمایندگی ناموفق بود؛ برای بررسی، مدیریت نمایندگان را باز کنید.";
      state.dataset.portalEn = "Reseller totals could not be loaded. Open Manage resellers to inspect them.";
    } finally { clearTimeout(timer); apply(); }
  }
  function init() {
    if(document.getElementById("portal-language-toggle")) return;
    document.querySelectorAll("nav .sb2-link[data-settings-nav]").forEach((settings) => {
      if(document.getElementById("nx-dashboard")?.dataset.admin === "0") return;
      if(settings.parentElement.querySelector('a[href="/resellers"]')) return;
      const link = document.createElement("a"); link.className = "sb2-link"; link.href = "/resellers";
      const icon = document.createElement("i"); icon.className = "fas fa-user-group"; icon.setAttribute("aria-hidden","true");
      const label = document.createElement("span"); label.className="sb2-text"; label.textContent="Resellers";
      link.append(icon,label); settings.before(link);
    });
    const container = document.querySelector(".nx-topbar-actions") || document.querySelector(".sb2-bottom") || document.querySelector(".auth-form") || document.querySelector("form");
    if(container){
      const tools = document.createElement("div"); tools.className = "portal-shell-tools";
      const button = document.createElement("button"); button.type = "button"; button.id = "portal-language-toggle";
      button.addEventListener("click",()=>{language = language === "fa" ? "en" : "fa";apply();themeCharts();});tools.append(button);
      if(document.querySelector(".sidebar") && !container.matches(".nx-topbar-actions")){
        const link = document.createElement("a");link.id="portal-management-link";link.href="/manage";tools.append(link);
      }
      container.append(tools);
    }
    const mobileBar=document.querySelector(".wg-mobile-topbar");
    if(mobileBar){
      const button=document.createElement("button");button.type="button";button.id="portal-language-toggle-mobile";button.className="portal-mobile-language";
      button.addEventListener("click",()=>{language=language === "fa" ? "en" : "fa";apply();themeCharts();});
      mobileBar.append(button);
    }
    apply();
    themeCharts();
    loadResellerSummary();
    document.getElementById("nx-refresh")?.addEventListener("click", loadResellerSummary);
    const dashboard = document.getElementById("nx-dashboard");
    if(dashboard){
      let queued=false;
      const observer=new MutationObserver(()=>{
        if(queued) return;
        queued=true;requestAnimationFrame(()=>{queued=false;apply();});
      });
      observer.observe(dashboard,{subtree:true,childList:true,characterData:true});
      window.addEventListener("pagehide",()=>observer.disconnect(),{once:true});
    }
    document.querySelectorAll('.peerx-form-modal,#iface-create-modal,.portal-interface-defaults').forEach(modal => {
      let queued = false;
      const observer = new MutationObserver(() => {if (queued) return;queued = true;requestAnimationFrame(() => {queued = false;apply();});});
      observer.observe(modal,{subtree:true,childList:true,characterData:true});
      window.addEventListener('pagehide', () => observer.disconnect(), {once:true});
    });
    window.addEventListener("wg-theme-change",themeCharts);
    window.addEventListener("storage",(event)=>{if(event.key === "wgportal-language"){language=event.newValue === "en" ? "en" : "fa";apply();themeCharts();}});
  }
  // This small local script is the first script in <head>. Translate each parsed
  // chunk before paint, rather than waiting for later deferred dashboard code.
  apply();
  if(document.readyState === "loading") {
    const earlyObserver = new MutationObserver(apply);
    earlyObserver.observe(document.documentElement,{subtree:true,childList:true,characterData:true});
    const ready = () => {
      if(document.readyState === "loading") return;
      earlyObserver.disconnect();
      document.removeEventListener("readystatechange",ready);
      init();
    };
    document.addEventListener("readystatechange",ready);
  } else init();
})();
