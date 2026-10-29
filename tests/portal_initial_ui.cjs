// Network is fully mocked. No server, installer, or production data is touched.
const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');
const {chromium} = require(process.env.PLAYWRIGHT_MODULE_PATH || 'playwright');
const [root, rendered] = process.argv.slice(2);
const account = {id:1, username:'dealer', active:true, enabled:true, expires_at:4102444800, max_peers:10, interfaces:[1], approval_status:'approved'};
(async () => {
  const browser = await chromium.launch({headless:true, executablePath:process.env.PORTAL_BROWSER_PATH || undefined});
  try {
    // Render the operational dashboard script against owner mappings, never inferred from Interface.
    for(const language of ['fa','en']) for(const width of [1440,390]){
      const context=await browser.newContext({viewport:{width,height:1000}});
      await context.addInitScript(value=>localStorage.setItem('wgportal-language',value),language);
      let unavailable=false;
      const peers=[1,2,3,4,5].map(id=>({id,public_key:'key-'+id,name:'connection-'+id,iface:'wg0',latest_handshake:100-id}));
      const owners=[
        {peer_id:1,public_key:'key-1',reseller:{id:10,username:'dealer-alpha',parent_id:null,deleted:false}},
        {peer_id:2,public_key:'key-2',reseller:{id:11,username:'child-beta',parent_id:10,deleted:false}},
        {peer_id:3,public_key:'key-3',reseller:{id:12,username:'<img src=x onerror=alert(1)>',parent_id:null,deleted:true}},
        {peer_id:4,public_key:'OLD-KEY',reseller:{id:13,username:'WRONG-OWNER',deleted:false}}
      ];
      await context.route('**/*',route=>{
        const url=new URL(route.request().url());
        if(url.pathname==='/')return route.fulfill({contentType:'text/html',body:fs.readFileSync(path.join(rendered,'dashboard-modern.html'),'utf8')});
        if(['/static/js/dashboard-modern.js','/static/js/portal-locale.js'].includes(url.pathname))return route.fulfill({contentType:'text/javascript',body:fs.readFileSync(path.join(root,url.pathname.slice(1)),'utf8')});
        if(url.pathname==='/api/peers')return route.fulfill({json:{peers}});
        if(url.pathname==='/resellers/api/peer-owners')return route.fulfill({status:unavailable?503:200,json:unavailable?{error:'owner_mapping_unavailable'}:{owners}});
        if(url.pathname.startsWith('/api/')||url.pathname==='/resellers/api/summary')return route.fulfill({json:{subscriptions:[],nodes:[]}});
        if(url.pathname.endsWith('.css'))return route.fulfill({contentType:'text/css',body:''});
        return route.fulfill({contentType:'text/javascript',body:''});
      });
      const page=await context.newPage();
      await page.goto('http://panel.test/');
      const cells=page.locator('#nx-recent-peers .nx-connection-owner');
      await page.waitForFunction(()=>document.querySelector('#nx-recent-peers')?.textContent.includes('dealer-alpha'));
      assert.equal(await cells.count(),5);
      assert.equal(await cells.nth(0).textContent(),'dealer-alpha');
      assert.equal(await cells.nth(1).textContent(),'child-beta');
      assert((await cells.nth(2).textContent()).includes(language==='fa'?'حساب بسته‌شده':'Closed account'));
      assert.equal(await cells.nth(2).locator('img').count(),0,'Owner names are escaped');
      assert.equal(await cells.nth(3).textContent(),language==='fa'?'بدون نماینده':'No reseller','Reused ID never inherits old owner');
      assert.equal(await cells.nth(4).textContent(),language==='fa'?'بدون نماینده':'No reseller');
      assert.equal(await page.locator('#nx-recent-peers tr').first().locator('td').count(),6);
      unavailable=true;await page.reload();
      await page.waitForFunction(()=>document.querySelectorAll('.nx-connection-owner').length===5);
      assert((await cells.allTextContents()).every(text=>text===(language==='fa'?'نامشخص':'Unknown')),'Failed mapping is unknown, not admin/unowned');
      await context.close();
    }
    for (const language of ['fa', 'en', 'unavailable']) {
      const context = await browser.newContext();
      await context.addInitScript((value) => {
        if (value === 'unavailable') Object.defineProperty(window, 'localStorage', {get(){throw new Error('Storage disabled');}});
        else localStorage.setItem('wgportal-language', value);
        window.portalDomReady = false;
        document.addEventListener('DOMContentLoaded', () => {window.portalDomReady = true;});
      }, language);
      let release;
      const deferred = new Promise(resolve => {release = resolve;});
      await context.route('**/*', async route => {
        const url = new URL(route.request().url());
        if (url.pathname === '/') return route.fulfill({contentType:'text/html',body:fs.readFileSync(path.join(rendered,'dashboard-modern.html'),'utf8')});
        if (url.pathname.endsWith('/modern.js')) {await deferred;return route.fulfill({contentType:'text/javascript',body:''});}
        if (url.pathname.endsWith('/portal-locale.js')) return route.fulfill({contentType:'text/javascript',body:fs.readFileSync(path.join(root,'static/js/portal-locale.js'),'utf8')});
        if (url.pathname === '/resellers/api/summary') return route.fulfill({json:{summary:{total_resellers:4,subresellers:3,total_creation_fee_toman:9000000,pending_approvals:2}}});
        if (url.pathname.endsWith('.js')) return route.fulfill({contentType:'text/javascript',body:''});
        return route.fulfill({body:''});
      });
      const page = await context.newPage();
      try {
        await page.goto('http://panel.test/', {waitUntil:'commit'});
        const expected = language === 'en' ? 'ltr' : 'rtl';
        await page.waitForFunction(value => document.documentElement.dir === value && document.querySelector('#nx-dashboard h1')?.textContent.includes(value === 'rtl' ? 'سلام' : 'Welcome back'), expected, {timeout:5000});
        assert.equal(await page.evaluate(() => window.portalDomReady), false, 'Locale must render before delayed native deferred scripts');
        await page.waitForFunction(() => document.getElementById('portal-total-resellers')?.textContent.match(/[4۴]/));
      } finally {release();await context.close();}
    }
    for (const language of ['fa', 'en']) {
      for (const width of [1920, 390]) {
        const context = await browser.newContext({viewport:{width,height:1000}});
        await context.addInitScript(value => localStorage.setItem('wgportal-language', value), language);
        await context.route('**/*', route => {
          const url = new URL(route.request().url());
          if (url.pathname === '/users') return route.fulfill({contentType:'text/html',body:fs.readFileSync(path.join(rendered,'users.html'),'utf8')});
          if (url.pathname.endsWith('/portal-locale.js')) return route.fulfill({contentType:'text/javascript',body:fs.readFileSync(path.join(root,'static/js/portal-locale.js'),'utf8')});
          if (url.pathname.endsWith('.js')) return route.fulfill({contentType:'text/javascript',body:''});
          if (url.pathname.startsWith('/static/') && url.pathname.endsWith('.css')) {
            const target = path.join(root, url.pathname.slice(1));
            if (fs.existsSync(target)) return route.fulfill({contentType:'text/css',body:fs.readFileSync(target,'utf8')});
          }
          return route.fulfill({body:''});
        });
        const page = await context.newPage();
        await page.goto('http://panel.test/users');
        await page.evaluate(() => {
          const modal = document.getElementById('peer-modal');
          modal.classList.add('open'); modal.setAttribute('aria-hidden','false');
          document.body.classList.add('modal-open');
        });
        for (const collapsed of [false,true]) {
          await page.evaluate(value => document.querySelector('.sidebar.sb2')?.classList.toggle('is-collapsed',value), collapsed);
          const modal = page.locator('#peer-modal .modal-content');
          await modal.waitFor({state:'visible'});
          const bounds = await modal.boundingBox();
          assert(bounds.x >= -1 && bounds.x + bounds.width <= width + 1, `Peer modal clipped for ${language}/${width}/${collapsed}: ${JSON.stringify(bounds)}`);
          const close = await page.locator('#modal-close').boundingBox();
          assert(close.x >= 0 && close.x + close.width <= width, 'Close button must stay visible');
        }
        assert.equal(await page.locator('#peer-modal').evaluate(node => node.parentElement === document.body), true, 'Desktop and mobile modal must be outside transformed/sidebar layout');
        await context.close();
      }
    }
    const context = await browser.newContext();
    let signedIn = false;
    const children = [];
    const peers = [];
    let peerPayload, renamePayload, renewalPayload;
    let childPayload, delayUserLink=false, releaseUserLink, observeUserLink;
    let reservedCapacity=2, httpsBotValidated=false, botSaveError=null;
    let defaultsPayload;
    let interfaceDefaults={configured:true,defaults:{endpoint_host:'vpn.example.test',dns:'1.1.1.1,1.0.0.1',mtu:1420,allowed_ips:'0.0.0.0/0,::/0',persistent_keepalive:10,network_pool:'10.80.0.0/16',subnet_prefix:24,child_max_peers:10,auto_firewall:true},port_range:{min:51825,max:52000}};
    await context.route('**/*', route => {
      const url = new URL(route.request().url());
      if (url.pathname === '/login' && route.request().method() === 'POST') {signedIn=true;return route.fulfill({status:302,headers:{location:'/reseller'}});}
      if (url.pathname === '/login') return route.fulfill({contentType:'text/html',body:fs.readFileSync(path.join(rendered,'login.html'),'utf8')});
      if (url.pathname === '/reseller' || url.pathname === '/resellers') return route.fulfill({headers:{'Referrer-Policy':'no-referrer'},contentType:'text/html',body:fs.readFileSync(path.join(root,'deploy/linux/reseller_ui/index.html'),'utf8')});
      if (url.pathname === '/reseller/api/session') return route.fulfill({json:{csrf:'browser-test-csrf',account:signedIn?account:null}});
      if (url.pathname === '/reseller/api/credit') {const now=Math.floor(Date.now()/1000);return route.fulfill({json:{credit:{server_time:now,expires_at:account.expires_at,effective_expires_at:account.expires_at,remaining_seconds:184320,max_peers:10,used_peers:peers.length,reserved_peers:reservedCapacity,remaining_peers:Math.max(0,10-peers.length-reservedCapacity),active:account.active,approval_status:account.approval_status,enabled:account.enabled}}});}
      if (url.pathname === '/reseller/api/login') {signedIn = true;return route.fulfill({json:{ok:true,csrf:'browser-test-csrf',account}});}
      if (url.pathname === '/reseller/api/bot') {
        if (route.request().method() === 'POST') {
          if(url.protocol==='https:'){assert.equal(route.request().headers()['referer'],'https://panel.test/reseller');assert.equal(route.request().headers()['origin'],'https://panel.test');httpsBotValidated=true;}
          if(botSaveError)return route.fulfill({status:400,json:{error:botSaveError}});
          assert.equal(route.request().headers()['x-reseller-csrf'],'browser-test-csrf');
          assert.equal(route.request().headers()['x-csrftoken'],'browser-native-csrf');assert.equal(route.request().postDataJSON().owner_id,'987654321');return route.fulfill({json:{native_csrf:'browser-native-csrf',bot:{configured:true,enabled:true,username:'dealer_config_bot',owner_id:987654321,status:'starting'}}});}
        return route.fulfill({json:{native_csrf:'browser-native-csrf',bot:{configured:false,enabled:false,status:'not_configured'}}});
      }
      if (url.pathname === '/reseller/api/peers/1/user-link') {
        const deliver=()=>route.fulfill({json:{url:'http://panel.test/u/customer-token'}});
        if(delayUserLink) return new Promise(resolve=>{releaseUserLink=()=>deliver().then(resolve);observeUserLink();});
        return deliver();
      }
      if (url.pathname === '/resellers/api/interface-defaults') {
        if(route.request().method()==='PUT'){defaultsPayload=route.request().postDataJSON();interfaceDefaults={...interfaceDefaults,defaults:defaultsPayload};}
        return route.fulfill({json:interfaceDefaults});
      }
      if (url.pathname === '/resellers/api/accounts/2/approval') {assert.deepEqual(route.request().postDataJSON(),{approved:true,days:30});children[0].approval_status='approved';return route.fulfill({json:{account:children[0]}});}
      if (url.pathname === '/reseller/api/peers') {
        if (route.request().method() === 'POST') {
          peerPayload = route.request().postDataJSON();
          peers.push({id:1,name:peerPayload.name,status:'online',used_bytes:30});
          return route.fulfill({status:201,json:{ok:true,id:1}});
        }
        return route.fulfill({json:{peers,assigned_interface:{id:1,name:'wg0'}}});
      }
      if (url.pathname === '/reseller/api/peers/1/edit') {
        renamePayload=route.request().postDataJSON();peers[0].name=renamePayload.name;
        return route.fulfill({json:{ok:true}});
      }
      if (url.pathname === '/reseller/api/peers/1/renew') {
        renewalPayload=route.request().postDataJSON();
        assert(route.request().headers()['idempotency-key']);
        return route.fulfill({json:{ok:true}});
      }
      if (url.pathname === '/reseller/api/accounts') {
        if (route.request().method() === 'POST') {
          childPayload = route.request().postDataJSON();
          const child = {...childPayload,id:2,parent_id:1,active:false,enabled:true,expires_at:0,creation_fee_toman:3000000,interfaces:[3],assigned_interfaces:[{id:3,name:'wg_r2',listen_port:51825}],interface_provision:{state:'complete',port:51825},approval_status:'pending'};
          children.push(child);
          return route.fulfill({status:201,json:{account:child,interface:{id:3,name:'wg_r2',listen_port:51825},signin_url:'/login'}});
        }
        return route.fulfill({json:{accounts:children,interfaces:[{id:1,name:'wg0'}],creation_fee:{amount:3000000,currency:'toman',one_time:true,payment_gateway:false}}});
      }
      if (url.pathname === '/resellers/api/accounts') return route.fulfill({json:{accounts:[{...account,direct_child_count:1,descendant_count:1},...children],interfaces:[{id:1,name:'wg0'}],audit:[],operations:[],summary:{total_resellers:2,subresellers:1,total_creation_fee_toman:3000000,pending_approvals:1}}});
      const file = url.pathname === '/reseller/assets/reseller.js' ? 'deploy/linux/reseller_ui/reseller.js' : url.pathname.endsWith('/portal-locale.js') ? 'static/js/portal-locale.js' : null;
      if (file) return route.fulfill({contentType:'text/javascript',body:fs.readFileSync(path.join(root,file),'utf8')});
      if (url.pathname === '/reseller/assets/reseller.css') return route.fulfill({contentType:'text/css',body:fs.readFileSync(path.join(root,'deploy/linux/reseller_ui/reseller.css'),'utf8')});
      if (url.pathname.endsWith('.js')) return route.fulfill({contentType:'text/javascript',body:''});
      return route.fulfill({body:''});
    });
    const page = await context.newPage();
    await page.goto('http://panel.test/login');
    assert.equal(await page.locator('#portal-reseller-login').count(),0);
    assert.equal(await page.locator('form').count(),1);
    await page.locator('#username').fill('dealer');
    await page.locator('#password').fill('a-safe-test-password');
    await page.locator('#submit-btn').click();
    await page.locator('#account-name').waitFor({state:'visible'});
    await page.locator('[data-reseller-section="services"]').click();
    assert.equal(await page.locator('#account-name').textContent(), 'dealer');
    assert.equal(await page.locator('#peer-interface').count(),0,'Reseller must not select an interface');
    assert.equal(await page.locator('#peer-form').isVisible(),true);
    assert.equal(await page.locator('#reseller-peers').isVisible(),true,'Create and customers share one services panel');
    await page.locator('#peer-name').fill('limited-customer');
    await page.locator('#peer-form button[type=submit]').click();
    await page.waitForFunction(() => document.getElementById('peer-rows')?.textContent.includes('limited-customer'));
    assert.deepEqual(Object.keys(peerPayload).sort(),['days','name','volume_gb']);
    await page.locator('[data-reseller-section="services"]').click();
    await page.locator('#peer-rows button').filter({hasText:/نام \/ تمدید|Name \/ renew/}).click();
    await page.locator('#peer-edit-name').fill('renamed-customer');
    await page.locator('#peer-edit-form button[type=submit]').click();
    await page.waitForFunction(() => document.getElementById('peer-rows')?.textContent.includes('renamed-customer'));
    assert.deepEqual(renamePayload,{name:'renamed-customer'});
    await page.locator('#peer-renew-days').fill('10');
    await page.locator('#peer-renew-volume').fill('20');
    await page.locator('#peer-renew').click();
    await page.waitForFunction(() => document.getElementById('peer-edit-result')?.textContent.length>0);
    assert.deepEqual(renewalPayload,{days:10,volume_gb:20});
    await page.locator('#peer-edit-close').click();
    await page.locator('.peer-user-link').click();
    await page.locator('#user-link-dialog').waitFor({state:'visible'});
    assert.equal(await page.locator('#user-link-url').inputValue(),'http://panel.test/u/customer-token');
    await page.locator('#user-link-close').click();
    delayUserLink=true;
    const observedUserLink=new Promise(resolve=>{observeUserLink=resolve;});
    await page.locator('.peer-user-link').click();
    await observedUserLink;
    await page.locator('[data-reseller-section="bot"]').click();
    await releaseUserLink();
    await page.waitForFunction(()=>!document.querySelector('.peer-user-link').disabled);
    assert.equal(await page.locator('#user-link-dialog').isVisible(),false,'Old-tab response must not open a dialog');
    delayUserLink=false;
    await page.locator('#bot-owner-id').fill('987654321');
    await page.locator('#bot-token').fill('123456:'+ 'A'.repeat(35));
    await page.locator('#bot-form button[type=submit]').click();
    await page.waitForFunction(() => document.getElementById('bot-token').value==='');
    assert.equal(await page.locator('#bot-disable').isVisible(),true);
    await page.locator('[data-reseller-section="children"]').click();
    await page.locator('#add-child').click();
    assert.match(await page.locator('#child-fee-amount').textContent(), /[3۳]/);
    await page.locator('#child-username').fill('child-dealer');
    await page.locator('#child-password').fill('a-safe-child-password');
    assert.equal(await page.locator('#child-form input').count(),2,'Parent enters only username and password');
    assert.equal(await page.locator('#child-limit').count(),0);
    await page.locator('#child-form button[type=submit]').click();
    await page.waitForFunction(() => document.getElementById('child-rows')?.textContent.includes('child-dealer'));
    assert.deepEqual(Object.keys(childPayload).sort(), ['password','username']);
    assert.match(await page.locator('#signin-url').inputValue(), /\/login$/);
    assert.equal(await page.locator('.child-port-propose,.child-interface-create,.child-port').count(),0,'Parents cannot select technical settings');
    assert.match(await page.locator('#child-rows').textContent(), /51825/);
    await page.goto('http://panel.test/resellers');
    await page.waitForFunction(() => document.getElementById('account-rows')?.textContent.includes('child-dealer'));
    assert.match(await page.locator('#summary-creation-fees').textContent(), /[3۳]/);
    await page.locator('#provision-endpoint-host').waitFor({state:'visible'});
    await page.locator('#provision-endpoint-host').fill('draft.example.test');
    const originalDirection=await page.locator('html').getAttribute('dir');
    await page.locator('#language-toggle').click();
    assert.notEqual(await page.locator('html').getAttribute('dir'),originalDirection);
    assert.equal(await page.locator('#provision-endpoint-host').inputValue(),'draft.example.test','Language preserves owner draft');
    assert.equal(await page.locator('#provision-defaults-form input[name="port"]').count(),0,'Port is always allocated, not configurable');
    await page.locator('#provision-defaults-save').click();
    await page.waitForFunction(()=>document.getElementById('provision-defaults-result').textContent.length>0);
    assert.equal(defaultsPayload.endpoint_host,'draft.example.test');
    assert(!Object.keys(defaultsPayload).some(key=>key.includes('port')),'Owner defaults cannot override allocated ports');
    await page.locator('.account-approval').last().click();
    await page.waitForFunction(() => document.getElementById('account-rows')?.textContent.includes('child-dealer'));
    // Sidebar navigation is real HTML/CSS/JS; every network response stays mocked.
    for (const language of ['fa','en']) for (const width of [1440,390]) {
      await page.setViewportSize({width,height:900});
      await page.evaluate(value => localStorage.setItem('wgportal-language',value),language);
      await page.goto('http://panel.test/reseller#overview');
      await page.reload();
      await page.locator('#account-name').waitFor({state:'visible'});
      await page.waitForFunction(value=>document.documentElement.lang===value,language);
      assert.equal(await page.locator('[data-reseller-panel]:visible').count(),1);
      if(width===1440) {
        const side=await page.locator('.portal-sidebar').boundingBox(), main=await page.locator('main').boundingBox();
        assert(language==='fa'?side.x>main.x:side.x<main.x,'Sidebar follows Persian/English direction');
      }
      assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1),'No page overflow');
      await page.locator('[data-reseller-section="children"]').click();
      await page.locator('#add-child').click();
      const childBounds=await page.locator('#child-dialog').boundingBox();
      assert(childBounds.x>=0 && childBounds.x+childBounds.width<=width+1,'Child dialog fits viewport');
      assert.equal(await page.locator('#child-form input').count(),2);
      const childSubmit=await page.locator('#child-form button[type=submit]').boundingBox();
      assert(childSubmit.height<90,'Child submit button keeps normal form height');
      for(const selector of ['#child-username','#child-password']) {
        const input=await page.locator(selector).boundingBox();
        assert(input.width>=150 && input.x>=childBounds.x && input.x+input.width<=childBounds.x+childBounds.width,'Child fields remain readable inside dialog');
      }
      await page.locator('#close-child-dialog').click();
      for(const section of ['services','children','bot','overview']) {
        const link=page.locator(`[data-reseller-section="${section}"]`);
        await link.click();
        await page.waitForFunction(value=>location.hash==='#'+value,section);
        assert.equal(await link.getAttribute('aria-current'),'page');
        assert.equal(await page.locator('[data-reseller-panel]:visible').count(),1,section);
      }
      if(language==='fa' && process.env.PORTAL_SCREENSHOTS) {
        await page.locator('[data-reseller-section="services"]').click();
        await page.screenshot({path:path.join(process.env.PORTAL_SCREENSHOTS,`sidebar-${width}.png`),fullPage:true});
      }
      await page.locator('[data-reseller-section="services"]').focus();
      await page.keyboard.press('Enter');
      await page.locator('#peer-name').fill('unsaved-customer');
      await page.locator('[data-reseller-section="bot"]').click();
      await page.locator('#bot-token').fill('temporary-unsaved-token');
      await page.locator('[data-reseller-section="services"]').click();
      assert.equal(await page.locator('#bot-token').inputValue(),'','Leaving bot clears its token');
      await page.goBack();
      await page.locator('#bot-form').waitFor({state:'visible'});
      await page.goBack();
      await page.locator('#peer-name').waitFor({state:'visible'});
      assert.equal(await page.locator('#peer-name').inputValue(),'unsaved-customer');
      await page.goForward();
      await page.locator('#bot-form').waitFor({state:'visible'});
      await page.reload();
      await page.locator('#bot-form').waitFor({state:'visible'});
      await page.locator('#reseller-refresh').click();
      await page.locator('#credit-values').waitFor({state:'visible'});
      assert.match(await page.locator('#credit-remaining-peers').textContent(),/[7۷]/);
      assert.match(await page.locator('#credit-reserved-peers').textContent(),/[2۲]/);
      assert.match(await page.locator('#credit-remaining-time').textContent(),/[2۲].*(روز|days)/);
      await page.locator('#credit-close').click();
      assert.equal(await page.locator('[data-reseller-section="bot"]').getAttribute('aria-current'),'page');
      await page.locator('#language-toggle').click();
      assert.equal(await page.locator('#bot-form').isVisible(),true,'Language switch preserves section');
      await page.goto('http://panel.test/reseller#children-workspace');
      await page.locator('#add-child').waitFor({state:'visible'});
      await page.goto('http://panel.test/reseller#unknown-section');
      await page.locator('[data-reseller-section="overview"][aria-current="page"]').waitFor();
    }
    // HTTPS settings must keep strict native CSRF despite the document no-referrer policy.
    await page.goto('https://panel.test/reseller#bot');
    await page.locator('#bot-form').waitFor({state:'visible'});
    await page.locator('#bot-owner-id').fill('987654321');
    await page.locator('#bot-token').fill('123456:'+'A'.repeat(35));
    await page.locator('#bot-form button[type=submit]').click();
    await page.waitForFunction(()=>document.getElementById('bot-token').value==='');
    assert.equal(httpsBotValidated,true,'HTTPS requests must carry same-origin Referer');
    botSaveError='telegram_invalid_token';
    await page.locator('#bot-token').fill('123456:'+'A'.repeat(35));
    await page.locator('#bot-form button[type=submit]').click();
    await page.waitForFunction(()=>document.getElementById('bot-result').textContent.includes('توکن'));
    assert(!(await page.locator('#bot-result').textContent()).includes('عملیات انجام نشد. اطلاعات'));
    botSaveError=null;
    await page.locator('#reseller-refresh').click();
    await page.locator('#credit-values').waitFor({state:'visible'});
    reservedCapacity=1;
    await page.locator('#credit-refresh').click();
    await page.waitForFunction(()=>/[8۸]/.test(document.getElementById('credit-remaining-peers').textContent));
    await page.locator('#credit-close').click();
    reservedCapacity=2;
    for(const alias of ['create-peer','peers']) {
      await page.goto('https://panel.test/reseller#'+alias);
      await page.locator('[data-reseller-section="services"][aria-current="page"]').waitFor();
      assert.equal(await page.locator('#peer-form').isVisible(),true);
      assert.equal(await page.locator('#reseller-peers').isVisible(),true);
    }
    // An arbitrary hash must not expose operational panels while access is paused.
    for (const approval of ['pending','approved']) {
      account.active=false; account.approval_status=approval;
      await page.goto('http://panel.test/reseller#bot');
      await page.reload();
      await page.locator(approval==='pending'?'#payment-pending-panel':'#paused-view').waitFor({state:'visible'});
      assert.equal(await page.locator('#bot-form').isVisible(),false);
      assert.equal(await page.locator('[data-reseller-section="bot"]').isVisible(),false);
    }
    account.active=true; account.approval_status='approved';
    await context.close();
    // New operations stay completely mocked, including rotating CSRF and uncertain outcomes.
    for (const language of ['fa','en']) for (const width of [1440,390]) {
      const operationsContext=await browser.newContext({viewport:{width,height:1000}});
      await operationsContext.addInitScript(value=>localStorage.setItem('wgportal-language',value),language);
      let token='operations-csrf-0', serial=0, bulkStatus=201, bulkCalls=[], deleteCalls=[], approvalCalls=[], ownerCreates=[], ownerCreateUncertain=false;
      const operationPeers=[{id:51,name:'delete-success'},{id:52,name:'delete-uncertain'}];
      const operationAccounts=[{...account,id:11,username:'approval-one',approval_status:'pending'}, {...account,id:12,username:'approval-two',approval_status:'pending'}, {...account,id:13,username:'archive-child',parent_id:1,approval_status:'approved'}];
      await operationsContext.route('**/*',route=>{
        const request=route.request(), url=new URL(request.url()), method=request.method();
        const json=(value,status=200)=>route.fulfill({status,json:{...value,csrf:token}});
        if(url.pathname==='/reseller'||url.pathname==='/resellers')return route.fulfill({contentType:'text/html',body:fs.readFileSync(path.join(root,'deploy/linux/reseller_ui/index.html'),'utf8')});
        if(url.pathname==='/reseller/assets/reseller.js')return route.fulfill({contentType:'text/javascript',body:fs.readFileSync(path.join(root,'deploy/linux/reseller_ui/reseller.js'),'utf8')});
        if(url.pathname==='/reseller/assets/reseller.css')return route.fulfill({contentType:'text/css',body:fs.readFileSync(path.join(root,'deploy/linux/reseller_ui/reseller.css'),'utf8')});
        if(method!=='GET')assert.equal(request.headers()['x-reseller-csrf'],token,'Every mutation uses the latest response CSRF');
        if(url.pathname==='/reseller/api/session')return json({account});
        if(url.pathname==='/resellers/api/interface-defaults')return json(interfaceDefaults);
        if(url.pathname==='/resellers/api/accounts'){
          if(method==='POST'){
            const payload=request.postDataJSON();ownerCreates.push(payload);
            assert.deepEqual(Object.keys(payload).sort(),['max_peers','password','username']);
            const created={...account,...payload,id:ownerCreateUncertain?15:14,interfaces:[5],assigned_interfaces:[{id:5,name:'wg_r14',listen_port:51825}],approval_status:'pending',active:false,interface_provision:{state:'complete',port:51825}};
            if(ownerCreateUncertain){created.interfaces=[];created.assigned_interfaces=[];created.interface_provision={state:'uncertain',port:51826};}
            operationAccounts.push(created);token='operations-csrf-'+(++serial);
            if(ownerCreateUncertain)return json({error:'interface_create_uncertain',account:created},502);
            return json({account:created,interface:created.assigned_interfaces[0],signin_url:'/login'},201);
          }
          return json({accounts:operationAccounts,interfaces:[{id:1,name:'wg0'}],audit:[],operations:[]});
        }
        if(url.pathname==='/reseller/api/accounts'&&method==='GET')return json({accounts:operationAccounts.filter(value=>value.parent_id===1),creation_fee:{amount:3000000}});
        if(/\/accounts\/\d+\/approval$/.test(url.pathname)) {
          const id=Number(url.pathname.split('/').at(-2));approvalCalls.push(id);
          assert.deepEqual(request.postDataJSON(),{approved:true,days:30});
          operationAccounts.find(value=>value.id===id).approval_status='approved';token='operations-csrf-'+(++serial);
          return json({ok:true});
        }
        if(url.pathname==='/reseller/api/peers/bulk') {
          const payload=request.postDataJSON(), key=request.headers()['idempotency-key'];
          assert(key);bulkCalls.push({payload,key});
          const created=[{id:71,name:payload.name_prefix+'-1'}];
          if(bulkStatus===201)created.push({id:72,name:payload.name_prefix+'-2'});
          for(const peer of created)if(!operationPeers.some(value=>value.id===peer.id))operationPeers.push(peer);
          token='operations-csrf-'+(++serial);
          return json({ok:bulkStatus===201,requested:2,created,failed:bulkStatus===201?[]:[{index:2,error:bulkStatus===409?'create_uncertain':'capacity_exhausted'}],uncertain:bulkStatus===409,complete:bulkStatus!==409},bulkStatus);
        }
        if(url.pathname==='/reseller/api/peers')return json({peers:operationPeers});
        if(method==='DELETE') {
          assert.deepEqual(request.postDataJSON(),{confirm:true});deleteCalls.push(url.pathname);
          const id=Number(url.pathname.split('/').at(-1));token='operations-csrf-'+(++serial);
          if(id===52)return json({error:'delete_uncertain',message:'native deletion outcome unknown'},502);
          if(url.pathname.includes('/peers/'))operationPeers.splice(operationPeers.findIndex(value=>value.id===id),1);
          else operationAccounts.find(value=>value.id===id).deleted=true;
          return json({ok:true,resources_preserved:!url.pathname.includes('/peers/')});
        }
        if(url.pathname==='/reseller/api/bot')return json({native_csrf:'mock-native',bot:{configured:false,enabled:false,status:'not_configured'}});
        return route.fulfill({body:''});
      });
      const operationsPage=await operationsContext.newPage();
      const pageErrors=[];operationsPage.on('pageerror',error=>pageErrors.push(error.message));
      await operationsPage.goto('http://panel.test/resellers');
      await operationsPage.locator('#add-reseller').click();
      assert.equal(await operationsPage.locator('#account-form input[type=radio],#account-form input[type=checkbox],#account-form select,#interface-options').count(),0,'Owner creation has no manual Interface selector');
      await operationsPage.locator('#account-username').fill('automatic-owner-dealer');
      await operationsPage.locator('#account-password').fill('safe-owner-password');
      await operationsPage.locator('#account-limit').fill('50');
      const ownerBounds=await operationsPage.locator('#account-dialog').boundingBox();
      assert(ownerBounds.x>=0&&ownerBounds.x+ownerBounds.width<=width+1);
      await operationsPage.locator('#account-form button[type=submit]').click();
      await operationsPage.waitForFunction(()=>!document.querySelector('#account-dialog').open&&document.querySelector('#account-rows tr[data-account-id="14"]'));
      assert.equal(ownerCreates.length,1);
      assert.deepEqual(ownerCreates[0],{username:'automatic-owner-dealer',password:'safe-owner-password',max_peers:50});
      assert((await operationsPage.locator('#account-rows tr[data-account-id="14"]').textContent()).includes('wg_r14'));
      for(const id of [11,12]) {
        const row=operationsPage.locator(`#account-rows tr[data-account-id="${id}"]`);
        await row.locator('.account-approval').click();
        await operationsPage.waitForFunction(value=>document.querySelector(`#account-rows tr[data-account-id="${value}"] .account-approval`)?.textContent.match(/لغو|Revoke/),id);
      }
      assert.deepEqual(approvalCalls,[11,12],'Sequential approval targets each selected row');
      await operationsPage.locator('#account-rows tr[data-account-id="13"] .danger').click();
      assert.match(await operationsPage.locator('#delete-copy').textContent(),/خودکار حذف نمی‌شوند|not automatically deleted/);
      const deletionBounds=await operationsPage.locator('#delete-dialog').boundingBox();
      assert(deletionBounds.x>=0&&deletionBounds.x+deletionBounds.width<=width+1,'Delete dialog fits both directions and mobile');
      await operationsPage.locator('#delete-cancel').click();assert.equal(deleteCalls.length,0,'Cancel must not mutate');
      await operationsPage.locator('#account-rows tr[data-account-id="13"] .danger').click();
      await operationsPage.locator('#delete-confirm').click();
      await operationsPage.waitForFunction(()=>!document.querySelector('#delete-dialog').open);
      await operationsPage.waitForFunction(()=>document.querySelector('#account-rows tr[data-account-id="13"]')&&!document.querySelector('#account-rows tr[data-account-id="13"] button'));
      assert.equal(await operationsPage.locator('#account-rows tr[data-account-id="13"] button').count(),0,'Archived row is readonly');
      assert.equal(deleteCalls[0],'/resellers/api/accounts/13');
      ownerCreateUncertain=true;
      await operationsPage.locator('#add-reseller').click();
      await operationsPage.locator('#account-username').fill('uncertain-owner');
      await operationsPage.locator('#account-password').fill('safe-owner-password');
      await operationsPage.locator('#account-form button[type=submit]').click();
      await operationsPage.waitForFunction(()=>document.querySelector('#account-form').dataset.locked&&document.querySelector('#account-form button[type=submit]').disabled);
      assert.equal(await operationsPage.locator('#account-password').inputValue(),'');
      await operationsPage.locator('#account-rows tr[data-account-id="15"]').waitFor({state:'attached'});
      assert.equal(await operationsPage.locator('#account-rows tr[data-account-id="15"]').count(),1);
      await operationsPage.locator('#close-account-dialog').click();
      await operationsPage.locator('#add-reseller').click();
      assert.equal(await operationsPage.locator('#account-form button[type=submit]').isDisabled(),true,'Uncertain creation remains locked after reopening');
      assert.equal(ownerCreates.length,2,'No repeat provisioning request');
      await operationsPage.locator('#close-account-dialog').click();
      for(const status of [201,207,409]) {
        bulkStatus=status;bulkCalls=[];
        await operationsPage.goto('http://panel.test/reseller#services');
        await operationsPage.reload();
        await operationsPage.locator('#bulk-name-prefix').fill('batch-'+status);
        await operationsPage.locator('#bulk-count').fill('2');
        const submit=operationsPage.locator('#bulk-peer-form button[type=submit]');
        await submit.click();await operationsPage.waitForFunction(()=>document.querySelector('#bulk-outcomes li')&&!document.querySelector('#bulk-peer-form button[type=submit]').disabled);
        assert.equal(await operationsPage.locator('#bulk-outcomes li').count(),2);
        assert.equal(await operationsPage.locator('#bulk-name-prefix').getAttribute('readonly'),'');
        assert.equal(await operationsPage.locator('#peer-rows a[download]').count()>0,true,'Created items use existing delivery actions');
        const storageBefore=await operationsPage.evaluate(()=>Object.keys(sessionStorage).filter(key=>key.startsWith('wgportal-operation:')).sort());
        assert(storageBefore.length>0);
        await submit.click();await operationsPage.waitForFunction(()=>!document.querySelector('#bulk-peer-form button[type=submit]').disabled);
        assert.equal(bulkCalls.length,2);assert.equal(bulkCalls[0].key,bulkCalls[1].key,'Recheck preserves batch identity');
        assert.deepEqual(bulkCalls[0].payload,bulkCalls[1].payload);
        const newBatch=operationsPage.locator('#bulk-peer-form button[type=button]');
        assert.equal(await newBatch.isVisible(),status===201,'Partial/uncertain batches cannot silently start over');
        if(status===201){await newBatch.click();assert.equal(await operationsPage.locator('#bulk-name-prefix').getAttribute('readonly'),null);}
        else assert.deepEqual(await operationsPage.evaluate(()=>Object.keys(sessionStorage).filter(key=>key.startsWith('wgportal-operation:')).sort()),storageBefore,'Partial results retain their receipt');
      }
      await operationsPage.locator('#peer-rows tr').filter({hasText:'delete-success'}).locator('.danger').click();
      await operationsPage.locator('#delete-cancel').click();assert.equal(deleteCalls.length,1);
      await operationsPage.locator('#peer-rows tr').filter({hasText:'delete-success'}).locator('.danger').click();
      await operationsPage.locator('#delete-confirm').click();
      await operationsPage.waitForFunction(()=>!document.querySelector('#delete-dialog').open);
      // The dialog closes after DELETE succeeds, before the awaited GET refresh renders.
      // Wait for that visible outcome; a failed deletion or refresh still times out here.
      await operationsPage.locator('#peer-rows tr').filter({hasText:'delete-success'}).waitFor({state:'detached'});
      assert.equal(await operationsPage.locator('#peer-rows tr').filter({hasText:'delete-success'}).count(),0);
      await operationsPage.locator('#peer-rows tr').filter({hasText:'delete-uncertain'}).locator('.danger').click();
      await operationsPage.locator('#delete-confirm').click();
      await operationsPage.waitForFunction(()=>document.querySelector('#delete-confirm').disabled&&document.querySelector('#delete-result').textContent.includes('native deletion outcome unknown'));
      assert.equal(deleteCalls.filter(value=>value==='/reseller/api/peers/52').length,1);
      await operationsPage.locator('#delete-cancel').click();
      await operationsPage.waitForFunction(()=>Array.from(document.querySelectorAll('#peer-rows tr')).find(row=>row.textContent.includes('delete-uncertain'))?.querySelector('.danger')?.disabled);
      assert.equal(await operationsPage.locator('#peer-rows tr').filter({hasText:'delete-uncertain'}).locator('.danger').isDisabled(),true,'Uncertain delete cannot be repeated from refreshed row');
      assert.deepEqual(pageErrors,[],'No browser runtime errors');
      await operationsContext.close();
    }
    process.stdout.write('Locale rendered before delayed scripts in Persian/English/storage-disabled; reseller entry and login passed.\n');
  } finally {await browser.close();}
})().catch(error => {console.error(error);process.exitCode = 1;});
