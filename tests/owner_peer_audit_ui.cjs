const fs=require('fs'),path=require('path'),assert=require('assert/strict');
const {chromium}=require(process.env.PLAYWRIGHT_MODULE_PATH);const root=path.resolve(process.argv[2]);
(async()=>{const browser=await chromium.launch({headless:true,executablePath:process.env.PORTAL_BROWSER_PATH});const results=[];
try{for(const lang of ['fa','en'])for(const width of [320,390,1440]){
 const context=await browser.newContext({viewport:{width,height:900}});await context.addInitScript(l=>localStorage.setItem('wgportal-language',l),lang);
 const accounts=[{id:1,username:'Parent',active:true,enabled:true,interfaces:[1],max_peers:500,peer_count:151},{id:2,username:'<img src=x onerror=alert(1)>',parent_id:1,deleted:true,enabled:false,interfaces:[],peer_count:1}];
 const peers=Array.from({length:152},(_,i)=>({id:i+1,name:'Customer '+i,iface:'wg0',address:'10.0.0.2',used_bytes:1048576,ttl_seconds:86400,reseller:{id:i===151?2:1,username:accounts[i===151?1:0].username,parent_id:i===151?1:null,deleted:i===151}}));let fail=false;const requests=[];
 await context.route('**/*',route=>{const u=new URL(route.request().url());requests.push(u.pathname+u.search);
 if(u.pathname==='/resellers')return route.fulfill({contentType:'text/html',body:fs.readFileSync(path.join(root,'deploy/linux/reseller_ui/index.html'),'utf8')});
 if(u.pathname.startsWith('/reseller/assets/')){const f=path.join(root,'deploy/linux/reseller_ui',path.basename(u.pathname));return route.fulfill({contentType:f.endsWith('.css')?'text/css':'text/javascript',body:fs.readFileSync(f)});}
 let value={csrf:'fixture',native_csrf:'native'};
 if(u.pathname.endsWith('/session'))value.account=null;
 if(u.pathname==='/resellers/api/accounts')Object.assign(value,{accounts,interfaces:[{id:1,name:'wg0'}],audit:[],operations:[],summary:{}});
 if(u.pathname==='/resellers/api/peers'){if(fail)return route.fulfill({status:503,json:{error:'peer_audit_unavailable',message:'فهرست در دسترس نیست'}});const id=u.searchParams.get('reseller_id');Object.assign(value,{peers:id?peers.filter(p=>p.reseller.id===Number(id)):peers,counts:accounts.map(a=>({reseller_id:a.id,peer_count:a.peer_count})),total:id?peers.filter(p=>p.reseller.id===Number(id)).length:peers.length});}
 if(u.pathname.endsWith('/interface-defaults'))Object.assign(value,{configured:false,defaults:{}});
 return route.fulfill({json:value});});
 const page=await context.newPage(),errors=[];page.on('pageerror',e=>errors.push(e.message));await page.goto('http://fixture/resellers');
 await page.waitForFunction(()=>document.querySelectorAll('#owner-peer-rows tr').length===152);
 assert.equal(await page.locator('#account-rows .owner-peer-count').first().textContent(),new Intl.NumberFormat(lang==='fa'?'fa-IR':'en-GB').format(151));
 assert.equal(await page.locator('#owner-peer-rows img').count(),0,'Account data is plain text');
 await page.locator('#account-rows tr[data-account-id="2"] .owner-peer-inspect').click();await page.waitForFunction(()=>document.querySelectorAll('#owner-peer-rows tr').length===1);
 assert.equal(await page.locator('#owner-peer-filter').inputValue(),'2');assert((await page.locator('#owner-peer-rows').textContent()).includes('Parent'));
 await page.locator('#owner-peer-filter').selectOption('');await page.waitForFunction(()=>document.querySelectorAll('#owner-peer-rows tr').length===152);
 assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1),'No outer overflow');
 fail=true;await page.locator('#owner-peer-refresh').click();await page.waitForFunction(()=>document.querySelector('#owner-peer-state').textContent.includes('فهرست در دسترس نیست'));
 assert.equal(await page.locator('#owner-peer-rows tr').count(),0,'Stale rows cleared on API failure');assert.deepEqual(errors,[]);
 assert(requests.includes('/resellers/api/peers?reseller_id=2'));results.push({lang,width,status:'passed'});await context.close();
}}finally{await browser.close();}console.log(JSON.stringify(results,null,2));})().catch(e=>{console.error(e);process.exitCode=1});
