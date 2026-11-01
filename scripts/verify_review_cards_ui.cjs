// All browser requests are intercepted. This fixture never contacts the live app.
const {chromium} = require('playwright');
const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');
const assert = require('node:assert/strict');
const root = path.resolve(__dirname, '..');
const output = path.join(root, 'output/verification');
fs.mkdirSync(output, {recursive:true});
const receiptPath = path.join(output, 'review-cards-ui-smoke.json');
const hashes = Object.fromEntries(['src/invoice_optimizer/static/app.js','src/invoice_optimizer/static/app.css','src/invoice_optimizer/templates/index.html']
  .map(name => [name, crypto.createHash('sha256').update(fs.readFileSync(path.join(root,name))).digest('hex')]));
let browser;
const calls = [], errors = [], external = [], unexpected = [], checks = [];
const until = async predicate => {
  for (let count=0; count<600; count++) {
    if (await predicate()) return;
    await new Promise(resolve => setTimeout(resolve,10));
  }
  throw new Error('Bounded fixture wait exceeded six seconds');
};
const casesGetCount = () => calls.filter(call => call.method==='GET' && call.path==='/api/cases').length;
const detailCalls = () => calls.filter(call => /^\/api\/cases\/case-[0-9]+$/.test(call.path));
const lazyCalls = () => calls.filter(call => /^\/api\/cases\/case-[0-9]+(?:\/.*)?$/.test(call.path) || call.path==='/api/model');
(async () => {
  browser = await chromium.launch({headless:true,...(process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE?{executablePath:process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE}:{})});
  const page = await browser.newPage({viewport:{width:1440,height:1100}});
  page.setDefaultTimeout(8000);
  let role = 'admin', listGate = null;
  const sourceA = {id:'fixture-A',source:'A',period:'2026-09',original_name:'Recorded source A.csv',download_url:'/api/batches/fixture-A/file'};
  const sourceB = {id:'fixture-B',source:'B',period:'2026-09',original_name:'Recorded source B.csv',download_url:'/api/batches/fixture-B/file'};
  const customers = [{id:'C1',legal_name:'Aster Services Ltd · synthetic fixture'}, {id:'C2',legal_name:'Birch Trading Ltd · synthetic fixture'}];
  const records = Array.from({length:51}, (_, index) => {
    const n=index+1;
    return {id:'case-'+n,movement_id:String(n).padStart(64,'0'),external_movement_id:'MOV-'+String(n).padStart(4,'0'),
      customer_id:n<=25?'C1':'C2',period:'2026-09',reason:'MODEL_RISK',severity:'MEDIUM',status:'OPEN',
      version_id:'version-'+n,version_no:6,current_version:6,score:.81,release_eligible:true,
      source_files:[sourceA,sourceB],provenance_status:'COMPLETE',
      transaction:{amount:'1234.56',currency:'GBP',source_a_amount:'1234.56',source_b_amount:'1234.56',source_a_currency:'GBP',source_b_currency:'GBP',description:'Recorded synthetic transaction',event_at:'2026-09-15T12:00:00Z',supplier_id:'SUP-01'},
      model:{status:'SCORED',recorded_score:.81,threshold:.6,threshold_status:'RECORDED_RECEIPT',model_version:'lr-recorded-fixture',policy_version:'policy-recorded-fixture',recorded_reasons:['MODEL_RISK'],flagged:true},
      rule:{status:'PASS',recorded_reasons:[]}};
  });
  Object.assign(records[0], {reason:'CURRENCY_MISMATCH',severity:'HIGH',release_eligible:false,
    transaction:{...records[0].transaction,amount:'9007199254740993.27',source_a_amount:'9007199254740993.27',source_b_amount:'9007199254740992.01',source_b_currency:'EUR'},
    rule:{status:'FAIL',recorded_reasons:['CURRENCY_MISMATCH']}});
  Object.assign(records[1], {reason:'AMOUNT_MISMATCH',score:0,release_eligible:false,
    model:{...records[1].model,recorded_score:0,threshold:0,flagged:true},rule:{status:'FAIL',recorded_reasons:['AMOUNT_MISMATCH']}});
  Object.assign(records[2], {reason:'MODEL_UNAVAILABLE',score:null,release_eligible:false,
    transaction:{...records[2].transaction,amount:null,source_a_amount:null,source_b_amount:'0.00'},
    model:{status:'UNAVAILABLE',recorded_score:null,threshold:null,threshold_status:'UNAVAILABLE',model_version:null,policy_version:null,recorded_reasons:['MISSING_FEATURE: event_at'],flagged:false}});
  Object.assign(records[4], {version_no:2,current_version:3,release_eligible:false,
    model:{...records[4].model,model_version:'lr-frozen-case-version-2'}});
  records[0].model.recorded_reasons=['MODEL_RISK','FEATURE_SNAPSHOT_OLD'];
  records[0].rule.recorded_reasons=['CURRENCY_MISMATCH','AMOUNT_MISMATCH'];
  Object.assign(records[6],{reason:'MODEL_UNAVAILABLE',release_eligible:false,model:{status:'UNAVAILABLE',recorded_score:null,threshold:null,threshold_status:'UNAVAILABLE',model_version:null,recorded_reasons:['MISSING_FEATURE'],flagged:false}});
  records[5].external_movement_id='MOV-escaped-<script>window.fixtureInjected=true</script>';
  const session = () => ({authenticated:true,csrf_token:'review-cards-csrf',identity_type:role==='legacy'?'legacy':'operator',can_review:role!=='legacy',
    actor:'user:fixture-admin',user_id:'fixture-admin',username:'fixture.admin',display_name:'Fixture Administrator',role:role==='legacy'?null:role});
  const json = (route, body, status=200) => route.fulfill({status,contentType:'application/json',body:JSON.stringify(body)});
  page.on('pageerror', error => errors.push(String(error)));
  await page.addInitScript(() => {
    window.streams=[];
    window.EventSource=class {constructor(){this.listeners={};window.streams.push(this);} addEventListener(name,callback){this.listeners[name]=callback;} close(){this.closed=true;}};
    window.pushFixtureUpdate=revision=>window.streams.at(-1).listeners.update({data:JSON.stringify({revision,stream_version:revision+':1',changed_resources:['cases']})});
  });
  await page.route('**/*', async route => {
    const request=route.request(), url=new URL(request.url()), endpoint=url.pathname;
    if(url.origin!=='http://invoice.test'){external.push(url.origin);return route.abort();}
    calls.push({path:endpoint,query:url.search,method:request.method()});
    if(request.method()!=='GET') {
      unexpected.push({method:request.method(),path:endpoint});
      return json(route,{detail:'Fixture rejects every mutation request'},409);
    }
    if(endpoint==='/')return route.fulfill({contentType:'text/html',body:fs.readFileSync(path.join(root,'src/invoice_optimizer/templates/index.html'),'utf8')});
    if(endpoint.startsWith('/static/'))return route.fulfill({contentType:endpoint.endsWith('.css')?'text/css':'application/javascript',body:fs.readFileSync(path.join(root,'src/invoice_optimizer',endpoint),'utf8')});
    if(endpoint==='/api/auth/status')return json(route,{setup_required:false});
    if(endpoint==='/api/session')return json(route,session());
    if(endpoint==='/api/customers')return json(route,customers);
    if(endpoint==='/api/dashboard')return json(route,{mode:'SIMULATION',counts:{open_cases:51,movements:51,customers:2,pending_approvals:0},periods:['2026-09'],revision:1,worker:{status:'RUNNING',stale:false}});
    if(endpoint==='/api/cases') {
      assert.equal(url.searchParams.get('limit'),'50');
      const selected=records.filter(row=>!url.searchParams.has('customer_id') || row.customer_id===url.searchParams.get('customer_id'));
      const offset=Number(url.searchParams.get('offset'));
      if(listGate){const waiting=listGate;listGate=null;waiting.started=true;await waiting.promise;}
      return json(route,selected.slice(offset,offset+50));
    }
    if(endpoint==='/api/case-drivers') {
      // Card layout batches read-only driver evidence for the visible page (<=50 unique ids).
      const ids=url.searchParams.getAll('case_id');assert.ok(ids.length>0 && ids.length<=50);assert.equal(ids.length,new Set(ids).size);
      return json(route,{items:ids.map(id=>{const row=records.find(item=>item.id===id);assert.ok(row);
        return {case_id:id,version_id:row.version_id,status:'UNAVAILABLE',reason:'Bounded fixture does not retain driver evidence.',features:[]};}),read_only:true});
    }
    const matched=endpoint.match(/^\/api\/cases\/(case-[0-9]+)(\/.*)?$/);
    if(matched) {
      const row=records.find(item=>item.id===matched[1]);assert.ok(row);
      if(!matched[2])return json(route,{case:row,movement:{id:row.movement_id,external_id:row.external_movement_id,current_version:row.current_version},
        version:{id:row.version_id,version_no:row.version_no,payload:{...row.transaction,source_values:{A:{amount:row.transaction.source_a_amount,currency:row.transaction.source_a_currency},B:{amount:row.transaction.source_b_amount,currency:row.transaction.source_b_currency}}}},
        assessments:[{kind:'RULE',status:row.rule.status,reasons:row.rule.recorded_reasons},{kind:'MODEL',status:row.model.status,score:row.model.recorded_score,model_version:row.model.model_version,reasons:row.model.recorded_reasons}],source_files:[sourceA,sourceB]});
      if(matched[2]==='/model/explanation')return json(route,{case_id:row.id,version_no:row.version_no,case_reason:row.reason,recorded_score:row.model.recorded_score,preview_available:false,
        explanation:{status:'UNAVAILABLE',reason:'Bounded fixture supplies list evidence; technical explanation is not retained.'},
        narrative:{status:'UNAVAILABLE',headline:'Inspect the recorded case evidence.',summary:'This synthetic fixture does not assert a model explanation.',drivers:[],mitigating_drivers:[]}});
    }
    unexpected.push({method:request.method(),path:endpoint});
    return json(route,{detail:'Unexpected fixture request'},404);
  });

  await page.goto('http://invoice.test/#review');
  await until(async()=>await page.locator('.review-card').count()===50);
  assert.equal(await page.locator('[data-review-layout=cards]').getAttribute('aria-pressed'),'true');
  assert.equal(await page.locator('.review-table').count(),0);
  assert.equal(lazyCalls().length,0);
  assert.equal(await page.evaluate(()=>window.fixtureInjected),undefined);
  const card=id=>page.locator(`[data-review-card="${id}"]`);
  assert.match(await card('case-1').locator('.review-transaction > div > strong').first().innerText(),/£9,007,199,254,740,993\.27/);
  const sources=card('case-1').locator('.review-source-amounts strong');
  assert.equal(await sources.nth(0).innerText(),'£9,007,199,254,740,993.27');
  assert.equal(await sources.nth(1).innerText(),'EUR 9,007,199,254,740,992.01');
  assert.match(await card('case-1').locator('.review-case-reason').innerText(),/Currency mismatch[\s\S]*Source checks: Fail/);
  assert.match(await card('case-1').innerText(),/Logistic regression · lr-recorded-fixture/);
  assert.match(await card('case-1').locator('.review-recorded-reasons').innerText(),/Model risk[\s\S]*Feature snapshot old[\s\S]*Amount mismatch/);
  assert.doesNotMatch(await card('case-7').locator('.review-model-score').innerText(),/81\.0%/);
  assert.match(await card('case-1').locator('.review-model-score').innerText(),/p = 0\.81[\s\S]*p = 0\.6/);
  assert.match(await card('case-2').locator('.review-model-score').innerText(),/0\.0%[\s\S]*p = 0/);
  assert.equal(await card('case-2').locator('.score.missing').count(),0);
  assert.match(await card('case-3').locator('.review-transaction').innerText(),/Amount unavailable/);
  assert.match(await card('case-3').locator('.review-model-score').innerText(),/p = Unavailable/);
  assert.doesNotMatch(await card('case-3').locator('.review-model-score').innerText(),/0\.0%/);
  assert.match(await card('case-5').locator('.review-version-label').innerText(),/Case version 2 · current 3/);
  assert.match(await card('case-5').innerText(),/lr-frozen-case-version-2/);
  assert.equal(await page.locator('[data-select-case="case-5"]').isDisabled(),true);
  for(const id of ['case-1','case-3','case-5'])assert.equal(await page.locator(`[data-select-case="${id}"]`).isDisabled(),true);
  assert.equal(await card('case-1').locator('a[href="/api/batches/fixture-A/file"]').count(),1);
  checks.push('Cards default; exact decimal strings above 2^53; own source currencies; zero and unavailable remain distinct; recorded model, case reason, threshold, source files and case/current versions');
  await page.screenshot({path:path.join(output,'review-cards-desktop-fixture.png'),fullPage:false});

  await page.locator('[data-inspect-case="case-2"]').check();
  await page.locator('[data-select-case="case-4"]').check();
  const beforeSwitch=casesGetCount();
  await page.locator('[data-review-layout=list]').click();
  assert.equal(await page.locator('.review-table').count(),1);
  const unavailableTable=page.locator('.review-table tr').filter({has:page.locator('[data-case="case-7"]')});
  assert.equal(await unavailableTable.locator('.score.missing').count(),1);
  assert.doesNotMatch(await unavailableTable.innerText(),/81\.0%/);
  assert.equal(await page.locator('[data-inspect-case="case-2"]').isChecked(),true);
  assert.equal(await page.locator('[data-select-case="case-4"]').isChecked(),true);
  await page.locator('[data-review-layout=cards]').click();
  assert.equal(await page.locator('[data-inspect-case="case-2"]').isChecked(),true);
  assert.equal(await page.locator('[data-select-case="case-4"]').isChecked(),true);
  assert.equal(casesGetCount(),beforeSwitch);
  assert.equal(lazyCalls().length,0);
  await page.evaluate(()=>{window.pushFixtureUpdate(8);window.pushFixtureUpdate(9);});
  await page.locator('#update-notice').waitFor({state:'visible'});
  assert.equal(casesGetCount(),beforeSwitch);
  assert.equal(await page.locator('[data-select-case="case-4"]').isChecked(),true);
  await page.locator('#bulk-release-open').click();
  await page.locator('#bulk-release-form').waitFor();
  assert.equal(await page.locator('#bulk-release-submit').isDisabled(),true);
  assert.equal(await page.locator('#bulk-release-confirm').isChecked(),false);
  assert.match(await page.locator('.bulk-selection-table').innerText(),/case-4[\s\S]*6/);
  await page.locator('#bulk-release-reason').fill('Fixture checks reviewed, without submitting this action.');
  await page.locator('#bulk-release-confirm').check();
  assert.equal(await page.locator('#bulk-release-submit').isDisabled(),false);
  await page.locator('#bulk-release-confirm').uncheck();
  assert.equal(await page.locator('#bulk-release-submit').isDisabled(),true);
  await page.locator('#close-detail').click();
  checks.push('Cards/List causes zero GETs and preserves separate inspect/release selections; SSE preserves frozen selections; bulk confirmation remains explicit and unchecked');

  let releaseList;
  const waiting={started:false,promise:new Promise(resolve=>releaseList=resolve)};
  listGate=waiting;
  await page.locator('#review-customer').selectOption('C2');
  await until(()=>waiting.started);
  const gatedCount=casesGetCount();
  await page.locator('[data-review-layout=list]').click();
  assert.equal(await page.locator('[data-review-layout=cards]').getAttribute('aria-pressed'),'true');
  assert.equal(await page.locator('[data-inspect-case="case-2"]').isDisabled(),true);
  releaseList();
  await until(async()=>await page.locator('.review-card').count()===26 && await page.locator('#tab-content').getAttribute('aria-busy')!=='true');
  assert.equal(casesGetCount(),gatedCount);
  assert.equal(await page.locator('[data-inspect-case]:checked').count(),0);
  assert.equal(await page.locator('[data-select-case]:checked').count(),0);
  assert.ok(calls.filter(call=>call.path==='/api/cases').at(-1).query.includes('customer_id=C2'));
  assert.match(await page.locator('.review-card h3').first().innerText(),/Birch/);
  await page.locator('#review-customer').selectOption('');
  await until(async()=>await page.locator('.review-card').count()===50);
  await page.locator('[data-page=next]').click();
  await until(async()=>await page.locator('.review-card').count()===1);
  assert.equal(await card('case-51').count(),1);
  assert.ok(calls.filter(call=>call.path==='/api/cases').at(-1).query.includes('offset=50'));
  assert.equal(await page.locator('[data-page=next]').isDisabled(),true);
  await page.locator('[data-page=previous]').click();
  await until(async()=>await page.locator('.review-card').count()===50);
  assert.equal(lazyCalls().length,0);
  checks.push('Loading guard rejects stale layout interaction; client filter and 50-row pagination use bounded requests and clear selections');

  await page.setViewportSize({width:390,height:844});
  assert.equal(await page.locator('body').evaluate(element=>element.scrollWidth>innerWidth),false);
  assert.equal(await page.locator('#review-presentation').evaluate(element=>element.scrollWidth>element.clientWidth),false);
  assert.equal(await card('case-1').evaluate(element=>element.scrollWidth>element.clientWidth),false);
  await card('case-1').scrollIntoViewIfNeeded();
  await page.screenshot({path:path.join(output,'review-cards-mobile-fixture.png'),fullPage:false});
  await page.setViewportSize({width:1440,height:1100});
  await page.locator('#inspect-page-cases').check();
  assert.equal(await page.locator('[data-inspect-case]:checked').count(),8);
  await page.locator('[data-inspect-case="case-9"]').click();
  assert.equal(await page.locator('[data-inspect-case="case-9"]').isChecked(),false);
  assert.match(await page.locator('#toast').innerText(),/at most 8/);
  assert.equal(await page.locator('[data-select-case]:checked').count(),0);
  await page.locator('[data-review-layout=list]').click();
  assert.equal(await page.locator('[data-inspect-case]:checked').count(),8);
  await page.locator('[data-review-layout=cards]').click();
  assert.equal(await page.locator('[data-inspect-case]:checked').count(),8);
  assert.equal(lazyCalls().length,0);
  await page.locator('#open-inspected-cases').click();
  await until(()=>detailCalls().length===8);
  await page.locator('#detail-dialog').waitFor({state:'visible'});
  await until(async()=>await page.locator('#dialog-case-tabs [data-workbench-case]').count()===8);
  assert.equal(new Set(detailCalls().map(call=>call.path)).size,8);
  assert.equal(calls.filter(call=>call.path.endsWith('/versions')).length,0);
  assert.equal(calls.filter(call=>call.path==='/api/model').length,0);
  assert.equal(calls.filter(call=>call.path.endsWith('/model/explanation')).length,1);
  await page.locator('#close-detail').click();
  checks.push('Mobile cards do not overflow; inspection stays read-only, caps at eight, fetches eight bounded details only after explicit open, and never eagerly fetches history/current model');

  role='legacy';
  // Inspecting a case sets its deep link. Start the new identity on the queue,
  // otherwise the app correctly restores that case dialog after the reload.
  await page.evaluate(()=>history.replaceState(null,'','#review'));
  await page.reload();
  await until(async()=>await page.locator('.review-card').count()===50);
  assert.equal(await page.locator('[data-review-layout=cards]').getAttribute('aria-pressed'),'true');
  assert.equal(await page.locator('[data-select-case]:not(:disabled)').count(),0);
  assert.equal(await page.locator('#select-page-cases').isDisabled(),true);
  assert.equal(await page.locator('#bulk-release-open').isDisabled(),true);
  assert.equal(await page.locator('[data-inspect-case="case-4"]').isDisabled(),false);
  await page.locator('[data-review-layout=list]').click();
  assert.equal(await page.locator('[data-select-case]:not(:disabled)').count(),0);
  assert.equal(await page.locator('#bulk-release-open').isDisabled(),true);
  checks.push('Shared/non-personal access cannot select or submit releases in either layout; inspection remains available');
  assert.equal(calls.filter(call=>call.method!=='GET').length,0);
  assert.deepEqual(unexpected,[]);assert.deepEqual(external,[]);assert.deepEqual(errors,[]);
  const receipt={pass:true,scope:'Fully intercepted local browser fixtures; no live network, database writes, release, email, training or invoice action.',
    checks,case_fixture_count:records.length,max_list_rows:50,explicit_detail_requests:detailCalls().length,
    method_counts:Object.fromEntries([...new Set(calls.map(call=>call.method))].map(method=>[method,calls.filter(call=>call.method===method).length])),
    errors,external_requests:external,unexpected_requests:unexpected,source_sha256:hashes,
    screenshots:['review-cards-desktop-fixture.png','review-cards-mobile-fixture.png'],calls};
  fs.writeFileSync(receiptPath,JSON.stringify(receipt,null,2)+'\n');
  console.log(JSON.stringify({pass:true,checks:checks.length,receipt:receiptPath}));
})().catch(error=>{
  const receipt={pass:false,error:String(error.stack || error),checks,errors,external_requests:external,unexpected_requests:unexpected,source_sha256:hashes,calls};
  fs.writeFileSync(receiptPath,JSON.stringify(receipt,null,2)+'\n');
  console.error(error);process.exitCode=1;
}).finally(async()=>{if(browser)await browser.close();});
