// Network is fully mocked. No server, installer, or production data is touched.
const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');
const {chromium} = require(process.env.PLAYWRIGHT_MODULE_PATH || 'playwright');
const [root, rendered] = process.argv.slice(2);
const account = {id:1, username:'dealer', active:true, enabled:true, expires_at:4102444800, max_peers:10, interfaces:[1]};
(async () => {
  const browser = await chromium.launch({headless:true, executablePath:process.env.PORTAL_BROWSER_PATH || undefined});
  try {
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
        if (url.pathname === '/resellers/api/summary') return route.fulfill({json:{summary:{total_resellers:4,subresellers:3,total_creation_fee_toman:9000000}}});
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
    let childPayload;
    await context.route('**/*', route => {
      const url = new URL(route.request().url());
      if (url.pathname === '/login') return route.fulfill({contentType:'text/html',body:fs.readFileSync(path.join(rendered,'login.html'),'utf8')});
      if (url.pathname === '/reseller' || url.pathname === '/resellers') return route.fulfill({contentType:'text/html',body:fs.readFileSync(path.join(root,'deploy/linux/reseller_ui/index.html'),'utf8')});
      if (url.pathname === '/reseller/api/session') return route.fulfill({json:{csrf:'browser-test-csrf',account:signedIn?account:null}});
      if (url.pathname === '/reseller/api/login') {signedIn = true;return route.fulfill({json:{ok:true,csrf:'browser-test-csrf',account}});}
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
          const child = {...childPayload,id:2,parent_id:1,active:false,enabled:true,expires_at:0,creation_fee_toman:3000000};
          children.push(child);
          return route.fulfill({status:201,json:{account:child,signin_url:'/reseller'}});
        }
        return route.fulfill({json:{accounts:children,interfaces:[{id:1,name:'wg0'}],creation_fee:{amount:3000000,currency:'toman',one_time:true,payment_gateway:false}}});
      }
      if (url.pathname === '/resellers/api/accounts') return route.fulfill({json:{accounts:[{...account,direct_child_count:1,descendant_count:1},...children],interfaces:[{id:1,name:'wg0'}],audit:[],operations:[],summary:{total_resellers:2,subresellers:1,total_creation_fee_toman:3000000}}});
      const file = url.pathname === '/reseller/assets/reseller.js' ? 'deploy/linux/reseller_ui/reseller.js' : url.pathname.endsWith('/portal-locale.js') ? 'static/js/portal-locale.js' : null;
      if (file) return route.fulfill({contentType:'text/javascript',body:fs.readFileSync(path.join(root,file),'utf8')});
      if (url.pathname.endsWith('.js')) return route.fulfill({contentType:'text/javascript',body:''});
      return route.fulfill({body:''});
    });
    const page = await context.newPage();
    await page.goto('http://panel.test/login');
    await page.locator('#portal-reseller-login').click();
    await page.locator('#login-username').fill('dealer');
    await page.locator('#login-password').fill('a-safe-test-password');
    await page.locator('#login-form button[type=submit]').click();
    await page.locator('#active-view').waitFor({state:'visible'});
    assert.equal(await page.locator('#account-name').textContent(), 'dealer');
    assert.equal(await page.locator('#peer-interface').count(),0,'Reseller must not select an interface');
    await page.locator('#peer-name').fill('limited-customer');
    await page.locator('#peer-form button[type=submit]').click();
    await page.waitForFunction(() => document.getElementById('peer-rows')?.textContent.includes('limited-customer'));
    assert.deepEqual(Object.keys(peerPayload).sort(),['days','name','volume_gb']);
    await page.locator('#peer-rows button').last().click();
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
    await page.locator('#add-child').click();
    assert.match(await page.locator('#child-fee-amount').textContent(), /[3۳]/);
    await page.locator('#child-username').fill('child-dealer');
    await page.locator('#child-password').fill('a-safe-child-password');
    await page.locator('#child-limit').fill('2');
    await page.locator('#child-form button[type=submit]').click();
    await page.waitForFunction(() => document.getElementById('child-rows')?.textContent.includes('child-dealer'));
    assert.deepEqual(Object.keys(childPayload).sort(), ['max_peers','password','username']);
    assert.match(await page.locator('#signin-url').inputValue(), /\/reseller$/);
    await page.goto('http://panel.test/resellers');
    await page.waitForFunction(() => document.getElementById('account-rows')?.textContent.includes('child-dealer'));
    assert.match(await page.locator('#summary-creation-fees').textContent(), /[3۳]/);
    await context.close();
    process.stdout.write('Locale rendered before delayed scripts in Persian/English/storage-disabled; reseller entry and login passed.\n');
  } finally {await browser.close();}
})().catch(error => {console.error(error);process.exitCode = 1;});
