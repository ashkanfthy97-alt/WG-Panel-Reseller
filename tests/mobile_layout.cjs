const fs=require('fs'),path=require('path'),assert=require('assert/strict');
const {chromium}=require(process.env.PLAYWRIGHT_MODULE_PATH);
const native=path.resolve(process.argv[2]),stage=path.resolve(process.argv[3] || native);
(async()=>{const browser=await chromium.launch({headless:true,executablePath:process.env.PORTAL_BROWSER_PATH});const results=[];
try{for(const width of [320,360,390,430])for(const language of ['fa','en']){
const context=await browser.newContext({viewport:{width,height:850},reducedMotion:'reduce'});await context.addInitScript(l=>localStorage.setItem('wgportal-language',l),language);
await context.route('**/*',route=>{const u=new URL(route.request().url());
if(u.pathname==='/native')return route.fulfill({contentType:'text/html',body:`<html lang="${language}" dir="${language==='fa'?'rtl':'ltr'}" data-theme="dark" data-design="modern"><head><meta name="viewport" content="width=device-width,initial-scale=1"><style>body{margin:0}.main{min-height:100vh}.sidebar{position:fixed}</style><link rel="stylesheet" href="/static/css/base-responsive.css"><link rel="stylesheet" href="/static/css/portal-theme.css"></head><body><header class="wg-mobile-topbar"><button id="wg-mobile-menu-btn">Menu</button></header><div id="wg-mobile-menu-backdrop" class="wg-mobile-menu-backdrop"></div><aside id="panel-sidebar" class="sidebar sb2"><nav><a class="sb2-link" href="#home">Home</a></nav></aside><main class="main"><h1>Dashboard</h1><input placeholder="name"></main><script src="/static/js/base.js"></script></body></html>`});
if(u.pathname==='/reseller'||u.pathname==='/resellers')return route.fulfill({contentType:'text/html',body:fs.readFileSync(path.join(stage,'deploy/linux/reseller_ui/index.html'))});
if(u.pathname.startsWith('/reseller/assets/')){const f=path.join(stage,'deploy/linux/reseller_ui',path.basename(u.pathname));return route.fulfill({contentType:f.endsWith('.css')?'text/css':'text/javascript',body:fs.readFileSync(f)});}
if(u.pathname.startsWith('/static/')){const staged=path.join(stage,'.'+u.pathname),f=fs.existsSync(staged)?staged:path.join(native,'.'+u.pathname);if(fs.existsSync(f))return route.fulfill({contentType:f.endsWith('.js')?'text/javascript':f.endsWith('.css')?'text/css':'font/woff2',body:fs.readFileSync(f)});}
const owner=route.request().headers().referer?.includes('/resellers');const account={id:1,username:'dealer',active:true,enabled:true,approval_status:'approved',max_peers:100,interfaces:[1],expires_at:4102444800};
let value={csrf:'fixture',native_csrf:'native'};
if(u.pathname.endsWith('/session'))value.account=account;
if(u.pathname.endsWith('/accounts'))Object.assign(value,{accounts:owner?[account]:[],interfaces:[{id:1,name:'wg0'}],audit:[],operations:[],summary:{},creation_fee:{amount:5000000}});
if(u.pathname.endsWith('/peers'))value.peers=[{id:1,name:'long-customer-name-for-mobile-layout',used_bytes:0,ttl_seconds:86400}];
if(u.pathname.endsWith('/bot'))value.bot={configured:false};
if(u.pathname.endsWith('/interface-defaults'))Object.assign(value,{configured:false,defaults:{}});
return route.fulfill({json:value});});
const page=await context.newPage();const errors=[];page.on('pageerror',e=>errors.push(e.message));page.setDefaultTimeout(5000);await page.goto('http://fixture/native');
const closed=()=>page.locator('#panel-sidebar').boundingBox();
let b=await closed();assert(language==='fa'?b.x>=width-1:b.x+b.width<=1,`closed drawer overlaps ${language} ${width}: ${JSON.stringify(b)}`);
await page.locator('#wg-mobile-menu-btn').click();await page.waitForFunction(()=>document.body.classList.contains('wg-mobile-menu-open'));b=await closed();assert(b.x>=-1&&b.x+b.width<=width+1,'open drawer bounded');
await page.keyboard.press('Escape');await page.waitForFunction(()=>!document.body.classList.contains('wg-mobile-menu-open'));
await page.locator('#wg-mobile-menu-btn').click();await page.locator('#panel-sidebar nav a').click();await page.waitForFunction(()=>!document.body.classList.contains('wg-mobile-menu-open'));
await page.locator('#wg-mobile-menu-btn').click();await page.locator('#wg-mobile-menu-backdrop').click({position:{x:language==='fa'?2:width-2,y:200}});await page.waitForFunction(()=>!document.body.classList.contains('wg-mobile-menu-open'));
await page.evaluate(()=>document.body.classList.add('subx-modal-open'));b=await closed();assert(language==='fa'?b.x>=width-1:b.x+b.width<=1,'subscription modal must hide drawer');results.push({width,language,native:'passed'});
for(const role of ['reseller','resellers']){await page.goto('http://fixture/'+role);await page.waitForFunction(()=>document.querySelector('#loading').hidden);assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1),`${role} page overflow ${width}`);
if(role==='reseller'){await page.locator('[data-reseller-section="services"]').click();await page.locator('#peer-name').fill('mobile customer');await page.locator('[data-reseller-section="children"]').click();await page.locator('#add-child').click();const d=await page.locator('#child-dialog').boundingBox();assert(d.x>=-1&&d.x+d.width<=width+1,'child dialog bounds');await page.locator('#close-child-dialog').click();}
results.push({width,language,role,status:'passed'});}
assert.deepEqual(errors,[],'No browser runtime errors');await context.close();}
console.log(JSON.stringify(results,null,2));}finally{await browser.close();}})().catch(e=>{console.error(e);process.exitCode=1});
