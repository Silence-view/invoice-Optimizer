// Independent browser fixtures: all HTTP responses intercepted, no live business data accessed.
const {chromium}=require('playwright');
const fs=require('node:fs'),path=require('node:path'),crypto=require('node:crypto'),assert=require('node:assert/strict');
const root=path.resolve(__dirname,'..'),out=path.join(root,'output/verification');
fs.mkdirSync(out,{recursive:true});
const receiptPath=path.join(out,'swipe-pdf-ui-smoke.json');
const sourceFiles=['src/invoice_optimizer/static/app.js','src/invoice_optimizer/static/app.css','src/invoice_optimizer/static/card_deck.js','src/invoice_optimizer/static/card_deck.css','src/invoice_optimizer/static/swipe_workspace.js','src/invoice_optimizer/static/swipe_pdf.js','src/invoice_optimizer/static/swipe_pdf.css','src/invoice_optimizer/static/risk_bars.js','src/invoice_optimizer/static/risk_bars.css','src/invoice_optimizer/templates/index.html'];
const hashes=()=>Object.fromEntries(sourceFiles.map(file=>[file,crypto.createHash('sha256').update(fs.readFileSync(path.join(root,file))).digest('hex')]));
const calls=[],checks=[],errors=[],external=[],unexpected=[],screenshots=[];
let browser,sourceHashes,png;const pngs=new Map();
const actor='user:'+'a'.repeat(32);
async function until(predicate,description='condition'){
  for(let n=0;n<800;n++){if(await predicate())return;await new Promise(resolve=>setTimeout(resolve,10));}
  throw Error('Bounded wait exceeded eight seconds: '+description);
}
const invoiceRow=n=>({id:'invoice-'+n,number:'SYNTHETIC-PDF-'+n,customer_id:n===3?'C2':'C1',period:'2026-09',status:'DRAFT',net_total:'100.00',vat_total:'20.00',gross_total:'120.00',currency:'GBP',line_format:'SUMMARY',line_count:1,pdf_ready:true,fingerprint:String(n).repeat(64),pdf_sha256:['a','b','c'][n-1].repeat(64),source_files:[]});
function hold(fx,path){let release;const wait=new Promise(resolve=>release=resolve);const gate={path,release,wait,started:false,done:false};fx.gates.push(gate);return gate;}
async function newFixture({role='admin',viewport={width:1440,height:1080},configure}={}){
  const context=await browser.newContext({viewport,hasTouch:true,reducedMotion:'reduce'});
  const page=await context.newPage();page.setDefaultTimeout(8000);
  const fx={page,context,role,invoices:[1,2,3].map(invoiceRow),gates:[],calls:[],writes:[],allowWrites:false,metaPatch:{},packagePatch:{},pagePatch:{}};
  if(configure)configure(fx);
  page.on('pageerror',error=>errors.push(String(error)));
  await context.addInitScript(()=>{
    window.streams=[];
    window.EventSource=class{constructor(){this.listeners={};streams.push(this);}addEventListener(name,fn){this.listeners[name]=fn;}close(){this.closed=true;}};
    window.fixtureStream=(revision,resources)=>streams.at(-1).listeners.update({data:JSON.stringify({revision,stream_version:revision+':1',changed_resources:resources})});
    window.fixtureExpire=()=>streams.at(-1).listeners.auth_expired({data:'{}'});
    const create=URL.createObjectURL.bind(URL),revoke=URL.revokeObjectURL.bind(URL);
    window.fixtureBlobs={created:[],revoked:[]};
    URL.createObjectURL=blob=>{const url=create(blob);fixtureBlobs.created.push({url,size:blob.size,type:blob.type});return url;};
    URL.revokeObjectURL=url=>{fixtureBlobs.revoked.push(url);return revoke(url);};
    const fetchOriginal=window.fetch.bind(window);window.fixtureFetches=[];
    window.fetch=(input,options={})=>{const record={url:String(input),signal:Boolean(options.signal),aborted:Boolean(options.signal?.aborted)};fixtureFetches.push(record);options.signal?.addEventListener('abort',()=>record.aborted=true,{once:true});return fetchOriginal(input,options);};
  });
  await context.route('**/*',async route=>{
    const request=route.request(),url=new URL(request.url()),endpoint=url.pathname,method=request.method();
    if(url.origin!=='http://invoice.test'){external.push(url.origin);return route.abort();}
    const call={path:endpoint,query:url.search,method,body:method==='GET'?null:request.postDataJSON(),csrf:request.headers()['x-csrf-token']||null};calls.push(call);fx.calls.push(call);
    const json=(body,status=200)=>route.fulfill({status,contentType:'application/json',body:JSON.stringify(body)});
    const defer=async()=>{const gate=fx.gates.find(value=>value.path===endpoint&&!value.started);if(gate){gate.started=true;await gate.wait;gate.done=true;}};
    if(endpoint.startsWith('/static/'))return route.fulfill({contentType:endpoint.endsWith('.css')?'text/css':'application/javascript',body:fs.readFileSync(path.join(root,'src/invoice_optimizer',endpoint),'utf8')});
    if(endpoint==='/')return route.fulfill({contentType:'text/html',body:fs.readFileSync(path.join(root,'src/invoice_optimizer/templates/index.html'),'utf8')});
    if(method!=='GET'){
      assert.equal(fx.allowWrites,true,'Fixture never authorizes an implicit mutation');assert.equal(fx.role,'admin');assert.equal(call.csrf,'swipe-pdf-csrf');
      assert.equal(Object.hasOwn(call.body||{},'actor'),false);assert.ok(call.body.request_id);
      fx.writes.push(call);
      if(['/api/review/postpone','/api/review/resume'].includes(endpoint)){
        const input=call.body,item=fx.invoices.find(row=>row.id===input.id);assert.ok(item);assert.equal(input.kind,'INVOICE');assert.equal(input.fingerprint,item.fingerprint);assert.ok(input.reason.trim().length>=5);
        const binding={invoice_id:item.id,fingerprint:item.fingerprint};
        item.personal_postponement=endpoint.endsWith('postpone')?{until:'2099-01-01T12:00:00Z',binding,actor,reason:input.reason}:null;
        return json({id:item.id,kind:'INVOICE',status:item.personal_postponement?'POSTPONED':'RESUMED',binding,personal_postponement:item.personal_postponement,reviewer:{actor_id:actor,identity_kind:'REGISTERED_OPERATOR'},financial_status_changed:false});
      }
      unexpected.push(call);return json({detail:'Unexpected mutation blocked'},409);
    }
    if(endpoint==='/api/auth/status')return json({setup_required:false});
    if(endpoint==='/api/session')return json({authenticated:true,csrf_token:'swipe-pdf-csrf',identity_type:fx.role==='legacy'?'legacy':'operator',can_review:fx.role!=='legacy',actor:fx.role==='legacy'?'legacy-shared':actor,user_id:'a'.repeat(32),username:'fixture.pdf',display_name:'Fixture PDF Reviewer',role:fx.role==='legacy'?null:fx.role});
    if(endpoint==='/api/customers')return json([{id:'C1',legal_name:'Synthetic Aster Ltd'},{id:'C2',legal_name:'Synthetic Birch Ltd'}]);
    if(endpoint==='/api/dashboard')return json({mode:'SIMULATION',counts:{open_cases:0,pending_approvals:3,movements:3,customers:2},periods:['2026-09'],worker:{status:'RUNNING',stale:false},auto_dispatch_after_approval:true,revision:1});
    if(endpoint==='/api/invoices'){
      assert.equal(url.searchParams.get('limit'),'50');const customer=url.searchParams.get('customer_id');
      return json(fx.invoices.filter(item=>!customer||item.customer_id===customer));
    }
    if(['/api/cases','/api/batches','/api/outbox','/api/audit','/api/model/runs'].includes(endpoint))return json([]);
    const packageMatch=endpoint.match(/^\/api\/invoices\/(invoice-\d+)\/package$/);
    if(packageMatch){
      const item=fx.invoices.find(row=>row.id===packageMatch[1]);assert.ok(item);
      const data={...item,document:{invoice_number:item.number,currency:'GBP',period:item.period,customer:{legal_name:'Synthetic Aster Ltd'},net_total:'100.00',vat_total:'20.00',gross_total:'120.00',movement_count:1},message:{from:'fixture@example.test',to:['recipient@example.test'],subject:'Isolated preview fixture',body:'No real email is sent.'},...fx.packagePatch};
      await defer();return json(data);
    }
    const previewMatch=endpoint.match(/^\/api\/invoices\/(invoice-\d+)\/preview$/);
    if(previewMatch){const item=fx.invoices.find(row=>row.id===previewMatch[1]);assert.ok(item);const data={pdf_sha256:item.pdf_sha256,page_count:3,page_url:`/api/invoices/${item.id}/preview/pages/1?sha256=${item.pdf_sha256}`,source:'EXACT_RETAINED_PDF',read_only:true,...fx.metaPatch};await defer();return json(data);}
    const pageMatch=endpoint.match(/^\/api\/invoices\/(invoice-\d+)\/preview\/pages\/(\d+)$/);
    if(pageMatch){
      const item=fx.invoices.find(row=>row.id===pageMatch[1]);assert.ok(item);assert.equal(url.searchParams.get('sha256'),item.pdf_sha256);const pageNumber=Number(pageMatch[2]);assert.ok(pageNumber>=1&&pageNumber<=3);
      const patch={...fx.pagePatch},headers={'X-PDF-SHA256':item.pdf_sha256,'X-PDF-Page':String(pageNumber),...patch.headers};
      await defer();if(patch.status)return json({detail:patch.detail||'Preview renderer unavailable'},patch.status);
      return route.fulfill({contentType:patch.contentType||'image/png',headers,body:patch.body||pngs.get(item.id+':'+pageNumber)});
    }
    if(/^\/api\/invoices\/invoice-\d+\/pdf$/.test(endpoint))return route.fulfill({contentType:'application/pdf',body:Buffer.from('%PDF-1.4\n% Synthetic intercepted response.\n')});
    unexpected.push(call);return json({detail:'Unexpected request blocked'},404);
  });
  await page.goto('http://invoice.test/#invoices');await until(async()=>await page.locator('.invoice-card').count()===fx.invoices.length,'initial invoice cards');
  return fx;
}
const active=page=>page.locator('.card-deck-layer[data-layer="0"]');
const host=page=>active(page).locator('[data-swipe-pdf]');
const activeKey=page=>active(page).getAttribute('data-card-key');
const previewCalls=fx=>fx.calls.filter(call=>/\/package$|\/preview(?:\/|$)/.test(call.path));
async function enterSwipe(page){await page.locator('[data-invoice-layout=swipe]').click();await page.locator('.card-deck-stage').waitFor();}
async function imageReady(page,pageNumber=1){
  await until(async()=>await host(page).locator('img').count()===1&&await host(page).locator('img').evaluate(image=>image.complete&&image.naturalWidth>0),'decoded active PDF image');
  assert.match(await host(page).innerText(),new RegExp('Page '+pageNumber+' of 3','i'));
  assert.equal(await page.locator('.card-deck-layer:not([data-layer="0"]) img').count(),0);
  assert.equal(await host(page).locator('iframe,object,embed').count(),0);
  assert.equal(await host(page).locator('img').getAttribute('data-pdf-page'),String(pageNumber));
  const id=await activeKey(page);assert.equal(await host(page).locator('img').getAttribute('data-pdf-sha256'),['a','b','c'][Number(id.split('-')[1])-1].repeat(64));
  const displayed=await host(page).locator('img').evaluate(async image=>Array.from(new Uint8Array(await (await fetch(image.src)).arrayBuffer())));
  assert.equal(crypto.createHash('sha256').update(Buffer.from(displayed)).digest('hex'),crypto.createHash('sha256').update(pngs.get(id+':'+pageNumber)).digest('hex'),'Displayed blob matches exact fixture invoice and page bytes');
}
async function blobs(page){return page.evaluate(()=>({live:fixtureBlobs.created.filter(entry=>!fixtureBlobs.revoked.includes(entry.url)),created:fixtureBlobs.created.length,revoked:fixtureBlobs.revoked.length}));}
async function noBlob(page){await until(async()=>(await blobs(page)).live.length===0,'all preview object URLs revoked');}
async function closeFixture(fx){for(const gate of fx.gates)gate.release();await fx.context.close();}
const next=page=>host(page).locator('[data-swipe-pdf-page="2"]');
async function errorReady(page,pattern){await until(async()=>await host(page).locator('[data-swipe-pdf-retry]').count()===1,'explicit preview error with retry');if(pattern)assert.match(await host(page).innerText(),pattern);assert.equal(await host(page).locator('img').count(),0);await noBlob(page);}
async function screen(page,name){await page.locator('#toast').evaluate(el=>el.hidden=true);const file='swipe-pdf-'+name+'.png';await page.locator('.swipe-workspace').screenshot({path:path.join(out,file)});screenshots.push(file);}
async function normalChecks(){
  const fx=await newFixture(),page=fx.page;
  assert.deepEqual(previewCalls(fx),[],'Cards view makes no automatic preview or package GET');
  await enterSwipe(page);await imageReady(page);
  assert.equal(await activeKey(page),'invoice-1');assert.equal(await page.locator('.card-deck-layer').count(),3);
  assert.equal(await active(page).locator('.invoice-paper').count(),0,'Schematic paper is replaced in Swipe');
  assert.deepEqual(previewCalls(fx).map(c=>c.path),['/api/invoices/invoice-1/preview','/api/invoices/invoice-1/preview/pages/1']);
  assert.deepEqual(await host(page).locator('img').evaluate(image=>({width:image.naturalWidth,height:image.naturalHeight})),{width:600,height:840});
  assert.equal((await blobs(page)).live.length,1);assert.match(await host(page).locator('img').getAttribute('src'),/^blob:/);
  const firstUrl=await host(page).locator('img').getAttribute('src');
  assert.equal(await host(page).locator('[data-swipe-pdf-page="0"]').isDisabled(),true);
  const link=host(page).getByRole('link',{name:'Open exact PDF ↗'});assert.equal(await link.getAttribute('href'),'/api/invoices/invoice-1/pdf');assert.equal(await link.getAttribute('target'),'_blank');assert.match(await link.getAttribute('rel'),/noopener/);
  await next(page).click();await imageReady(page,2);assert.notEqual(await host(page).locator('img').getAttribute('src'),firstUrl);
  assert.equal((await blobs(page)).live.length,1);assert.ok(await page.evaluate(url=>fixtureBlobs.revoked.includes(url),firstUrl));
  assert.equal(fx.calls.filter(c=>c.path==='/api/invoices/invoice-1/preview').length,1,'Metadata is reused during this active-card lifetime');
  assert.equal(await activeKey(page),'invoice-1');assert.equal(fx.writes.length,0,'Nested page navigation never advances or decides a card');
  await host(page).locator('[data-swipe-pdf-page="3"]').click();await imageReady(page,3);
  assert.equal(await host(page).locator('[data-swipe-pdf-page="4"]').isDisabled(),true);
  await host(page).locator('[data-swipe-pdf-page="2"]').click();await imageReady(page,2);
  const button=host(page).locator('[data-swipe-pdf-page="1"]');
  await button.dispatchEvent('pointerdown',{pointerId:11,pointerType:'touch',isPrimary:true,button:0,buttons:1,clientX:100,clientY:100});
  await button.dispatchEvent('pointermove',{pointerId:11,pointerType:'touch',isPrimary:true,button:0,buttons:1,clientX:500,clientY:100});
  await button.dispatchEvent('pointerup',{pointerId:11,pointerType:'touch',isPrimary:true,button:0,buttons:0,clientX:500,clientY:100});
  assert.equal(await activeKey(page),'invoice-1');assert.equal(fx.writes.length,0);assert.equal(await page.locator('#detail-dialog').evaluate(el=>el.open),false);
  await button.click();await imageReady(page,1);await screen(page,'desktop-fixture');
  checks.push('Only active invoice loads exact list-bound PNG; no package or underlying-card reads; three DOM layers, no schematic/embedded PDF; page controls reuse metadata and revoke prior blob; nested interactions never swipe or mutate');

  const beforeEnlarge=previewCalls(fx).length;
  await active(page).getByRole('button',{name:'Enlarge PDF '+fx.invoices[0].number}).click();
  await page.locator('#invoice-pdf-panel .invoice-pdf-image').waitFor();
  assert.equal(await page.locator('#detail-dialog').evaluate(el=>el.open),true);
  assert.ok(previewCalls(fx).slice(beforeEnlarge).some(c=>c.path==='/api/invoices/invoice-1/package'),'Enlarge explicitly fetches full package');
  assert.equal(fx.writes.length,0);await page.locator('#close-detail').click();await until(async()=>(await blobs(page)).live.length===1);
  assert.equal(await activeKey(page),'invoice-1');

  fx.allowWrites=true;await page.locator('[data-deck-action=later]').click();await until(async()=>await activeKey(page)==='invoice-2');await imageReady(page);
  assert.equal(fx.writes.length,1);assert.equal(fx.writes[0].path,'/api/review/postpone');assert.equal(fx.invoices[0].status,'DRAFT');
  assert.equal((await blobs(page)).live.length,1);assert.equal(fx.calls.some(c=>c.path.startsWith('/api/invoices/invoice-3/')),false);
  await page.locator('[data-swipe-view=deferred]').click();await until(async()=>await activeKey(page)==='invoice-1');await imageReady(page);
  await page.locator('[data-swipe-return]').click();await until(()=>fx.writes.length===2);assert.equal(fx.writes[1].path,'/api/review/resume');
  await page.locator('[data-swipe-view=pending]').click();await until(async()=>await activeKey(page)==='invoice-1');await imageReady(page);assert.equal((await blobs(page)).live.length,1);
  fx.allowWrites=false;await page.locator('#invoices-customer').selectOption('C2');await until(async()=>await activeKey(page)==='invoice-3');await imageReady(page);
  assert.equal((await blobs(page)).live.length,1);assert.equal(await page.locator('[data-deck-count=pending]').innerText(),'1');
  await page.setViewportSize({width:390,height:844});await page.locator('.swipe-workspace').scrollIntoViewIfNeeded();
  assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1),true,'No mobile document overflow');
  const widths=await host(page).locator('img').evaluate(image=>({image:image.getBoundingClientRect().width,host:image.parentElement.getBoundingClientRect().width}));assert.ok(widths.image<=widths.host+1);
  await screen(page,'mobile-fixture');
  await page.locator('[data-invoice-layout=cards]').click();await noBlob(page);assert.equal(await page.locator('[data-swipe-pdf]').count(),0);
  assert.ok(fx.writes.every(write=>['/api/review/postpone','/api/review/resume'].includes(write.path)));
  checks.push('Enlarge uses the full package modal without approval; Later/Return retain exact fingerprint and financial status; active-card and client changes replace/revoke one inline image; mobile fit and Cards cleanup');
  await closeFixture(fx);
}
async function unavailableChecks(){
  for(const mode of ['not-ready','missing-hash','invalid-hash']){
    const fx=await newFixture({configure:fx=>{if(mode==='not-ready')fx.invoices[0].pdf_ready=false;else fx.invoices[0].pdf_sha256=mode==='missing-hash'?null:'../untrusted';}});
    await enterSwipe(fx.page);await until(async()=>(await host(fx.page).innerText()).length>0);
    assert.deepEqual(previewCalls(fx),[],mode+' suppresses all reads');assert.equal(await host(fx.page).locator('img').count(),0);await noBlob(fx.page);assert.equal(fx.writes.length,0);await closeFixture(fx);
  }
  const fx=await newFixture({role:'legacy'});await enterSwipe(fx.page);await imageReady(fx.page);assert.equal(await fx.page.locator('[data-deck-action=review]').isDisabled(),true);
  await fx.page.locator('[data-deck-action=later]').click();await until(async()=>await activeKey(fx.page)==='invoice-2');await imageReady(fx.page);assert.equal(fx.writes.length,0);
  checks.push('Not-ready, missing hash and malformed hash do not fetch; read-only sessions can preview and navigate locally but cannot decide or persist');await closeFixture(fx);
}
async function faultChecks(){
  const faults=[
    ['metadata-hash',{metaPatch:{pdf_sha256:'f'.repeat(64)}},/match|changed|hash/i],
    ['metadata-pages',{metaPatch:{page_count:0}},/page count|page/i],
    ['header-hash',{pagePatch:{headers:{'X-PDF-SHA256':'f'.repeat(64)}}},/match|provenance|hash/i],
    ['header-page',{pagePatch:{headers:{'X-PDF-Page':'2'}}},/match|provenance|page/i],
    ['invalid-png',{pagePatch:{body:Buffer.from('Not a PNG image')}},/PNG|image/i],
    ['oversized-dimensions',{pagePatch:{body:(()=>{const image=Buffer.from(png);image.writeUInt32BE(1601,16);return image;})()}},/dimension|limit/i],
    ['oversized-length',{pagePatch:{headers:{'Content-Length':String(16*1024*1024+1)}}},/limit|large|size/i],
    ['renderer-unavailable',{pagePatch:{status:503}},/unavailable/i]
  ];
  for(const [name,patch,pattern] of faults){
    const fx=await newFixture({configure:fx=>Object.assign(fx,patch)}),page=fx.page;
    await enterSwipe(page);await errorReady(page,pattern);assert.equal(fx.writes.length,0);
    if(name.startsWith('metadata'))assert.equal(fx.calls.filter(c=>/preview\/pages/.test(c.path)).length,0,'Invalid metadata blocks page fetch');
    fx.metaPatch={};fx.pagePatch={};await host(page).locator('[data-swipe-pdf-retry]').click();await imageReady(page);assert.equal((await blobs(page)).live.length,1);
    await page.locator('[data-invoice-layout=list]').click();await noBlob(page);await closeFixture(fx);
  }
  checks.push('Eight explicit fault/retry cases: metadata hash/pages, page hash/page number, invalid PNG, oversized dimensions/declared length, unavailable renderer; no broken or unbound image and no decision');
}
async function lifecycleChecks(){
  // A queued obsolete response may complete after a new active card. It must not attach.
  const fx=await newFixture({role:'legacy'}),page=fx.page,gate=hold(fx,'/api/invoices/invoice-1/preview/pages/1');
  await enterSwipe(page);await until(()=>gate.started);await page.locator('[data-deck-action=later]').click();await until(async()=>await activeKey(page)==='invoice-2');await imageReady(page);
  const currentUrl=await host(page).locator('img').getAttribute('src');gate.release();await until(()=>gate.done);
  assert.equal(await host(page).locator('img').getAttribute('src'),currentUrl);assert.equal((await blobs(page)).live.length,1);
  assert.equal(await page.evaluate(()=>fixtureFetches.find(entry=>entry.url.includes('/invoice-1/preview/pages/1')).aborted),true);
  await closeFixture(fx);

  for(const mode of ['cards','filter','tab','logout']){
    const fx=await newFixture(),page=fx.page;await enterSwipe(page);await imageReady(page);
    const gate=hold(fx,'/api/invoices/invoice-1/preview/pages/2');await next(page).click();await until(()=>gate.started);
    if(mode==='cards')await page.locator('[data-invoice-layout=cards]').click();
    if(mode==='filter')await page.locator('#invoices-customer').selectOption('C2');
    if(mode==='tab')await page.locator('nav [data-tab=review]').click();
    if(mode==='logout'){await page.evaluate(()=>fixtureExpire());await page.locator('#login-screen').waitFor();}
    gate.release();await until(()=>gate.done);
    assert.equal(await page.evaluate(()=>fixtureFetches.find(entry=>entry.url.includes('/invoice-1/preview/pages/2')).aborted),true);
    if(mode==='filter'){await until(async()=>await activeKey(page)==='invoice-3');await imageReady(page);assert.equal((await blobs(page)).live.length,1);}
    else{await noBlob(page);assert.equal(await page.locator('.swipe-pdf-image').count(),0);}
    if(mode==='logout'){assert.equal(await page.locator('#workspace').isVisible(),false);assert.equal(await page.locator('#detail-dialog').evaluate(el=>el.open),false);}
    assert.equal(fx.writes.length,0);await closeFixture(fx);
  }
  checks.push('Delayed page completion after active card, layout, customer, tab or session change is aborted/ignored; obsolete image cannot attach and URLs are revoked');
}
async function staleUpdateCheck(){
  const fx=await newFixture(),page=fx.page;await enterSwipe(page);await imageReady(page);await next(page).click();await imageReady(page,2);
  const url=await host(page).locator('img').getAttribute('src'),before=previewCalls(fx).length;
  await page.evaluate(()=>{fixtureStream(1,['invoices']);fixtureStream(2,['invoices']);});await page.locator('[data-swipe-stale]').waitFor();
  await imageReady(page,2);assert.equal(await host(page).locator('img').getAttribute('src'),url);assert.equal(previewCalls(fx).length,before);
  assert.equal(await page.locator('[data-deck-action=review]').isDisabled(),true);assert.equal((await blobs(page)).live.length,1);
  checks.push('Relevant stale SSE disables financial decisions but preserves current page 2 and its blob without additional metadata/page GETs');await closeFixture(fx);
}
async function unauthorizedCheck(){
  const fx=await newFixture({configure:fx=>{fx.pagePatch={status:401,detail:'Session expired'};}}),page=fx.page;
  await page.locator('[data-invoice-layout=swipe]').click();await page.locator('#login-screen').waitFor();await noBlob(page);assert.equal(await page.locator('#workspace').isVisible(),false);assert.equal(await page.locator('.swipe-pdf-image').count(),0);assert.equal(fx.writes.length,0);
  checks.push('A page response with HTTP 401 returns to login and removes preview resources');await closeFixture(fx);
}
async function createFixturePng(n,pageNumber){
  // Generated fixture pixels, not evidence of Poppler output: real retained-PDF rendering is covered separately by backend/live checks.
  const context=await browser.newContext();const page=await context.newPage();
  await context.route('**/*',route=>{external.push(route.request().url());return route.abort();});
  const data=await page.evaluate(({n,pageNumber})=>{
    const canvas=document.createElement('canvas');canvas.width=600;canvas.height=840;const c=canvas.getContext('2d');
    c.fillStyle='#ffffff';c.fillRect(0,0,600,840);c.fillStyle='#153e35';c.fillRect(40,42,520,6);c.font='bold 30px sans-serif';c.fillText('SYNTHETIC INVOICE',40,92);
    c.fillStyle='#4b5563';c.font='14px sans-serif';c.fillText('ISOLATED BROWSER FIXTURE — NO LIVE DOCUMENT',40,124);
    c.fillStyle='#111827';c.font='bold 20px sans-serif';c.fillText(n===3?'Synthetic Birch Ltd':'Synthetic Aster Ltd',40,186);c.font='16px sans-serif';c.fillText('Invoice: SYNTHETIC-PDF-'+n,40,222);c.fillText('Billing period: September 2026 · Page '+pageNumber+' of 3',40,248);
    c.fillStyle='#f0f4f3';c.fillRect(40,300,520,38);c.fillStyle='#111827';c.font='bold 16px sans-serif';c.fillText('Description',50,325);c.fillText('Net',482,325);c.font='16px sans-serif';c.fillText('Synthetic service transaction',50,370);c.fillText('£100.00',466,370);
    c.fillStyle='#e5e7eb';c.fillRect(40,398,520,1);c.fillStyle='#111827';c.fillText('Net subtotal',330,450);c.fillText('£100.00',466,450);c.fillText('VAT (20%)',330,484);c.fillText('£20.00',475,484);c.font='bold 20px sans-serif';c.fillText('TOTAL',330,536);c.fillText('£120.00',458,536);
    c.fillStyle='#4b5563';c.font='14px sans-serif';c.fillText('This PNG is generated test input for the preview API fixture.',40,740);c.fillText('No approval, issue, dispatch or email occurs in this fixture.',40,766);return canvas.toDataURL('image/png').split(',')[1];
  },{n,pageNumber});await context.close();return Buffer.from(data,'base64');
}
(async()=>{
  sourceHashes=hashes();browser=await chromium.launch({headless:true});
  for(let n=1;n<=3;n++)for(let pageNumber=1;pageNumber<=3;pageNumber++)pngs.set('invoice-'+n+':'+pageNumber,await createFixturePng(n,pageNumber));png=pngs.get('invoice-1:1');
  await normalChecks();await unavailableChecks();await faultChecks();await lifecycleChecks();await staleUpdateCheck();await unauthorizedCheck();
  assert.deepEqual(errors,[]);assert.deepEqual(external,[]);assert.deepEqual(unexpected,[]);assert.deepEqual(hashes(),sourceHashes,'Production bytes changed during verification');
  const receipt={pass:true,scope:'All HTTP responses intercepted. PNG is synthetic test input, not a live PDF rendering receipt. Only two explicit fixture worklist mutations; no live business write, financial decision, dispatch or email.',checks,requests:calls.length,fixture_mutations:calls.filter(call=>call.method!=='GET').length,source_sha256:sourceHashes,fixture_png_sha256:Object.fromEntries([...pngs].map(([key,image])=>[key,crypto.createHash('sha256').update(image).digest('hex')])),screenshots,errors,external,unexpected,calls};
  fs.writeFileSync(receiptPath,JSON.stringify(receipt,null,2)+'\n');console.log(JSON.stringify({pass:true,checks:checks.length,receipt:receiptPath}));
})().catch(error=>{fs.writeFileSync(receiptPath,JSON.stringify({pass:false,error:String(error.stack||error),checks,source_sha256:sourceHashes,errors,external,unexpected,calls},null,2)+'\n');console.error(error);process.exitCode=1;}).finally(async()=>{if(browser)await browser.close();});
