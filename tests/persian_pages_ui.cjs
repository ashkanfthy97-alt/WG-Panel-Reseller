// All requests terminate in this fixture. Only actual CSS/fonts and locale JS run.
const fs=require('node:fs');
const path=require('node:path');
const assert=require('node:assert/strict');
const {chromium}=require(process.env.PLAYWRIGHT_MODULE_PATH || 'playwright');
const [root,rendered]=process.argv.slice(2);
(async()=>{
 const browser=await chromium.launch({headless:true,executablePath:process.env.PORTAL_BROWSER_PATH || undefined});
 const results=[],inventory={},fragments={};
 try {
  for(const file of fs.readdirSync(rendered).filter(x=>x.endsWith('.html') && x!=='subscription_public_custom.html')) for(const width of [1440,390]){
   const context=await browser.newContext({viewport:{width,height:1000},serviceWorkers:'block'});
   await context.addInitScript(()=>{try{localStorage.setItem('wgportal-language','fa');}catch{/* Sandboxed native preview iframe. */}});
   await context.route('**/*',route=>{
    const url=new URL(route.request().url());
    if(url.pathname==='/fixture')return route.fulfill({contentType:'text/html',body:fs.readFileSync(path.join(rendered,file),'utf8')});
    if(url.pathname==='/custom')return route.fulfill({contentType:'text/html',body:fs.readFileSync(path.join(rendered,'subscription_public_custom.html'),'utf8')});
    const target=path.resolve(root,'.'+url.pathname);
    if(target.startsWith(path.resolve(root)+path.sep) && url.pathname.startsWith('/static/') && fs.existsSync(target) && (/\.(css|woff2?|ttf)$/.test(target) || /\/(portal-locale|portal-translations-fa)\.js$/.test(url.pathname)))
     return route.fulfill({contentType:target.endsWith('.js')?'text/javascript':target.endsWith('.css')?'text/css':'font/woff2',body:fs.readFileSync(target)});
    return route.fulfill({contentType:'text/javascript',body:''});
   });
   const page=await context.newPage();
   const errors=[];page.on('pageerror',error=>errors.push(error.message));
   page.setDefaultTimeout(5000);
   try {
    await page.goto('http://panel.test/fixture');
    await page.waitForFunction(()=>document.documentElement.lang==='fa' && document.documentElement.dir==='rtl',{},{timeout:5000});
    await page.evaluate(()=>new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve))));
    const native=await page.evaluate(()=>({headings:[...document.querySelectorAll('h1,h2,h3')].map(x=>x.textContent.trim()), controls:[...document.querySelectorAll('button,label,th')].filter(x=>!x.id.startsWith('portal-language-toggle') && /[\u0600-\u06ff]/.test(x.textContent)).length}));
    if(width===1440){
     const remaining=await page.evaluate(()=>[...new Set([...document.querySelectorAll('[data-portal-ui],button,label,h1,h2,h3')].filter(x=>!x.closest('script,style,code,pre,textarea,[data-no-translate],[translate=no],#peer-name,#sub-name,.peer-name,.node-name,.client-name')).map(x=>[...x.childNodes].filter(n=>n.nodeType===Node.TEXT_NODE).map(n=>n.textContent).join(' ').replace(/\s+/g,' ').trim()).filter(x=>/[A-Za-z]{3}/.test(x) && !/[\u0600-\u06ff]/.test(x) && !/^(EN|API|DNS|MTU|CPU|RAM|UTC|TCP|UDP|HTTPS?|SSH|IP|ID|QR|GB|MB|KiB|MiB|GiB|WireGuard|Telegram|WG Portal|IPv[46]|Poppins|JSON|CSS|HTML|TOTP|CIDR|NAT|AllowedIPs|PrivateKey|PublicKey|Endpoint|PersistentKeepalive|PostUp|PostDown|SaveConfig|PresharedKey|ListenPort|Address|Interface)$/i.test(x)))]);
     inventory[file]=remaining;
     fragments[file]=await page.evaluate(()=>[...new Set([...document.querySelectorAll('[data-portal-ui],button,label,h1,h2,h3')].filter(x=>!x.closest('script,style,code,pre,textarea,[data-no-translate],[translate=no],#peer-name,#sub-name,.peer-name,.node-name,.client-name')).flatMap(x=>[...x.childNodes].filter(n=>n.nodeType===Node.TEXT_NODE).map(n=>n.textContent.replace(/\s+/g,' ').trim())).filter(x=>/[A-Za-z]{3}/.test(x) && !/[\u0600-\u06ff]/.test(x)))]);
     console.log('ENGLISH_UI_INVENTORY '+file+' '+JSON.stringify(remaining.slice(0,20)));
    }
    assert(native.controls>0,`${file}: no Persian native controls`);
    assert(native.headings.some(x=>/[\u0600-\u06ff]/.test(x)),`${file}: no Persian native heading: ${native.headings.slice(0,4)}`);
    if(file==='subscription_public.html')assert.equal(await page.locator('#sub-name').textContent(),'Settings','Customer subscription name must remain unchanged');
    await page.evaluate(()=>{
     const box=document.createElement('div');box.id='locale-regression-fixture';
     box.innerHTML='<span role="status">Online</span><label title="Close">Settings</label><input placeholder="Password" value="Settings"><select><option value="Online">Online</option></select><span class="peer-name">Settings</span><span class="node-name">Online</span><code>Settings Online key=abc/+==</code><textarea>PrivateKey = Settings</textarea>';
     document.body.append(box);
    });
    await page.waitForFunction(()=>/[\u0600-\u06ff]/.test(document.querySelector('#locale-regression-fixture [role=status]').textContent));
    const canary=page.locator('#locale-regression-fixture');
    assert.equal(await canary.locator('.peer-name').textContent(),'Settings');
    assert.equal(await canary.locator('.node-name').textContent(),'Online');
    assert.equal(await canary.locator('input').inputValue(),'Settings');
    assert.equal(await canary.locator('option').getAttribute('value'),'Online');
    assert.equal(await canary.locator('code').textContent(),'Settings Online key=abc/+==');
    assert.equal(await canary.locator('textarea').inputValue(),'PrivateKey = Settings');
    assert.notEqual(await canary.locator('input').getAttribute('placeholder'),'Password');
    assert.notEqual(await canary.locator('label').getAttribute('title'),'Close');
    if(file==='users.html'){
     await page.evaluate(()=>{const modal=document.querySelector('#peer-modal');modal.classList.add('open');modal.setAttribute('aria-hidden','false');document.body.classList.add('modal-open');});
     const bounds=await page.locator('#peer-modal .modal-content').boundingBox();
     assert(bounds && bounds.x>=-1 && bounds.x+bounds.width<=width+1,`Modal clipped ${width}: ${JSON.stringify(bounds)}`);
    }
    if(await page.locator('#portal-language-toggle').count()){
     await page.evaluate(()=>document.querySelector('#portal-language-toggle').click());
     await page.waitForFunction(()=>document.documentElement.dir==='ltr');
     assert.equal(await canary.locator('[role=status]').textContent(),'Online');
     assert.equal(await canary.locator('input').getAttribute('placeholder'),'Password');
     assert.equal(await canary.locator('label').getAttribute('title'),'Close');
    }
    if(file==='subscription_public.html'){
     await page.goto('http://panel.test/custom');
     await page.waitForFunction(()=>document.documentElement.dir==='rtl');
     assert.deepEqual((await page.locator('h2').allTextContents()).map(x=>x.trim()),['Settings','Online','Custom customer configs','Customer support name']);
     assert.equal(await page.locator('#sub-name').textContent(),'Settings');
    }
    results.push({page:file,width,status:'passed',persianControls:native.controls});
   } catch(error){results.push({page:file,width,status:'failed',error:String(error.message),pageErrors:errors});}
   finally{await context.close();}
  }
 } finally{await browser.close();}
 if(process.env.PERSIAN_UI_INVENTORY_OUTPUT){
  fs.writeFileSync(process.env.PERSIAN_UI_INVENTORY_OUTPUT,JSON.stringify(inventory,null,2));
  fs.writeFileSync(process.env.PERSIAN_UI_INVENTORY_OUTPUT.replace(/\.json$/,'.fragments.json'),JSON.stringify(fragments,null,2));
 }
 console.log(JSON.stringify(results,null,2));
 assert.equal(results.filter(x=>x.status==='failed').length,0,'See page results above');
})().catch(error=>{console.error(error);process.exitCode=1;});
