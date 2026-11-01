// Independent bounded browser fixtures. Every request is intercepted; no live service is contacted.
const {chromium}=require('playwright');
const fs=require('node:fs'),path=require('node:path'),crypto=require('node:crypto'),assert=require('node:assert/strict');
const root=path.resolve(__dirname,'..'),out=process.env.INVOICE_VERIFY_OUTPUT || path.join(root,'output/verification');
fs.mkdirSync(out,{recursive:true});
const receiptPath=path.join(out,'card-deck-ui-smoke.json');
const moduleOnly=process.argv.includes('--module-only');
const sourceFiles=moduleOnly?['src/invoice_optimizer/static/card_deck.js','src/invoice_optimizer/static/card_deck.css']:['src/invoice_optimizer/static/app.js','src/invoice_optimizer/static/app.css','src/invoice_optimizer/static/card_deck.js','src/invoice_optimizer/static/card_deck.css','src/invoice_optimizer/static/swipe_workspace.js','src/invoice_optimizer/static/risk_bars.js','src/invoice_optimizer/static/risk_bars.css','src/invoice_optimizer/static/swipe_pdf.js','src/invoice_optimizer/static/swipe_pdf.css','src/invoice_optimizer/templates/index.html'];
const hashSources=()=>Object.fromEntries(sourceFiles.map(file=>[file,crypto.createHash('sha256').update(fs.readFileSync(path.join(root,file))).digest('hex')]));
const checks=[],calls=[],errors=[],external=[],unexpected=[];
let browser,sourceHashes;
async function until(predicate,description='condition'){
  for(let n=0;n<800;n++){if(await predicate())return;await new Promise(resolve=>setTimeout(resolve,10));}
  throw Error('Bounded wait exceeded eight seconds: '+description);
}
const writeCount=()=>calls.filter(call=>!['GET','HEAD'].includes(call.method)).length;
const actor='user:'+'a'.repeat(32);
const zlib=require('node:zlib');
const crc32=buf=>{let crc=0xffffffff;for(const byte of buf){crc^=byte;for(let n=0;n<8;n++)crc=(crc>>>1)^((crc&1)?0xedb88320:0);}return(crc^0xffffffff)>>>0;};
const pngChunk=(type,data)=>{const name=Buffer.from(type),size=Buffer.alloc(4),crc=Buffer.alloc(4);size.writeUInt32BE(data.length);crc.writeUInt32BE(crc32(Buffer.concat([name,data])));return Buffer.concat([size,name,data,crc]);};
const pngHeader=Buffer.alloc(13);pngHeader.writeUInt32BE(2,0);pngHeader.writeUInt32BE(2,4);pngHeader[8]=8;pngHeader[9]=2;
const png=Buffer.concat([Buffer.from([137,80,78,71,13,10,26,10]),pngChunk('IHDR',pngHeader),pngChunk('IDAT',zlib.deflateSync(Buffer.from([0,230,240,235,255,255,255,0,255,255,255,230,240,235]))),pngChunk('IEND',Buffer.alloc(0))]);
const source={id:'batch-deck',source:'A',period:'2026-09',original_name:'Synthetic retained source.csv',download_url:'/api/batches/batch-deck/file'};
const caseRow=(n)=>({id:'case-'+n,movement_id:String(n).padStart(64,'0'),external_movement_id:'MOV-'+n,customer_id:n===3?'C2':'C1',period:'2026-09',reason:'MODEL_RISK',severity:'HIGH',status:'OPEN',score:.81,version_id:'version-'+n,version_no:6,current_version:6,release_eligible:true,source_files:[source],provenance_status:'COMPLETE',transaction:{amount:'1234.56',currency:'GBP',source_a_amount:'1234.56',source_b_amount:'1234.56',source_a_currency:'GBP',source_b_currency:'GBP',description:'Synthetic recorded movement',event_at:'2026-09-15T12:00:00Z',supplier_id:'SUP-1'},model:{status:'SCORED',recorded_score:.81,threshold:.6,threshold_status:'RECORDED_RECEIPT',model_version:'lr-frozen-deck',policy_version:'policy-deck',recorded_reasons:['MODEL_RISK'],flagged:true},rule:{status:'PASS',recorded_reasons:[]}});
const invoiceRow=n=>({id:'invoice-'+n,number:'DEMO-2026-'+n,customer_id:n===3?'C2':'C1',period:'2026-09',status:'DRAFT',net_total:'100.00',vat_total:'20.00',gross_total:'120.00',currency:'GBP',line_format:'SUMMARY',line_count:1,pdf_ready:true,fingerprint:String(n).repeat(64),pdf_sha256:'a'.repeat(64),source_files:[source]});
async function newFixture({module=false,role='admin',reducedMotion='no-preference',viewport={width:1440,height:1080},configure}={}){
  const context=await browser.newContext({viewport,reducedMotion,hasTouch:true});
  const page=await context.newPage();page.setDefaultTimeout(8000);
  const fx={page,context,role,gate:null,cases:[1,2,3].map(caseRow),invoices:[1,2,3].map(invoiceRow),writes:[],allowWrites:false};
  if(configure)configure(fx);
  page.on('pageerror',error=>errors.push(String(error)));
  await page.addInitScript(()=>{
    window.streams=[];
    window.EventSource=class{constructor(){this.listeners={};window.streams.push(this);}addEventListener(name,fn){this.listeners[name]=fn;}close(){this.closed=true;}};
    window.fixtureStream=(revision,resources)=>window.streams.at(-1).listeners.update({data:JSON.stringify({revision,stream_version:revision+':1',changed_resources:resources})});
    window.fixtureExpire=()=>window.streams.at(-1).listeners.auth_expired({data:'{}'});
  });
  await page.route('**/*',async route=>{
    const request=route.request(),url=new URL(request.url()),endpoint=url.pathname,method=request.method();
    if(url.origin!=='http://invoice.test'){external.push(url.origin);return route.abort();}
    const call={path:endpoint,query:url.search,method,body:method==='GET'?null:request.postDataJSON(),csrf:request.headers()['x-csrf-token']||null,role:fx.role};calls.push(call);
    const json=(body,status=200)=>route.fulfill({status,contentType:'application/json',body:JSON.stringify(body)});
    if(endpoint.startsWith('/static/'))return route.fulfill({contentType:endpoint.endsWith('.css')?'text/css':'application/javascript',body:fs.readFileSync(path.join(root,'src/invoice_optimizer',endpoint),'utf8')});
    if(endpoint==='/')return route.fulfill({contentType:'text/html',body:module?'<!doctype html><html><head><link rel="stylesheet" href="/static/app.css"><link rel="stylesheet" href="/static/card_deck.css"></head><body><main style="max-width:900px;margin:40px auto"><div id="fixture-deck"></div></main><script src="/static/card_deck.js"></script></body></html>':fs.readFileSync(path.join(root,'src/invoice_optimizer/templates/index.html'),'utf8')});
    if(method!=='GET'){
      assert.equal(fx.allowWrites,true,'A business request happened before explicit fixture confirmation');
      assert.equal(fx.role,'admin');assert.equal(call.csrf,'deck-fixture-csrf');
      assert.equal(Object.hasOwn(call.body||{},'actor'),false,'Actor must derive from authenticated session, not an editable payload');
      assert.ok(call.body.request_id,'Confirmed mutation must carry an idempotency key');
      fx.writes.push(call);
      if(fx.onWrite)return fx.onWrite(call,json);
      if(endpoint==='/api/review/postpone'||endpoint==='/api/review/resume'){
        const input=call.body,item=(input.kind==='CASE'?fx.cases:fx.invoices).find(row=>row.id===input.id);assert.ok(item);
        assert.ok(input.reason.trim().length>=5);
        const binding=input.kind==='CASE'?{case_id:item.id,version_id:item.version_id,version_no:item.version_no}:{invoice_id:item.id,fingerprint:item.fingerprint};
        if(input.kind==='CASE')assert.equal(input.expected_version,item.version_no);else assert.equal(input.fingerprint,item.fingerprint);
        item.personal_postponement=endpoint.endsWith('postpone')?{until:'2099-01-01T12:00:00Z',reason:input.reason,binding,actor,postponed_at:'2026-10-27T12:00:00Z'}:null;
        return json({id:item.id,kind:input.kind,status:item.personal_postponement?'POSTPONED':'RESUMED',personal_postponement:item.personal_postponement,binding,reviewer:{actor_id:actor,identity_kind:'REGISTERED_OPERATOR',display_name:'Fixture Deck Reviewer'},financial_status_changed:false});
      }
      const resolve=endpoint.match(/^\/api\/cases\/(case-\d+)\/resolve$/);
      if(resolve){const item=fx.cases.find(row=>row.id===resolve[1]);assert.ok(item);assert.equal(call.body.expected_version,item.version_no);assert.ok(call.body.reason.trim().length>=5);item.outcome=call.body.outcome;item.release_eligible=false;if(item.outcome==='VERIFIED_VALID')item.status='RESOLVED';return json({id:item.id,status:item.status,outcome:item.outcome,movement_state:item.outcome==='VERIFIED_VALID'?'ELIGIBLE':'BLOCKED',reviewed_by:actor,event_id:fx.writes.length,reviewer:{actor_id:actor,identity_kind:'REGISTERED_OPERATOR',display_name:'Fixture Deck Reviewer'}});}
      const decision=endpoint.match(/^\/api\/invoices\/(invoice-\d+)\/(approve|reject)$/);
      if(decision){const item=fx.invoices.find(row=>row.id===decision[1]);assert.ok(item);assert.equal(call.body.fingerprint,item.fingerprint);assert.ok(call.body.reason.trim().length>=5);item.status=decision[2]==='approve'?'APPROVED':'REJECTED';if(item.status==='APPROVED'){item.approved_at='2026-10-27T12:00:00Z';item.approved_by=actor;}return json({id:item.id,status:item.status,fingerprint:item.fingerprint,reviewer:{actor_id:actor,identity_kind:'REGISTERED_OPERATOR'}});}
      unexpected.push(call);return json({detail:'Unexpected mutation blocked by fixture'},409);
    }
    if(endpoint==='/api/auth/status')return json({setup_required:false});
    if(endpoint==='/api/session')return json({authenticated:true,csrf_token:'deck-fixture-csrf',identity_type:fx.role==='legacy'?'legacy':'operator',can_review:fx.role!=='legacy',actor:fx.role==='legacy'?'legacy-shared':actor,user_id:'a'.repeat(32),username:'fixture.deck',display_name:'Fixture Deck Reviewer',role:fx.role==='legacy'?null:fx.role});
    if(endpoint==='/api/customers')return json([{id:'C1',legal_name:'Synthetic Aster Ltd'},{id:'C2',legal_name:'Synthetic Birch Ltd'}]);
    if(endpoint==='/api/dashboard')return json({mode:'SIMULATION',counts:{open_cases:3,pending_approvals:3,movements:3,customers:2},periods:['2026-09'],worker:{status:'RUNNING',stale:false},auto_dispatch_after_approval:true,revision:1});
    if(endpoint==='/api/cases'||endpoint==='/api/invoices'){
      assert.equal(url.searchParams.get('limit'),'50');
      const items=endpoint==='/api/cases'?fx.cases:fx.invoices,customer=url.searchParams.get('customer_id'),status=url.searchParams.get('status');
      return json(items.filter(item=>(!customer||item.customer_id===customer)&&(!status||item.status===status)).slice(Number(url.searchParams.get('offset')),Number(url.searchParams.get('offset'))+50));
    }
    if(endpoint==='/api/case-drivers'){
      const ids=url.searchParams.getAll('case_id');assert.ok(ids.length>0&&ids.length<=50);assert.equal(ids.length,new Set(ids).size);
      const result={items:ids.map(id=>{
        const row=fx.cases.find(item=>item.id===id);assert.ok(row);
        if(row.model.status!=='SCORED')return{case_id:id,version_id:row.version_id,status:'UNAVAILABLE',reason:'Recorded assessment is unavailable.',features:[]};
        const keys=['log_amount','amount_to_expected','log_customer_age_days','log_supplier_age_days','night_hour','cross_border'];
        const labels=['Movement amount','Amount relative to expected amount','Customer age','Supplier age','Night-time indicator','Cross-border indicator'];
        const raw=[row.transaction.amount,{amount:row.transaction.amount,expected_amount:'1000.00'},100,20,{event_at:'2026-09-15T12:00:00Z',hour_utc:12},1];
        return{case_id:id,version_id:row.version_id,version_no:row.version_no,status:'AVAILABLE',model_version:row.model.model_version,recorded_score:row.model.recorded_score,threshold:.6,raw_binding:'VERIFIED_SOURCE_SNAPSHOT',currency:'GBP',contribution_units:'log_odds',features:keys.map((feature,index)=>({feature,label:labels[index],raw_value:raw[index],raw_value_status:'RETAINED',transformed_value:index,contribution:index===1?-.5:.1,effect:index===1?'DECREASE':'INCREASE',share_percent:index===1?50:10,transformation:'Synthetic fixture transformation'}))};
      }),read_only:true};
      if(fx.gate?.path===endpoint){const gate=fx.gate;fx.gate=null;gate.started=true;await gate.wait;gate.done=true;}
      return json(result);
    }
    const caseMatch=endpoint.match(/^\/api\/cases\/(case-\d+)(\/.*)?$/);
    if(caseMatch){
      const item=fx.cases.find(row=>row.id===caseMatch[1]);assert.ok(item);
      if(fx.gate?.path===endpoint){const gate=fx.gate;fx.gate=null;gate.started=true;await gate.wait;gate.done=true;}
      if(!caseMatch[2])return json({case:item,movement:{id:item.movement_id,external_id:item.external_movement_id,current_version:item.current_version},version:{id:item.version_id,version_no:item.version_no,amount:'1234.56',currency:'GBP',payload:{...item.transaction,expected_amount:'1000.00',source_values:{A:{amount:'1234.56',currency:'GBP'},B:{amount:'1234.56',currency:'GBP'}}}},assessments:[{kind:'RULE',status:'PASS',reasons:[]},{kind:'MODEL',status:'SCORED',score:.81,model_version:'lr-frozen-deck',reasons:['MODEL_RISK']}],source_files:[source]});
      if(caseMatch[2]==='/model/explanation')return json({case_id:item.id,version_no:6,case_reason:item.reason,recorded_score:.81,preview_available:false,explanation:{status:'UNAVAILABLE',reason:'Synthetic fixture only.'},narrative:{status:'UNAVAILABLE',headline:'Recorded model concern.',summary:'Synthetic evidence supports review, not proof of fraud.',drivers:[],mitigating_drivers:[]}});
    }
    const invoiceMatch=endpoint.match(/^\/api\/invoices\/(invoice-\d+)\/package$/);
    if(invoiceMatch){
      const item=fx.invoices.find(row=>row.id===invoiceMatch[1]);assert.ok(item);
      if(fx.gate?.path===endpoint){const gate=fx.gate;fx.gate=null;gate.started=true;await gate.wait;gate.done=true;}
      return json({...item,document:{invoice_number:item.number,currency:'GBP',period:item.period,customer:{legal_name:'Synthetic Aster Ltd'},net_total:'100.00',vat_total:'20.00',gross_total:'120.00',movement_count:1},message:{from:'fixture@example.test',to:['recipient@example.test'],subject:'Synthetic invoice for explicit testing',body:'This intercepted fixture sends no email.'}});
    }
    const preview=endpoint.match(/^\/api\/invoices\/(invoice-\d+)\/preview$/);
    if(preview)return json({pdf_sha256:'a'.repeat(64),page_count:1,page_url:'/api/invoices/'+preview[1]+'/preview/pages/1?sha256='+'a'.repeat(64)});
    if(/^\/api\/invoices\/invoice-\d+\/preview\/pages\/1$/.test(endpoint)){assert.equal(url.searchParams.get('sha256'),'a'.repeat(64));return route.fulfill({contentType:'image/png',headers:{'X-PDF-SHA256':'a'.repeat(64),'X-PDF-Page':'1'},body:png});}
    if(['/api/batches','/api/outbox','/api/audit','/api/model/runs'].includes(endpoint))return json([]);
    unexpected.push(call);return json({detail:'Unexpected request blocked by fixture'},404);
  });
  await page.goto('http://invoice.test/'+(module?'':'#review'));
  return fx;
}
async function pointer(page,type,options={}){
  await page.locator('.card-deck-stage').dispatchEvent(type,{pointerId:7,pointerType:'touch',isPrimary:true,button:0,buttons:type==='pointerup'?0:1,clientX:250,clientY:180,...options});
}
async function mouseDrag(page,dx,dy=0){
  const stage=page.locator('.card-deck-stage');await stage.evaluate(el=>el.scrollIntoView({block:'start'}));const box=await stage.boundingBox();
  // Start on the non-interactive card header, away from evidence buttons.
  const start={x:box.x+Math.min(160,box.width/3),y:box.y+(box.height>400?120:30)};
  await page.mouse.move(start.x,start.y);await page.mouse.down();await page.mouse.move(start.x+dx,start.y+dy,{steps:8});await page.mouse.up();
}

async function moduleChecks(){
  const fx=await newFixture({module:true}),page=fx.page;
  await page.evaluate(()=>{
    window.actions=[];window.acceptAction=false;window.pendingResolve=null;
    const items=Array.from({length:50},(_,index)=>({id:'module-'+index,version:6}));
    window.mountFixture=()=>window.deck=CardDeck.mount(document.querySelector('#fixture-deck'),{items,getKey:item=>item.id,getToken:item=>'version:'+item.version,counts:{pending:50,deferred:0,confirmed:0,rejected:0},getActionState:(_item,action)=>({disabled:window.disabledAction===action,reason:window.disabledAction===action?'Personal review unavailable':''}),renderCard:item=>`<article><h2>${item.id}</h2><p>Recorded evidence · version ${item.version}</p><button type="button" id="nested-control">Inspect evidence</button></article>`,onAction:async action=>{actions.push({action:action.action,key:action.key,token:action.token,signal:action.signal});if(window.waitAction)return new Promise(resolve=>window.pendingResolve=resolve);return window.actionResult||{accepted:window.acceptAction};}});
    mountFixture();
  });
  assert.ok(await page.locator('.card-deck-layer').count()<=3);
  assert.match(await page.locator('[data-deck-count=pending]').innerText(),/50/);
  await mouseDrag(page,30);assert.equal(await page.evaluate(()=>actions.length),0);
  await pointer(page,'pointerdown');await pointer(page,'pointermove',{clientX:260,clientY:310});await pointer(page,'pointerup',{clientX:260,clientY:310});
  assert.equal(await page.evaluate(()=>actions.length),0);
  for(const cancel of ['pointercancel','lostpointercapture']){
    await pointer(page,'pointerdown');await pointer(page,'pointermove',{clientX:500});await pointer(page,cancel,{clientX:500});await pointer(page,'pointerup',{clientX:500});
    assert.equal(await page.evaluate(()=>actions.length),0);
  }
  await page.locator('.card-deck-stage').focus();await pointer(page,'pointerdown');await pointer(page,'pointermove',{clientX:500});await page.keyboard.press('Escape');await pointer(page,'pointerup',{clientX:500});
  assert.equal(await page.evaluate(()=>actions.length),0);
  await page.locator('.card-deck-layer[data-layer="0"] #nested-control').click();assert.equal(await page.evaluate(()=>actions.length),0);
  const stageBox=await page.locator('.card-deck-stage').boundingBox(),drag=Math.max(180,stageBox.width*.35);
  await mouseDrag(page,-drag);await until(()=>page.evaluate(()=>actions.length===1));
  assert.deepEqual(await page.evaluate(()=>({action:actions[0].action,key:actions[0].key,token:actions[0].token})),{action:'later',key:'module-0',token:'version:6'});
  assert.match(await page.locator('[data-deck-count=pending]').innerText(),/50/);
  await mouseDrag(page,drag);await until(()=>page.evaluate(()=>actions.length===2));assert.equal(await page.evaluate(()=>actions[1].action),'review');
  await page.locator('[data-deck-action=reject]').click();await until(()=>page.evaluate(()=>actions.length===3));assert.equal(await page.evaluate(()=>actions[2].action),'reject');
  assert.match(await page.locator('[data-deck-count=rejected]').innerText(),/0/);
  await page.evaluate(()=>window.waitAction=true);
  await page.locator('[data-deck-action=later]').click();await until(()=>page.evaluate(()=>Boolean(pendingResolve)));
  await page.evaluate(()=>deck.update({items:[{id:'replacement',version:7}],counts:{pending:1,deferred:0,confirmed:0,rejected:0},message:'New current view'}));
  assert.equal(await page.evaluate(()=>actions.at(-1).signal.aborted),true);
  await page.evaluate(()=>pendingResolve({accepted:true,items:[{id:'late-obsolete',version:1}],counts:{pending:0,deferred:50,confirmed:0,rejected:0}}));
  await until(async()=>!(await page.evaluate(()=>deck.isBusy)));
  assert.match(await page.locator('.card-deck-layer[data-layer="0"]').innerText(),/replacement/);
  assert.doesNotMatch(await page.locator('.card-deck').innerText(),/late-obsolete/);
  await page.evaluate(()=>{window.disabledAction='review';deck.update({});});
  const beforeDisabled=await page.evaluate(()=>actions.length);
  assert.equal(await page.locator('[data-deck-action=review]').isDisabled(),true);
  await page.locator('.card-deck-stage').focus();await page.keyboard.press('ArrowRight');
  assert.equal(await page.evaluate(()=>actions.length),beforeDisabled);
  await page.evaluate(()=>{window.disabledAction=null;window.waitAction=false;window.actionResult={accepted:true,items:[{id:'confirmed-next',version:8}],counts:{pending:1,deferred:0,confirmed:1,rejected:0}};deck.update({});});
  await page.locator('[data-deck-action=review]').focus();await page.keyboard.press('Enter');
  await until(async()=>(await page.locator('.card-deck-layer[data-layer="0"]').innerText()).includes('confirmed-next'));
  assert.equal(await page.locator('[data-deck-count=confirmed]').innerText(),'1');
  await page.emulateMedia({reducedMotion:'reduce'});
  assert.equal(await page.evaluate(()=>matchMedia('(prefers-reduced-motion: reduce)').matches),true);
  const style=await page.locator('.card-deck-layer[data-layer="0"]').evaluate(el=>({transition:getComputedStyle(el).transitionDuration,animation:getComputedStyle(el).animationDuration}));
  assert.ok(style.transition.split(',').every(value=>parseFloat(value)<=.001));
  await page.evaluate(()=>{window.pendingResolve=null;window.waitAction=true;});
  await page.locator('[data-deck-action=later]').click();await until(()=>page.evaluate(()=>Boolean(pendingResolve)));
  await page.evaluate(()=>deck.destroy());assert.equal(await page.evaluate(()=>actions.at(-1).signal.aborted),true);
  await page.evaluate(()=>pendingResolve({accepted:true,items:[{id:'late-after-destroy',version:1}]}));
  assert.equal(await page.locator('.card-deck-layer').count(),0);
  checks.push('Pure deck: bounded three layers, explicit button alternatives, release-only left/right, short/vertical/cancel/capture/Escape abandon, nested controls safe, authoritative counts, reduced motion, update/destroy invalidate late callbacks');
  await contextClose(fx);
}
async function contextClose(fx){await fx.context.close();}
const activeKey=page=>page.locator('.card-deck-layer[data-layer="0"]').getAttribute('data-card-key');
const deckCount=(page,key)=>page.locator(`[data-deck-count="${key}"]`).innerText();
async function enterSwipe(page,kind='review'){
  if(kind==='invoices')await page.locator('nav [data-tab="invoices"]').click();
  await page.locator(`[data-${kind==='review'?'review':'invoice'}-layout="swipe"]`).click();
  await page.locator('.card-deck-stage').waitFor();
}
async function confirmation(page){await page.locator('#swipe-confirm-form').waitFor();assert.equal(await page.locator('#swipe-confirm-checked').isChecked(),false);assert.equal(await page.locator('#swipe-confirm-submit').isDisabled(),true);}
async function fillConfirmation(page,reason='Checked the exact retained evidence and source files.'){
  await page.locator('#swipe-decision-reason').fill(reason);
  assert.equal(await page.locator('#swipe-confirm-submit').isDisabled(),true);
  await page.locator('#swipe-confirm-checked').check();assert.equal(await page.locator('#swipe-confirm-submit').isDisabled(),false);
}
async function settledBars(container){
  await until(()=>container.evaluate(el=>el.getAnimations({subtree:true}).every(animation=>animation.playState==='finished')),'risk bars finish bounded entrance animation');
  const widths=await container.locator('.risk-driver-fill').evaluateAll(elements=>elements.map(el=>({declared:parseFloat(el.style.width),rendered:el.getBoundingClientRect().width/el.parentElement.getBoundingClientRect().width*100})));
  assert.ok(widths.length>0);for(const width of widths)assert.ok(Math.abs(width.declared-width.rendered)<.1,JSON.stringify(width));
}
function gateFixture(fx,path){let release;const wait=new Promise(resolve=>release=resolve);fx.gate={path,wait,release,started:false,done:false};return fx.gate;}

async function integrationChecks(){
  const fx=await newFixture({configure:fx=>{fx.cases[2].release_eligible=false;fx.cases[2].model={...fx.cases[2].model,status:'UNAVAILABLE',recorded_score:null};}}),page=fx.page;
  await until(async()=>await page.locator('.review-card').count()===3,'initial review cards');
  assert.equal(await page.locator('[data-review-layout=cards]').getAttribute('aria-pressed'),'true');
  await until(async()=>await page.locator('[data-review-card="case-1"] .risk-driver').count()===6,'six recorded feature bars');
  const bar=page.locator('[data-review-card="case-1"] [data-risk-feature="amount_to_expected"]');
  assert.match(await bar.innerText(),/1234\.56 \/ 1000\.00 GBP/);
  await bar.hover();assert.equal(await bar.locator('[role=tooltip]').isVisible(),true);
  assert.match(await bar.locator('[role=tooltip]').innerText(),/50\.0% contribution share/);
  assert.match(await bar.getAttribute('aria-label'),/not a risk probability/);
  assert.ok((await bar.getAttribute('class')).includes('risk-down'));
  await settledBars(page.locator('[data-review-card="case-1"]'));
  assert.equal(await page.locator('[data-review-card="case-3"] .risk-driver').count(),0);
  assert.match(await page.locator('[data-review-card="case-3"] .risk-drivers-unavailable').innerText(),/unavailable/);
  const getBefore=calls.filter(call=>call.method==='GET').length;
  await enterSwipe(page);assert.equal(await activeKey(page),'case-1');assert.equal(await deckCount(page,'pending'),'3');
  assert.equal(calls.filter(call=>call.method==='GET').length,getBefore,'Switching to Swipe reuses the bounded page and drivers');
  assert.equal(await page.locator('.card-deck-layer').count(),3);
  await mouseDrag(page,220);await confirmation(page);assert.equal(fx.writes.length,0);
  assert.match(await page.locator('.swipe-full-evidence').innerText(),/Case version 6 · current business version 6/);
  assert.match(await page.locator('.swipe-full-evidence').innerText(),/£1,234\.56/);
  assert.match(await page.locator('#swipe-confirm-form .recorded-identity').innerText(),/Fixture Deck Reviewer/);
  await page.locator('#swipe-confirm-form').dispatchEvent('submit');assert.equal(fx.writes.length,0);
  await fillConfirmation(page);await page.locator('#swipe-cancel').click();
  await until(async()=>!(await page.locator('#detail-dialog').evaluate(el=>el.open)));
  assert.equal(await activeKey(page),'case-1');assert.equal(await deckCount(page,'confirmed'),'0');assert.equal(fx.writes.length,0);

  fx.allowWrites=true;await mouseDrag(page,-220);
  await until(async()=>await activeKey(page)==='case-2','confirmed personal postponement');
  assert.equal(fx.writes[0].path,'/api/review/postpone');assert.equal(fx.writes[0].body.expected_version,6);
  assert.equal(fx.cases[0].status,'OPEN');assert.equal(await deckCount(page,'deferred'),'1');
  await page.locator('[data-swipe-view=deferred]').click();assert.equal(await activeKey(page),'case-1');
  await page.locator('[data-swipe-return]').click();await until(()=>fx.writes.length===2);
  assert.equal(fx.writes[1].path,'/api/review/resume');assert.equal(fx.cases[0].status,'OPEN');
  await page.locator('[data-swipe-view=pending]').click();await until(async()=>await activeKey(page)==='case-1');
  fx.allowWrites=false;await page.locator('[data-deck-action=review]').click();await confirmation(page);await fillConfirmation(page);
  fx.allowWrites=true;await page.locator('#swipe-confirm-submit').click();
  await until(async()=>await activeKey(page)==='case-2','review success advances once');
  assert.equal(fx.writes[2].path,'/api/cases/case-1/resolve');assert.equal(fx.writes[2].body.outcome,'VERIFIED_VALID');assert.equal(fx.writes[2].body.expected_version,6);
  assert.equal(await deckCount(page,'confirmed'),'1');
  fx.allowWrites=false;await page.locator('[data-deck-action=reject]').click();await confirmation(page);
  assert.match(await page.locator('.swipe-confirm-notice').innerText(),/stays open for remediation/);
  assert.equal(fx.writes.length,3);await fillConfirmation(page,'Verified a source discrepancy; keep this movement blocked.');
  fx.allowWrites=true;await page.locator('#swipe-confirm-submit').click();await until(async()=>await activeKey(page)==='case-3');
  assert.equal(fx.writes[3].body.outcome,'CONFIRMED_ISSUE');assert.equal(fx.cases[1].status,'OPEN');assert.equal(await deckCount(page,'rejected'),'1');
  assert.equal(await page.locator('[data-deck-action=review]').isDisabled(),true,'Unavailable model evidence never becomes releasable');
  await page.locator('#review-customer').selectOption('C1');await until(async()=>await activeKey(page)==='case-2');
  await until(async()=>await page.locator('.card-deck-layer[data-layer="0"] .risk-driver').count()===6);
  await settledBars(page.locator('.card-deck-layer[data-layer="0"]'));
  await page.locator('#toast').evaluate(el=>el.hidden=true);
  await page.locator('.swipe-workspace').screenshot({path:path.join(out,'card-deck-review-focused.png')});
  await page.locator('#review-customer').selectOption('C2');await until(async()=>await activeKey(page)==='case-3');
  assert.equal(await deckCount(page,'confirmed'),'0','Changed scope resets session counters');
  fx.allowWrites=false;
  await page.screenshot({path:path.join(out,'card-deck-review-desktop.png'),fullPage:false});
  checks.push('Live-app fixture: Cards remains default; six snapshot-bound feature bars, hover share labels, zero-fetch Swipe toggle, explicit reason/unchecked confirmation, exact actor/version/CSRF, durable Later and matching resume, valid/rejected case outcomes and counts');
  await contextClose(fx);

  const inv=await newFixture(),ip=inv.page;
  await until(async()=>await ip.locator('.review-card').count()===3);await enterSwipe(ip,'invoices');
  assert.equal(await activeKey(ip),'invoice-1');await mouseDrag(ip,220);await confirmation(ip);
  await ip.locator('.invoice-pdf-image').waitFor();
  assert.match(await ip.locator('.swipe-confirm-notice').innerText(),/worker automatically queues an approved package/);
  assert.match(await ip.locator('.swipe-full-evidence').innerText(),/recipient@example\.test/);
  assert.equal(inv.writes.length,0);await fillConfirmation(ip);await ip.locator('#swipe-cancel').click();assert.equal(inv.writes.length,0);
  await ip.locator('[data-deck-action=review]').click();await confirmation(ip);await fillConfirmation(ip);
  inv.allowWrites=true;await ip.locator('#swipe-confirm-submit').click();await until(async()=>await activeKey(ip)==='invoice-2');
  assert.equal(inv.writes[0].path,'/api/invoices/invoice-1/approve');assert.equal(inv.writes[0].body.fingerprint,'1'.repeat(64));assert.equal(await deckCount(ip,'confirmed'),'1');
  inv.allowWrites=false;await ip.locator('[data-deck-action=reject]').click();await confirmation(ip);
  assert.match(await ip.locator('.swipe-confirm-notice').innerText(),/billing scope stays on hold/);assert.equal(inv.writes.length,1);
  await fillConfirmation(ip,'The exact invoice package requires correction before dispatch.');inv.allowWrites=true;await ip.locator('#swipe-confirm-submit').click();
  await until(async()=>await activeKey(ip)==='invoice-3');assert.equal(inv.writes[1].path,'/api/invoices/invoice-2/reject');assert.equal(inv.writes[1].body.fingerprint,'2'.repeat(64));assert.equal(await deckCount(ip,'rejected'),'1');
  assert.equal(calls.some(call=>/\/(dispatch|revoke|send)$/.test(call.path)),false);
  await ip.setViewportSize({width:390,height:844});await ip.locator('.card-deck-stage').scrollIntoViewIfNeeded();
  assert.equal(await ip.locator('body').evaluate(el=>el.scrollWidth>innerWidth),false);
  await ip.locator('#toast').evaluate(el=>el.hidden=true);
  await ip.locator('.swipe-workspace').screenshot({path:path.join(out,'card-deck-invoice-mobile.png')});
  checks.push('Invoice swipe opens the exact package/PNG, sender/recipients/email and automatic-dispatch notice; cancellation has no mutation; approval and explicit rejection require personal reason/confirmation and exact fingerprint; no dispatch/revoke/send request');
  await contextClose(inv);

  const stale=await newFixture(),sp=stale.page;await until(async()=>await sp.locator('.review-card').count()===3);await enterSwipe(sp);
  await sp.locator('[data-deck-action=review]').click();await confirmation(sp);await fillConfirmation(sp);
  await sp.evaluate(()=>{fixtureStream(1,['cases']);fixtureStream(2,['cases']);});
  await until(async()=>await sp.locator('#swipe-confirm-submit').isDisabled(),'SSE freezes old confirmation');
  await sp.locator('#swipe-confirm-form').dispatchEvent('submit');assert.equal(stale.writes.length,0);
  await sp.locator('#close-detail').click();
  await sp.locator('nav [data-tab=invoices]').click();await sp.locator('[data-invoice-layout=swipe]').click();
  stale.invoices[0].fingerprint='b'.repeat(64);
  await sp.locator('[data-deck-action=review]').click();
  await until(async()=>(await sp.locator('.card-deck-live').innerText()).includes('changed')||(await sp.locator('#detail-content').innerText()).includes('changed'),'changed package rejection');
  assert.equal(await sp.locator('#swipe-confirm-form').count(),0);assert.equal(stale.writes.length,0);
  await sp.locator('#close-detail').click();await sp.locator('nav [data-tab=review]').click();
  await until(async()=>await activeKey(sp)==='case-1');
  stale.cases[0].current_version=7;
  await sp.locator('[data-deck-action=review]').click();
  await until(async()=>(await sp.locator('.card-deck-live').innerText()).includes('changed')||(await sp.locator('#detail-content').innerText()).includes('changed'),'new movement version rejection');
  assert.equal(await sp.locator('#swipe-confirm-form').count(),0);assert.equal(stale.writes.length,0);
  await sp.locator('#close-detail').click();
  checks.push('Relevant SSE invalidates unchecked/checked old confirmations; newer movement version and changed invoice fingerprint cannot create actionable decision forms');
  await contextClose(stale);

  const late=await newFixture(),lp=late.page;await until(async()=>await lp.locator('.review-card').count()===3);await enterSwipe(lp);
  const delayed=gateFixture(late,'/api/cases/case-1');await lp.locator('[data-deck-action=review]').click();await until(()=>delayed.started);
  await lp.evaluate(()=>fixtureExpire());await lp.locator('#login-screen').waitFor();delayed.release();await until(()=>delayed.done);
  assert.equal(await lp.locator('#detail-dialog').evaluate(el=>el.open),false);assert.equal(await lp.locator('#workspace').isVisible(),false);assert.equal(late.writes.length,0);
  checks.push('Session expiry during an in-flight evidence GET prevents a late decision form and leaves workspace hidden');await contextClose(late);

  const shared=await newFixture({role:'legacy'}),rp=shared.page;await until(async()=>await rp.locator('.review-card').count()===3);await enterSwipe(rp);
  assert.equal(await rp.locator('[data-deck-action=review]').isDisabled(),true);assert.equal(await rp.locator('[data-deck-action=reject]').isDisabled(),true);
  assert.match(await rp.locator('[data-swipe-note]').innerText(),/local|session|read.only/i);
  await rp.locator('[data-deck-action=later]').click();await until(async()=>await activeKey(rp)==='case-2');assert.equal(shared.writes.length,0);
  await rp.locator('[data-swipe-view=deferred]').click();await rp.locator('[data-swipe-return]').click();await rp.locator('[data-swipe-view=pending]').click();
  assert.equal(shared.writes.length,0);await enterSwipe(rp,'invoices');assert.equal(await rp.locator('[data-deck-action=review]').isDisabled(),true);assert.equal(await rp.locator('[data-deck-action=reject]').isDisabled(),true);
  checks.push('Shared identity can only navigate/defer locally with an explicit label; review/reject disabled for both queues and resume sends no mutation');await contextClose(shared);

  // A command may generate its audit event before its HTTP response. It must
  // count only the confirmed result, while preventing a second stale command.
  const own=await newFixture(),op=own.page;await until(async()=>await op.locator('.review-card').count()===3);await enterSwipe(op);
  let releaseWrite;const writeWait=new Promise(resolve=>releaseWrite=resolve);let writeStarted=false;
  own.onWrite=async(call,json)=>{assert.equal(call.path,'/api/review/postpone');writeStarted=true;await writeWait;const item=own.cases[0],binding={case_id:item.id,version_id:item.version_id,version_no:6};item.personal_postponement={binding,until:'2099-01-01T12:00:00Z',actor};return json({id:item.id,kind:'CASE',status:'POSTPONED',binding,personal_postponement:item.personal_postponement});};
  own.allowWrites=true;await op.locator('[data-deck-action=later]').click();await until(()=>writeStarted);
  assert.equal(await deckCount(op,'deferred'),'0');
  await op.evaluate(()=>{fixtureStream(1,['cases']);fixtureStream(2,['cases']);});releaseWrite();
  await until(async()=>await activeKey(op)==='case-2');assert.equal(await deckCount(op,'deferred'),'1');
  assert.equal(await op.locator('[data-deck-action=review]').isDisabled(),true);assert.equal(await op.locator('[data-swipe-stale]').isVisible(),true);
  await contextClose(own);

  const lateWrite=await newFixture(),wp=lateWrite.page;await until(async()=>await wp.locator('.review-card').count()===3);await enterSwipe(wp);
  let finishLate;const lateWait=new Promise(resolve=>finishLate=resolve);let lateStarted=false,lateFinished=false;
  lateWrite.onWrite=async(call,json)=>{assert.equal(call.path,'/api/review/postpone');lateStarted=true;await lateWait;const item=lateWrite.cases[0],binding={case_id:item.id,version_id:item.version_id,version_no:6};const result=await json({id:item.id,kind:'CASE',status:'POSTPONED',binding,personal_postponement:{binding,until:'2099-01-01T12:00:00Z',actor}});lateFinished=true;return result;};
  lateWrite.allowWrites=true;await wp.locator('[data-deck-action=later]').click();await until(()=>lateStarted);
  await wp.locator('#review-customer').selectOption('C2');await until(async()=>await activeKey(wp)==='case-3');finishLate();await until(()=>lateFinished);
  assert.equal(await activeKey(wp),'case-3');assert.equal(await deckCount(wp,'pending'),'1');assert.equal(await deckCount(wp,'deferred'),'0');
  await contextClose(lateWrite);
  checks.push('Own audit SSE before a successful postpone response preserves one confirmed Later count and blocks new stale actions; a late submitted response cannot alter a different filtered scope');

  const retry=await newFixture(),tp=retry.page;await until(async()=>await tp.locator('.review-card').count()===3);await enterSwipe(tp);
  await tp.locator('[data-deck-action=review]').click();await confirmation(tp);await fillConfirmation(tp);
  let attempt=0;retry.onWrite=(call,json)=>{assert.equal(call.path,'/api/cases/case-1/resolve');attempt++;return json(attempt===1?{}:{id:'case-1',status:'RESOLVED',outcome:'VERIFIED_VALID',event_id:1001,reviewer:{actor_id:actor,identity_kind:'REGISTERED_OPERATOR'}});};
  retry.allowWrites=true;await tp.locator('#swipe-confirm-submit').click();await tp.locator('#swipe-confirm-error').waitFor();
  assert.match(await tp.locator('#swipe-confirm-error').innerText(),/did not confirm/);assert.equal(await deckCount(tp,'confirmed'),'0');assert.equal(await activeKey(tp),'case-1');
  await tp.locator('#swipe-confirm-submit').click();await until(async()=>await activeKey(tp)==='case-2');
  assert.equal(retry.writes[0].body.request_id,retry.writes[1].body.request_id);assert.equal(await deckCount(tp,'confirmed'),'1');
  checks.push('An HTTP200 incomplete decision receipt cannot advance the card or counts; retry reuses its idempotency key and advances only after the exact attributed event receipt');
  await contextClose(retry);

  let driversGate;
  const drivers=await newFixture({configure:fx=>{driversGate=gateFixture(fx,'/api/case-drivers');}}),dp=drivers.page;
  await until(()=>driversGate.started);await until(async()=>await dp.locator('.review-card').count()===3);
  drivers.cases[2].transaction.amount='9876.54';drivers.cases[2].model.model_version='lr-new-filter';
  await dp.locator('#review-customer').selectOption('C2');
  await until(async()=>await dp.locator('[data-review-card="case-3"] .risk-driver').count()===6);
  assert.match(await dp.locator('[data-risk-feature=log_amount] .risk-driver-value').innerText(),/9876\.54/);
  driversGate.release();await until(()=>driversGate.done);
  assert.match(await dp.locator('[data-risk-feature=log_amount] .risk-driver-value').innerText(),/9876\.54/);
  assert.equal(await dp.locator('.review-card').count(),1);assert.equal(drivers.writes.length,0);
  checks.push('Delayed feature-batch response from an old filter cannot replace the newer model/raw values or add old cards');
  await contextClose(drivers);

  const detach=await newFixture(),ep=detach.page;await until(async()=>await ep.locator('.review-card').count()===3);await enterSwipe(ep);
  await ep.locator('[data-deck-action=review]').click();await confirmation(ep);await fillConfirmation(ep);
  let finishDecision;const decisionWait=new Promise(resolve=>finishDecision=resolve);let decisionStarted=false;
  detach.onWrite=async(call,json)=>{assert.equal(call.path,'/api/cases/case-1/resolve');decisionStarted=true;await decisionWait;return json({id:'case-1',status:'RESOLVED',outcome:'VERIFIED_VALID',event_id:1002,reviewer:{actor_id:actor,identity_kind:'REGISTERED_OPERATOR'}});};
  detach.allowWrites=true;await ep.locator('#swipe-confirm-submit').click();await until(()=>decisionStarted);
  assert.equal(await ep.locator('#swipe-cancel').isDisabled(),true);
  await ep.locator('#close-detail').click();await ep.locator('[data-review-layout=cards]').click();await ep.locator('[data-review-layout=swipe]').click();
  assert.equal(await deckCount(ep,'confirmed'),'0');assert.equal(await ep.locator('[data-deck-action=review]').isDisabled(),true);
  finishDecision();await until(async()=>await activeKey(ep)==='case-2');assert.equal(await deckCount(ep,'confirmed'),'1');
  assert.equal(await ep.locator('[data-deck-action=review]').isDisabled(),false);
  await ep.locator('[data-deck-action=reject]').click();await confirmation(ep);await fillConfirmation(ep);
  let endOldSession;const oldSessionWait=new Promise(resolve=>endOldSession=resolve);let oldSessionStarted=false,oldSessionFinished=false;
  detach.onWrite=async(call,json)=>{assert.equal(call.path,'/api/cases/case-2/resolve');oldSessionStarted=true;await oldSessionWait;const result=await json({id:'case-2',status:'OPEN',outcome:'CONFIRMED_ISSUE',event_id:1003,reviewer:{actor_id:actor,identity_kind:'REGISTERED_OPERATOR'}});oldSessionFinished=true;return result;};
  await ep.locator('#swipe-confirm-submit').click();await until(()=>oldSessionStarted);await ep.evaluate(()=>fixtureExpire());await ep.locator('#login-screen').waitFor();
  endOldSession();await until(()=>oldSessionFinished);
  assert.equal(await ep.locator('#workspace').isVisible(),false);assert.equal(await ep.locator('#detail-dialog').evaluate(el=>el.open),false);assert.equal(await ep.locator('.card-deck').count(),0);
  checks.push('A submitted decision survives closing its dialog and same-scope layout switches, updates exactly once after the attributed receipt, and never reopens UI or updates a later session after logout');
  await contextClose(detach);
}

(async()=>{
  sourceHashes=hashSources();browser=await chromium.launch({headless:true});
  await moduleChecks();if(!moduleOnly)await integrationChecks();
  assert.deepEqual(errors,[]);assert.deepEqual(external,[]);assert.deepEqual(unexpected,[]);assert.deepEqual(hashSources(),sourceHashes,'Source changed while browser fixture was running');
  const receipt={pass:true,module_only:moduleOnly,scope:'Every browser response intercepted; no live business write, approval, email or provider call.',checks,requests:calls.length,fixture_mutations:writeCount(),source_sha256:sourceHashes,screenshots:moduleOnly?[]:['card-deck-review-desktop.png','card-deck-review-focused.png','card-deck-invoice-mobile.png'],errors,external,unexpected,calls};
  fs.writeFileSync(receiptPath,JSON.stringify(receipt,null,2)+'\n');console.log(JSON.stringify({pass:true,checks:checks.length,receipt:receiptPath}));
})().catch(error=>{fs.writeFileSync(receiptPath,JSON.stringify({pass:false,error:String(error.stack||error),checks,source_sha256:sourceHashes,errors,external,unexpected,calls},null,2)+'\n');console.error(error);process.exitCode=1;}).finally(async()=>{if(browser)await browser.close();});
