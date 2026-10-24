/* Translate the shared shell without replacing native elements or event bindings. */
(() => {
  "use strict";
  const dictionary = new Map(Object.entries({
    "Dashboard":"داشبورد","Overview":"نمای کلی","Peers":"Peerها","Subscriptions":"اشتراک‌ها","Nodes":"سرورها","Panel Center":"مرکز عملیات","Activity logs":"گزارش فعالیت","Backup":"پشتیبان‌گیری","Settings":"تنظیمات","API Docs":"مستندات API","Main workspace":"فضای اصلی","Manage nodes":"مدیریت سرورها","Network overview":"نمای کلی شبکه","Peer workspace":"مدیریت Peerها","Create peer":"ساخت Peer","Refresh":"تازه‌سازی","Connected peers":"Peerهای متصل","Active subscriptions":"اشتراک‌های فعال","Telegram hub":"ربات تلگرام","Telegram":"تلگرام","Sign in":"ورود","Sign In":"ورود","Log in":"ورود","Login":"ورود","Register":"ثبت حساب","Create account":"ساخت حساب","Username":"نام کاربری","Email":"ایمیل","Password":"رمز عبور","Confirm password":"تکرار رمز عبور","Remember me":"مرا به خاطر بسپار","Sign out":"خروج","Password & security":"رمز و امنیت","Save profile":"ذخیرهٔ پروفایل","Navigation":"دسترسی‌ها","Management":"مدیریت","Secure management":"مدیریت اتصال و سرویس‌ها","Resellers":"نمایندگی‌ها"
  }));
  dictionary.set("Welcome back","خوش آمدید");
  dictionary.set("Sign in to your WG Panel","وارد پنل خود شوید");
  dictionary.set("Create Admin Account","ساخت حساب مدیر");
  dictionary.set("Setup token","کد نصب");
  dictionary.set("Set up your administrator credentials.","نام کاربری و رمز مدیر اصلی را تنظیم کنید.");
  dictionary.set("A clear view of your network and connected clients.","وضعیت شبکه و مشتریان متصل را یک‌جا ببینید.");
  const originals = new WeakMap();
  let language = "fa";
  try { if(localStorage.getItem("wgportal-language") === "en") language = "en"; } catch { /* Storage is optional. */ }
  const selector = ".sb2-text,.sb2-section,.nx-workspace-picker a,.nx-workspace-picker summary span,.nx-mobile-nav a,.nx-page-head h1,.nx-page-head p,.nx-page-actions a,.nx-page-actions button,.nx-metric div>span,.ws-shortcuts a,.account-signout,.auth-header .muted,.auth-card .hint,label,button[type=submit],h1,h2";
  function apply() {
    document.documentElement.lang = language;
    document.documentElement.dir = language === "fa" ? "rtl" : "ltr";
    document.querySelectorAll(selector).forEach((element) => {
      element.childNodes.forEach((node) => {
        if(node.nodeType !== Node.TEXT_NODE) return;
        const original = originals.get(node) ?? node.textContent;
        const key = original.trim();
        if(!dictionary.has(key)) return;
        originals.set(node, original);
        node.textContent = language === "fa" ? original.replace(key,dictionary.get(key)) : original;
      });
    });
    const toggle = document.getElementById("portal-language-toggle");
    if(toggle){toggle.textContent = language === "fa" ? "EN" : "فارسی";toggle.setAttribute("aria-label",language === "fa" ? "Switch to English" : "تغییر زبان به فارسی");}
    const management = document.getElementById("portal-management-link");
    if(management) management.textContent = language === "fa" ? "مدیریت اتصال و سرویس‌ها" : "Secure management";
    try {localStorage.setItem("wgportal-language",language);} catch { /* This page still uses the selected language. */ }
  }
  function init() {
    if(document.getElementById("portal-language-toggle")) return;
    document.querySelectorAll("nav .sb2-link[data-settings-nav]").forEach((settings) => {
      if(settings.parentElement.querySelector('a[href="/resellers"]')) return;
      const link = document.createElement("a"); link.className = "sb2-link"; link.href = "/resellers";
      const icon = document.createElement("i"); icon.className = "fas fa-user-group"; icon.setAttribute("aria-hidden","true");
      const label = document.createElement("span"); label.className="sb2-text"; label.textContent="Resellers";
      link.append(icon,label); settings.before(link);
    });
    const container = document.querySelector(".sb2-bottom") || document.querySelector(".auth-form") || document.querySelector("form");
    if(container){
      const tools = document.createElement("div"); tools.className = "portal-shell-tools";
      const button = document.createElement("button"); button.type = "button"; button.id = "portal-language-toggle";
      button.addEventListener("click",()=>{language = language === "fa" ? "en" : "fa";apply();});tools.append(button);
      if(document.querySelector(".sidebar")){
        const link = document.createElement("a");link.id="portal-management-link";link.href="/manage";tools.append(link);
      }
      container.append(tools);
    }
    apply();
  }
  if(document.readyState === "loading") document.addEventListener("DOMContentLoaded",init,{once:true});else init();
})();
