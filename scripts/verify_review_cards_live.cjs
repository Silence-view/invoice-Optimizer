// Read-only smoke against the local running console. No review or delivery writes.
const {chromium}=require('playwright');
const fs=require('node:fs'),path=require('node:path'),assert=require('node:assert/strict'),crypto=require('node:crypto');
const root=path.resolve(__dirname,'..'),out=path.join(root,'output/verification'),origin='http://127.0.0.1:8765';
const token=fs.readFileSync(path.join(root,'.env'),'utf8').match(/^INVOICE_API_TOKEN=(.*)$/m)?.[1].trim().replace(/^['"]|['"]$/g,'');
assert.ok(token,'Existing local read-only sign-in credential required');
const hash=value=>crypto.createHash('sha256').update(JSON.stringify(value)).digest('hex');
(async()=>{
 const browser=await chromium.launch({headless:true}),context=await browser.newContext({viewport:{width:1440,height:1060}}),page=await context.newPage();
 const calls=[],blocked=[],errors=[];let directGets=0;
 page.on('pageerror',error=>errors.push(String(error)));
 await context.route('**/*',route=>{
  const req=route.request(),url=new URL(req.url());calls.push({method:req.method(),path:url.pathname,query:url.search});
  if(url.origin!==origin || (!['GET','HEAD'].includes(req.method()) && !(req.method()==='POST' && url.pathname==='/api/login'))){blocked.push({method:req.method(),path:url.pathname});return route.abort();}
  return route.continue();
 });
 const get=async endpoint=>{assert.ok(endpoint.startsWith('/api/'));directGets++;const res=await context.request.get(origin+endpoint);assert.ok(res.ok(),endpoint);return res.json();};
 await page.goto(origin+'/#review');await page.locator('[data-login-mode="legacy"]').click();await page.locator('#access-token').fill(token);await page.locator('#login-form button[type=submit]').click();await page.locator('.review-card').first().waitFor();
 assert.equal(await page.locator('[data-review-layout="cards"]').getAttribute('aria-pressed'),'true');
 const initialQuery=calls.filter(x=>x.path==='/api/cases').at(-1).query,initial=await get('/api/cases'+initialQuery);
 assert.equal(await page.locator('.review-card').count(),initial.length);assert.ok(initial.length<=50 && initial.length>0);
 assert.equal(calls.filter(x=>/^\/api\/cases\/[^/]+/.test(x.path)).length,0,'No per-card detail/model/history fetch');
 for(const row of initial){
  const card=page.locator(`[data-review-card="${row.id}"]`),body=await card.innerText();
  if(row.version_no!=null)assert.ok(body.includes((row.current_version===row.version_no?'Current version ':'Case version ')+row.version_no));
  assert.ok(body.includes(row.model.model_version || 'model unavailable'));
  if(row.model.recorded_score!=null)assert.ok(body.includes((row.model.recorded_score*100).toFixed(1)+'%'));
  else assert.match(await card.locator('.review-model-score').innerText(),/Unavailable/);
  assert.equal(await card.locator('[data-select-case]').isDisabled(),true,'Shared access cannot approve releases');
  for(const file of row.source_files.slice(0,2))assert.ok(await card.locator(`a[href="${file.download_url}"]`).count()>0);
 }
 const id=initial[0].id;await page.locator(`[data-inspect-case="${id}"]`).check();const beforeSwitch=calls.filter(x=>x.path==='/api/cases').length;
 await page.locator('[data-review-layout="list"]').click();assert.equal(await page.locator('.review-table tbody tr').count(),initial.length);assert.equal(await page.locator(`[data-inspect-case="${id}"]`).isChecked(),true);
 await page.locator('[data-review-layout="cards"]').click();assert.equal(await page.locator(`[data-inspect-case="${id}"]`).isChecked(),true);assert.equal(calls.filter(x=>x.path==='/api/cases').length,beforeSwitch);
 await page.locator(`[data-inspect-case="${id}"]`).uncheck();
 // Filter to a customer with a recorded model result; display the actual list's values.
 const chosen=initial.find(x=>x.model.recorded_score!=null && x.customer_id);assert.ok(chosen,'Live demo needs at least one scored case');
 await Promise.all([page.waitForResponse(r=>new URL(r.url()).pathname==='/api/cases' && new URL(r.url()).searchParams.get('customer_id')===chosen.customer_id),page.locator('[data-customer-filter="review"]').selectOption(chosen.customer_id)]);
 await page.locator('.review-card').first().waitFor();const filteredQuery=calls.filter(x=>x.path==='/api/cases').at(-1).query,filtered=await get('/api/cases'+filteredQuery);
 assert.ok(filtered.every(x=>x.customer_id===chosen.customer_id));assert.equal(await page.locator('.review-card').count(),filtered.length);
 const picked=filtered.find(x=>x.model.recorded_score!=null),detail=await get('/api/cases/'+picked.id),recorded=detail.assessments.find(x=>x.kind==='MODEL');
 assert.equal(picked.version_id,detail.version.id);assert.equal(picked.transaction.amount,detail.version.amount);assert.equal(picked.model.recorded_score,recorded.score);assert.equal(picked.model.model_version,recorded.model_version);
 for(const side of ['A','B']){const s=side.toLowerCase(),source=detail.version.payload.source_values?.[side];assert.equal(picked.transaction[`source_${s}_amount`],source?.amount ?? null);assert.equal(picked.transaction[`source_${s}_currency`],source?.currency ?? null);}
 await page.locator('.review-card').first().scrollIntoViewIfNeeded();await page.screenshot({path:path.join(out,'review-cards-live-desktop.png'),fullPage:false});
 await page.setViewportSize({width:390,height:844});assert.equal(await page.locator('body').evaluate(e=>e.scrollWidth>innerWidth),false);assert.equal(await page.locator('#review-presentation').evaluate(e=>e.scrollWidth>e.clientWidth),false);await page.locator('.review-card').first().scrollIntoViewIfNeeded();await page.screenshot({path:path.join(out,'review-cards-live-mobile.png'),fullPage:false});
 await page.setViewportSize({width:1440,height:1060});await page.locator(`[data-case="${picked.id}"]`).click();await page.locator('#case-version-browser').waitFor();assert.equal(await page.locator('#resolve-form button[type=submit]').isDisabled(),true);await page.locator('#close-detail').click();
 assert.deepEqual(await get('/api/cases'+initialQuery),initial,'Read-only inspection cannot alter case projection');assert.equal(blocked.length,0);assert.equal(errors.length,0);
 const writes=calls.filter(x=>!['GET','HEAD'].includes(x.method));assert.deepEqual(writes.map(x=>[x.method,x.path]),[['POST','/api/login']]);
 const receipt={pass:true,scope:'Live localhost, existing read-only session permissions; sole POST is shared sign-in. No reviews, approvals, source ingestion or delivery.',origin,cards:initial.length,customer:chosen.customer_id,filtered_cards:filtered.length,case_checked:picked.id,recorded_score:picked.model.recorded_score,recorded_model:picked.model.model_version,amount:picked.transaction.amount,case_projection_sha256:hash(initial),case_projection_unchanged:true,cards_list_extra_gets:0,direct_gets:directGets,browser_requests:calls.length,requests_by_path:Object.fromEntries([...new Set(calls.map(x=>x.path))].map(p=>[p,calls.filter(x=>x.path===p).length])),writes,blocked,errors};
 fs.writeFileSync(path.join(out,'review-cards-live-smoke.json'),JSON.stringify(receipt,null,2)+'\n');console.log(JSON.stringify(receipt,null,2));await browser.close();
})().catch(error=>{console.error(error);process.exit(1);});
