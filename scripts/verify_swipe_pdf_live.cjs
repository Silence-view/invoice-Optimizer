// Real local PDF inspection. Route guard permits only GET/HEAD and one shared login.
const {chromium}=require('playwright');
const fs=require('node:fs'),path=require('node:path'),assert=require('node:assert/strict'),crypto=require('node:crypto');
const root=path.resolve(__dirname,'..'),out=path.join(root,'output/verification'),origin='http://127.0.0.1:8765';
const token=fs.readFileSync(path.join(root,'.env'),'utf8').match(/^INVOICE_API_TOKEN=(.*)$/m)?.[1].trim().replace(/^['"]|['"]$/g,'');
assert.ok(token,'Existing local read-only credential required');fs.mkdirSync(out,{recursive:true});
const sha=value=>crypto.createHash('sha256').update(value).digest('hex'),hash=value=>sha(JSON.stringify(value));
const sources=['src/invoice_optimizer/static/app.js','src/invoice_optimizer/static/app.css','src/invoice_optimizer/static/card_deck.js','src/invoice_optimizer/static/card_deck.css','src/invoice_optimizer/static/swipe_workspace.js','src/invoice_optimizer/static/swipe_pdf.js','src/invoice_optimizer/static/swipe_pdf.css','src/invoice_optimizer/static/risk_bars.js','src/invoice_optimizer/static/risk_bars.css','src/invoice_optimizer/templates/index.html'];
const sourceHashes=()=>Object.fromEntries(sources.map(file=>[file,sha(fs.readFileSync(path.join(root,file)))]));
const calls=[],direct=[],blocked=[],errors=[],pages=[],checks=[];let browser;
async function run(){
 const sourceBefore=sourceHashes();
 browser=await chromium.launch({headless:true});const context=await browser.newContext({viewport:{width:1440,height:1080}}),page=await context.newPage();page.setDefaultTimeout(20000);
 page.on('pageerror',error=>errors.push(String(error)));
 await page.addInitScript(()=>{
  window.previewUrls={created:[],revoked:[],active:new Set(),blobs:new Map()};
  const create=URL.createObjectURL.bind(URL),revoke=URL.revokeObjectURL.bind(URL);
  URL.createObjectURL=blob=>{const value=create(blob);previewUrls.created.push(value);previewUrls.active.add(value);previewUrls.blobs.set(value,blob);return value;};
  URL.revokeObjectURL=value=>{previewUrls.revoked.push(value);previewUrls.active.delete(value);previewUrls.blobs.delete(value);revoke(value);};
 });
 await context.route('**/*',route=>{
  const request=route.request(),url=new URL(request.url()),entry={method:request.method(),path:url.pathname,query:url.search};calls.push(entry);
  if(url.origin!==origin || (!['GET','HEAD'].includes(request.method()) && !(request.method()==='POST'&&url.pathname==='/api/login'&&calls.filter(x=>x.method==='POST'&&x.path==='/api/login').length===1))){blocked.push(entry);return route.abort();}
  return route.continue();
 });
 const response=async endpoint=>{assert.ok(endpoint.startsWith('/api/'));assert.equal(new URL(endpoint,origin).origin,origin);direct.push({method:'GET',path:endpoint});const result=await context.request.get(origin+endpoint);assert.ok(result.ok(),`${endpoint} returned ${result.status()}`);return result;};
 const get=async endpoint=>(await response(endpoint)).json();
 const top=()=>page.locator('.card-deck-layer[data-layer="0"]');
 const image=()=>top().locator('.swipe-pdf-image');
 const verifyImage=async(row,number)=>{
  await page.waitForFunction(({id,sha,number})=>{const card=document.querySelector('.card-deck-layer[data-layer="0"]'),img=card?.querySelector('.swipe-pdf-image');return card?.dataset.cardKey===id&&img?.complete&&img.naturalWidth>0&&img.dataset.pdfSha256===sha&&Number(img.dataset.pdfPage)===number;},{id:row.id,sha:row.pdf_sha256,number});
  assert.equal(await page.locator('.card-deck .swipe-pdf-image').count(),1,'Only the active card may have a decoded page');
  assert.equal(await page.locator('.card-deck-layer:not([data-layer="0"]) img').count(),0,'Underlying stack cards must not contain page images');
  assert.ok(await page.locator('.card-deck-layer').count()<=3);
  assert.equal(await page.locator('.card-deck iframe,.card-deck object').count(),0);
  assert.equal(await top().locator('.invoice-paper').count(),0,'The schematic preview must be absent from Swipe');
  const observed=await image().evaluate(async img=>{const bytes=await previewUrls.blobs.get(img.src).arrayBuffer();const checksum=await crypto.subtle.digest('SHA-256',bytes);return {sha256:Array.from(new Uint8Array(checksum),n=>n.toString(16).padStart(2,'0')).join(''),bytes:bytes.byteLength,width:img.naturalWidth,height:img.naturalHeight,draggable:img.draggable,alt:img.alt,src:img.src};});
  assert.ok(observed.bytes>0&&observed.bytes<=16*1024*1024);assert.ok(observed.width>0&&observed.height>0&&observed.width<=1600&&observed.height<=1600);assert.equal(observed.draggable,false);
  const actual=await response(`/api/invoices/${row.id}/preview/pages/${number}?sha256=${row.pdf_sha256}`),binary=await actual.body();
  assert.equal(actual.headers()['x-pdf-sha256'],row.pdf_sha256);assert.equal(actual.headers()['x-pdf-page'],String(number));assert.equal(sha(binary),observed.sha256,'Visible image must be the exact server PNG for this retained PDF/page');
  assert.equal(await page.evaluate(()=>previewUrls.active.size),1,'At most one inline object URL remains live');
  pages.push({invoice_id:row.id,pdf_sha256:row.pdf_sha256,page:number,png_sha256:observed.sha256,png_bytes:observed.bytes,width:observed.width,height:observed.height});
  return observed;
 };
 const noOverflow=async()=>{assert.equal(await page.locator('body').evaluate(el=>el.scrollWidth>innerWidth),false);assert.equal(await top().evaluate(el=>el.scrollWidth>el.clientWidth),false);};
 const screenshot=async name=>{await page.locator('#toast').waitFor({state:'hidden'});await top().locator('.swipe-pdf-title').evaluate(el=>el.scrollIntoView({block:'start'}));await page.evaluate(()=>scrollBy(0,-65));await page.screenshot({path:path.join(out,name),fullPage:false,animations:'disabled'});};
 await page.goto(origin+'/#invoices');await page.locator('[data-login-mode=legacy]').click();await page.locator('#access-token').fill(token);await page.locator('#login-form button[type=submit]').click();await page.locator('.invoice-card').first().waitFor();
 const initialQuery=calls.filter(x=>x.path==='/api/invoices').at(-1).query,initial=await get('/api/invoices'+initialQuery);assert.ok(initial.length>=2&&initial.length<=50);
 let list=initial,first=list[0],metadata=await get(`/api/invoices/${first.id}/preview`);
 if(metadata.page_count<2){
  const candidate=list.filter(row=>row.pdf_ready&&row.line_count>25).sort((a,b)=>b.line_count-a.line_count)[0];assert.ok(candidate,'A live multipage example is needed');
  await Promise.all([page.waitForResponse(r=>new URL(r.url()).pathname==='/api/invoices'&&new URL(r.url()).searchParams.get('customer_id')===candidate.customer_id),page.locator('[data-customer-filter=invoices]').selectOption(candidate.customer_id)]);
  await page.waitForFunction(()=>document.querySelector('#tab-content')?.getAttribute('aria-busy')==='false');
  const query=calls.filter(x=>x.path==='/api/invoices').at(-1).query;list=await get('/api/invoices'+query);first=list[0];metadata=await get(`/api/invoices/${first.id}/preview`);
 }
 assert.ok(list.length>=2);assert.ok(metadata.page_count>=2);assert.equal(metadata.pdf_sha256,first.pdf_sha256);assert.equal(metadata.source,'EXACT_RETAINED_PDF');
 const pdf=(await response(`/api/invoices/${first.id}/pdf`));assert.equal(sha(await pdf.body()),first.pdf_sha256,'Original PDF bytes must match the frozen row hash');
 const beforePreview=calls.filter(x=>/\/preview(?:\/|$)/.test(x.path)).length;
 await page.locator('[data-invoice-layout=swipe]').click();const firstImage=await verifyImage(first,1);
 assert.equal(await top().locator('.swipe-pdf-navigation [data-swipe-pdf-page="0"]').isDisabled(),true);
 assert.equal(await page.locator('[data-deck-action=review]').isDisabled(),true);assert.equal(await page.locator('[data-deck-action=reject]').isDisabled(),true);
 const requested=calls.filter(x=>/\/preview(?:\/|$)/.test(x.path)).slice(beforePreview);assert.equal(requested.length,2,'Active first card loads metadata and exactly one page');assert.ok(requested.every(x=>x.path.startsWith(`/api/invoices/${first.id}/preview`)));
 assert.equal(calls.filter(x=>/\/package$/.test(x.path)).length,0,'Inline preview does not fetch the full invoice package');
 assert.match(await top().locator('.swipe-pdf-navigation').innerText(),new RegExp(`Page 1 of ${metadata.page_count}`));await noOverflow();await screenshot('swipe-pdf-live-desktop-page1.png');
 await top().locator('[data-swipe-pdf-page="2"]').click();const secondPage=await verifyImage(first,2);assert.notEqual(secondPage.sha256,firstImage.sha256);assert.ok(await page.evaluate(url=>previewUrls.revoked.includes(url),firstImage.src));
 await screenshot('swipe-pdf-live-desktop-page2.png');await top().locator('[data-swipe-pdf-page="1"]').click();assert.equal((await verifyImage(first,1)).sha256,firstImage.sha256);
 checks.push('The active card displays exact retained PDF pages; page 1/2/1 match server PNG bytes and original PDF SHA, with one image and no underlayer requests.');
 await page.locator('[data-deck-action=later]').click();await page.waitForFunction(id=>document.querySelector('.card-deck-layer[data-layer="0"]')?.dataset.cardKey!==id,first.id);const next=list[1];const nextImage=await verifyImage(next,1);assert.notEqual(next.id,first.id);
 const nextPdf=await response(`/api/invoices/${next.id}/pdf`);assert.equal(sha(await nextPdf.body()),next.pdf_sha256);assert.equal(await page.locator('[data-deck-count=deferred]').innerText(),'1');
 await screenshot('swipe-pdf-live-next-invoice.png');
 await page.locator('[data-swipe-view=deferred]').click();await verifyImage(first,1);await page.locator('[data-swipe-return]').click();await page.locator('[data-swipe-view=pending]').click();await verifyImage(first,1);assert.equal(await page.locator('[data-deck-count=deferred]').innerText(),'0');
 checks.push('Shared-access Later swaps the exact active invoice/PDF locally; Later view and Return restore the original invoice without saved postponements.');
 const listReads=calls.filter(x=>x.path==='/api/invoices').length;
 await page.locator('[data-invoice-layout=cards]').click();assert.equal(await page.locator('.swipe-pdf-image').count(),0);assert.equal(await page.evaluate(()=>previewUrls.active.size),0,'Layout change revokes the inline object URL');
 await page.locator('[data-invoice-layout=swipe]').click();await verifyImage(first,1);assert.equal(calls.filter(x=>x.path==='/api/invoices').length,listReads);
 await page.setViewportSize({width:390,height:844});await noOverflow();await screenshot('swipe-pdf-live-mobile.png');
 await top().locator('[data-swipe-pdf-page="2"]').click();await verifyImage(first,2);await screenshot('swipe-pdf-live-mobile-page2.png');
 await page.locator('[data-tab=review]').first().click();await page.locator('.review-card').first().waitFor();assert.equal(await page.locator('.swipe-pdf-image').count(),0);assert.equal(await page.evaluate(()=>previewUrls.active.size),0,'Tab navigation revokes the last image');
 checks.push('Desktop/mobile preview has no horizontal overflow; layout switch and tab navigation destroy the active image and revoke its URL.');
 assert.deepEqual(await get('/api/invoices'+initialQuery),initial);assert.equal(sha(await(await response(`/api/invoices/${first.id}/pdf`)).body()),first.pdf_sha256);
 assert.equal(blocked.length,0);assert.equal(errors.length,0);const writes=calls.filter(x=>!['GET','HEAD'].includes(x.method));assert.deepEqual(writes.map(x=>[x.method,x.path]),[['POST','/api/login']]);assert.deepEqual(sourceHashes(),sourceBefore,'Source files changed during verification');
 const urlLifecycle=await page.evaluate(()=>({created:previewUrls.created.length,revoked:previewUrls.revoked.length,active:previewUrls.active.size}));assert.equal(urlLifecycle.created,urlLifecycle.revoked);
 const receipt={pass:true,scope:'Real local exact-PDF browser verification, shared read-only login; all other non-GET requests blocked. No approval, reject, durable postpone, source or delivery write.',origin,created_at:new Date().toISOString(),checks,invoice_count:initial.length,first_invoice_id:first.id,second_invoice_id:next.id,page_count:metadata.page_count,first_pdf_sha256:first.pdf_sha256,second_pdf_sha256:next.pdf_sha256,observed_pages:pages,invoice_projection_sha256:hash(initial),invoice_projection_unchanged:true,original_pdf_sha_unchanged:true,max_inline_images:1,max_card_layers:3,no_underlayer_preview_requests:true,desktop_mobile_no_overflow:true,url_lifecycle:urlLifecycle,browser_requests:calls.length,direct_gets:direct.length,requests_by_path:Object.fromEntries([...new Set(calls.map(x=>x.path))].map(p=>[p,calls.filter(x=>x.path===p).length])),writes,blocked,errors,source_sha256:sourceBefore};
 fs.writeFileSync(path.join(out,'swipe-pdf-live-smoke.json'),JSON.stringify(receipt,null,2)+'\n');console.log(JSON.stringify({pass:true,browser_requests:calls.length,direct_gets:direct.length,page_count:metadata.page_count,verified_pages:pages.length,url_lifecycle:urlLifecycle,receipt:'output/verification/swipe-pdf-live-smoke.json'}));
}
run().catch(error=>{fs.writeFileSync(path.join(out,'swipe-pdf-live-smoke.json'),JSON.stringify({pass:false,error:String(error),browser_requests:calls.length,direct_gets:direct.length,checks,blocked,errors},null,2)+'\n');console.error(String(error));process.exitCode=1;}).finally(async()=>{if(browser)await browser.close();});
