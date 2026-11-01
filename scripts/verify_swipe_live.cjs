// Live read-only verification. The sole permitted POST is the existing shared login.
const {chromium}=require('playwright');
const fs=require('node:fs'),path=require('node:path'),assert=require('node:assert/strict'),crypto=require('node:crypto');
const root=path.resolve(__dirname,'..'),out=path.join(root,'output/verification'),origin='http://127.0.0.1:8765';
const token=fs.readFileSync(path.join(root,'.env'),'utf8').match(/^INVOICE_API_TOKEN=(.*)$/m)?.[1].trim().replace(/^['"]|['"]$/g,'');
assert.ok(token,'Existing local read-only sign-in credential required');
fs.mkdirSync(out,{recursive:true});
const sourceFiles=['src/invoice_optimizer/static/app.js','src/invoice_optimizer/static/app.css','src/invoice_optimizer/static/card_deck.js','src/invoice_optimizer/static/card_deck.css','src/invoice_optimizer/static/swipe_workspace.js','src/invoice_optimizer/static/risk_bars.js','src/invoice_optimizer/static/risk_bars.css','src/invoice_optimizer/templates/index.html'];
const sha=value=>crypto.createHash('sha256').update(value).digest('hex'),hash=value=>sha(JSON.stringify(value));
const sourceHashes=()=>Object.fromEntries(sourceFiles.map(file=>[file,sha(fs.readFileSync(path.join(root,file)))]));
const money=(value,currency)=>{
 const match=String(value).match(/^(-?)(\d+)(?:\.(\d{1,2}))?$/);assert.ok(match,'Live fixture expects exact decimal money');
 return `${match[1]}${currency==='GBP'?'£':currency+' '}${match[2].replace(/\B(?=(\d{3})+(?!\d))/g,',')}.${(match[3]||'').padEnd(2,'0')}`;
};
const calls=[],blocked=[],errors=[],checks=[];let directGets=0,browser;
async function run(){
 const sourceBefore=sourceHashes();
 browser=await chromium.launch({headless:true});
 const context=await browser.newContext({viewport:{width:1440,height:1080}}),page=await context.newPage();page.setDefaultTimeout(15000);
 page.on('pageerror',error=>errors.push(String(error)));
 await context.route('**/*',route=>{
  const request=route.request(),url=new URL(request.url());const entry={method:request.method(),path:url.pathname,query:url.search};calls.push(entry);
  if(url.origin!==origin || (!['GET','HEAD'].includes(request.method()) && !(request.method()==='POST' && url.pathname==='/api/login' && calls.filter(x=>x.method==='POST'&&x.path==='/api/login').length===1))){blocked.push(entry);return route.abort();}
  return route.continue();
 });
 const get=async endpoint=>{assert.ok(endpoint.startsWith('/api/'));assert.equal(new URL(endpoint,origin).origin,origin);directGets++;const response=await context.request.get(origin+endpoint);assert.ok(response.ok(),`${endpoint} returned ${response.status()}`);return response.json();};
 const top=()=>page.locator('.card-deck-layer[data-layer="0"]');
 const counts=async()=>Object.fromEntries(await page.locator('[data-deck-count]').evaluateAll(nodes=>nodes.map(node=>[node.dataset.deckCount,Number(node.textContent.replaceAll(',',''))])));
 const waitCount=async(name,value)=>page.waitForFunction(({name,value})=>Number(document.querySelector(`[data-deck-count="${name}"]`)?.textContent.replaceAll(',',''))===value,{name,value});
 const readonly=async()=>{assert.equal(await page.locator('[data-deck-action=review]').isDisabled(),true);assert.equal(await page.locator('[data-deck-action=reject]').isDisabled(),true);};
 const noOverflow=async()=>{assert.equal(await page.locator('body').evaluate(el=>el.scrollWidth>innerWidth),false,'Body must not overflow horizontally');assert.equal(await page.locator('.swipe-workspace').evaluate(el=>el.scrollWidth>el.clientWidth),false,'Swipe worklist must not overflow horizontally');};
 await page.goto(origin+'/#review');await page.locator('[data-login-mode=legacy]').click();await page.locator('#access-token').fill(token);await page.locator('#login-form button[type=submit]').click();await page.locator('.review-card').first().waitFor();
 const initialQuery=calls.filter(x=>x.path==='/api/cases').at(-1).query,initial=await get('/api/cases'+initialQuery);
 const chosen=initial.find(row=>row.model?.recorded_score!=null && row.reason==='MODEL_RISK' && row.customer_id);assert.ok(chosen,'Seeded data must contain a scored model-risk example');
 await Promise.all([page.waitForResponse(response=>new URL(response.url()).pathname==='/api/cases'&&new URL(response.url()).searchParams.get('customer_id')===chosen.customer_id),page.locator('[data-customer-filter=review]').selectOption(chosen.customer_id)]);
 await page.locator('.review-card').first().waitFor();
 await Promise.all([page.waitForResponse(response=>new URL(response.url()).pathname==='/api/cases'&&new URL(response.url()).searchParams.get('reason')==='MODEL_RISK'),page.locator('#case-reason').fill('MODEL_RISK')]);
 await page.waitForFunction(()=>document.querySelector('#tab-content')?.getAttribute('aria-busy')==='false');
 const filteredQuery=calls.filter(x=>x.path==='/api/cases').at(-1).query,filtered=await get('/api/cases'+filteredQuery);assert.ok(filtered.length>=2&&filtered.length<=50);assert.ok(filtered.every(row=>row.reason==='MODEL_RISK'&&row.customer_id===chosen.customer_id));
 await page.locator('[data-review-layout=swipe]').click();await page.locator('.card-deck').waitFor();
 assert.ok(await page.locator('.card-deck-layer').count()<=3);assert.equal(await top().getAttribute('data-card-key'),filtered[0].id);
 await top().locator('.risk-driver').first().waitFor();assert.equal(await top().locator('.risk-driver').count(),6);await readonly();
 const selected=filtered[0],detail=await get('/api/cases/'+selected.id),model=detail.assessments.find(row=>row.kind==='MODEL');
 assert.equal(selected.version_id,detail.version.id);assert.equal(selected.transaction.amount,detail.version.amount);assert.equal(selected.model.recorded_score,model.score);assert.equal(selected.model.model_version,model.model_version);
 assert.match(await top().locator('.review-model-score').innerText(),new RegExp('p = '+String(selected.model.recorded_score).replace(/[.*+?^${}()|[\]\\]/g,'\\$&')));
 const body=await top().innerText();assert.ok(body.includes(money(detail.version.amount,detail.version.currency)));
 for(const side of ['A','B']){const raw=detail.version.payload.source_values?.[side];assert.equal(selected.transaction[`source_${side.toLowerCase()}_amount`],raw?.amount??null);if(raw?.amount!=null)assert.ok(body.includes(money(raw.amount,raw.currency)));}
 for(const file of selected.source_files.slice(0,2))assert.ok(await top().locator(`a[href="${file.download_url}"]`).count()>0);
 const drivers=(await get('/api/case-drivers?case_id='+encodeURIComponent(selected.id))).items[0];assert.equal(drivers.status,'AVAILABLE');assert.equal(drivers.version_id,selected.version_id);assert.equal(drivers.model_version,selected.model.model_version);assert.equal(drivers.recorded_score,selected.model.recorded_score);assert.equal(drivers.features.length,6);
 assert.ok(Math.abs(drivers.features.reduce((sum,f)=>sum+f.share_percent,0)-100)<1e-8);
 for(const feature of drivers.features){const control=top().locator(`[data-risk-feature="${feature.feature}"]`);assert.equal(await control.count(),1);assert.ok((await control.getAttribute('aria-label')).includes(String(feature.contribution)));}
 const firstDriver=top().locator('.risk-driver').first();await firstDriver.hover();await firstDriver.locator('.risk-driver-tooltip').waitFor({state:'visible'});assert.match(await firstDriver.locator('.risk-driver-tooltip').innerText(),/contribution share/);assert.match(await firstDriver.getAttribute('aria-label'),/not a risk probability/);
 await top().evaluate(el=>el.scrollIntoView({block:'start'}));await page.evaluate(()=>scrollBy(0,-90));await page.mouse.move(10,10);await noOverflow();await page.screenshot({path:path.join(out,'swipe-live-review-desktop.png'),fullPage:false,animations:'disabled'});
 for(const feature of drivers.features){const control=top().locator(`[data-risk-feature="${feature.feature}"]`);const geometry=await control.evaluate(el=>({track:el.querySelector('.risk-driver-track').getBoundingClientRect().width,fill:el.querySelector('.risk-driver-fill').getBoundingClientRect().width}));assert.ok(Math.abs(100*geometry.fill/geometry.track-feature.share_percent)<.1,'Rendered bar width must match its recorded contribution share');}
 await firstDriver.hover();await page.screenshot({path:path.join(out,'swipe-live-parameter-hover.png'),fullPage:false,animations:'disabled'});
 checks.push('Current card amount, source amounts, version, risk score and six model contribution bars match exact frozen API evidence; hover explains contribution share.');
 const readsBeforeSwitch=calls.filter(x=>['/api/cases','/api/case-drivers'].includes(x.path)).length;
 await page.locator('[data-review-layout=cards]').click();await page.locator('[data-review-layout=swipe]').click();assert.equal(await top().getAttribute('data-card-key'),selected.id);assert.equal(calls.filter(x=>['/api/cases','/api/case-drivers'].includes(x.path)).length,readsBeforeSwitch);
 const before=await counts();assert.deepEqual(before,{pending:filtered.length,deferred:0,confirmed:0,rejected:0});
 const heading=top().locator('h3');await heading.scrollIntoViewIfNeeded();const box=await heading.boundingBox(),x=box.x+Math.min(240,box.width*.7),y=box.y+box.height/2;
 await page.mouse.move(x,y);await page.mouse.down();await page.mouse.move(x-220,y+3,{steps:10});await page.mouse.up();await waitCount('deferred',1);assert.deepEqual(await counts(),{pending:filtered.length-1,deferred:1,confirmed:0,rejected:0});
 await page.locator('[data-swipe-view=deferred]').click();assert.equal(await top().getAttribute('data-card-key'),selected.id);assert.match(await page.locator('[data-swipe-note]').innerText(),/local worklist/);await readonly();
 await page.locator('[data-swipe-return]').click();await waitCount('deferred',0);await page.locator('[data-swipe-view=pending]').click();assert.equal(await top().getAttribute('data-card-key'),selected.id);assert.deepEqual(await counts(),before);
 checks.push('Native left drag moves only the shared-session local worklist; Pending/Later counters and individual Return preserve financial state. Same-page Cards/Swipe switch makes no extra list/driver GET.');
 await page.locator('#toast').waitFor({state:'hidden'});await page.setViewportSize({width:390,height:844});await top().evaluate(el=>el.scrollIntoView({block:'start'}));await noOverflow();await page.screenshot({path:path.join(out,'swipe-live-review-mobile.png'),fullPage:false,animations:'disabled'});await readonly();assert.ok(await page.locator('.card-deck-layer').count()<=3);
 await page.locator('[data-deck-action=later]').scrollIntoViewIfNeeded();await page.screenshot({path:path.join(out,'swipe-live-review-mobile-controls.png'),fullPage:false,animations:'disabled'});
 await page.setViewportSize({width:1440,height:1080});await page.locator('[data-tab=invoices]').first().click();await page.locator('.invoice-card').first().waitFor();
 const invoiceQuery=calls.filter(x=>x.path==='/api/invoices').at(-1).query,invoices=await get('/api/invoices'+invoiceQuery);assert.ok(invoices.length>=2&&invoices.length<=50);
 const invoiceReads=calls.filter(x=>x.path==='/api/invoices').length;await page.locator('[data-invoice-layout=swipe]').click();await page.locator('.card-deck').waitFor();assert.equal(calls.filter(x=>x.path==='/api/invoices').length,invoiceReads);await readonly();
 const invoice=invoices[0];assert.equal(await top().getAttribute('data-card-key'),invoice.id);assert.ok((await top().locator('.paper-total').innerText()).includes(money(invoice.gross_total,invoice.currency)));assert.match(await page.locator('[data-swipe-note]').innerText(),/does not pause approved invoice dispatch/);assert.ok(await page.locator('.card-deck-layer').count()<=3);
 const sourceDetails=top().locator('details.card-source-details');await sourceDetails.locator('summary').click();for(const file of invoice.source_files.slice(0,2))assert.ok(await sourceDetails.locator(`a[href="${file.download_url}"]`).count()>0);
 await page.locator('#toast').waitFor({state:'hidden'});await top().evaluate(el=>el.scrollIntoView({block:'start'}));await noOverflow();await page.screenshot({path:path.join(out,'swipe-live-invoices-desktop.png'),fullPage:false,animations:'disabled'});
 await page.locator('[data-deck-action=later]').click();await waitCount('deferred',1);await page.locator('[data-swipe-view=deferred]').click();assert.equal(await top().getAttribute('data-card-key'),invoice.id);await page.locator('[data-swipe-return]').click();await waitCount('deferred',0);await page.locator('[data-swipe-view=pending]').click();assert.equal(await top().getAttribute('data-card-key'),invoice.id);assert.deepEqual(await counts(),{pending:invoices.length,deferred:0,confirmed:0,rejected:0});
 await page.locator('#toast').waitFor({state:'hidden'});await page.setViewportSize({width:390,height:844});await top().evaluate(el=>el.scrollIntoView({block:'start'}));await noOverflow();await page.screenshot({path:path.join(out,'swipe-live-invoices-mobile.png'),fullPage:false,animations:'disabled'});await readonly();
 checks.push('Invoice Swipe shows exact gross total and source-file references, clearly distinguishes Later from dispatch hold, and supports local Later/Return on desktop and mobile.');
 assert.deepEqual(await get('/api/cases'+initialQuery),initial);assert.deepEqual(await get('/api/cases'+filteredQuery),filtered);assert.deepEqual(await get('/api/invoices'+invoiceQuery),invoices);assert.equal(blocked.length,0);assert.equal(errors.length,0);
 const writes=calls.filter(x=>!['GET','HEAD'].includes(x.method));assert.deepEqual(writes.map(x=>[x.method,x.path]),[['POST','/api/login']]);
 assert.deepEqual(sourceHashes(),sourceBefore,'Source bytes changed during live verification; rerun against frozen source');
 const receipt={pass:true,scope:'Live localhost; shared read-only identity; sole POST shared login. All decisions, durable postponements, source writes and email sends prohibited by route guard.',origin,created_at:new Date().toISOString(),checks,initial_case_count:initial.length,filtered_case_count:filtered.length,invoice_count:invoices.length,case_id:selected.id,case_amount:selected.transaction.amount,recorded_model:selected.model.model_version,recorded_score:selected.model.recorded_score,threshold:drivers.threshold,driver_features:drivers.features.map(f=>({feature:f.feature,raw_value:f.raw_value,share_percent:f.share_percent,contribution:f.contribution})),case_projection_sha256:hash(initial),filtered_projection_sha256:hash(filtered),invoice_projection_sha256:hash(invoices),case_invoice_projections_unchanged:true,cards_swipe_extra_gets:0,max_card_layers:3,desktop_mobile_no_overflow:true,browser_requests:calls.length,direct_gets:directGets,requests_by_path:Object.fromEntries([...new Set(calls.map(x=>x.path))].map(p=>[p,calls.filter(x=>x.path===p).length])),writes,blocked,errors,source_sha256:sourceBefore};
 fs.writeFileSync(path.join(out,'swipe-live-smoke.json'),JSON.stringify(receipt,null,2)+'\n');console.log(JSON.stringify({pass:true,browser_requests:calls.length,direct_gets:directGets,cases:filtered.length,invoices:invoices.length,sole_post:'/api/login',receipt:'output/verification/swipe-live-smoke.json'}));
}
run().catch(error=>{const receipt={pass:false,error:String(error),browser_requests:calls.length,direct_gets:directGets,blocked,errors,checks};fs.writeFileSync(path.join(out,'swipe-live-smoke.json'),JSON.stringify(receipt,null,2)+'\n');console.error(String(error));process.exitCode=1;}).finally(async()=>{if(browser)await browser.close();});
