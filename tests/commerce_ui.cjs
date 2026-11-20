const fs=require('node:fs'),path=require('node:path'),assert=require('node:assert/strict');
const {chromium}=require(process.env.PLAYWRIGHT_MODULE_PATH);
const[root,shell,rendered]=process.argv.slice(2);
const png=Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+j3ioAAAAASUVORK5CYII=','base64');
const product={id:1,name:'طرح آزمایشی <img src=x onerror=alert(1)>',category:'custom',traffic_gb:30,days:30,devices:1,price_toman:200000,active:true,target:{scope:'local',iface_id:1},pricing:{traffic_options:[30,50],duration_options:[30,60],device_options:[1,2],traffic_prices:{30:200000,50:300000},duration_percent:{60:200},additional_device_toman:50000}};
const fixedProduct={...product,id:2,name:'طرح ثابت آزمایشی',category:'economy',pricing:{}};
const unitRates={per_gb_toman:1000,per_day_toman:2000,per_device_toman:3000,
 traffic_amount_prices:{37:25000},device_amount_prices:{3:0}};
const unitProduct={...product,id:3,name:'طرح با نرخ واحد',pricing:unitRates};
const destination={id:1,card_number:'0000000000000000',cardholder_name:'حساب آزمایشی',bank_name:'بانک آزمایشی',instructions:'رسید را ارسال کنید.',active:true};
const customer={id:1,telegram_id:12345,first_name:'کاربر <img src=x>',username:'fixture',created_at:1700000000,suspended:false,subscription_count:2,config_count:2};
const subscription={id:1,product_name:'اشتراک آزمایشی',status:'active',traffic_gb:30,used_bytes:1073741824,remaining_bytes:29*1073741824,expires_at:1900000000,remaining_seconds:86400*15,devices:1,peers:[{id:7,name:'دستگاه اول'}]};
const customerConfigs=[{...subscription,trial:false,configs:[{id:7,name:'دستگاه پولی'}]},
 {id:2,trial:true,status:'active',product_name:'تست رایگان',configs:[{id:8,name:null}]}];
const csp="default-src 'self'; script-src 'self' 'nonce-test-nonce' https://telegram.org; style-src 'self'; img-src 'self' blob: data:; font-src 'self'; connect-src 'self'; object-src 'none'; base-uri 'none'";
async function liveContracts(){
 const origin=shell,fixture=JSON.parse(fs.readFileSync(rendered,'utf8'));
 const browser=await chromium.launch({headless:true,executablePath:process.env.PORTAL_BROWSER_PATH});
 try{
  const customerContext=await browser.newContext({viewport:{width:390,height:1000}}),ownerContext=await browser.newContext({viewport:{width:390,height:1000}});
  for(const context of [customerContext,ownerContext]){
   await context.route('**/*',route=>{const url=new URL(route.request().url());if(url.origin===origin){if(url.pathname.startsWith('/api/'))return route.fulfill({json:{}});return route.continue();}if(url.hostname==='telegram.org')return route.fulfill({contentType:'text/javascript',body:'window.Telegram={WebApp:{initData:'+JSON.stringify(fixture.init_data)+',ready(){},expand(){},setHeaderColor(){},setBackgroundColor(){},onEvent(){},BackButton:{onClick(){},show(){}}}}'});return route.abort();});
  }
  const page=await customerContext.newPage(),admin=await ownerContext.newPage(),errors=[];
  for(const tab of [page,admin]){tab.setDefaultTimeout(12000);tab.on('pageerror',error=>errors.push(error.message));}
  await page.goto(origin+'/telegram-app');await page.locator('#customer-workspace').waitFor({state:'visible'});await page.waitForFunction(()=>document.querySelector('#customer-workspace').getAttribute('aria-busy')==='false');
  await page.locator('#bottom-nav [data-go=catalog]').click();await page.locator('#product-list').getByRole('button',{name:'خرید و مشاهدهٔ شماره کارت'}).click();await page.locator('#transaction-text').fill('تراکنش آزمایشی قرارداد واقعی');await page.locator('#dialog-content form button[type=submit]').click();await page.waitForFunction(()=>document.querySelector('#dialog-content').textContent.includes('صف بررسی'));await page.locator('#close-dialog').click();
  await admin.goto(origin+'/telegram-commerce#payments');await admin.locator('#commerce-panel button').filter({hasText:'بررسی رسید'}).click();await admin.locator('#commerce-field-review-confirm').check();await admin.locator('#commerce-dialog form button[type=submit]').click();await admin.waitForFunction(()=>!document.querySelector('#commerce-dialog').open);
  await page.locator('#refresh-app').click();await page.waitForFunction(()=>document.querySelector('#customer-workspace').getAttribute('aria-busy')==='false');await page.locator('#bottom-nav [data-go=subscriptions]').click();await page.locator('#subscription-list button').filter({hasText:'کانفیگ و QR'}).click();await page.locator('#dialog-content button').filter({hasText:'مشاهده و کپی کانفیگ'}).click();await page.locator('.config-text').waitFor();assert((await page.locator('.config-text').textContent()).includes('[Interface]'));await page.locator('#dialog-content button').filter({hasText:'بازگشت'}).click();await page.locator('#dialog-content button').filter({hasText:'نمایش QR'}).click();await page.locator('.qr-image').waitFor();await page.waitForFunction(()=>document.querySelector('.qr-image')?.naturalWidth>0);await page.locator('#close-dialog').click();
  await admin.locator('#commerce-tab-customers').click();
  await admin.locator('#commerce-panel').getByRole('button',{name:'جزئیات'}).click();
  await admin.getByRole('heading',{name:'کانفیگ‌های پولی'}).waitFor();
  await admin.getByRole('button',{name:'نمایش QR'}).click();
  await admin.waitForFunction(()=>document.querySelector('.customer-config-preview img')?.naturalWidth>0);
  await admin.locator('#commerce-dialog-close').click();
  await page.reload();await page.locator('#customer-workspace').waitFor({state:'visible'});await page.waitForFunction(()=>document.querySelector('#customer-workspace').getAttribute('aria-busy')==='false');assert.equal(await page.locator('#auth-panel').isVisible(),false,'Existing real session survives reload without initData replay');
  assert.deepEqual(errors,[]);assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1));
  console.log(JSON.stringify({mode:'actual Flask/Auth/Service/Store with mock Native',status:'passed',flow:'login → direct card order → receipt → owner approval → native fulfillment → config/QR → reload'}));
 }finally{await browser.close();}
}
async function attach(context,{owner=false,authenticated=false,unavailableTargets=false,storedBotTarget={}}={}){
 let authed=authenticated,authCalls=0,receipt=false,approved=false,targetFailures=unavailableTargets?1:0,settingsFailure=null,managerID='321987654';const calls=[],errors=[];
 let order={id:1,status:'pending_payment',payment_id:1,total_toman:200000,created_at:1700000000,payment_destination:destination};
 let payment={id:1,order_id:1,customer_id:1,status:'pending_review',kind:'order',amount_toman:200000,payment_destination:destination,transaction_text:'شماره پیگیری آزمایشی',receipt_reference:'fixture.png',created_at:1700000000};
 const legacyBody='پاسخ آزمایشی\\n\\nVPN <script>alert(1)</script>';
 const customerReceipt={id:1,customer_id:1,order_id:1,status:'pending_review',amount_toman:200000,created_at:1700000000};
 const unlinkedReceipt={id:3,customer_id:2,order_id:null,status:'pending_review',amount_toman:100000,created_at:1700000000};
 let ticket={id:1,customer_id:1,category:'connection',status:'open',created_at:1700000000,receipt_count:1},messages=[{id:1,actor:'admin:owner',body:legacyBody,attachment_reference:'fixture.png',created_at:1700000000}];
 const respond=(route,value,status=200)=>route.fulfill({status,json:{csrf:'commerce-fixture',...value}});
 await context.route('**/*',async route=>{
  const request=route.request(),u=new URL(request.url()),p=u.pathname,method=request.method();
  if(u.hostname==='telegram.org')return route.fulfill({contentType:'text/javascript',body:"window.Telegram={WebApp:{initData:'signed-fixture',ready(){},expand(){},setHeaderColor(){},setBackgroundColor(){},onEvent(){},BackButton:{onClick(){},show(){}}}}"});
  if(p==='/telegram-app'||p==='/telegram-commerce')return route.fulfill({contentType:'text/html',headers:{'Content-Security-Policy':csp},body:fs.readFileSync(path.join(rendered,p==='/telegram-app'?'customer.html':'owner.html'))});
  if(p.startsWith('/static/')){let f;for(const source of[root,shell,process.env.OPERATIONS_SUPPORT_ROOT]){if(source&&fs.existsSync(path.join(source,'.'+p))){f=path.join(source,'.'+p);break;}}if(f)return route.fulfill({contentType:f.endsWith('.js')?'text/javascript':f.endsWith('.css')?'text/css':f.endsWith('.png')?'image/png':'font/woff2',body:fs.readFileSync(f)});return route.fulfill({status:404,body:''});}
  if(!p.startsWith('/telegram-app/api/')&&!p.startsWith('/telegram-commerce/api/'))return respond(route,{});
  calls.push({path:p,method,data:request.headers()['content-type']?.includes('application/json')?request.postDataJSON():request.postData()});
  if(method!=='GET'&&!p.endsWith('/auth')){assert.equal(request.headers()['x-commerce-csrf'],'commerce-fixture');assert(request.headers()['idempotency-key'],'State changing requests have idempotency key');}
  const endpoint=p.replace(/^\/telegram-(app|commerce)\/api/,'');
  if(endpoint==='/session')return authed?respond(route,{customer}):respond(route,{error:'authentication_required'},401);
  if(endpoint==='/auth'){assert.deepEqual(request.postDataJSON(),{init_data:'signed-fixture'});authed=true;authCalls++;return respond(route,{customer});}
  if(endpoint==='/dashboard')return respond(route,owner?{dashboard:{total_customers:1,active_subscriptions:1,trial_subscriptions:0,pending_payments:1,revenue_toman:200000,wallet_liabilities_toman:100000,referral_rewards_toman:0,failed_provisioning:1}}:{wallet_balance_toman:100000,active_subscriptions:1,remaining_bytes:29*1073741824,subscriptions:[subscription]});
  if(endpoint==='/targets'&&targetFailures-->0)return respond(route,{error:'inventory_unavailable',message:'دریافت اینترفیس‌ها ناموفق بود.'},503);
  if(endpoint==='/targets')return respond(route,{targets:[{label:'محلی wg0',target:{scope:'local',iface_id:1}},{label:'فرانسه · wg1',target:{scope:'node',node_id:2,iface:'wg1'}}]});
  if(endpoint==='/products'&&method==='GET')return respond(route,{products:[product,fixedProduct,unitProduct]});
  if(endpoint.match(/^\/products(\/\d+)?$/)&&method!=='GET'){assert(request.postDataJSON().pricing);assert.equal(request.postDataJSON().target.scope,'local');return respond(route,{product});}
  if(endpoint==='/checkout'&&method==='POST'){assert.deepEqual(request.postDataJSON(),{product_id:2});return respond(route,{order,payment:{...payment,status:'pending_payment'}});}
  if(endpoint==='/quotes'||endpoint.endsWith('-quote')){
   const selection=request.postDataJSON();
   const quote={id:'quote-fixture',product_id:1,product_name:product.name,traffic_gb:30,days:30,devices:1,base_toman:200000,discount_toman:0,total_toman:200000};
   if(selection.product_id===unitProduct.id){
    const {traffic_gb,days,devices}=selection;
    const total=(unitRates.traffic_amount_prices[traffic_gb]??traffic_gb*unitRates.per_gb_toman)+
      days*unitRates.per_day_toman+(unitRates.device_amount_prices[devices]??devices*unitRates.per_device_toman);
    return respond(route,{quote:{...quote,...selection,product_name:unitProduct.name,base_toman:total,total_toman:total}});
   }
   return respond(route,{quote});
  }
  if(endpoint==='/orders'&&method==='POST'){assert.deepEqual(request.postDataJSON(),{quote_id:'quote-fixture',payment_method:'card'});return respond(route,{order,payment:{...payment,status:'pending_payment'}});}
  if(endpoint==='/orders')return respond(route,{orders:[order]});
  if(endpoint==='/orders/1')return respond(route,{order:{...order,payment_status:receipt?'pending_review':'pending_payment'},payment:{...payment,status:receipt?'pending_review':'pending_payment'}});
  if(endpoint==='/subscriptions')return respond(route,{subscriptions:[subscription]});
  if(endpoint.endsWith('/config'))return route.fulfill({contentType:'text/plain',body:'[Interface]\nAddress = 10.0.0.2/32\n# test fixture only'});
  if(endpoint.endsWith('/qr')||endpoint.endsWith('/receipt')&&method==='GET'||endpoint.includes('/attachments/'))return route.fulfill({contentType:'image/png',body:png});
  if(endpoint==='/payments/1/receipt'&&method==='POST'||endpoint==='/payments/2/receipt'&&method==='POST'){assert(request.postData().includes('transaction_text'));receipt=true;return respond(route,{payment:{...payment,status:'pending_review'}});}
  if(endpoint==='/wallet')return respond(route,{balance_toman:100000,topup_amounts:[100000,250000],transactions:[{id:1,kind:'topup',amount_toman:100000,reason:'شارژ آزمایشی',created_at:1700000000}]});
  if(endpoint==='/wallet/topups')return respond(route,{payment:{id:2,kind:'topup',status:'pending_payment',amount_toman:100000,payment_destination:destination,created_at:1700000000}});
  if(endpoint==='/payments/2')return respond(route,{payment:{id:2,kind:'topup',status:receipt?'pending_review':'pending_payment',amount_toman:100000,payment_destination:destination}});
  if(endpoint==='/referrals')return respond(route,{referral_count:2,successful_purchases:1,earnings_toman:10000,referral_link:'https://t.me/fixture_bot?start=ref_abc'});
  if(endpoint==='/trial')return respond(route,{subscription:{id:2,status:'pending',product_name:'تست رایگان',peers:[]}});
  if(endpoint==='/tickets'&&method==='GET')return respond(route,{tickets:[ticket],...(owner?{receipts:[customerReceipt,unlinkedReceipt]}:{})});
  if(endpoint==='/tickets'&&method==='POST'){messages.push({id:2,actor:'customer:1',body:request.postDataJSON().body});return respond(route,{ticket});}
  if(endpoint==='/tickets/1')return respond(route,{ticket,messages,...(owner?{receipts:[customerReceipt]}:{})});
  if(endpoint==='/tickets/1/messages'||endpoint==='/tickets/1/reply'){messages.push({id:3,actor:owner?'admin:owner':'customer:1',body:request.postDataJSON().body});return respond(route,{ticket,messages});}
  if(endpoint==='/guides')return respond(route,{guides:[{title:'اندروید',steps:['نصب WireGuard','ورود کانفیگ'],links:[{title:'دانلود رسمی',url:'https://www.wireguard.com/install/'}]}]});
  if(endpoint==='/downloads')return respond(route,{downloads:[{title:'WireGuard',url:'https://www.wireguard.com/install/'}]});
  if(endpoint==='/payment-destinations'&&method==='GET')return respond(route,{destinations:[destination]});
  if(endpoint.startsWith('/payment-destinations'))return respond(route,{destination});
  if(endpoint==='/payments')return respond(route,{payments:[{...payment,status:approved?'approved':'pending_review'}]});
  if(endpoint==='/payments/1')return respond(route,{payment:{...payment,status:approved?'approved':'pending_review'}});
  if(endpoint==='/payments/1/review'){assert.equal(request.postDataJSON().decision,'approved');approved=true;return respond(route,{payment:{...payment,status:'approved'}});}
  if(endpoint==='/customers')return respond(route,{customers:[customer]});
  if(endpoint==='/customers/1')return respond(route,{customer,subscriptions:customerConfigs,transactions:[{amount_toman:100000,reason:'آزمایشی',created_at:1700000000}]});
  if(endpoint==='/customers/1/wallet-adjustment')return respond(route,{balance_toman:100001});
  if(endpoint==='/settings'&&method==='PUT'&&settingsFailure!==null){const status=settingsFailure;settingsFailure=null;return route.fulfill({status,contentType:'text/html',body:'<!doctype html><html>PRIVATE-DIAGNOSTIC-DO-NOT-REFLECT</html>'});}
  if(endpoint==='/settings'&&method==='GET')return respond(route,{settings:{manager_chat_id:managerID,bot_target:storedBotTarget,trial_enabled:false,trial_hours:24,trial_traffic_gb:1,trial_target:{scope:'local',iface_id:1},referral_percent:20,topup_amounts:[100000,250000],bot_enabled:false,public_base_url:'https://fixture',extra_device_price_toman:50000}});
  if(endpoint==='/settings'){if('manager_chat_id' in request.postDataJSON())managerID=request.postDataJSON().manager_chat_id;return respond(route,{settings:request.postDataJSON()});}
  if(endpoint==='/discounts'&&method==='GET')return respond(route,{discounts:[{id:1,code:'TEST',kind:'percent',value:10,active:false,max_uses:5,per_customer:1,first_purchase:false,product_ids:[]}]});
  if(endpoint.startsWith('/discounts'))return respond(route,{discount:{id:1}});
  if(endpoint==='/announcements')return respond(route,{ok:true});
  if(endpoint==='/jobs')return respond(route,{jobs:[{id:1,kind:'provision',status:'uncertain',attempts:1,created_at:1700000000,last_error:'fixture failure'}]});
  if(endpoint==='/jobs/1/reconcile')return respond(route,{ok:true});
  if(endpoint==='/audit')return respond(route,{audit:[{id:1,actor:'owner',action:'fixture',created_at:1700000000}]});
  if(endpoint==='/health')return respond(route,{ok:true,bot:{enabled:false},runtime:{state:'running',heartbeat:1700000000}});
  return respond(route,{error:'not_found'},404);
 });
 const page=await context.newPage();page.setDefaultTimeout(8000);page.on('pageerror',error=>errors.push(error.message));return{page,calls,errors,authCalls:()=>authCalls,failNextSettings:status=>settingsFailure=status};
}
(async()=>{if(root==='--live'){await liveContracts();return;}const browser=await chromium.launch({headless:true,executablePath:process.env.PORTAL_BROWSER_PATH}),results=[];try{for(const width of[320,390,1440]){
 const context=await browser.newContext({viewport:{width,height:1000},reducedMotion:'reduce'}),fixture=await attach(context),{page,calls,errors}=fixture;
 await page.goto('https://fixture/telegram-app');await page.waitForFunction(()=>document.querySelector('#customer-workspace').getAttribute('aria-busy')==='false');assert.equal(fixture.authCalls(),1);assert.equal(await page.locator('#customer-name img').count(),0);assert((await page.locator('#home-balance').textContent()).includes('۱۰۰'));assert.equal(await page.locator('#bottom-nav button').count(),5);assert.equal(await page.locator('html').getAttribute('dir'),'rtl');
 await page.reload();await page.waitForFunction(()=>document.querySelector('#customer-workspace').getAttribute('aria-busy')==='false');assert.equal(fixture.authCalls(),1,'Valid customer session avoids initData replay on reload');
 await page.locator('#bottom-nav [data-go=catalog]').click();
 await page.locator('#product-list .product').filter({hasText:unitProduct.name}).getByRole('button').click();
 assert.equal(await page.locator('#buy-traffic').inputValue(),'');
 await page.locator('#buy-traffic').fill('37');await page.locator('#buy-days').fill('44');await page.locator('#buy-devices').fill('3');
 assert((await page.getByText('برآورد قیمت:').textContent()).includes(new Intl.NumberFormat('fa-IR').format(113000)));
 await page.locator('#buy-traffic').fill('38');
 assert((await page.getByText('برآورد قیمت:').textContent()).includes(new Intl.NumberFormat('fa-IR').format(126000)));
 await page.locator('#buy-traffic').fill('37');
 await page.locator('#dialog-content form button[type=submit]').click();await page.locator('#payment-method').waitFor();
 assert.deepEqual(calls.filter(item=>item.path.endsWith('/quotes')).at(-1).data,{product_id:3,days:44,devices:3,traffic_gb:37});
 assert((await page.locator('.invoice-total strong').textContent()).includes(new Intl.NumberFormat('fa-IR').format(113000)));
 await page.locator('#close-dialog').click();
 await page.locator('#product-list .product').filter({hasText:product.name}).getByRole('button').click();
 assert.equal(await page.locator('#buy-traffic').evaluate(input=>input.tagName),'SELECT','Legacy options stay available');
 await page.locator('#close-dialog').click();
 await page.locator('#product-list').getByRole('button',{name:'خرید و مشاهدهٔ شماره کارت'}).click();await page.locator('.card-number').waitFor();
 assert.deepEqual(calls.filter(item=>item.path.endsWith('/checkout')).at(-1).data,{product_id:2});
 assert.equal(await page.locator('#buy-days').count(),0);
 await page.locator('#transaction-text').fill('شماره پیگیری آزمایشی');await page.locator('#dialog-content form button[type=submit]').click();await page.waitForFunction(()=>document.querySelector('#dialog-content').textContent.includes('صف بررسی'));await page.locator('#close-dialog').click();
 await page.locator('#bottom-nav [data-go=subscriptions]').click();await page.locator('#subscription-list button').filter({hasText:'کانفیگ و QR'}).click();await page.locator('#dialog-content button').filter({hasText:'مشاهده و کپی کانفیگ'}).click();await page.locator('.config-text').waitFor();assert((await page.locator('.config-text').textContent()).includes('[Interface]'));await page.locator('#dialog-content button').filter({hasText:'بازگشت'}).click();await page.locator('#dialog-content button').filter({hasText:'نمایش QR'}).click();await page.locator('.qr-image').waitFor();assert(await page.locator('.qr-image').evaluate(image=>image.complete&&image.naturalWidth>0));await page.locator('#close-dialog').click();
 await page.locator('#bottom-nav [data-go=wallet]').click();assert((await page.locator('#wallet-ledger').textContent()).includes('شارژ آزمایشی'));await page.locator('#topup-amount').fill('100000');await page.locator('#topup-form button[type=submit]').click();await page.locator('#transaction-text').fill('پیگیری شارژ');await page.locator('#dialog-content form button[type=submit]').click();await page.waitForFunction(()=>document.querySelector('#dialog-content').textContent.includes('صف بررسی'));await page.locator('#close-dialog').click();
 await page.locator('#bottom-nav [data-go=profile]').click();await page.locator('#ticket-list button').click();
 const customerMessage=page.locator('#dialog-content .ticket-message-body').first();
 assert.equal(await customerMessage.textContent(),'پاسخ آزمایشی\n\nVPN <script>alert(1)</script>');
 assert.equal(await customerMessage.evaluate(el=>getComputedStyle(el).whiteSpace),'pre-wrap');
 assert.equal(await page.locator('#dialog-content script').count(),0);
 assert((await page.locator('#dialog-content a').first().getAttribute('href')).endsWith('/attachments/1'));
 await page.locator('#ticket-message').fill('پیام آزمایشی');await page.locator('#dialog-content form button[type=submit]').click();await page.locator('#ticket-message').waitFor();await page.locator('#close-dialog').click();
 await page.locator('#new-ticket').click();
 await page.locator('#ticket-subject').fill('موضوع');
 await page.locator('#ticket-message').fill('خط اول\nخط دوم');
 await page.locator('#dialog-content form button[type=submit]').click();
 await page.locator('#dialog-content .ticket-message-body').last().waitFor();
 assert.equal(calls.filter(item=>item.path.endsWith('/tickets')&&item.method==='POST').at(-1).data.body,'موضوع\n\nخط اول\nخط دوم');
 await page.locator('#close-dialog').click();await page.locator('#share-referral').click();assert((await page.locator('#dialog-content input').inputValue()).startsWith('https://t.me/'));await page.locator('#close-dialog').click();
 await page.locator('#bottom-nav [data-go=home]').click();await page.locator('#open-guides').click();assert.equal(await page.locator('#dialog-content ol li').count(),2);await page.locator('#close-dialog').click();await page.locator('#request-trial').click();await page.locator('#dialog-content .empty-state').waitFor();await page.locator('#close-dialog').click();assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1),'Customer viewport fits');assert.deepEqual(errors,[]);results.push({role:'customer',width,status:'passed',requests:calls.length});await context.close();
 const ownerContext=await browser.newContext({viewport:{width,height:1000},reducedMotion:'reduce'}),ownerFixture=await attach(ownerContext,{owner:true}),admin=ownerFixture.page;await admin.goto('https://fixture/telegram-commerce');await admin.waitForFunction(()=>document.querySelector('#commerce-panel').getAttribute('aria-busy')==='false');assert.equal(await admin.locator('#commerce-tabs button').count(),11);assert(await admin.locator('#commerce-panel').textContent());
 for(const theme of ['modern','legacy','modern']){
  const nav=admin.locator('#panel-sidebar nav.'+(theme==='modern'?'modern-only':'legacy-only'));
  assert.equal(await nav.locator('a[href="/telegram-commerce"]').count(),1,'Commerce appears exactly once in '+theme+' navigation');
  if(width<=760&&!await admin.locator('#panel-sidebar').evaluate(el=>el.classList.contains('wg-mobile-open')))await admin.locator('#wg-mobile-menu-btn').click();
  await admin.locator('[data-design-choice="'+theme+'"]').click();
  await admin.waitForFunction(mode=>document.documentElement.dataset.design===mode,theme);
  const link=nav.locator('a[href="/telegram-commerce"]');assert.equal(await link.isVisible(),true);assert.equal(await link.getAttribute('aria-current'),'page');
  await link.click();await admin.waitForFunction(()=>document.querySelector('#commerce-panel').getAttribute('aria-busy')==='false');
  assert.equal(await admin.locator('html').getAttribute('data-design'),theme,'Following commerce link preserves selected theme');
 }
 const tab=async name=>{await admin.locator('#commerce-tab-'+name).click();await admin.waitForFunction(()=>document.querySelector('#commerce-panel').getAttribute('aria-busy')==='false');};
 await tab('products');assert.equal(await admin.locator('#commerce-panel img').count(),0);await admin.locator('#commerce-panel button').filter({hasText:'افزودن طرح'}).click();assert.equal(await admin.locator('#commerce-field-product-target').inputValue(),'{}','New product inherits configured bot interface');
 assert.equal(await admin.locator('#commerce-field-product-per-gb').isVisible(),false);
 await admin.locator('#commerce-dialog-close').click();
 await admin.locator('#commerce-panel tr').filter({hasText:unitProduct.name}).getByRole('button',{name:'ویرایش'}).click();
 assert.equal(await admin.locator('#commerce-field-product-per-gb').inputValue(),'1000');
 assert.equal(await admin.locator('#commerce-field-product-price').isVisible(),false);
 assert.equal(await admin.locator('#commerce-field-amount-price-traffic_amount_prices-0-quantity').inputValue(),'37');
 await admin.getByRole('button',{name:'افزودن قیمت حجم'}).click();
 await admin.locator('#commerce-field-amount-price-traffic_amount_prices-1-quantity').fill('40');
 await admin.locator('#commerce-field-amount-price-traffic_amount_prices-1-price').fill('30000');
 await admin.locator('#commerce-field-product-per-gb').fill('1500');await admin.locator('#commerce-dialog form button[type=submit]').click();await admin.waitForFunction(()=>!document.querySelector('#commerce-dialog').open);
 assert.deepEqual(ownerFixture.calls.filter(item=>item.path.endsWith('/products/3')&&item.method==='PUT').at(-1).data.pricing,{...unitRates,per_gb_toman:1500,traffic_amount_prices:{37:25000,40:30000}});
 await tab('bank');await admin.locator('#commerce-panel button').filter({hasText:'افزودن مقصد'}).click();assert.equal(await admin.locator('#commerce-field-bank-card').inputValue(),'','No owner bank data in frontend');await admin.locator('#commerce-dialog-close').click();
 await tab('payments');await admin.locator('#commerce-panel button').filter({hasText:'بررسی رسید'}).click();await admin.locator('#commerce-field-review-confirm').check();await admin.locator('#commerce-dialog form button[type=submit]').click();await admin.waitForFunction(()=>!document.querySelector('#commerce-dialog').open);
 await tab('customers');
 await admin.locator('#commerce-panel button').filter({hasText:'جزئیات'}).click();
 await admin.getByRole('heading',{name:'کانفیگ‌های تست رایگان'}).waitFor();
 assert(await admin.getByRole('heading',{name:'کانفیگ‌های پولی'}).isVisible());
 assert((await admin.locator('#commerce-dialog-body').textContent()).includes('سابقهٔ کیف پول'));
 await admin.getByRole('row').filter({hasText:'دستگاه پولی'}).getByRole('button',{name:'نمایش QR'}).click();
 await admin.waitForFunction(()=>document.querySelector('.customer-config-preview img')?.naturalWidth>0);
 assert((await admin.locator('.customer-config-preview img').getAttribute('src')).endsWith('/customers/1/subscriptions/1/peers/7/qr'));
 const downloadEvent=admin.waitForEvent('download');
 await admin.getByRole('row').filter({hasText:'تست رایگان'}).getByRole('button',{name:'دانلود کانفیگ'}).click();
 assert.equal((await downloadEvent).suggestedFilename(),'wireguard-8.conf');
 assert(ownerFixture.calls.some(item=>item.path.endsWith('/customers/1/subscriptions/2/peers/8/config')));
 await admin.locator('#commerce-dialog-close').click();
 await tab('trials');await admin.locator('#commerce-panel form button[type=submit]').click();await tab('notifications');await admin.locator('#commerce-field-notification-title').fill('خبر آزمایشی');await admin.locator('#commerce-field-notification-body').fill('<img src=x> پیام آزمایشی');await admin.locator('#commerce-field-notification-confirm').check();await admin.locator('#commerce-panel form button[type=submit]').click();await tab('settings');assert.equal(await admin.locator('#commerce-field-bot_token').inputValue(),'');
 assert.equal(await admin.locator('#commerce-field-manager_chat_id').inputValue(),'321987654');
 const beforeID=ownerFixture.calls.filter(item=>item.path.endsWith('/settings')&&item.method==='PUT').length;
 await admin.locator('#commerce-field-manager_chat_id').fill('');await admin.locator('#commerce-field-bot_enabled').check();
 await admin.locator('#commerce-panel form button[type=submit]').click();
 assert.equal(await admin.locator('#commerce-field-manager_chat_id').evaluate(input=>input.required&&!input.checkValidity()),true,'Enabled public bot needs numeric manager id');
 assert.equal(ownerFixture.calls.filter(item=>item.path.endsWith('/settings')&&item.method==='PUT').length,beforeID);
 await admin.locator('#commerce-field-manager_chat_id').fill('۱۲۳۴۵۶۷۸۹');
 assert.deepEqual(await admin.locator('.commerce-interface-section h3').allTextContents(),['اینترفیس فروش بات','اینترفیس تست رایگان']);
 assert.equal(await admin.locator('textarea[name=trial_target]').count(),0,'Trial interface is a selector, not JSON');
 await admin.locator('#commerce-field-bot_target').selectOption(JSON.stringify({scope:'local',iface_id:1}));
 await admin.locator('#commerce-field-trial_target').selectOption(JSON.stringify({scope:'node',node_id:2,iface:'wg1'}));
 await admin.locator('#commerce-panel form button[type=submit]').click();await admin.waitForFunction(()=>document.querySelector('#commerce-panel').getAttribute('aria-busy')==='false');
 const settingCall=ownerFixture.calls.filter(item=>item.path.endsWith('/settings')&&item.method==='PUT').at(-1);
 assert.deepEqual(settingCall.data.bot_target,{scope:'local',iface_id:1});assert.deepEqual(settingCall.data.trial_target,{scope:'node',node_id:2,iface:'wg1'});
 assert.equal(settingCall.data.manager_chat_id,'123456789','Persian numeric id normalized before submission');
 assert.equal(await admin.locator('#commerce-field-manager_chat_id').inputValue(),'123456789','Saved numeric id reloads without exposing token');
 assert.equal(await admin.locator('#commerce-field-bot_token').inputValue(),'');
 const validWrites=ownerFixture.calls.filter(item=>item.path.endsWith('/settings')&&item.method==='PUT').length;
 for(const invalid of ['-12','12.5','4503599627370496','10000000000000000']){
  await admin.locator('#commerce-field-manager_chat_id').fill(invalid);await admin.locator('#commerce-panel form button[type=submit]').click();
  assert((await admin.locator('#commerce-status').textContent()).includes('آیدی عددی مدیر بات'));
  assert.equal(ownerFixture.calls.filter(item=>item.path.endsWith('/settings')&&item.method==='PUT').length,validWrites,'Invalid manager id does not reach API');
 }
 await admin.locator('#commerce-field-manager_chat_id').fill('123456789');
 for(const code of [400,500,502,200]){
  ownerFixture.failNextSettings(code);const reads=ownerFixture.calls.filter(item=>item.path.endsWith('/settings')&&item.method==='GET').length;
  await Promise.all([admin.waitForResponse(response=>response.url().endsWith('/telegram-commerce/api/settings')&&response.request().method()==='PUT'),admin.locator('#commerce-panel form button[type=submit]').click()]);await admin.waitForFunction(()=>!document.querySelector('#commerce-panel form button[type=submit]').disabled&&document.querySelector('#commerce-status').classList.contains('error'));
  const error=await admin.locator('#commerce-status').textContent();assert(/[\u0600-\u06ff]/.test(error),'Friendly Persian response error');assert(!/Unexpected token|doctype|PRIVATE-DIAGNOSTIC/.test(error),'No parser error or server body disclosed');
  assert.equal(ownerFixture.calls.filter(item=>item.path.endsWith('/settings')&&item.method==='GET').length,reads,'Invalid response never reports save success or reloads settings');
  assert.equal(await admin.locator('#commerce-field-manager_chat_id').inputValue(),'123456789');
 }

 if(width===390){
  const missing={scope:'local',iface_id:77},unavailableContext=await browser.newContext({viewport:{width,height:1000}}),unavailable=await attach(unavailableContext,{owner:true,unavailableTargets:true,storedBotTarget:missing}),screen=unavailable.page;
  await screen.goto('https://fixture/telegram-commerce#settings');await screen.waitForFunction(()=>document.querySelector('#commerce-panel').getAttribute('aria-busy')==='false');
  assert((await screen.locator('#commerce-panel').textContent()).includes('انتخاب ذخیره‌شده حفظ شده است'));
  assert.equal(await screen.locator('#commerce-field-bot_target').inputValue(),JSON.stringify(missing),'Failed inventory never clears stored interface');
  await screen.locator('#commerce-panel form button[type=submit]').click();await screen.waitForFunction(()=>document.querySelector('#commerce-panel').getAttribute('aria-busy')==='false');
  assert.deepEqual(unavailable.calls.filter(item=>item.path.endsWith('/settings')&&item.method==='PUT').at(-1).data.bot_target,missing);
  await screen.locator('#commerce-panel button').filter({hasText:'دریافت دوبارهٔ اینترفیس‌ها'}).click();
  assert.equal(await screen.locator('#commerce-field-bot_target option').count(),4);assert.equal(await screen.locator('#commerce-field-bot_target').inputValue(),JSON.stringify(missing),'Refresh preserves unavailable stored selection');
  assert.deepEqual(unavailable.errors,[]);await unavailableContext.close();
 }

 await tab('discounts');await admin.locator('#commerce-panel button').filter({hasText:'ویرایش'}).click();await admin.locator('#commerce-dialog form button[type=submit]').click();await admin.waitForFunction(()=>!document.querySelector('#commerce-dialog').open);
 await tab('support');
 assert.equal(await admin.locator('#commerce-panel th').filter({hasText:'رسیدهای پرداخت'}).count(),1);
 assert.equal(await admin.locator('#commerce-panel a[href$="/payments/3/receipt"]').count(),1,'Payment receipt without ticket is still listed');
 await admin.getByRole('button',{name:'۱ رسید',exact:true}).click();
 const supportBody=admin.locator('#commerce-dialog-body');
 await supportBody.locator('a[href$="/payments/1/receipt"]').waitFor();
 assert.equal(await supportBody.locator('a[href$="/payments/3/receipt"]').count(),0,'Ticket shows only its own customer receipts');
 assert.equal(await supportBody.locator('a[href$="/attachments/1"]').count(),1);
 assert.equal(await supportBody.locator('.ticket-message-body').textContent(),'پاسخ آزمایشی\n\nVPN <script>alert(1)</script>');
 assert.equal(await supportBody.locator('.ticket-message-body').evaluate(el=>getComputedStyle(el).whiteSpace),'pre-wrap');
 assert.equal(await supportBody.locator('script').count(),0);
 await admin.locator('#commerce-field-support-reply').fill('پاسخ آزمایشی');await admin.locator('#commerce-dialog form button[type=submit]').click();await admin.waitForFunction(()=>!document.querySelector('#commerce-dialog').open);
 await tab('jobs');await admin.locator('#commerce-panel button').filter({hasText:'بررسی و بازیابی'}).click();await admin.locator('#commerce-field-reconcile-note').fill('بررسی آزمایشی');await admin.locator('#commerce-dialog form button[type=submit]').click();await admin.waitForFunction(()=>!document.querySelector('#commerce-dialog').open);assert(await admin.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1),'Owner viewport fits');assert.deepEqual(ownerFixture.errors,[]);results.push({role:'owner',width,status:'passed',requests:ownerFixture.calls.length});await ownerContext.close();
}}finally{await browser.close();}console.log(JSON.stringify(results,null,2));})().catch(error=>{console.error(error);process.exitCode=1;});
