/* Invoice Operations: no frontend framework, no telemetry, no browser token storage. */
(() => {
  'use strict';
  const $ = (selector, root = document) => root.querySelector(selector);
  const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const human = value => String(value ?? 'Unknown').replace(/_/g, ' ').toLowerCase().replace(/^./, c => c.toUpperCase());
  const state = {session:null,setupRequired:false,loginMode:'personal',historySequence:0,workbench:new Map(),activeCaseId:null,inspectionSelection:new Set(),compareCases:new Set(),caseFilters:{q:'',reason:'',severity:'',min_score:'',max_score:'',sort:'score_desc'},filterTimer:null,tab:'overview', cardDrivers:new Map(), cardDriversRequest:null, reviewLayout:'cards', invoiceLayout:'cards', sessionEpoch:0, csrf:null, summary:null, revision:null, events:null, offset:0, limit:50, caseStatus:'OPEN', invoiceStatus:'', customerFilters:{review:'',invoices:'',delivery:'',data:'',insights:''}, insightPeriod:'', selectedCases:new Map(), loading:false, pendingRefresh:false, customers:[], requestSequence:0, renderedReviewSequence:null, dialogSequence:0, sourceSequence:0, detailId:null, toastTimer:null};
  const pages = {
    overview:['WORKSPACE AT A GLANCE','Overview','From source movements to approved invoices.'],
    review:['EXCEPTIONS & EVIDENCE','Review queue','Investigate flagged movements and record an evidenced decision.'],
    invoices:['MONTHLY BILLING','Invoices','Prepare, inspect and explicitly approve each invoice package.'],
    insights:['SIGNALS FOR REVIEW','Insights','Read-only statistical signals for one period. They prompt a human review and are not findings of error or fraud.'],
    delivery:['DISPATCH & RECEIPTS','Delivery','Follow submission and delivery evidence independently.'],
    data:['VERIFICATION & LINEAGE','Data & model','Inspect source batches, model evidence and the audit trail.'],
    email:['ACCOUNT CONNECTION','Email setup','Connect Gmail, send yourself a test, then month-end delivery runs on its own after your approval.'],
    team:['PERSONAL ACCOUNTS','Team access','Manage individual accounts for attributable operational decisions.']
  };
  const numeric=value=>Number.isFinite(value)?String(value):'Unavailable';
  const integer = value => Number.isFinite(Number(value)) && value !== null ? Number(value).toLocaleString('en-GB') : '—';
  const canReview=()=>state.session?.identity_type==='operator' && state.session?.can_review===true;
  const isAdmin=()=>canReview() && state.session?.role==='admin';
  const count = (name) => state.summary?.counts?.[name] ?? null;
  const date = value => { if(!value) return 'Not recorded'; const d = new Date(value); return Number.isNaN(d.getTime()) ? esc(value) : d.toLocaleString('en-GB',{dateStyle:'medium',timeStyle:'short'}); };
  const amount = (value, currency='GBP') => {
    if(value === null || value === undefined || value === '') return '—';
    // Format decimal strings without binary-floating-point conversion of money.
    const match = String(value).match(/^(-?)(\d+)(?:\.(\d{1,2}))?$/);
    if(!match) return `${esc(currency)} ${esc(value)}`;
    const grouped = match[2].replace(/\B(?=(\d{3})+(?!\d))/g, ',');
    return `${match[1]}${currency === 'GBP' ? '£' : esc(currency)+' '}${grouped}.${(match[3] || '').padEnd(2,'0')}`;
  };
  function badge(value) {
    const name=String(value ?? 'UNKNOWN').toUpperCase();
    const kind=/^(ELIGIBLE|APPROVED|SCORED|RESOLVED|DELIVERED|ACCEPTED|COMPLETE|VERIFIED_VALID|READY|PASS|ACTIVE|RUNNING|SUCCEEDED|DISPATCHED|CONNECTED|ENABLED)$/.test(name)?'good':/FAIL|REJECT|BOUNCE|QUARANTIN|CONFIRMED_ISSUE|BLOCK/.test(name)?'bad':/OPEN|PENDING|UNKNOWN|UNAVAILABLE|DRAFT|REVIEW|WAIT|STALE/.test(name)?'warn':'neutral';
    const label={DRAFT:'Awaiting approval',ISSUED:'Sent'}[name];
    return `<span class="badge ${kind}"${label?` title="${esc(human(name))}"`:''}>${esc(label || human(name))}</span>`;
  }
  /* Risk score: number first, plus a 3-band meter (colour is never the only cue). NULL stays "Unavailable", never 0. */
  const scoreBand = v => v>=0.5?'high':v>=0.2?'mid':'low';
  const score = value => value === null || value === undefined ? '<span class="score missing" title="No usable model score was recorded">Unavailable</span>' : Number.isFinite(Number(value)) ? `<span class="score" data-band="${scoreBand(Number(value))}" style="--v:${Math.max(0,Math.min(1,Number(value))).toFixed(3)}" title="p = ${esc(String(value))}">${percentScore(Number(value))}</span>` : '<span class="score missing">Unavailable</span>';
  /* 0.99998 is not certainty: never round a probability to 100.0% or 0.0%. */
  function percentScore(v){if(v>=0.9995 && v<1)return '&gt;99.9%';if(v>0 && v<0.0005)return '&lt;0.1%';return `${(v*100).toFixed(1)}%`;}
  const rows = result => Array.isArray(result) ? result : (result?.items || result?.data || []);
  const customerName = id => state.customers.find(x => String(x.id) === String(id))?.legal_name || id || 'Unknown customer';
  const json = value => `<pre>${esc(JSON.stringify(value,null,2))}</pre>`;
  const empty = (title, text, kind='') => `<div class="empty-state${kind?' '+kind:''}"><strong>${esc(title)}</strong><p>${esc(text)}</p></div>`;
  const metric = (label, value, note, tab, accent=false, visual='', index=null) => `<button class="metric" data-tab="${tab}"${index===null?'':` style="--i:${index}"`}><div class="metric-label">${esc(label)}<span class="metric-arrow" aria-hidden="true">↗</span></div><div class="metric-value ${accent?'metric-accent':''}" data-key="${esc(label)}"${Number.isFinite(Number(value)) && value!==null?` data-value="${Number(value)}"`:''}>${integer(value)}</div><div class="metric-note">${esc(note)}</div>${visual?`<span class="metric-visual">${visual}</span>`:''}</button>`;
  const detail = (label, value, full=false) => `<div${full?' class="full"':''}><dt>${esc(label)}</dt><dd>${value}</dd></div>`;
  const customerQuery = tab => state.customerFilters[tab]?'&customer_id='+encodeURIComponent(state.customerFilters[tab]):'';
  const customerFilter = tab => `<div class="customer-filter"><label for="${tab}-customer">Customer</label><select id="${tab}-customer" data-customer-filter="${tab}"><option value="">All customers</option>${state.customers.map(c=>`<option value="${esc(c.id)}" ${state.customerFilters[tab]===String(c.id)?'selected':''}>${esc(c.legal_name || c.id)}</option>`).join('')}</select></div>`;
  function safeApiUrl(value){try{const u=new URL(value,location.origin);return u.origin===location.origin && !u.username && !u.password && u.pathname.startsWith('/api/')?u.pathname+u.search:null;}catch{return null;}}
  function sourceFileList(files,compact=false){
    if(!Array.isArray(files) || !files.length)return '<span class="small muted">No source files recorded</span>';
    const shown=files.slice(0,compact?2:50);
    return `<ul class="source-file-list ${compact?'compact':''}">${shown.map(f=>{const download=safeApiUrl(f.download_url);return `<li><span class="source-file-name">${download?`<a href="${esc(download)}" download>${esc(f.original_name || 'Unnamed source file')}</a>`:esc(f.original_name || 'Unnamed source file')}</span><span class="source-file-meta">${esc(f.source?'Source '+f.source:'Source unknown')}${f.period?' · '+esc(f.period):''}${f.id?` · <a href="/#batch/${encodeURIComponent(f.id)}" target="_blank" rel="noopener">Inspect batch ↗</a>`:''}</span></li>`;}).join('')}</ul>${files.length>shown.length?`<span class="small muted">+${integer(files.length-shown.length)} more source files</span>`:''}`;
  }
  function sourceReferences(item,compact=false){
    const files=item?.source_files,url=safeApiUrl(item?.sources_url);
    return `<div class="source-references">${Array.isArray(files) && files.length?sourceFileList(files,compact):url?'<span class="small muted">Exact source files available on request</span>':'<span class="small muted">Source references unavailable</span>'}${typeof item?.source_file_count==='number'?`<span class="source-file-meta">${integer(item.source_file_count)} source files recorded</span>`:''}${url?`<button class="text-button small" data-source-url="${esc(url)}">${Array.isArray(files) && files.length?'View full source list':'Load source files'}</button>`:''}${sourceWarning(item)}${item?.provenance_status?`<span class="source-file-meta">${esc(human(item.provenance_status))}</span>`:''}</div>`;
  }
  function sourceWarning(item){return item?.missing_reference_count>0 || /INCOMPLETE/i.test(item?.provenance_status || '')?`<span class="source-warning">Incomplete provenance${item?.missing_reference_count>0?`: ${integer(item.missing_reference_count)} missing references`:''}. Displayed files are not the complete evidence.</span>`:item?.truncated?'<span class="source-file-meta">Preview truncated. Open the full source list.</span>':'';}
  function markPendingRefresh(){
    state.pendingRefresh=true;$('#update-notice').hidden=false;
    $('#update-notice-message').textContent=state.selectedCases.size?'New data is available. Selected cases and their reviewed versions are preserved.':'New data is available. Your open form is preserved.';
    $('#load-update').textContent=state.selectedCases.size?'Clear selection & refresh':'Refresh list';
  }
  function clearCaseSelection(){state.selectedCases.clear();updateCaseSelection();}
  function updateCaseSelection(){
    const total=state.selectedCases.size,button=$('#bulk-release-open');if(button)button.disabled=!total;
    if($('#selected-case-count'))$('#selected-case-count').textContent=`${total} selected on this page`;
    document.querySelectorAll('[data-select-case]').forEach(input=>input.checked=state.selectedCases.has(input.dataset.selectCase));
    const allowed=(state.caseRows || []).filter(releaseEligible),all=$('#select-page-cases');if(all){all.checked=allowed.length>0 && allowed.every(c=>state.selectedCases.has(c.id));all.indeterminate=total>0 && !all.checked;}
    if($('#clear-case-selection'))$('#clear-case-selection').disabled=!total;
  }
  function releaseEligible(item){return canReview() && item.release_eligible===true && item.status==='OPEN' && Number.isInteger(item.current_version) && item.current_version>0;}
  function errorText(error) { return error?.message || 'The request could not be completed. Please try again.'; }
  function showError(error) { const target=$('#global-error'); target.textContent=errorText(error);target.insertAdjacentHTML('beforeend',' <button type="button" class="text-button" data-dismiss-error>Dismiss</button>');target.hidden=false; }
  function toast(text,kind='info') { clearTimeout(state.toastTimer);const current=$('#toast');if(!current.hidden && current.dataset.message){const older=current.cloneNode(true);older.removeAttribute('id');older.setAttribute('role','status');current.before(older);const stack=[...current.parentElement.querySelectorAll('.toast:not(#toast)')];stack.slice(0,Math.max(0,stack.length-2)).forEach(node=>node.remove());if(older.dataset.kind!=='error')setTimeout(()=>dismissToast(older),6000);}current.dataset.message=text;current.dataset.kind=kind;current.setAttribute('role',kind==='error'?'alert':'status');current.innerHTML=`<span class="toast-message">${esc(text)}</span><button type="button" class="icon-button toast-dismiss" aria-label="Dismiss notification">×</button>`;current.hidden=false;state.toastShown=Date.now();if(kind!=='error')state.toastTimer=setTimeout(()=>$('#toast').hidden=true,6000); }
  function dismissToast(node){if(!node || !node.isConnected)return;if(node.id==='toast'){clearTimeout(state.toastTimer);node.hidden=true;return;}node.classList.add('is-leaving');setTimeout(()=>node.remove(),400);}
  function announce(text){const region=$('#sr-status');if(!region)return;region.textContent='';setTimeout(()=>{region.textContent=text;},30);}
  const listTotal=result=>Number.isInteger(result?.total)?result.total:null;
  function rowSnapshot(){const map=new Map();document.querySelectorAll('#tab-content [data-case], #tab-content [data-invoice]').forEach(control=>{const row=control.closest('tr, .review-card, .invoice-card, .review-item');if(!row)return;const key=control.dataset.case?`c:${control.dataset.case}`:`i:${control.dataset.invoice}`;if(!map.has(key))map.set(key,{row,text:row.textContent});});return map;}
  function highlightChanges(before){if(!before.size)return 0;let changed=0;rowSnapshot().forEach((value,key)=>{const old=before.get(key);if(old && old.text!==value.text){changed++;value.row.classList.add('is-updated');value.row.addEventListener('animationend',()=>value.row.classList.remove('is-updated'),{once:true});}});return changed;}
  function skeleton(tab){const shape=tab==='overview'?'overview':tab==='insights'?'insights':tab==='email'?'form':(tab==='review' && state.reviewLayout==='cards') || (tab==='invoices' && state.invoiceLayout==='cards')?'cards':'table';return `<div class="empty-state" data-skeleton="${shape}"><strong>Loading…</strong><p>Retrieving this view only.</p></div>`;}
  function askConfirm({title,message,confirmLabel='Continue',cancelLabel='Cancel',danger=false,reason=null}){return new Promise(resolve=>{let dialog=$('#confirm-dialog');if(!dialog){dialog=document.createElement('dialog');dialog.id='confirm-dialog';dialog.className='confirm-dialog';dialog.setAttribute('aria-labelledby','confirm-dialog-title');dialog.setAttribute('aria-describedby','confirm-dialog-message');document.body.appendChild(dialog);}if(dialog.open)dialog.close();dialog.innerHTML=`<form class="stack" id="confirm-dialog-form" novalidate><h2 id="confirm-dialog-title">${esc(title)}</h2><p id="confirm-dialog-message" class="muted">${esc(message)}</p>${reason?`<label for="confirm-dialog-reason">${esc(reason)}</label><textarea id="confirm-dialog-reason" rows="3" maxlength="1000" required></textarea>`:''}<div class="form-actions"><button type="button" data-confirm-cancel>${esc(cancelLabel)}</button><button type="submit" class="${danger?'danger':'primary'}" ${reason?'disabled':''}>${esc(confirmLabel)}</button></div></form>`;const form=$('form',dialog),field=$('#confirm-dialog-reason',dialog),submit=$('button[type="submit"]',form);let result=false;if(field)field.addEventListener('input',()=>{submit.disabled=!field.value.trim();});form.addEventListener('submit',event=>{event.preventDefault();event.stopPropagation();if(field && !field.value.trim())return;result=field?field.value:true;dialog.close();});$('[data-confirm-cancel]',dialog).addEventListener('click',event=>{event.stopPropagation();dialog.close();});dialog.addEventListener('close',()=>resolve(result),{once:true});dialog.showModal();(field || $('[data-confirm-cancel]',dialog)).focus();});}
  const pendingReads=new Map();
  function api(path,body,method,options={}){
    const verb=method || (body===undefined?'GET':'POST');
    if(verb!=='GET')return performApi(path,body,method,options);
    const key=`${state.sessionEpoch}:${options.scope || 'shared'}:${path}`;
    if(pendingReads.has(key))return pendingReads.get(key);
    const request=performApi(path,body,method,options);pendingReads.set(key,request);
    request.then(()=>{if(pendingReads.get(key)===request)pendingReads.delete(key);},()=>{if(pendingReads.get(key)===request)pendingReads.delete(key);});return request;
  }
  const viewApi=path=>api(path,undefined,undefined,{scope:`view:${state.requestSequence}`,signal:state.viewAbort?.signal});
  async function performApi(path, body, method, options={}) {
    const headers = {'Accept':'application/json'};
    const requestMethod=method || (body===undefined?'GET':'POST'), multipart=body instanceof FormData;
    if(!['GET','HEAD','OPTIONS'].includes(requestMethod) && !['/api/login','/api/operators/bootstrap','/api/logout'].includes(path) && !/^\/api\/cases\/[^/]+\/model\/preview$/.test(path) && !canReview())throw new Error('Shared access is read-only. Sign in with your personal account to make changes.');
    if(body !== undefined && !multipart)headers['Content-Type']='application/json';
    if(!['GET','HEAD','OPTIONS'].includes(requestMethod) && state.csrf)headers['X-CSRF-Token']=state.csrf;
    let response;
    try { response = await fetch(path,{method:requestMethod,headers,credentials:'same-origin',signal:options.signal,body:body===undefined?undefined:(multipart?body:JSON.stringify(body))}); }
    catch(error) { if(error.name==='AbortError')throw error;throw new Error('Cannot reach the workspace. Check that the local API is running, then refresh.'); }
    let result;try{result=await response.json();}catch{result={};}
    if(!response.ok){
      if(response.status === 401 && !['/api/login','/api/operators/bootstrap'].includes(path)){showLogin();throw new Error('Your session has expired. Sign in again.');}
      const message=typeof result.detail==='string'?result.detail:JSON.stringify(result.detail || result.error || `Request failed (${response.status})`);
      throw new Error(message);
    }
    return result;
  }
  function uuid(){
    if(typeof crypto.randomUUID==='function')return crypto.randomUUID();
    const bytes=crypto.getRandomValues(new Uint8Array(16));bytes[6]=(bytes[6]&15)|64;bytes[8]=(bytes[8]&63)|128;
    const hex=Array.from(bytes,b=>b.toString(16).padStart(2,'0')).join('');
    return `${hex.slice(0,8)}-${hex.slice(8,12)}-${hex.slice(12,16)}-${hex.slice(16,20)}-${hex.slice(20)}`;
  }
  function setLoginMode(mode){
    if(mode==='setup' && !state.setupRequired)mode='personal';state.loginMode=mode;
    $('#setup-mode').hidden=!state.setupRequired;
    for(const [name,id] of Object.entries({personal:'personal-login-form',legacy:'login-form',setup:'bootstrap-form'}))$('#'+id).hidden=name!==mode;
    document.querySelectorAll('[data-login-mode]').forEach(b=>{b.setAttribute('aria-pressed',String(b.dataset.loginMode===mode));});
    $('#login-error').hidden=true;const target=mode==='legacy'?'#access-token':mode==='setup'?'#setup-token':'#operator-username';if(!$('#login-screen').hidden)$(target).focus();
  }
  function showLogin(){state.swipe?.reset();cancelSwipeConfirmation();state.cardDrivers.clear();state.cardDriversRequest=null;state.sessionEpoch++;clearTimeout(state.viewRefreshTimer);state.pendingViewRefresh=false;state.viewAbort?.abort();state.dialogAbort?.abort();pendingReads.clear();unloadInvoicePdf();disconnect();stopModelMonitor();clearTimeout(state.filterTimer);state.automationRequest=null;state.automationPending=false;state.trainingRequest=null;state.recoveryRequests?.clear();state.invoiceFormatRequests?.clear();state.invoiceFormatPending?.clear();state.modelRuns=[];state.invoiceRows=[];state.invoiceListMarkup=null;state.caseRows=[];state.reviewListMarkup=null;state.automation=null;for(const entry of state.workbench.values())entry.versionsController?.abort();state.workbench.clear();state.activeCaseId=null;state.inspectionSelection.clear();state.compareCases.clear();renderWorkbench();state.session=null;state.csrf=null;state.revision=null;state.streamVersion=null;state.requestSequence++;state.pendingRefresh=false;state.selectedCases.clear();state.bulkResult=null;state.bulkRequest=null;state.bulkSnapshot=null;$('#workspace').hidden=true;$('#login-screen').hidden=false;if($('#detail-dialog').open)$('#detail-dialog').close();setLoginMode(state.loginMode);}
  function updateIdentity(){
    const s=state.session || {};$('#operator-display').textContent=canReview()?(s.display_name || s.username):'Shared access · read-only';
    $('#operator-description').textContent=canReview()?`@${s.username} · ${human(s.role)}`:'Individual reviewer unknown';
    $('#identity-notice').hidden=canReview();$('#team-navigation').hidden=!isAdmin();
  }
  const utc=value=>{if(!value)return 'Not recorded';const raw=String(value);if(/^\d{4}-\d{2}-\d{2}[T ].*(?:Z|\+00:00)$/.test(raw))return esc(raw.replace('T',' ').replace(/(?:Z|\+00:00)$/,' UTC'));const d=new Date(value);return Number.isNaN(d.getTime())?esc(value):esc(d.toISOString().replace('T',' ').replace('Z',' UTC'));};
  function reviewerMarkup(value){
    if(!value)return '<span class="small muted">No attributed review recorded</span>';
    const kind=value.identity_kind || value.identity_type,actor=value.actor_id || value.actor || (value.user_id?'user:'+value.user_id:null);
    const registered=kind==='REGISTERED_OPERATOR' || kind==='operator';
    if(registered)return `<span class="reviewer-name">${esc(value.display_name || value.username || 'Registered operator')}${value.username?` <span class="muted">(@${esc(value.username)})</span>`:''}</span><span class="secondary mono actor-id">${esc(actor || 'Stable ID unavailable')}</span>`;
    if(kind==='EXPLICIT_LOCAL_ACTOR')return `<span class="reviewer-name">${esc(value.display_name || actor || 'Local actor')}</span><span class="secondary">Explicit local actor · personal authentication not established</span><span class="secondary mono actor-id">${esc(actor)}</span>`;
    return `<span class="reviewer-name">Legacy shared identity — individual reviewer unknown</span>${actor?`<span class="secondary mono actor-id">Recorded actor label: ${esc(actor)}</span>`:''}`;
  }
  function recordedIdentity(){return `<div class="recorded-identity"><span class="small-label">RECORDED AS</span>${canReview()?reviewerMarkup(state.session):'<strong>Shared access · read-only</strong><span class="small muted">Sign in with a personal account to record a decision.</span>'}</div>`;}
  const mutationSelector='[data-invoice-format],[data-action="prepare"],[data-action="upload-source"],[data-action="import-worklist"],[data-action="set-provider"],[data-action="worker-tick"],[data-action="gmail-config"],[data-action="gmail-connect"],[data-action="gmail-disconnect"],[data-action="gmail-test"],[data-action="bulk-release"],[data-dispatch],[data-revoke],[data-reopen],[data-sim-event],[data-reconcile-attempt],[data-select-case],#select-page-cases';
  function applyIdentityPermissions(){
    document.querySelectorAll('#detail-content form').forEach(form=>{if(form.id!=='model-preview-form' && !$('.recorded-identity',form))form.insertAdjacentHTML('afterbegin',recordedIdentity());if(!canReview() && form.id!=='model-preview-form')form.querySelectorAll('input,select,textarea,button').forEach(c=>c.disabled=true);});
    if(!canReview())document.querySelectorAll(mutationSelector).forEach(c=>{c.disabled=true;c.title='Personal account required. Shared access is read-only.';});
  }
  function openProfile(){openDialog('SIGNED-IN IDENTITY','Your workspace access');$('#detail-content').innerHTML=`${recordedIdentity()}<dl class="details-grid">${detail('Access level',canReview()?esc(human(state.session.role)):'Read-only shared credential')}${detail('Personal attribution',canReview()?'Current decisions use your stable account ID':'Individual reviewer unknown')}</dl><p class="small muted">Account identity applies to new authenticated actions. Historical shared-token actions are never reassigned to this account.</p><div class="section-actions"><button data-action="profile-logout">Sign out</button>${!canReview()?'<button class="primary" data-action="switch-personal">Use personal account</button>':''}</div><p id="detail-error" class="error-text" hidden></p>`;}
  async function signOut(personal=false){await api('/api/logout',{});showLogin();try{const status=await api('/api/auth/status');state.setupRequired=status.setup_required===true;}catch{}setLoginMode(personal?(state.setupRequired?'setup':'personal'):state.loginMode);}

  async function enterWorkspace(session){
    state.sessionEpoch++;state.session=session;state.csrf=session.csrf_token;updateIdentity();document.querySelectorAll('#login-screen input[type=password]').forEach(i=>i.value='');$('#login-screen').hidden=true;$('#workspace').hidden=false;$('#access-token').value='';
    state.customers=rows(await api('/api/customers?limit=1000&offset=0'));
    state.summary=await api('/api/dashboard');updateSummary(state.summary);
    await openHashRoute();connect();
  }
  async function openHashRoute(){
    const hash=location.hash.slice(1),match=hash.match(/^case\/([^/]+)$/),batch=hash.match(/^batch\/([^/]+)$/);
    if(match){let id;try{id=decodeURIComponent(match[1]);}catch{showError(new Error('The case link is malformed.'));return;}await changeTab('review');await openCase(id);}
    else if(batch){let id;try{id=decodeURIComponent(batch[1]);}catch{showError(new Error('The batch link is malformed.'));return;}await changeTab('data');await openBatch(id);}
    else await changeTab(pages[hash]?hash:'overview');
  }
  function updateSummary(summary){
    if(!summary)return;state.summary=summary;
    const open=count('open_cases');$('#review-count').textContent=integer(open);$('#review-count').hidden=!open;
    $('#mode-badge').textContent=String(summary.mode || 'SIMULATION').toUpperCase();
    const synced=new Date();$('#last-synced').textContent=`${synced.toLocaleDateString('en-GB',{day:'numeric',month:'short'})} ${synced.toLocaleTimeString('en-GB')}`;$('#last-synced').title=synced.toISOString();
  }
  function connection(label,kind){$('#connection-label').textContent=label;$('#connection-dot').className=`connection-dot ${kind||''}`;}
  function disconnect(){state.events?.close();state.events=null;}
  function connect(){
    disconnect();if(document.hidden || !state.csrf)return;
    connection('Connecting');const events=new EventSource('/api/events');state.events=events;
    events.onopen=()=>connection('Live · 2s updates','live');
    events.onerror=()=>connection('Reconnecting…','retry');
    const receive=event=>{
      let data;try{data=JSON.parse(event.data);}catch{return;}
      if(data.summary || data.dashboard)updateSummary(data.summary || data.dashboard);
      const streamVersion=data.stream_version ?? data.revision;if(data.revision!==undefined)state.revision=data.revision;
      if(streamVersion===undefined || streamVersion===state.streamVersion)return;
      const first=state.streamVersion==null;state.streamVersion=streamVersion;
      if(first)return;
      const resources=Array.isArray(data.changed_resources)?new Set(data.changed_resources):null;
      const known=new Set(['cases','invoices','outbox','batches','model','automation','audit','customers']);
      const broad=!resources || [...resources].some(name=>!known.has(name));
      const changed=(...names)=>broad || names.some(name=>resources.has(name));
      if(changed('cases','batches','model')){for(const entry of state.workbench.values())entry.stale=true;renderWorkbench();}
      const relevant={overview:['cases','invoices','outbox','batches','automation','audit'],review:['cases','batches','model','customers'],invoices:['invoices','batches','customers'],insights:['cases','batches','model','customers'],delivery:['outbox','invoices'],data:['batches','model','audit','automation'],email:['automation'],team:['audit']};
      if(!changed(...(relevant[state.tab] || [])))return;
      if(swipeViewActive()){state.swipe?.invalidate(state.tab==='review'?'CASE':'INVOICE');const pending=state.swipeConfirmation;if(pending && !pending.submitted){pending.stale=true;const submit=$('#swipe-confirm-submit');if(submit)submit.disabled=true;const warning=$('#swipe-confirm-error');if(warning){warning.textContent='New data is available. Cancel and refresh the worklist before deciding.';warning.hidden=false;}}markPendingRefresh();return;}
      if($('#detail-dialog').open || state.loading || state.selectedCases.size){if(state.loading && !$('#detail-dialog').open && !state.selectedCases.size)state.pendingViewRefresh=true;markPendingRefresh();return;}
      refreshCurrent(false);
    };
    events.addEventListener('auth_expired',()=>{if(state.events!==events)return;disconnect();showLogin();$('#login-error').textContent='Your session has expired or this account no longer has access. Sign in again.';$('#login-error').hidden=false;});
    events.addEventListener('update',receive);events.onmessage=receive;
  }
  async function changeTab(tab){
    captureCaseDraft();state.swipe?.deactivate();cancelSwipeConfirmation();state.viewAbort?.abort();clearTimeout(state.filterTimer);stopModelMonitor();state.inspectionSelection.clear();
    if(!pages[tab])return;const requested=tab;if(tab==='team' && !isAdmin())tab='overview';clearCaseSelection();state.tab=tab;state.offset=0;state.requestSequence++;
    if(location.hash!==`#${tab}`){if(pages[location.hash.slice(1)] && location.hash!==`#${requested}`)history.pushState(null,'',`#${tab}`);else history.replaceState(null,'',`#${tab}`);}state.announceView=true;
    document.querySelectorAll('#navigation [data-tab]').forEach(button=>{const active=button.dataset.tab===tab;button.classList.toggle('active',active);active?button.setAttribute('aria-current','page'):button.removeAttribute('aria-current');});
    revealActiveNavigation();
    const [eyebrow,title,description]=pages[tab];$('#page-eyebrow').textContent=eyebrow;$('#page-title').textContent=title;$('#page-description').textContent=description;
    $('#tab-content').innerHTML=skeleton(tab);await refreshCurrent(false);
  }
  function revealActiveNavigation(){
    const nav=$('#navigation'),active=$('[aria-current="page"]',nav);if(!active || nav.scrollWidth<=nav.clientWidth)return;
    const item=active.getBoundingClientRect(),bounds=nav.getBoundingClientRect();if(item.right>bounds.right)nav.scrollLeft+=Math.ceil(item.right-bounds.right);else if(item.left<bounds.left)nav.scrollLeft-=Math.ceil(bounds.left-item.left);
  }
  async function refreshCurrent(refreshSummary=true){
    if(state.tab==='review' && state.selectedCases.size){markPendingRefresh();return;}
    state.swipe?.deactivate();cancelSwipeConfirmation();stopModelMonitor();clearTimeout(state.viewRefreshTimer);state.pendingViewRefresh=false;state.viewAbort?.abort();state.viewAbort=new AbortController();const sequence=++state.requestSequence;state.loading=true;$('#tab-content').setAttribute('aria-busy','true');$('#global-error').hidden=true;
    document.querySelectorAll('#tab-content [data-inspect-case], #inspect-page-cases, #open-inspected-cases, #tab-content [data-select-case], #select-page-cases, #bulk-release-open, #tab-content [data-case], #tab-content [data-invoice], #tab-content [data-outbox], #tab-content [data-batch]').forEach(control=>control.disabled=true);
    document.querySelectorAll('#tab-content a').forEach(link=>link.setAttribute('aria-disabled','true'));
    try{
      if(refreshSummary)updateSummary(await viewApi('/api/dashboard'));
      let content;
      if(state.tab==='overview')content=await overview(sequence);
      if(state.tab==='review')content=await review(sequence);
      if(state.tab==='invoices')content=await invoices(sequence);
      if(state.tab==='insights')content=await insightsView(sequence);
      if(state.tab==='delivery')content=await delivery(sequence);
      if(state.tab==='data')content=await dataView(sequence);
      if(state.tab==='email')content=await emailView();
      if(state.tab==='team')content=await teamView(sequence);
      if(sequence!==state.requestSequence)return;
      if(state.tab==='review' && state.selectedCases.size){markPendingRefresh();return;}
      const focused=document.activeElement?.matches('[data-case-filter]')?{id:document.activeElement.id,start:document.activeElement.selectionStart,end:document.activeElement.selectionEnd}:null;
      const before=rowSnapshot();$('#tab-content').innerHTML=content;const changed=highlightChanges(before);if(state.announceView){state.announceView=false;announce(`${pages[state.tab][1]} loaded`);}else if(changed)announce(`${pages[state.tab][1]}: ${changed} ${changed===1?'record':'records'} updated`);if(focused && $('#'+focused.id)){const field=$('#'+focused.id);field.focus();if(field.type==='text')field.setSelectionRange(focused.start,focused.end);}
      if(state.tab==='review')state.renderedReviewSequence=sequence;state.pendingRefresh=false;$('#update-notice').hidden=true;mountSwipeView();void loadCardDrivers();
    }catch(error){if(sequence===state.requestSequence && error.name!=='AbortError')showError(error);}
    finally{if(sequence===state.requestSequence){state.loading=false;$('#tab-content').setAttribute('aria-busy','false');startModelMonitor();if(state.pendingViewRefresh && !$('#detail-dialog').open && !state.selectedCases.size && !document.hidden){state.pendingViewRefresh=false;state.viewRefreshTimer=setTimeout(()=>{if(sequence===state.requestSequence && state.csrf && !document.hidden && !$('#detail-dialog').open)refreshCurrent(false);},100);}}}
  }
  async function overview(sequence){
    const [caseData,automation,automationStatus]=await Promise.all([viewApi('/api/cases?status=OPEN&limit=5&offset=0&sort=score_desc'),viewApi('/api/automation').catch(error=>{if(error.name==='AbortError')throw error;return {unavailable:errorText(error)};}),viewApi('/api/automation/status').catch(error=>{if(error.name==='AbortError')throw error;return null;})]);
    if(sequence!==state.requestSequence)return null;state.automation=automation;state.automationStatus=automationStatus && typeof automationStatus==='object' && automationStatus.monthly_close?automationStatus:null;
    const cases=rows(caseData),worker=state.summary?.worker;
    const enter=state.announceView===true,stagger=i=>enter?i:null,charts=window.MiniCharts,n=name=>Number(count(name)) || 0,proportion=(label,parts)=>charts && parts.some(p=>p.value>0)?charts.stackedBar(parts,{label,compact:true}):'';
    const share=(part,total)=>total>0?`${Math.round(part/total*1000)/10}%`:'—';
    const cards=metric('Needs review',count('open_cases'),n('open_cases')?`${share(n('open_cases')-n('score_unavailable'),n('open_cases'))} have a model score`:'No open exceptions','review',true,proportion('Open cases by score availability',[{label:'Scored',value:n('open_cases')-n('score_unavailable'),tone:'accent'},{label:'Score unavailable',value:n('score_unavailable'),tone:'neutral'}]),stagger(0))+metric('Awaiting approval',count('pending_approvals'),n('pending_approvals')?'Open the PDF, then approve':'Nothing waiting for you','invoices',false,proportion('Invoice packages awaiting approval',[{label:'Awaiting approval',value:n('pending_approvals'),tone:'accent'},{label:'Other packages',value:Math.max(0,n('invoices')-n('pending_approvals')),tone:'neutral'}]),stagger(1))+metric('Delivery attention',count('unknown_delivery'),n('unknown_delivery')?'Uncertain submissions to reconcile':'All submissions accounted for','delivery',false,proportion('Outbox status',[{label:'Uncertain',value:n('unknown_delivery'),tone:'warning'},{label:'Accepted',value:n('accepted'),tone:'success'},{label:'Queued',value:n('queued'),tone:'info'}]),stagger(2))+metric('Delivered',count('delivered'),`${share(n('delivered'),n('delivered')+n('accepted')+n('queued')+n('unknown_delivery')+n('bounced'))} of messages confirmed`,'delivery',false,proportion('Delivery evidence',[{label:'Delivered',value:n('delivered'),tone:'success'},{label:'Bounced',value:n('bounced'),tone:'danger'}]),stagger(3));
    /* Quarantine and failed jobs block billing (warning/danger); a missing score is
       a neutral data gap, never a zero score and never a red alarm. */
    const escalations=[[n('quarantined'),'quarantined inputs','data','quarantined input','warning','Inspect'],[n('jobs_failed'),'failed jobs','data','failed job','danger','Inspect'],[n('score_unavailable'),'cases without a model score','review','case without a model score','neutral','Review']].filter(x=>x[0]>0).map(([value,label,tab,one,tone,verb])=>[value,value===1?one:label,tab,tone,verb]);
    const escalationMarkup=escalations.length?`<ul class="escalation-list" aria-label="Needs attention">${escalations.map(([value,label,tab,tone,verb],i)=>`<li><button class="escalation-card tone-${tone}" data-tab="${tab}"${enter?` style="--i:${i}"`:''}><strong>${integer(value)}</strong><span>${label}</span><span class="escalation-link">${verb} →</span></button></li>`).join('')}</ul>`:'';
    const events=Array.isArray(state.summary?.recent_events)?state.summary.recent_events:[],when=value=>String(value || '').replace(' ','T').replace(/(\.\d{3})\d+/,'$1');
    const feed=events.length?`<ol class="activity-feed">${events.map((e,i)=>`<li${enter?` style="--i:${i}"`:''}><span class="activity-dot" aria-hidden="true"></span><div><strong>${esc(human(e.action))}</strong><span class="small muted">${esc(actorLabel(e))}${e.entity_id && String(e.actor || '')!=='user:'+e.entity_id?` · <span class="mono" title="${esc(e.entity_id)}">${esc(String(e.entity_id).slice(0,10))}${String(e.entity_id).length>10?'…':''}</span>`:''}</span></div><time data-relative datetime="${esc(when(e.created_at))}" title="${esc(e.created_at || '')}">${esc(window.IOMotion?.relative(when(e.created_at)) || e.created_at || '')}</time></li>`).join('')}</ol>`:empty('No recent activity','Recorded actions appear here as they happen.');
    const deliveryParts=[{label:'Delivered',value:n('delivered'),tone:'success'},{label:'Accepted by provider',value:n('accepted'),tone:'info'},{label:'Queued',value:n('queued'),tone:'neutral'},{label:'Uncertain',value:n('unknown_delivery'),tone:'warning'},{label:'Bounced',value:n('bounced'),tone:'danger'}],shownParts=deliveryParts.filter(p=>p.value>0);
    const insights=charts?`<div class="insights-grid"><section class="panel"><div class="panel-header"><div><h2>Workflow</h2><p class="panel-subtitle">From source movements to delivered invoices · select a step</p></div></div><div class="panel-body">${charts.funnel([{label:'Movements received',value:n('movements'),tab:'data'},{label:'Eligible for billing',value:n('eligible'),tab:'data'},{label:'In review',value:n('open_cases'),tab:'review'},{label:'Invoiced',value:n('invoices'),tab:'invoices'},{label:'Delivered',value:n('delivered'),tab:'delivery'}],{label:'Bar length is relative to the largest step'})}</div></section><section class="panel"><div class="panel-header"><div><h2>Delivery status</h2><p class="panel-subtitle">Outbox and recipient evidence</p></div><button class="quiet small" data-tab="delivery">Open →</button></div><div class="panel-body">${charts.donut(shownParts.length?shownParts:deliveryParts,{label:'Delivery status',center:'messages'})}</div></section><section class="panel activity-panel"><div class="panel-header"><div><h2>Recent activity</h2><p class="panel-subtitle">Latest recorded audit events</p></div><span class="live-chip" aria-hidden="true">Live</span></div><div class="panel-body">${feed}</div></section></div>`:'';
    const next=count('open_cases')>0?['Review flagged movements',`${integer(count('open_cases'))} exceptions are holding up monthly invoices. Start with the highest risk.`,'review']:count('pending_approvals')>0?['Approve your draft invoices',`${integer(count('pending_approvals'))} packages are waiting. Open each exact PDF, then approve it.`,'invoices']:['You are up to date','No exceptions or drafts are waiting. New source files are checked automatically.','data'];
    const caseContent=cases.length?cases.map(item=>`<div class="review-item"><div><strong>${esc(customerName(item.customer_id))}</strong><small><span class="mono">${esc(item.external_movement_id || String(item.movement_id || item.id).slice(0,12))}</span> · ${esc(human(item.reason))} · ${esc(item.period || '')}</small></div><span class="review-item-figures">${caseTransactionAmount(item)}${score(caseListScore(item))}</span><button class="table-action" data-case="${esc(item.id)}">Review →</button></div>`).join(''):empty('No open exceptions','Flagged movements will appear here as the sources are checked.');
    return `<div class="overview-top"><div class="overview-main"><section class="action-center"><div><p class="eyebrow">YOUR NEXT STEP</p><h2>${next[0]}</h2><p>${next[1]}</p></div><div class="section-actions"><button class="primary" data-tab="${next[2]}">${next[2]==='review'?'Open review queue':next[2]==='invoices'?'Open invoices':'Open source checks'} →</button><button data-action="upload-source">Upload sources</button></div>${nextSteps()}</section><div class="summary-grid kpi-strip">${cards}</div></div>${automationPanel(automation,worker)}</div>${escalationMarkup}
      ${insights || `<div class="workflow-strip" aria-label="Current workflow"><div><strong>${integer(count('movements'))}</strong><span>Movements received</span></div><span aria-hidden="true">→</span><div><strong>${integer(count('eligible'))}</strong><span>Eligible for billing</span></div><span aria-hidden="true">→</span><div><strong>${integer(count('pending_approvals'))}</strong><span>Waiting for you</span></div><span aria-hidden="true">→</span><div><strong>${integer(count('queued'))}</strong><span>Messages queued</span></div></div>`}
      <section class="panel review-next-panel"><div class="panel-header"><div><h2>Review next</h2><p class="panel-subtitle">Highest risk open cases first</p></div><button class="quiet small" data-tab="review">View all →</button></div><div class="panel-body">${caseContent}</div></section>
      <details class="overview-verification"><summary>More verification details</summary><div class="overview-checks"><span>${integer(count('quarantined'))} quarantined inputs</span><span>${integer(count('score_unavailable'))} cases without a model score</span><span>${integer(count('jobs_failed'))} failed jobs</span><span>${integer(count('customers'))} customers</span></div><p class="small muted">Open Data & model for source files, model evidence, queues and the audit trail.</p><button data-tab="data">Open processing checks →</button></details>`;
  }
  /* Readable actor for the activity feed: never a raw user:<hex> string. */
  function actorLabel(e){
    const actor=String(e?.actor || ''),named=e?.actor_display_name || e?.display_name || e?.actor_name;
    if(named)return named;
    if(!actor)return 'Unknown actor';
    if(state.session?.actor && actor===state.session.actor)return 'You';
    if(actor==='automatic-scheduler' || /^worker/.test(actor))return 'Automation';
    if(actor==='cli-admin' || /^cli/.test(actor))return 'Command line';
    if(/^user:/.test(actor))return `Operator ${actor.slice(5,11)}…`;
    if(/^(legacy|shared|token)/i.test(actor))return 'Shared access';
    return human(actor);
  }
  /* Guided path: the first step that still has work is the current one. */
  function nextSteps(){
    const n=name=>Number(count(name)) || 0,auto=state.summary?.automation_control || {};
    const steps=[['Review exceptions',n('open_cases'),'open','review'],['Approve invoices',n('pending_approvals'),'waiting','invoices'],['Automatic sending',n('queued'),'queued','delivery'],['Delivery evidence',n('unknown_delivery'),'uncertain','delivery']];
    const current=steps.findIndex(s=>s[1]>0);
    return `<ol class="next-steps" aria-label="Billing steps">${steps.map(([label,value,unit,tab],i)=>{const stateName=current===-1 || i<current?'done':i===current?'current':'todo';const note=i===2 && auto.dispatch_after_approval_enabled===false?'Off':i===2 && value>0 && state.automationStatus?.mail?.ready===false?`${integer(value)} needs Gmail`:value>0?`${integer(value)} ${unit}`:stateName==='done'?'Clear':'Nothing yet';return `<li class="step is-${stateName}"><button type="button" data-tab="${tab}"${stateName==='current'?' aria-current="step"':''}><span class="step-index" aria-hidden="true">${stateName==='done'?'✓':i+1}</span><span class="step-text"><span class="step-label">${label}</span><span class="step-note">${note}</span></span></button></li>`;}).join('')}</ol>`;
  }
  /* Month-end automation runs on the first London day after a month ends. */
  function nextMonthEndRun(){
    const close=state.automationStatus?.monthly_close,explicit=close?.next_run_date?`${close.next_run_date}T12:00:00Z`:null;
    try{
      const parts=Object.fromEntries(new Intl.DateTimeFormat('en-GB',{timeZone:'Europe/London',year:'numeric',month:'numeric'}).formatToParts(new Date()).map(p=>[p.type,p.value]));
      const y=Number(parts.year),m=Number(parts.month),ny=m===12?y+1:y,nm=m===12?1:m+1;
      const day=explicit?new Date(explicit):new Date(Date.UTC(ny,nm-1,1,12));
      const closing=explicit?new Date(Date.UTC(day.getUTCFullYear(),day.getUTCMonth()-1,1,12)):new Date(Date.UTC(y,m-1,1,12));
      if(Number.isNaN(day.getTime()))return null;
      return {when:day.toLocaleDateString('en-GB',{day:'numeric',month:'short',year:'numeric',timeZone:'Europe/London'}),period:closing.toLocaleDateString('en-GB',{month:'long',year:'numeric',timeZone:'UTC'})};
    }catch{return null;}
  }
  function clock(value){if(!value)return null;const d=new Date(value);return Number.isNaN(d.getTime())?null:d.toLocaleTimeString('en-GB',{hour:'2-digit',minute:'2-digit'});}
  function automationPanel(value,worker){
    const config=value?.config,available=Boolean(config) && Number.isInteger(value?.revision),s=state.summary || {};
    const st=state.automationStatus;if(st?.worker)worker=st.worker;
    const running=worker && worker.stale===false && String(worker.status || '').toUpperCase()==='RUNNING';
    const status=!available?'Unavailable':config.paused?'Paused':running?'Running':'Worker stopped';
    const tone=status==='Running'?'good':status==='Paused'?'warn':status==='Unavailable'?'neutral':'bad';
    const flag=v=>v===true?'On':v===false?'Off':'Unavailable';
    const run=nextMonthEndRun(),close=s.automation?.['scheduler.close.report'],closeReport=close?.report;
    const refresh=st?.data_refresh || null,refreshEvery=Number(s.refresh_seconds) || null,inbox=refresh?.last_inbox_report;
    const mail=st?.mail || {},provider=String(mail.provider || s.provider || '').toLowerCase();
    const gmailConnected=mail.gmail_connected===true || state.gmail?.connected===true;
    const redirect=mail.redirect_active===true || state.gmail?.test_redirect?.active===true;
    const transport=provider==='gmail'?(gmailConnected?'Gmail · connected':'Gmail · not connected yet'):provider==='smtp'?'SMTP server':provider==='simulated' || provider==='simulator'?'Simulated · no real email is sent':'Unavailable';
    const workerLine=running?`Running · heartbeat ${worker.age_seconds!=null?`${Math.round(worker.age_seconds)} s ago`:'recent'}`:worker?.status==='UNAVAILABLE'?'Not started':'Not running';
    return `<section class="panel automation-panel tone-${tone}" id="automation-panel"><div class="panel-header"><div><h2>Automation</h2><p class="panel-subtitle">What happens without you, after your approval.</p></div><span class="badge ${tone}">${esc(status)}</span></div><div class="panel-body">
      ${redirect?`<div class="notice redirect-banner" role="note"><strong>Test mode:</strong> All emails are redirected to your connected address.</div>`:''}
      ${!running && available && !config.paused?`<div class="notice warning automation-fix"><strong>The background worker is not running,</strong> so month-end drafts and approved sending are on hold. Start the console with <code>scripts/run_local.py</code> (API and worker together) or run <code>invoice-optimizer worker</code>.</div>`:''}
      <dl class="automation-facts">
        <div><dt>Next month-end run</dt><dd>${run?`<strong>${esc(run.when)}</strong><span class="secondary">Prepares ${esc(run.period)} drafts for your approval${available && config.monthly_preparation_enabled===false?' · currently off':''}</span>`:'Unavailable'}</dd></div>
        <div><dt>Data refresh</dt><dd><strong>${refresh?(refresh.inbox_enabled || refresh.demo_feed_enabled?'Automatic':'Manual upload'):'Live view'}</strong><span class="secondary">${refresh?.inbox_enabled?`New files in the inbox are imported every ${esc(refresh.inbox_seconds)} s`:refresh?.demo_feed_enabled?`Demo feed every ${esc(refresh.demo_feed_seconds)} s`:refresh?'Upload sources to add data':''}${inbox?.checked_at && clock(inbox.checked_at)?` · last check ${esc(clock(inbox.checked_at))}`:''}${refresh?' · ':''}screen updates every ${esc(refreshEvery || 2)} s</span></dd></div>
        <div><dt>Email delivery</dt><dd><strong>${esc(transport)}</strong><span class="secondary">${provider==='gmail' && !gmailConnected?'Approved invoices wait until you connect Gmail in Email setup · ':mail.ready===false && mail.reason?`${esc(mail.reason)} · `:''}${available?`Sending after approval: ${flag(config.dispatch_after_approval_enabled)} · email: ${flag(config.email_delivery_enabled)}`:''}</span></dd></div>
        <div><dt>Background worker</dt><dd><strong>${esc(workerLine)}</strong>${closeReport?`<span class="secondary">Last month-end check ${esc(clock(close.checked_at) || '')}: ${integer(closeReport.prepared)} prepared · ${integer(closeReport.blocked)} blocked</span>`:''}</dd></div>
      </dl>
      ${available?'':`<p class="small muted">${esc(value?.unavailable || 'Automation configuration is unavailable.')}</p>`}
      <p class="hint">Human approval is always required. Automation prepares drafts and sends only invoices you approved.</p><div class="section-actions"><button data-action="automation-settings" ${available?'':'disabled'}>${isAdmin()?'Manage automation':'View automation settings'}</button><button class="quiet small" data-tab="email">Email setup →</button></div></div></section>`;
  }
  async function automationSettings(){
    const generation=openDialog('WORKSPACE AUTOMATION','Automation settings');
    try{
      const value=await api('/api/automation',undefined,undefined,{scope:`dialog:${generation}`,signal:state.dialogAbort?.signal});if(!dialogCurrent(generation))return;state.automation=value;
      if(!value.config || !Number.isInteger(value.revision))throw new Error('Automation configuration is unavailable.');
      const config=value.config,disabled=!isAdmin()?'disabled':'';
      const toggle=(key,label,hint)=>`<label class="automation-toggle"><input type="checkbox" name="${key}" ${config[key]?'checked':''} ${disabled}><span><strong>${label}</strong><small>${hint}</small></span></label>`;
      const limits=[['jobs_per_tick','Jobs per worker tick',1,100],['emails_per_tick','Messages per tick',0,100],['monthly_customers_per_cycle','Monthly customers per cycle',1,20],['dispatch_per_cycle','Approved packages per cycle',1,100],['queue_high_watermark','Queue intake limit',100,100000]];
      $('#detail-content').innerHTML=`<p class="small muted">Changes are recorded with your account and the inspected configuration revision. They never approve invoices or bypass source checks.</p>${!isAdmin()?'<div class="notice">A personal administrator account is required to change automation. You can inspect the current settings below.</div>':''}<form id="automation-form" data-revision="${value.revision}" class="stack">${toggle('paused','Pause background work','Stops new claims. Already running tasks, source intake and read access continue.')}${toggle('monthly_preparation_enabled','Prepare monthly invoices','Create draft packages after the required source and review checks pass.')}${toggle('dispatch_after_approval_enabled','Queue approved invoices','Only the exact package explicitly approved by a person is eligible.')}${toggle('email_delivery_enabled','Process simulated email delivery','This workspace still uses the simulator; enabling this does not connect real transport.')}<details><summary>Advanced processing limits</summary><div class="form-grid">${limits.map(([key,label,min,max])=>`<div><label for="automation-${key}">${label}</label><input id="automation-${key}" name="${key}" type="number" min="${min}" max="${max}" step="1" value="${esc(config[key])}" ${disabled}></div>`).join('')}</div></details><label for="automation-reason">Reason for this change</label><textarea id="automation-reason" required minlength="5" maxlength="1000" ${disabled} placeholder="Explain the operational reason."></textarea><button type="submit" class="primary" ${disabled}>Save automation settings</button><p class="hint">Inspected revision ${value.revision}. A newer configuration requires an explicit reload.</p><p id="detail-error" class="error-text" role="alert" hidden></p></form><details><summary>Recorded configuration and effective settings</summary>${json(value)}</details><button class="small" data-action="reload-automation">Reload current settings</button>`;
    }catch(error){if(dialogCurrent(generation) && error.name!=='AbortError')$('#detail-content').innerHTML=`<div class="notice error">${esc(errorText(error))}</div>`;}
  }
  async function saveAutomation(form,generation){
    if(!isAdmin())throw new Error('A personal administrator account is required.');if(state.automationPending)return;
    const patch={};for(const key of ['paused','monthly_preparation_enabled','dispatch_after_approval_enabled','email_delivery_enabled'])patch[key]=form.elements[key].checked;
    for(const key of ['jobs_per_tick','emails_per_tick','monthly_customers_per_cycle','dispatch_per_cycle','queue_high_watermark'])patch[key]=Number(form.elements[key].value);
    const payload={patch,expected_revision:Number(form.dataset.revision),reason:$('#automation-reason').value.trim()};const signature=JSON.stringify(payload);
    if(state.automationRequest?.signature!==signature)state.automationRequest={signature,id:uuid()};state.automationPending=true;
    try{const value=await api('/api/automation',{...payload,request_id:state.automationRequest.id},'PATCH');state.automation=value;state.automationRequest=null;toast('Automation settings saved. Existing approvals and running work retain their safeguards.');if(dialogCurrent(generation))$('#detail-dialog').close();await refreshCurrent();}
    finally{state.automationPending=false;}
  }
  function pagination(length,total=null){return `<div class="pagination"><span>${length?`${state.offset+1}–${state.offset+length}`:'0'}${Number.isInteger(total)?` of ${integer(total)}`:''} records · ${state.limit} per page</span><div><button data-page="previous" ${state.offset===0?'disabled':''}>← Previous</button><button data-page="next" ${length<state.limit?'disabled':''}>Next →</button></div></div>`;}
  async function review(sequence){
    const result=await viewApi(`/api/cases?${caseQuery()}`),items=rows(result);if(sequence!==state.requestSequence || state.selectedCases.size)return null;state.caseRows=items;state.cardDrivers.clear();state.cardDriversRequest=null;
    const eligible=items.filter(releaseEligible),exportQuery=caseQuery(false);
    const list=items.length?`<div class="table-wrap"><table class="review-table operations-table"><thead><tr><th class="selection-cell inspect-cell"><span>Inspect</span></th><th class="selection-cell"><span>Release</span></th><th>Customer / movement</th><th>Reason</th><th>Risk score</th><th>Status</th><th>Original source files</th><th>Period</th><th>Action</th></tr></thead><tbody>${items.map(item=>`<tr><td class="selection-cell inspect-cell"><input type="checkbox" data-inspect-case="${esc(item.id)}" aria-label="Select case ${esc(item.id)} for inspection"></td><td class="selection-cell"><input type="checkbox" data-select-case="${esc(item.id)}" aria-label="Select case ${esc(item.id)} for release review" ${releaseEligible(item)?'':'disabled'} title="${releaseEligible(item)?'Current-version model concern eligible for release review':'Not eligible for bulk release; inspect this case individually'}"></td><td><span class="cell-main">${esc(customerName(item.customer_id))}</span><span class="secondary mono" title="${esc(item.movement_id)}">${esc(item.external_movement_id || item.movement_id)}</span><span class="secondary">${caseVersionLabel(item)}</span><span class="secondary">${caseTransactionAmount(item)}</span></td><td>${esc(human(item.reason))}<span class="secondary">${esc(human(item.severity))}</span></td><td>${score(caseListScore(item))}</td><td>${badge(item.status)}</td><td class="source-cell">${sourceReferences(item,true)}</td><td>${esc(item.period)}</td><td><button class="table-action" data-case="${esc(item.id)}">Review →</button></td></tr>`).join('')}</tbody></table></div>`:empty('No cases in this view','Change the customer or status filter, or wait for new assessments.');
    state.reviewListMarkup=list;
    return `<section class="panel"><div class="panel-header"><div><h2>Movement exceptions</h2><p class="panel-subtitle">A risk score supports review; it does not prove a transaction is invalid.</p></div><div class="section-actions"><a class="small" id="case-export" href="/api/exports/cases.csv?${esc(exportQuery.toString())}">Export filtered worklist ↗</a><a class="small" id="history-export" href="/api/exports/review-history.csv?${esc(new URLSearchParams(state.customerFilters.review?{customer_id:state.customerFilters.review}:{}).toString())}">Structured history CSV ↗</a><button data-action="import-worklist">Import corrections</button></div></div><div class="filters">${customerFilter('review')}<div><label for="case-status">Case status</label><select id="case-status"><option value="OPEN" ${state.caseStatus==='OPEN'?'selected':''}>Open</option><option value="RESOLVED" ${state.caseStatus==='RESOLVED'?'selected':''}>Resolved</option><option value="" ${!state.caseStatus?'selected':''}>All statuses</option></select></div><span class="spacer"></span><div class="view-switch" role="group" aria-label="Review display"><button data-review-layout="cards" aria-pressed="${state.reviewLayout==='cards'}">Cards</button><button data-review-layout="list" aria-pressed="${state.reviewLayout==='list'}">List</button><button data-review-layout="swipe" aria-pressed="${state.reviewLayout==='swipe'}">Swipe</button></div></div>${caseFilterControls()}<div class="inspect-toolbar"><div><strong>Inspect any case</strong><p>Choose up to 8 cases to open side by side in your workbench. Inspection never releases a movement.</p></div><div class="section-actions"><label class="review-select-all"><input id="inspect-page-cases" type="checkbox" aria-label="Select first eight cases on this page for inspection" ${items.length?'':'disabled'}>Inspect first 8</label><button id="open-inspected-cases" data-action="open-inspected-cases" disabled>Open selected (0/8)</button></div></div><div class="bulk-toolbar"><div><strong id="selected-case-count">0 selected on this page</strong><p>Only eligible model concerns can be selected. Changing filters or pages clears the selection.</p></div><div class="section-actions"><label class="review-select-all"><input id="select-page-cases" type="checkbox" aria-label="Select eligible cases on this page" ${eligible.length?'':'disabled'}>Select eligible releases</label><button id="bulk-results-open" data-action="bulk-results" ${state.bulkResult?'':'hidden'}>Last release results</button><button id="clear-case-selection" data-action="clear-case-selection" disabled>Clear</button><button id="bulk-release-open" class="primary" data-action="bulk-release" disabled>Review selected releases</button></div></div><div id="review-presentation">${state.reviewLayout==='swipe'?'':state.reviewLayout==='cards'?reviewCards(items):list}</div>${pagination(items.length,listTotal(result))}</section>`;
  }
  function caseListScore(item){return item.model===undefined?item.score:(Number.isFinite(item.model?.recorded_score)?item.model.recorded_score:null);}
  function caseVersionLabel(item){
    const recorded=item.version_no,current=item.current_version;
    if(recorded==null)return 'Recorded version unavailable';
    return current===recorded?`Current version ${esc(recorded)}`:`Case version ${esc(recorded)}${current!=null?` · current ${esc(current)}`:''}`;
  }
  function caseTransactionAmount(item){
    const transaction=item.transaction || {};
    return transaction.amount==null?'<span class="muted">Amount unavailable</span>':amount(transaction.amount,transaction.currency || 'Currency unavailable');
  }
  function reviewCards(items){
    if(!items.length)return empty('No cases in this view','Change the customer or status filter, or wait for new assessments.');
    return `<div class="review-card-grid">${items.map(item=>{
      const transaction=item.transaction || {},model=item.model || {},rule=item.rule || {},canRelease=releaseEligible(item),recorded=Number.isFinite(model.recorded_score)?model.recorded_score:null,threshold=state.cardDrivers?.get(item.id)?.status==='AVAILABLE'?state.cardDrivers.get(item.id).threshold:(Number.isFinite(model.threshold)?model.threshold:null);
      const sourceAmount=(value,currency)=>value==null?'<span class="muted">Unavailable</span>':amount(value,currency || 'Currency unavailable');
      const reasons=[...new Set([...(Array.isArray(model.recorded_reasons)?model.recorded_reasons:[]),...(Array.isArray(rule.recorded_reasons)?rule.recorded_reasons:[])].filter(reason=>typeof reason==='string' && reason && reason!==item.reason))].slice(0,3);
      return `<article class="review-card" data-review-card="${esc(item.id)}"><div class="review-card-top"><div>${badge(item.status)}${badge(item.severity || 'UNKNOWN')}</div><span class="small muted">${esc(item.period || 'Period unavailable')}</span></div><h3>${esc(customerName(item.customer_id))}</h3><p class="review-movement-ref mono" title="${esc(item.movement_id || item.id)}">${esc(item.external_movement_id || item.movement_id || 'Movement reference unavailable')}</p><p class="review-version-label">${caseVersionLabel(item)}</p>${transaction.description?`<p class="review-card-description" title="${esc(transaction.description)}">${esc(transaction.description)}</p>`:''}<div class="review-transaction"><div><span class="small-label">Recorded amount</span><strong>${caseTransactionAmount(item)}</strong></div><div class="review-source-amounts"><div><span>Source A</span><strong>${sourceAmount(transaction.source_a_amount,transaction.source_a_currency)}</strong></div><div><span>Source B</span><strong>${sourceAmount(transaction.source_b_amount,transaction.source_b_currency)}</strong></div></div></div><div class="review-model-summary"><div class="review-model-score"><div><span class="small-label">Recorded risk score</span><strong>${score(recorded)}</strong><span class="secondary mono">p = ${esc(numeric(recorded))}</span></div><div><span class="small-label">Recorded threshold</span><div data-card-threshold="${esc(item.id)}"><strong>${score(threshold)}</strong><span class="secondary mono">p = ${esc(numeric(threshold))}</span></div></div></div><p class="small mono">Logistic regression · ${esc(model.model_version || 'model unavailable')}</p><p class="review-case-reason"><strong>${esc(human(item.reason))}</strong>${model.status?`<span class="secondary">Model assessment: ${esc(human(model.status))}</span>`:''}${rule.status?`<span class="secondary">Source checks: ${esc(human(rule.status))}</span>`:''}</p>${reasons.length?`<ul class="review-recorded-reasons" aria-label="Recorded model and source reasons">${reasons.map(reason=>`<li>${esc(human(reason))}</li>`).join('')}</ul>`:''}</div>${driverMarkup(item)}<div class="review-card-sources"><span class="small-label">Original source files</span>${sourceReferences(item,true)}</div><div class="review-card-selection"><label><input type="checkbox" data-inspect-case="${esc(item.id)}" aria-label="Select case ${esc(item.id)} for inspection">Inspect</label><label title="${canRelease?'Current-version model concern eligible for release review':'Not eligible for bulk release; inspect this case individually'}"><input type="checkbox" data-select-case="${esc(item.id)}" aria-label="Select case ${esc(item.id)} for release review" ${canRelease?'':'disabled'}>Release review</label></div><button class="primary review-card-open" data-case="${esc(item.id)}">Review movement →</button></article>`;
    }).join('')}</div>`;
  }
  function setReviewLayout(layout){
    if(!['cards','list','swipe'].includes(layout) || state.loading || state.renderedReviewSequence!==state.requestSequence)return;
    const target=$('#review-presentation');if(!target)return;state.reviewLayout=layout;
    state.swipe?.deactivate();target.innerHTML=layout==='swipe'?'':layout==='cards'?reviewCards(state.caseRows || []):state.reviewListMarkup;
    document.querySelectorAll('[data-review-layout]').forEach(button=>button.setAttribute('aria-pressed',String(button.dataset.reviewLayout===layout)));
    updateInspectSelection();updateCaseSelection();applyIdentityPermissions();mountSwipeView();void loadCardDrivers();
  }
  function swipeViewActive(){return (state.tab==='review' && state.reviewLayout==='swipe') || (state.tab==='invoices' && state.invoiceLayout==='swipe');}
  function swipeHost(){
    if(!state.swipe && window.SwipeWorkspace)state.swipe=window.SwipeWorkspace.create({api,canReview,getEpoch:()=>state.sessionEpoch,confirmAction:confirmSwipeAction,onMessage:toast});
    return state.swipe;
  }
  function renderSwipeCard(kind,item){
    const holder=document.createElement('div');holder.innerHTML=kind==='CASE'?reviewCards([item]):invoiceCards([item]);
    const card=holder.querySelector('article');card.classList.add('swipe-evidence-card');
    card.querySelectorAll('.review-card-selection,.review-card-open').forEach(node=>node.remove());
    if(kind==='CASE')card.insertAdjacentHTML('beforeend',`<button class="small" data-case="${esc(item.id)}">Open full evidence</button>`);
    if(kind==='INVOICE'){
      const paper=card.querySelector('.invoice-paper');
      paper.outerHTML=`<div class="swipe-invoice-heading"><div><h3>${esc(customerName(item.customer_id))}</h3><span class="mono">${esc(item.number || item.id)}</span></div><div><span class="small-label">Invoice total</span><strong>${amount(item.gross_total,item.currency)}</strong></div></div><section data-swipe-pdf="${esc(item.id)}" class="swipe-pdf" aria-label="Exact invoice PDF preview"><p class="small muted">The PDF loads when this card is active.</p></section>`;
      const enlarge=card.querySelector('[data-preview-invoice]');if(enlarge){enlarge.textContent='Enlarge PDF';enlarge.setAttribute('aria-label',`Enlarge PDF ${item.number || item.id}`);}
    }
    return card;
  }
  function mountSwipeView(){
    if(!swipeViewActive())return;const kind=state.tab==='review'?'CASE':'INVOICE';
    const target=$('#'+(kind==='CASE'?'review':'invoice')+'-presentation');if(!target)return;
    const host=swipeHost();if(!host){target.innerHTML=empty('Swipe is unavailable','Reload this page to load the interaction module.');return;}
    host.mount(target,{kind,items:kind==='CASE'?(state.caseRows || []):(state.invoiceRows || []),scope:`${state.sessionEpoch}:${state.tab}:${state.requestSequence}`,renderCard:item=>renderSwipeCard(kind,item),getToken:item=>kind==='CASE'?(item.version_no ?? null):(item.fingerprint ?? null),onActiveCard:kind==='INVOICE'?({item,element})=>{
      const panel=element.querySelector('[data-swipe-pdf]');
      if(window.SwipePdf)return window.SwipePdf.mount(panel,{item,api,onUnauthorized:showLogin});
      panel.textContent='Reload the workspace to load the PDF preview.';
    }:undefined});
  }
  function driverMarkup(item){
    const entry=state.cardDrivers?.get(item.id),valid=entry && entry.version_id===item.version_id;
    return `<div data-card-drivers="${esc(item.id)}">${window.RiskBars?.markup(valid?entry:null) || '<p class="hint">Open the case to inspect model parameters.</p>'}</div>`;
  }
  async function loadCardDrivers(){
    if(state.tab!=='review' || state.reviewLayout==='list' || !window.RiskBars)return;
    const items=(state.caseRows || []).slice(0,50);if(!items.length)return;
    const sequence=state.requestSequence,epoch=state.sessionEpoch,key=`${epoch}:${sequence}`;
    if(state.cardDriversRequest===key)return;state.cardDriversRequest=key;
    const params=new URLSearchParams();items.forEach(item=>params.append('case_id',item.id));
    const current=()=>state.sessionEpoch===epoch && state.requestSequence===sequence && state.tab==='review';
    try{
      const response=await viewApi('/api/case-drivers?'+params);
      if(!current())return;
      for(const result of response.items || []){
        const item=items.find(row=>row.id===result.case_id);if(!item || item.version_id!==result.version_id)continue;
        if(result.status==='AVAILABLE' && (result.recorded_score!==item.model?.recorded_score || result.model_version!==item.model?.model_version))continue;
        state.cardDrivers.set(item.id,result);
      }
      for(const item of items){
        const result=state.cardDrivers.get(item.id) || {status:'UNAVAILABLE',reason:'Matching evidence was not returned for this case version.'};
        document.querySelectorAll('[data-card-drivers]').forEach(node=>{if(node.dataset.cardDrivers===item.id)node.innerHTML=window.RiskBars.markup(result);});
        if(result.status==='AVAILABLE')document.querySelectorAll('[data-card-threshold]').forEach(node=>{if(node.dataset.cardThreshold===item.id)node.innerHTML=`${score(result.threshold)}<span class="secondary mono">p = ${esc(numeric(result.threshold))}</span><span class="secondary">Verified frozen model</span>`;});
      }
    }catch(error){if(current() && error.name!=='AbortError')document.querySelectorAll('[data-card-drivers]').forEach(node=>{node.innerHTML=window.RiskBars.markup({status:'UNAVAILABLE',reason:errorText(error)});});}
  }
  function cancelSwipeConfirmation(){
    const pending=state.swipeConfirmation;if(!pending)return;state.swipeConfirmation=null;
    if(!pending.submitted)pending.settle({accepted:false});
  }
  async function confirmSwipeAction({kind,item,action,signal}){
    if(!canReview())throw new Error('Sign in with a personal account to record a decision.');
    const epoch=state.sessionEpoch,actor=state.session.actor,generation=openDialog('SWIPE REVIEW',action==='reject'?'Review rejection':'Review and confirm');
    const current=()=>dialogCurrent(generation) && state.sessionEpoch===epoch && !signal?.aborted;
    try{
    const result=await api(kind==='CASE'?`/api/cases/${encodeURIComponent(item.id)}`:`/api/invoices/${encodeURIComponent(item.id)}/package`,undefined,undefined,{scope:`swipe:${generation}`,signal:state.dialogAbort.signal});
    if(!current())return {accepted:false};
    if(kind==='CASE'){
      if(result.case?.id!==item.id || result.case?.version_id!==item.version_id || result.version?.version_no!==item.version_no || result.movement?.current_version!==item.version_no || result.case?.status!=='OPEN')throw new Error('This case changed. Refresh the worklist before recording a decision.');
      if(action==='review' && !releaseEligible(item))throw new Error('Only eligible model concerns can be released. Source-control failures require correction.');
    }else{
      if(!item.fingerprint || result.fingerprint!==item.fingerprint || result.id!==item.id)throw new Error('This invoice package changed. Refresh the worklist before recording a decision.');
      if(action==='review' && (result.status!=='DRAFT' || result.pdf_ready===false))throw new Error('Only a complete current draft can be approved.');
      if(action==='reject' && !['DRAFT','APPROVED'].includes(result.status))throw new Error('Only an unissued draft or approved package can be rejected.');
      if(action==='reopen' && result.status!=='REJECTED')throw new Error('Only a rejected unissued package can be reopened.');
    }
    const title=action==='reopen'?'Reopen rejected invoice for review':kind==='CASE'?(action==='reject'?'Confirm issue — keep movement blocked':'Confirm this movement is valid'):(action==='reject'?'Reject this invoice package':'Approve this exact invoice package');
    $('#detail-title').textContent=title;state.detailId=kind==='INVOICE'?item.id:null;state.detailPdfSha256=result.pdf_sha256;
    const evidence=kind==='CASE'?swipeCaseEvidence(result,item):swipeInvoiceEvidence(result);
    const note=action==='reopen'?'Reopen this exact rejected package as a draft. This does not approve or dispatch it. The server rechecks source coverage and the retained PDF.':kind==='CASE'?(action==='reject'?'This records a confirmed issue. The case stays open for remediation and the movement stays blocked.':'This releases the current model concern only. It does not approve an invoice.'):(action==='reject'?'This rejects the unissued package and clears any approval. The billing scope stays on hold until an explicit reopen.':'Approval authorises dispatch. '+(state.summary?.auto_dispatch_after_approval===true?'The worker automatically queues an approved package.':state.summary?.auto_dispatch_after_approval===false?'Automatic dispatch is disabled; queuing remains separate.':'The server dispatch policy determines when the approved package is queued.'));
    $('#detail-content').innerHTML=`<div class="notice swipe-confirm-notice"><strong>${esc(title)}</strong><p>${esc(note)}</p></div>${evidence}<form id="swipe-confirm-form" class="stack form-section">${recordedIdentity()}<label for="swipe-decision-reason">Evidence and decision reason</label><textarea id="swipe-decision-reason" name="reason" required minlength="5" maxlength="2000" placeholder="Record the evidence supporting this exact decision."></textarea><div class="checkbox-line"><input id="swipe-confirm-checked" type="checkbox" required><label for="swipe-confirm-checked">I have reviewed the full evidence${kind==='INVOICE'?', PDF, amounts, sender, recipients and email text':''} and confirm this decision for the displayed ${kind==='CASE'?'version':'package'}.</label></div><div class="section-actions"><button type="button" id="swipe-cancel">Cancel — keep this card</button><button id="swipe-confirm-submit" class="${action==='reject'?'danger':'primary'}" type="submit" disabled>${action==='reject'?'Confirm rejection':'Confirm & next'}</button></div><p id="swipe-confirm-error" class="error-text" role="alert" hidden></p></form>`;
    if(kind==='INVOICE')void loadInvoicePdf(item.id);
    const form=$('#swipe-confirm-form'),button=$('#swipe-confirm-submit'),check=$('#swipe-confirm-checked'),reason=$('#swipe-decision-reason');
    return new Promise(resolve=>{
      const pending={kind,item,action,generation,epoch,submitted:false,request:null,settled:false};state.swipeConfirmation=pending;
      pending.settle=value=>{if(pending.settled)return;pending.settled=true;signal?.removeEventListener('abort',abort);if(state.swipeConfirmation===pending)state.swipeConfirmation=null;resolve(value);};
      const valid=()=>state.swipeConfirmation===pending && current();
      const abort=()=>{if(state.swipeConfirmation===pending)cancelSwipeConfirmation();};signal?.addEventListener('abort',abort,{once:true});
      const update=()=>{const cancel=form.querySelector('#swipe-cancel');if(cancel)cancel.disabled=pending.submitted;button.disabled=!valid() || pending.submitted || !canReview() || !check.checked || reason.value.trim().length<5 || pending.stale;};
      form.addEventListener('input',update);form.addEventListener('change',update);
      $('#swipe-cancel').addEventListener('click',()=>{cancelSwipeConfirmation();if(dialogCurrent(generation))$('#detail-dialog').close();});
      form.addEventListener('submit',async event=>{
        event.preventDefault();event.stopPropagation();if(!valid() || pending.submitted)return;
        if(!canReview() || !check.checked || reason.value.trim().length<5 || pending.stale){update();return;}
        const payload=kind==='CASE'?{expected_version:item.version_no,outcome:action==='reject'?'CONFIRMED_ISSUE':'VERIFIED_VALID',reason:reason.value.trim()}:{fingerprint:item.fingerprint,reason:reason.value.trim()};
        const signature=JSON.stringify(payload);if(pending.request?.signature!==signature)pending.request={signature,id:uuid()};
        pending.submitted=true;update();$('#swipe-confirm-error').hidden=true;
        try{
          // Deliberately no AbortSignal on a submitted financial command.
          const saved=await api(kind==='CASE'?`/api/cases/${encodeURIComponent(item.id)}/resolve`:`/api/invoices/${encodeURIComponent(item.id)}/${action==='reopen'?'reopen':action==='reject'?'reject':'approve'}`,{...payload,request_id:pending.request.id});
          const expectedStatus=kind==='CASE'?(action==='reject'?'OPEN':'RESOLVED'):(action==='reopen'?'DRAFT':action==='reject'?'REJECTED':'APPROVED');
          if(saved.id!==item.id || saved.status!==expectedStatus || (kind==='CASE'?(saved.outcome!==payload.outcome || !saved.event_id):(saved.fingerprint!==item.fingerprint)) || saved.reviewer?.identity_kind!=='REGISTERED_OPERATOR' || saved.reviewer?.actor_id!==actor)throw new Error('The server response did not confirm this exact decision. Retry with the same request or refresh the recorded history.');
          if(state.sessionEpoch!==epoch){pending.settle({accepted:false});return;}
          const ownsDialog=valid();pending.settle({accepted:true,message:action==='reject'?'Rejection recorded.':'Confirmed decision recorded.',result:saved});
          if(ownsDialog)$('#detail-dialog').close();
          toast(ownsDialog?(action==='reopen'?'Rejected package reopened as a draft.':action==='reject'?'Rejection recorded with your identity.':'Decision recorded with your identity.'):'The decision was recorded. Refresh the worklist to see its result.');
        }catch(error){if(valid()){$('#swipe-confirm-error').textContent=errorText(error);$('#swipe-confirm-error').hidden=false;}else{pending.settle({accepted:false});if(state.sessionEpoch===epoch)toast('The decision response could not be verified. Check recorded history before retrying.','error');}}
        finally{pending.submitted=false;if(valid())update();}
      });
    });
    }catch(error){if(current())$('#detail-content').innerHTML=`<div class="notice warning">${esc(errorText(error))}</div><p>Close this dialog and refresh the worklist before trying again.</p>`;throw error;}
  }
  function swipeCaseEvidence(result,item){
    const version=result.version,payload=version.payload || {},model=(result.assessments || []).find(a=>a.kind==='MODEL');
    return `<section class="swipe-full-evidence"><h3>${esc(customerName(item.customer_id))} · ${esc(result.movement.external_id)}</h3><p class="hint">Case version ${integer(version.version_no)} · current business version ${integer(result.movement.current_version)}</p>${caseReasonMarkup(result)}<dl class="details-grid">${detail('Recorded amount',amount(version.amount,version.currency))}${detail('Expected amount',amount(payload.expected_amount,payload.currency))}${detail('Source A',amount(payload.source_values?.A?.amount,payload.source_values?.A?.currency || 'Currency unavailable'))}${detail('Source B',amount(payload.source_values?.B?.amount,payload.source_values?.B?.currency || 'Currency unavailable'))}${detail('Recorded model score',score(model?.status==='SCORED'?model.score:null))}${detail('Recorded model',esc(model?.model_version || 'Unavailable'))}</dl>${driverMarkup(item)}<h3>Original source files</h3>${sourceReferences(result)}<details><summary>Exact retained inputs and assessments</summary>${json({version,assessments:result.assessments})}</details></section>`;
  }
  function swipeInvoiceEvidence(result){
    const doc=result.document || {},message=result.message || {};
    return `<section class="swipe-full-evidence"><div class="section-actions">${badge(result.status)}<a href="/api/invoices/${encodeURIComponent(result.id)}/pdf" target="_blank" rel="noopener">Open exact PDF ↗</a></div><h3>${esc(result.number || doc.invoice_number)}</h3><dl class="details-grid">${detail('Customer',esc(doc.customer?.legal_name || customerName(result.customer_id)))}${detail('Period',esc(result.period))}${detail('Net',amount(doc.net_total,doc.currency))}${detail('VAT',amount(doc.vat_total,doc.currency))}${detail('Invoice total',amount(doc.gross_total,doc.currency))}${detail('Movement count',integer(result.line_count ?? doc.movement_count))}${detail('From',esc(message.from || 'Unavailable'),true)}${detail('To',esc(Array.isArray(message.to)?message.to.join(', '):message.to || 'Unavailable'),true)}</dl><section id="invoice-pdf-panel" class="invoice-pdf-panel" hidden></section><h3>Original source files</h3>${sourceReferences(result)}<h3>Email preview</h3><p><strong>${esc(message.subject || 'Subject unavailable')}</strong></p><div class="email-preview">${esc(message.body || message.text || 'Message unavailable')}</div><details><summary>Exact package fingerprint and invoice document</summary><p class="mono">${esc(result.fingerprint)}</p>${json(doc)}</details></section>`;
  }
  function openBulkRelease(){
    if(!canReview()){toast('Sign in with a personal account to release cases.');return;}
    if(state.bulkPending){toast('A bulk review request is still running. Wait for its recorded results.');return;}
    if(state.loading || state.renderedReviewSequence!==state.requestSequence)return;
    const selected=[...state.selectedCases.values()];if(!selected.length)return;
    state.bulkSnapshot=selected.map(item=>({...item,source_files:(item.source_files || []).map(file=>({...file}))}));
    openDialog('HUMAN REVIEW','Review selected releases');
    $('#detail-content').innerHTML=`<div class="notice">Review each selected case and its source evidence. This action can release current-version model concerns only. Failed accounting checks, other blockers and issued movements remain protected. It does not approve or send invoices.</div><p class="small muted">${selected.length} ${selected.length===1?'case':'cases'} from the displayed page. The versions below are frozen for this request; newer data is checked again by the server.</p><div class="table-wrap"><table class="bulk-selection-table operations-table"><thead><tr><th>Case / customer</th><th>Reviewed version</th><th>Risk score</th><th>Original source files</th></tr></thead><tbody>${state.bulkSnapshot.map(c=>`<tr><td><span class="cell-main">${esc(customerName(c.customer_id))}</span><span class="secondary mono" title="${esc(c.id)}">${esc(c.id)}</span><span class="secondary mono" title="${esc(c.movement_id)}">${esc(c.external_movement_id || c.movement_id)}</span></td><td>${integer(c.current_version)}</td><td>${score(caseListScore(c))}</td><td class="source-cell">${sourceReferences(c,true)}</td></tr>`).join('')}</tbody></table></div><form id="bulk-release-form" class="stack form-section"><label for="bulk-release-reason">Shared evidence and release reason</label><textarea id="bulk-release-reason" name="reason" required minlength="5" placeholder="Explain the checks and evidence supporting every selected release. Use separate reviews when reasons differ."></textarea><div class="checkbox-line"><input id="bulk-release-confirm" type="checkbox" required><label for="bulk-release-confirm">I have reviewed every selected movement and its source evidence, and confirm that the model concerns are valid transactions at the displayed versions.</label></div><button id="bulk-release-submit" type="submit" class="primary" disabled>Release reviewed cases</button><p class="hint">Each case is checked independently. Stale or ineligible cases fail without overwriting evidence. Inspect the per-case result after release.</p><p id="detail-error" class="error-text" role="alert" hidden></p></form>`;
  }
  function showBulkResults(result){
    const items=Array.isArray(result.items)?result.items:[],counts=result.counts || {};
    $('#detail-title').textContent='Bulk release results';
    $('#detail-content').innerHTML=`<div class="inline-stats"><div><span class="small-label">CASES RESOLVED</span><span class="verification-total">${integer(counts.resolved)}</span></div><div><span class="small-label">MOVEMENTS RELEASED</span><span class="verification-total">${integer(counts.released)}</span></div><div><span class="small-label">FAILED</span><span class="verification-total">${integer(counts.failed)}</span></div></div><div class="notice">Resolved cases and released movements are separate outcomes. Review failures below; refresh current evidence before retrying a stale case. No invoice was approved or sent by this action.</div>${items.length?`<div class="table-wrap"><table><thead><tr><th>Case</th><th>Result</th><th>Movement state</th><th>Detail</th></tr></thead><tbody>${items.map(item=>`<tr><td class="mono wrap-cell">${esc(item.id)}</td><td>${badge(item.ok?(item.status || 'RESOLVED'):'FAILED')}</td><td>${badge(item.movement_state || 'UNKNOWN')}</td><td class="wrap-cell">${esc(item.error || item.outcome || 'No additional detail')}${item.code?`<span class="secondary">${esc(item.code)}</span>`:''}${item.reviewer?`<div class="result-attribution">${reviewerMarkup(item.reviewer)}<span class="secondary">${utc(item.reviewed_at)}</span></div>`:''}</td></tr>`).join('')}</tbody></table></div>`:empty('No per-case results returned','Refresh the review queue and check the recorded decisions before making another request.')}<p id="detail-error" class="error-text" role="alert" hidden></p>`;
  }
  async function teamView(sequence){
    if(!isAdmin())return empty('Administrator access required','Sign in with an administrator account to manage personal accounts.');
    const items=rows(await viewApi(`/api/operators?limit=${state.limit}&offset=${state.offset}`));if(sequence!==state.requestSequence)return null;state.operatorRows=items;
    return `<section class="panel"><div class="panel-header"><div><h2>Personal operator accounts</h2><p class="panel-subtitle">Every person signs in separately. Usernames and stable IDs identify future actions.</p></div><button class="primary" data-action="create-operator">Create account +</button></div><div class="panel-body"><div class="notice">Deactivation stops an account from making new authenticated requests. Historical decisions retain their original identity. Account changes do not change invoice approvals.</div></div>${items.length?`<div class="table-wrap"><table class="team-table"><thead><tr><th>Person / username</th><th>Role</th><th>Status</th><th>Created (UTC)</th><th>Action</th></tr></thead><tbody>${items.map(item=>`<tr><td><span class="cell-main">${esc(item.display_name)}</span><span class="secondary">@${esc(item.username)}</span><span class="secondary mono">${esc(item.id)}</span></td><td>${esc(human(item.role))}</td><td>${badge(item.active?'ACTIVE':'INACTIVE')}</td><td>${utc(item.created_at)}</td><td>${item.id===state.session.user_id?'<span class="small muted">Your account</span>':`<button data-operator-id="${esc(item.id)}" data-active="${!item.active}" class="${item.active?'danger':''}">${item.active?'Deactivate':'Reactivate'}</button>`}</td></tr>`).join('')}</tbody></table></div>`:empty('No accounts returned','Refresh to retrieve the account list.')}${pagination(items.length)}</section>`;
  }
  function createOperatorForm(){
    openDialog('PERSONAL ACCOUNT','Create operator account');
    $('#detail-content').innerHTML=`<p class="small muted">Create an account for one person. Ask them to keep their credentials private. A shared account cannot establish individual attribution.</p><form id="operator-create-form" class="stack"><label for="new-username">Username · immutable, lowercase</label><input id="new-username" name="username" required minlength="3" maxlength="80" pattern="[a-z0-9][a-z0-9._@\\-]{2,79}" autocomplete="off" autocapitalize="none" spellcheck="false"><label for="new-display-name">Person's display name</label><input id="new-display-name" name="display_name" required maxlength="120" autocomplete="off"><label for="new-password">Personal password · at least 12 characters</label><input id="new-password" name="password" type="password" required minlength="12" maxlength="256" autocomplete="new-password"><label for="new-role">Role</label><select id="new-role" name="role"><option value="reviewer">Reviewer · operational decisions</option><option value="admin">Administrator · decisions and account management</option></select><button type="submit" class="primary">Create personal account</button><p class="hint">The password is submitted directly to the server and is never stored in browser storage.</p><p id="detail-error" class="error-text" hidden></p></form>`;
  }
  function operatorStatusForm(button){
    const item=state.operatorRows?.find(o=>o.id===button.dataset.operatorId);if(!item)return;
    const active=button.dataset.active==='true';openDialog('ACCOUNT ACCESS',active?'Reactivate account':'Deactivate account');
    $('#detail-content').innerHTML=`<p>${active?'Restore access for':'Stop new authenticated requests from'} <strong>${esc(item.display_name)}</strong> (@${esc(item.username)}).</p><p class="small muted">Past review records retain this account's identity. The server protects your own account and the last active administrator.</p><form id="operator-status-form" data-operator-id="${esc(item.id)}" data-active="${active}" class="stack"><div class="checkbox-line"><input id="operator-status-confirm" type="checkbox" required><label for="operator-status-confirm">I confirm this account access change.</label></div><button type="submit" class="${active?'primary':'danger'}">${active?'Reactivate':'Deactivate'} account</button><p id="detail-error" class="error-text" hidden></p></form>`;
  }
  function eventCard(item,legacy=false){
    if(legacy)return `<article class="history-event legacy-event"><div class="history-event-header"><strong>${esc(human(item.action))}</strong><time>${utc(item.created_at)}</time></div><div class="history-person">${reviewerMarkup({actor:item.actor,identity_type:'legacy'})}</div><p class="small muted">${item.case_attribution==='LEGACY_MOVEMENT_EVENT_CASE_UNAVAILABLE'?'Movement-level record. Attribution to this exact case is unavailable.':'Original case-level record.'} Reviewed version and individual identity are not verified retrospectively.</p><details><summary>Original historical record</summary>${json(item)}</details></article>`;
    return `<article class="history-event"><div class="history-event-header"><div>${badge(item.outcome || item.action)}<span class="history-origin">${esc(item.origin || 'Origin unavailable')}</span></div><time>${utc(item.reviewed_at || item.occurred_at)}</time></div><div class="history-person">${reviewerMarkup(item.reviewer)}</div><dl class="details-grid compact-details">${detail('Action',esc(human(item.action)))}${detail('Reviewed business version',esc(item.reviewed_version_no ?? 'Unavailable'))}${detail('Evidence and reason',esc(item.reason || 'Not recorded'),true)}</dl><details><summary>Decision, before / after and immutable evidence</summary>${json({event_id:item.id,reviewed_version_id:item.reviewed_version_id,before:item.before,after:item.after,correction:item.correction,evidence:item.evidence,source_record_ids:item.source_refs,origin:item.origin,request_id:item.request_id,parent_request_id:item.parent_request_id,initiator_case_id:item.initiator_case_id,source_file_sha256:item.source_file_sha256,source_row_number:item.source_row_number,source_row_metadata:item.source_row_metadata})}</details>${/^[a-f0-9]{64}$/.test(item.source_file_sha256 || '')?`<p class="small"><a href="/api/review/files/${esc(item.source_file_sha256)}" download>Download exact correction CSV ↗</a>${item.source_row_number?` · Logical row ${integer(item.source_row_number)}`:''}</p>`:''}<p class="hint">Source-record IDs identify immutable observations. Download original files from the source references in this case.</p></article>`;
  }
  function historyControls(kind,cursor){const stack=state.historyPages[kind];return `<div class="pagination history-pagination"><span>20 events per page · newest first</span><div><button data-history-page="${kind}" data-history-direction="newer" ${stack.length?'':'disabled'}>← Newer</button><button data-history-page="${kind}" data-history-direction="older" data-history-cursor="${esc(cursor || '')}" ${cursor?'':'disabled'}>Older →</button></div></div>`;}
  async function loadCaseHistory(){
    const output=$('#case-history-content'),pages=state.historyPages;if(!output || !pages)return;
    const generation=state.dialogSequence,sequence=++state.historySequence;
    const current=()=>dialogCurrent(generation) && output.isConnected && sequence===state.historySequence;
    const query=new URLSearchParams({limit:'20'});if(pages.modern.length)query.set('before_id',String(pages.modern.at(-1)));if(pages.legacy.length)query.set('legacy_before_id',String(pages.legacy.at(-1)));if(pages.attempts.length)query.set('attempt_before_id',String(pages.attempts.at(-1)));
    output.setAttribute('aria-busy','true');output.innerHTML='<p class="small muted">Loading recorded review history…</p>';
    try{
      const result=await api(`/api/cases/${encodeURIComponent(pages.caseId)}/history?${query}`);if(!current())return;
      const events=rows(result.items),legacy=rows(result.legacy_items),attempts=rows(result.attempts);
      output.innerHTML=`<p class="small muted">Immutable review events retain the identity and evidence recorded at the time. Timestamps below are UTC. The CSV export contains structured review events; legacy records remain available separately below.</p><a class="small" href="/api/exports/review-history.csv?case_id=${encodeURIComponent(pages.caseId)}">Download structured review events ↗</a><h3 class="history-heading">Recorded review events</h3>${events.length?events.map(item=>eventCard(item)).join(''):empty('No structured review events on this page','Historical shared or local records, when present, appear separately below.')}${historyControls('modern',result.next_cursor)}<h3 class="history-heading">Legacy historical records</h3><div class="notice warning">Legacy shared identity — individual reviewer unknown. ${esc(result.legacy_note || 'Historical actor labels cannot establish who performed an action.')}</div>${legacy.length?legacy.map(item=>eventCard(item,true)).join(''):'<p class="small muted">No legacy review records on this page.</p>'}${historyControls('legacy',result.legacy_next_cursor)}<h3 class="history-heading">Rejected single-command attempts</h3><p class="small muted">These attempts did not change the case. They are separate from accepted review events.</p>${attempts.length?attempts.map(item=>`<article class="history-event"><div class="history-event-header">${badge('REJECTED')}<time>${utc(item.attempted_at)}</time></div><div class="history-person">${reviewerMarkup(item.reviewer)}</div><p class="small"><strong>${esc(human(item.action))}</strong><br>${esc(item.error || 'Command rejected')}</p><p class="small">${esc(item.reason || 'No reason recorded')}</p><details><summary>Rejected attempt and request correlation</summary>${json(item)}</details></article>`).join(''):'<p class="small muted">No rejected single-command attempts on this page.</p>'}${historyControls('attempts',result.attempt_next_cursor)}`;
    }catch(error){if(current())output.innerHTML=`<div class="notice error">${esc(errorText(error))}</div><button data-history-page="retry">Retry history</button>`;}
    finally{if(current())output.setAttribute('aria-busy','false');}
  }
  function historyPage(button){
    const pages=state.historyPages;if(!pages || $('#case-history-content')?.getAttribute('aria-busy')==='true')return;
    if(button.dataset.historyPage!=='retry'){const stack=pages[button.dataset.historyPage];if(!stack)return;if(button.dataset.historyDirection==='newer')stack.pop();else if(button.dataset.historyCursor)stack.push(button.dataset.historyCursor);}
    loadCaseHistory();
  }

  async function openSources(url,offset=0,container=null){
    const endpoint=safeApiUrl(url);if(!endpoint){showError(new Error('The source-file endpoint is not a local authenticated API URL.'));return;}
    const generation=container?state.dialogSequence:openDialog('ORIGINAL INPUT EVIDENCE','Original source files');
    const output=container || $('#detail-content'),sourceSequence=++state.sourceSequence;output.dataset.sourceSequence=String(sourceSequence);
    const current=()=>dialogCurrent(generation) && output.isConnected && output.dataset.sourceSequence===String(sourceSequence);
    if(container)output.innerHTML='<p class="small muted">Loading source references…</p>';
    try{
      const target=new URL(endpoint,location.origin);target.searchParams.set('limit','50');target.searchParams.set('offset',String(offset));
      const result=await api(target.pathname+target.search);if(!current())return;const files=Array.isArray(result)?result:(result.source_files || result.items || []);
      const total=result.total ?? result.source_file_count,hasMore=result.has_more ?? (typeof total==='number'?offset+files.length<total:files.length===50);
      state.sourcePage={url:endpoint,offset};
      output.innerHTML=`<p class="small muted">Source-file references supplied by the server for this exact record. Download links require your workspace session; inspecting a batch opens a separate tab.</p>${sourceFileList(files)}${sourceWarning({...result,truncated:false})}${result.provenance_status?`<p class="action-note">${esc(human(result.provenance_status))}</p>`:''}${result.truncated?'<div class="notice warning">This response is truncated. Continue through the available pages; the displayed files are not the complete provenance.</div>':''}<div class="pagination"><span>${files.length?`${offset+1}–${offset+files.length}`:'0'} source files${typeof total==='number'?' of '+integer(total):''}</span><div><button data-source-page="previous" data-source-page-url="${esc(endpoint)}" data-source-offset="${Math.max(0,offset-50)}" ${offset?'':'disabled'}>← Previous</button><button data-source-page="next" data-source-page-url="${esc(endpoint)}" data-source-offset="${offset+50}" ${hasMore?'':'disabled'}>Next →</button></div></div>`;
    }catch(error){if(current())output.innerHTML=`<div class="notice error">${esc(errorText(error))}</div>`;}
  }
  async function openCase(id,options={}){
    captureCaseDraft();const entry=ensureWorkbenchEntry(id);if(!entry)return;
    if(pages[location.hash.slice(1)])history.pushState({ioDialog:true},'',`#case/${encodeURIComponent(id)}`);else history.replaceState(history.state,'',`#case/${encodeURIComponent(id)}`);
    const generation=openDialog('MOVEMENT REVIEW','Loading review…');state.activeCaseId=id;renderWorkbench();
    try{
      const result=await fetchWorkbenchCase(entry,options.reload);if(!dialogCurrent(generation) || state.activeCaseId!==id || state.workbench.get(id)!==entry)return;state.detailId=id;
      const c=result.case || {}, v=result.version || {}, movement=result.movement || {}, payload=v.payload || v.data || movement.payload || {};
      const version=v.version_no ?? v.version ?? c.version_no ?? movement.current_version;
      const sourceValues=payload.source_values || {};
      const editableSource=source => sourceValues[source] && !Array.isArray(sourceValues[source]) && !payload.ambiguous && !sourceValues[source].ambiguous;
      const assessments=rows(result.assessments);const latest=assessments[assessments.length-1] || {};
      $('#detail-title').textContent=customerName(c.customer_id || movement.customer_id);
      $('#detail-content').innerHTML=`<div class="case-snapshot-notice"><span>Evidence snapshot · version ${esc(version ?? 'unavailable')}${entry.stale?' · New data may be available':''}</span><button class="small" data-refresh-case="${esc(id)}" ${entry.pending?'disabled':''}>Refresh this case</button></div><div class="section-actions">${badge(c.status)}${badge(c.severity || 'REVIEW')}<span class="small muted">Version ${esc(version ?? 'unknown')}</span></div><section id="case-version-browser" class="version-browser" data-case-id="${esc(id)}" aria-label="Movement version history"><div class="version-heading"><div><h3>Movement versions</h3><p class="hint">Browse recorded changes without changing your review target.</p></div><button data-load-versions="${esc(id)}">Browse versions</button></div><div id="case-version-content"></div></section><section id="case-readable-reason" class="case-readable-reason"><p class="small-label">REVIEW TARGET · VERSION ${esc(version ?? 'UNAVAILABLE')}</p><h3>Why this case was flagged</h3>${caseReasonMarkup(result)}<div id="case-narrative-content"><p class="small muted">Loading the recorded model explanation for this case…</p></div></section><dl class="details-grid">${detail('Movement',esc(c.movement_id || movement.id),true)}${detail('Reason',esc(human(c.reason)))}${detail('Risk score',score(c.score ?? latest.score))}${detail('Period',esc(c.period || payload.period))}${detail('Amount',amount(payload.amount,movement.currency || payload.currency || 'GBP'))}${detail('Last review action by',reviewerMarkup(c.reviewed_by_attribution),true)}${detail('Last review time (UTC)',utc(c.reviewed_at),true)}</dl><div class="notice">Keep the reason and evidence for every decision. Marking a model concern as valid does not clear a failed source or accounting control.</div>
      <details id="case-model-explanation" data-case-id="${esc(id)}" class="model-explanation-section"><summary>Exact model calculation, feature drivers & what-if sandbox</summary><div id="case-model-content"><p class="small muted">Open to inspect the recorded model, feature drivers and a hypothetical preview.</p></div></details><details id="case-history" data-case-id="${esc(id)}" class="history-section"><summary>Review history · people, decisions and evidence</summary><div id="case-history-content"><p class="small muted">Open to load recorded decisions, 20 events per page.</p></div></details><h3>Original source files</h3>${sourceReferences(result)}<h3>Verification evidence</h3>${assessments.length?`<div class="table-wrap"><table><thead><tr><th>Check</th><th>Result</th><th>Finding</th></tr></thead><tbody>${assessments.map(a=>`<tr><td>${a.kind==='MODEL'?'Risk model':'Source controls'}</td><td>${badge(a.status)}</td><td>${(a.reasons || []).map(r=>esc(human(r))).join(', ') || 'No exception recorded'}</td></tr>`).join('')}</tbody></table></div>`:'<p class="muted small">No assessment evidence available.</p>'}<dl class="details-grid">${detail('Source A business amount',amount(sourceValues.A?.amount))}${detail('Source B business amount',amount(sourceValues.B?.amount))}</dl><details><summary>Assessment, model inputs & snapshot source evidence</summary>${json({assessments,snapshot_version:payload,snapshot_observations:result.observations || [],quarantine:result.quarantine})}</details>
      ${c.reason==='SOURCE_CHANGE_AFTER_ISSUE'?`<section class="form-section"><h3>Latest source observations</h3><div class="notice warning">The source has changed after issuance. The issued snapshot above remains frozen. Compare these latest records and their revisions; an authorised adjustment is required before changing issued amounts.</div>${(result.latest_observations || []).length?`<div class="table-wrap"><table><thead><tr><th>Source / record</th><th>Revision</th><th class="number">Amount</th><th>Original source file</th><th>Received</th></tr></thead><tbody>${result.latest_observations.map(r=>`<tr><td>${esc(r.source)}<span class="secondary mono">${esc(r.record_id)}</span></td><td>${integer(r.revision)}</td><td class="number">${amount(r.payload?.amount,r.payload?.currency)}</td><td class="source-cell">${sourceFileList(r.source_file?[r.source_file]:[],true)}</td><td>${date(r.received_at)}</td></tr>`).join('')}</tbody></table></div><p class="action-note">At most 20 latest source observations, separate from the snapshot evidence.</p>`:'<p class="small muted">No recent source observations were returned.</p>'}<details><summary>Full latest observation evidence</summary>${json(result.latest_observations || [])}</details></section>`:''}
      ${c.status==='OPEN' && Number.isInteger(Number(version)) && c.movement_id?`<section class="form-section"><h3>Record a review decision</h3><form id="resolve-form" data-case-id="${esc(id)}" data-version="${esc(version)}" class="stack"><label for="review-outcome">Outcome</label><select id="review-outcome" name="outcome"><option value="UNRESOLVED">Unresolved — keep the issue open</option><option value="VERIFIED_VALID" ${c.reason==='MODEL_RISK'?'':'disabled'}>Verified valid — model concern only</option><option value="CONFIRMED_ISSUE">Confirmed issue — remains blocked</option></select><label for="review-reason">Evidence and reason</label><textarea id="review-reason" name="reason" required minlength="5" placeholder="Describe what you checked and reference supporting evidence."></textarea><button type="submit" class="primary">Save review decision</button></form></section><details ${c.reason==='SOURCE_CHANGE_AFTER_ISSUE'?'hidden':''}><summary>Make an evidenced correction</summary><p class="small muted">A correction creates a new business version and triggers reassessment. Original source records remain unchanged. Missing or ambiguous counterparts require corrected source input; they cannot be invented here.</p><form id="correct-form" data-case-id="${esc(id)}" data-version="${esc(version)}"><div class="form-grid"><div><label for="correction-a">Corrected source A amount (GBP)</label><input id="correction-a" name="source_a_amount" inputmode="decimal" placeholder="${editableSource('A')?'Leave empty to preserve':'Source absent or ambiguous'}" pattern="[0-9]+(\\.[0-9]{1,2})?" ${editableSource('A')?'':'disabled'}></div><div><label for="correction-b">Corrected source B amount (GBP)</label><input id="correction-b" name="source_b_amount" inputmode="decimal" placeholder="${editableSource('B')?'Leave empty to preserve':'Source absent or ambiguous'}" pattern="[0-9]+(\\.[0-9]{1,2})?" ${editableSource('B')?'':'disabled'}></div><div><label for="correction-description">Correct description</label><input id="correction-description" name="description" maxlength="500" placeholder="Leave empty to preserve"></div><div><label for="correction-expected">Correct expected amount (GBP)</label><input id="correction-expected" name="expected_amount" inputmode="decimal" placeholder="Leave empty to preserve" pattern="[0-9]+(\\.[0-9]{1,2})?"></div><div class="full"><label for="correction-reason">Evidence and reason</label><textarea id="correction-reason" name="reason" required minlength="5"></textarea></div></div><button class="primary" type="submit">Save correction & reassess</button></form></details>`:`<p class="small muted">${!c.movement_id?'This quarantined input requires corrected source evidence. It cannot be made billable through a review decision.':`This case is ${esc(human(c.status).toLowerCase())}. Its evidence is retained above.`}</p>`}<p id="detail-error" class="error-text" role="alert" hidden></p>`;
      restoreCaseDraft(entry);renderWorkbench();renderVersionBrowser(entry);const decision=$('#resolve-form')?.closest('.form-section');if(decision){decision.insertAdjacentHTML('afterbegin',`<p class="hint">Review target: the pinned version ${esc(version)}. Browsing other versions does not change this decision.</p>`);$('#case-model-explanation').before(decision);}$('#case-model-explanation').dataset.loaded='true';void loadCaseExplanation();
    }catch(error){if(dialogCurrent(generation))$('#detail-content').innerHTML=`<div class="notice error">${esc(errorText(error))}</div>`;}
  }
  async function loadCaseVersions(id,cursor=null,backStack=[],selectLast=false){
    const entry=state.workbench.get(id);if(!entry || entry.versionsLoading)return;
    const generation=state.dialogSequence,sequence=(entry.versionsSequence || 0)+1;entry.versionsSequence=sequence;entry.versionsLoading=true;entry.versionsError=null;
    entry.versionsController?.abort();const controller=new AbortController();entry.versionsController=controller;renderVersionBrowser(entry);
    try{
      const params=new URLSearchParams({limit:'5'});if(cursor)params.set('cursor',cursor);
      const result=await api(`/api/cases/${encodeURIComponent(id)}/versions?${params}`,undefined,undefined,{scope:`versions:${id}:${sequence}`,signal:controller.signal});
      if(state.workbench.get(id)!==entry || entry.versionsSequence!==sequence)return;
      if(result.case_id!==id || result.read_only!==true || !Array.isArray(result.items))throw new Error('Version history returned an unexpected evidence response.');
      entry.versions={...result,items:result.items.slice(0,5)};entry.versionIndex=selectLast?Math.max(0,entry.versions.items.length-1):0;entry.versionCursor=cursor;entry.versionBack=backStack.slice(-20);
      const current=result.current_version_no ?? result.current_version,pinned=entry.data?.version?.version_no;
      if(Number.isInteger(current) && Number.isInteger(pinned) && current!==pinned){entry.versionStale=true;entry.stale=true;renderWorkbench();if(state.activeCaseId===id)markPendingRefresh();}
    }catch(error){if(state.workbench.get(id)===entry && entry.versionsSequence===sequence && error.name!=='AbortError')entry.versionsError=errorText(error);}
    finally{if(state.workbench.get(id)===entry && entry.versionsSequence===sequence){entry.versionsLoading=false;if(dialogCurrent(generation) && state.activeCaseId===id)renderVersionBrowser(entry);}}
  }
  function renderVersionBrowser(entry){
    const target=$('#case-version-content');if(!target || state.activeCaseId!==entry.id || $('#case-version-browser')?.dataset.caseId!==entry.id)return;
    const load=$('[data-load-versions]');if(load){load.disabled=Boolean(entry.versionsLoading);load.textContent=entry.versions?'Reload latest':'Browse versions';}
    if(entry.versionsLoading){target.setAttribute('aria-busy','true');if(!entry.versions)target.innerHTML='<p class="small muted" role="status">Loading five recorded versions…</p>';target.querySelectorAll('button').forEach(b=>b.disabled=true);return;}target.setAttribute('aria-busy','false');
    if(entry.versionsError){target.innerHTML=`<div class="notice warning">${esc(entry.versionsError)}</div><button data-load-versions="${esc(entry.id)}">Retry version history</button>`;return;}
    const data=entry.versions;if(!data){target.replaceChildren();return;}
    const item=data.items[entry.versionIndex || 0],pinned=entry.data?.version?.version_no;
    if(!item){target.innerHTML='<p class="small muted">No movement versions are available for this input. Quarantined source evidence remains below.</p>';return;}
    const model=item.model || {},rule=item.rule || {},ruleReasons=rule.recorded_reasons || rule.reasons || [],narrative=model.narrative,older=entry.versionIndex<data.items.length-1 || data.has_more,newer=entry.versionIndex>0 || entry.versionCursor!==null;
    const changes=Array.isArray(item.diff?.changes)?item.diff.changes:[],files=item.source_files || [],returnToLatest=entry.versionIndex===0 && entry.versionCursor!==null && !(entry.versionBack || []).length;
    target.innerHTML=`<div class="version-nav"><button data-version-step="-1" ${newer?'':'disabled'} aria-label="${returnToLatest?'Return to latest movement version':'Newer movement version'}">← ${returnToLatest?'Latest':'Newer'}</button><span aria-live="polite">Version <strong>${integer(item.version_no)}</strong> · ${item.is_current?'current business version':'historical snapshot'}</span><button data-version-step="1" ${older?'':'disabled'} aria-label="Older movement version">Older →</button></div><article class="version-swipe-area" tabindex="0" role="group" aria-label="Version ${esc(item.version_no)}. Use left and right arrow keys or swipe to change version."><div class="section-actions">${badge(item.is_current?'CURRENT':'HISTORICAL')}${item.is_case_version?'<span class="badge neutral">THIS CASE SNAPSHOT</span>':''}<span class="small muted">${esc(human(item.origin || 'recorded'))} · ${utc(item.created_at)}</span></div><div class="version-key-facts"><div><span>Recorded amount</span><strong>${amount(item.amount,item.currency)}</strong></div><div><span>Logistic regression score</span><strong>${score(model.recorded_score)}</strong><small>p = ${esc(numeric(model.recorded_score))}</small></div></div><p class="small mono version-model-id">Recorded model: ${esc(model.model_version || 'Unavailable')}</p><h4>Recorded reasons</h4><p class="version-reason">${esc(narrative?.summary || (model.recorded_score==null?'No model score is available for this version. Check the retained rule and source evidence.':(model.recorded_reasons || []).map(human).join(', ') || 'No model exception recorded.'))}</p>${narrative?.drivers?.length?`<ul class="version-drivers">${narrative.drivers.slice(0,3).map(driver=>`<li>${esc(driver.text || driver.summary_phrase || driver.label)}</li>`).join('')}</ul>`:''}<p class="hint">Source controls: ${esc(human(rule.status || 'Unavailable'))}${ruleReasons.length?' · '+ruleReasons.map(human).map(esc).join(', '):''}. Synthetic model evidence is not proof of fraud.</p><details><summary>What changed in this version</summary>${changes.length?`<div class="table-wrap"><table class="version-diff-table"><thead><tr><th>Field</th><th>Before</th><th>After</th></tr></thead><tbody>${changes.slice(0,25).map(change=>`<tr><td class="mono">${esc(change.path)}</td><td>${change.before_present===false?'Not present':esc(typeof change.before==='object'?JSON.stringify(change.before):change.before ?? 'null')}</td><td>${change.after_present===false?'Not present':esc(typeof change.after==='object'?JSON.stringify(change.after):change.after ?? 'null')}</td></tr>`).join('')}</tbody></table></div>${changes.length>25?'<p class="hint">Showing the first 25 changes. Complete returned evidence is available below.</p>':''}`:`<p class="small muted">${esc(human(item.diff?.status || 'Difference evidence unavailable'))}</p>`}</details><details><summary>Original source files for version ${integer(item.version_no)}</summary>${sourceFileList(files,false)}${item.source_files_truncated?'<p class="source-warning">Source files are a bounded preview; additional files are not shown here.</p>':''}${item.source_refs_truncated?'<p class="source-warning">Source record references are a bounded preview, not the complete set.</p>':''}${item.provenance_status && item.provenance_status!=='COMPLETE'?`<p class="source-warning">Provenance: ${esc(human(item.provenance_status))}</p>`:''}</details><details><summary>Exact model, changes and review evidence</summary>${json({version_id:item.id,content_hash:item.content_hash,model,rule,diff:item.diff,source_refs:item.source_refs,source_files:files,source_files_truncated:item.source_files_truncated,source_refs_truncated:item.source_refs_truncated,review_events:item.review_events,review_events_truncated:item.review_events_truncated,eligibility:item.eligibility})}</details>${Array.isArray(item.related_cases) && item.related_cases.length?`<details><summary>Related cases</summary><div class="section-actions">${item.related_cases.slice(0,10).map(c=>`<button class="small" data-case="${esc(c.id || c.case_id)}">Open ${esc(human(c.reason || 'related case'))}</button>`).join('')}</div>${item.related_cases_truncated?'<p class="hint">Related cases are a bounded preview.</p>':''}</details>`:''}</article><p class="version-readonly-note">Read-only history · swipe left/right or use arrow keys. The review form remains bound to version ${esc(pinned ?? 'unavailable')}.</p>${entry.versionStale?'<div class="notice warning">A newer business version exists. Your notes are preserved. Refresh this case explicitly before recording a decision.</div>':''}`;
    if(entry.versionStale)document.querySelectorAll('#resolve-form button[type=submit],#correct-form button[type=submit]').forEach(button=>button.disabled=true);
  }
  function stepCaseVersion(delta){
    const entry=state.workbench.get(state.activeCaseId);if(!entry?.versions || entry.versionsLoading)return;const next=(entry.versionIndex || 0)+delta;
    if(next>=0 && next<entry.versions.items.length){entry.versionIndex=next;renderVersionBrowser(entry);$('.version-swipe-area')?.focus({preventScroll:true});return;}
    if(delta>0 && entry.versions.has_more && entry.versions.next_cursor){loadCaseVersions(entry.id,entry.versions.next_cursor,[...(entry.versionBack || []),entry.versionCursor]);return;}
    if(delta<0 && entry.versionCursor!==null){const back=[...(entry.versionBack || [])],hasPrevious=back.length>0,cursor=hasPrevious?back.pop():null;loadCaseVersions(entry.id,cursor,back,hasPrevious);}
  }
  async function invoices(sequence){
    const result=await viewApi(`/api/invoices?limit=${state.limit}&offset=${state.offset}${state.invoiceStatus?'&status='+encodeURIComponent(state.invoiceStatus):''}${customerQuery('invoices')}`),items=rows(result);
    if(sequence!==state.requestSequence)return null;state.invoiceRows=items;
    const list=items.length?`<div class="table-wrap"><table><thead><tr><th>Invoice / customer</th><th>Period</th><th>Status</th><th class="number">Total</th><th>Package</th><th>Original source files</th><th>Action</th></tr></thead><tbody>${items.map(item=>`<tr><td><span class="cell-main">${esc(item.number || item.invoice_number || item.id)}</span><span class="secondary">${esc(customerName(item.customer_id))}</span></td><td>${esc(item.period)}</td><td>${badge(item.status)}</td><td class="number">${amount(item.gross_total,item.currency)}</td><td>${item.pdf_ready?badge('READY'):'<span class="muted">Preparing</span>'}<span class="secondary">${esc(invoiceFormatLabel(item.line_format))}</span><span class="secondary">${item.line_count==null?'Movement count unavailable':integer(item.line_count)+' movements'}</span>${item.line_format==='ITEMIZED'?`<span class="secondary">${item.movement_vat_policy==='line_half_up_reconciled_v1'?'VAT breakdown included':'Historical net-only breakdown'}</span>`:''}</td><td class="source-cell">${sourceReferences(item,true)}</td><td><button class="table-action" data-invoice="${esc(item.id)}">Inspect →</button></td></tr>`).join('')}</tbody></table></div>`:state.invoiceStatus?empty('No invoices with this status','Choose All to see every package, or another status.'):empty('No invoice packages yet','Prepare a monthly invoice once both sources and all required reviews are complete.');
    state.invoiceListMarkup=list;return `<section class="panel"><div class="panel-header"><div><h2>Monthly invoice packages</h2><p class="panel-subtitle">Approval covers the exact PDF, sender, recipients and message.</p></div><button class="primary" data-action="prepare">Prepare invoice +</button></div><div class="filters">${customerFilter('invoices')}<div class="status-chips" role="group" aria-label="Show invoices by status">${[['','All'],['DRAFT','Awaiting approval'],['APPROVED','Approved'],['ISSUED','Sent'],['STALE','Superseded']].map(([value,label])=>`<button type="button" class="chip" data-invoice-status="${value}" aria-pressed="${state.invoiceStatus===value}">${label}${value==='DRAFT' && Number(count('pending_approvals'))>0?` <span class="chip-count">${integer(count('pending_approvals'))}</span>`:''}</button>`).join('')}</div><span class="spacer"></span><div class="view-switch" role="group" aria-label="Invoice display"><button data-invoice-layout="cards" aria-pressed="${state.invoiceLayout==='cards'}">Cards</button><button data-invoice-layout="list" aria-pressed="${state.invoiceLayout==='list'}">List</button><button data-invoice-layout="swipe" aria-pressed="${state.invoiceLayout==='swipe'}">Swipe</button></div></div><div id="invoice-presentation">${state.invoiceLayout==='swipe'?'':state.invoiceLayout==='cards'?invoiceCards(items):list}</div>${pagination(items.length,listTotal(result))}</section><div class="notice">Preparation runs the monthly close checks. Open blockers, incomplete source coverage or missing required model evidence prevent an invoice from being issued.</div>`;
  }
  function invoiceCards(items){
    if(!items.length)return state.invoiceStatus && state.tab==='invoices'?empty('No invoices with this status','Choose All to see every package, or another status.'):empty('No invoice packages yet','Prepare a monthly invoice after sources and required reviews are complete.');
    return `<div class="invoice-card-grid">${items.map(item=>`<article class="invoice-card"><div class="invoice-card-top">${badge(item.status)}<span class="small muted">${esc(item.period)}</span></div><div class="invoice-paper" aria-label="Invoice summary, not the PDF"><div class="paper-heading"><span class="paper-mark">io</span><span>INVOICE</span></div><p class="paper-customer">${esc(customerName(item.customer_id))}</p><p class="paper-number">${esc(item.number || item.id)}</p><div class="paper-lines" aria-hidden="true"><span></span><span></span><span></span></div><div class="paper-total"><span>Invoice total</span><strong>${amount(item.gross_total,item.currency)}</strong></div></div><div class="invoice-card-meta"><span>${esc(item.line_format==='ITEMIZED'?'Itemised':'Summary')} · ${item.line_count==null?'Count unavailable':integer(item.line_count)+' movements'}</span><span>${item.pdf_ready?'Exact PDF ready':'PDF preparing'}</span></div><div class="section-actions"><button class="primary" data-invoice="${esc(item.id)}">Inspect package</button><button data-preview-invoice="${esc(item.id)}" ${item.pdf_ready?'':'disabled'} aria-label="Preview PDF ${esc(item.number || item.id)}">Preview PDF</button></div><details class="card-source-details"><summary>Original source files</summary>${sourceReferences(item,true)}</details></article>`).join('')}</div>`;
  }
  function setInvoiceLayout(layout){
    if(!['cards','list','swipe'].includes(layout) || state.loading)return;state.invoiceLayout=layout;state.swipe?.deactivate();
    const target=$('#invoice-presentation');if(!target)return;target.innerHTML=layout==='swipe'?'':layout==='cards'?invoiceCards(state.invoiceRows || []):state.invoiceListMarkup;
    document.querySelectorAll('[data-invoice-layout]').forEach(button=>button.setAttribute('aria-pressed',String(button.dataset.invoiceLayout===layout)));applyIdentityPermissions();mountSwipeView();
  }
  async function openInvoice(id,options={}){
    const generation=openDialog('INVOICE PACKAGE','Loading invoice…');
    try{
      const result=await api(`/api/invoices/${encodeURIComponent(id)}/package`,undefined,undefined,{scope:`dialog:${generation}`,signal:state.dialogAbort?.signal});if(!dialogCurrent(generation))return;state.detailId=id;state.detailPdfSha256=result.pdf_sha256;
      const document=result.document || result.invoice || {}, message=result.message || result.email || {}, status=result.status || document.status || 'DRAFT';
      const fingerprint=result.fingerprint,lineFormat=result.line_format || document.line_format || 'SUMMARY',lineCount=result.line_count ?? document.movement_count,pdfReady=result.pdf_ready ?? Boolean(result.pdf_url || fingerprint);const recipients=Array.isArray(message.to)?message.to.join(', '):(message.to || result.recipient || document.customer?.email || 'Not recorded');
      $('#detail-title').textContent=document.invoice_number || result.number || 'Invoice package';
      const approved=/^(APPROVED|ISSUED|QUEUED|SENT|ACCEPTED|DELIVERED)$/.test(status);
      const canDispatch=status==='APPROVED' || result.approved===true;
      const canApprove=Boolean(fingerprint) && pdfReady && status==='DRAFT' && !approved;
      const dispatchPolicy=state.summary?.auto_dispatch_after_approval;
      const dispatchNote=dispatchPolicy===true?'After approval, the worker automatically queues this exact package. Queue now is an optional immediate request.':dispatchPolicy===false?'Automatic dispatch is disabled. Use Queue now after approval to request dispatch.':'Refresh the workspace to confirm its dispatch policy. Approval authorises this exact package for dispatch.';
      $('#detail-content').innerHTML=`<div class="section-actions">${badge(status)}<span class="badge simulation">SYNTHETIC DEMO</span>${pdfReady?`<a href="/api/invoices/${encodeURIComponent(id)}/pdf" target="_blank" rel="noopener" class="small">Open exact PDF ↗</a>`:'<span class="small muted">PDF generation pending</span>'}<button class="small" data-invoice="${esc(id)}">Refresh package</button>${pdfReady?`<button class="primary small" data-load-pdf="${esc(id)}">Preview exact PDF</button>`:''}</div><section id="invoice-pdf-panel" class="invoice-pdf-panel" hidden></section><dl class="details-grid">${detail('Customer',esc(document.customer?.legal_name || customerName(result.customer_id)))}${detail('Period',esc(document.period || result.period))}${detail('PDF format',esc(invoiceFormatLabel(lineFormat)))}${detail('Movements billed',lineCount===undefined?'Unavailable':integer(lineCount))}${detail('Net',amount(document.net_total,document.currency))}${detail('VAT',amount(document.vat_total,document.currency))}${detail('Invoice total',`<strong>${amount(document.gross_total || result.gross_total,document.currency)}</strong>`)}${detail('Due date',esc(document.due_date || 'Not recorded'))}${detail('From',esc(message.from || result.sender || 'Not recorded'),true)}${detail('To',esc(recipients),true)}</dl><dl class="details-grid">${detail('Package approved by',result.approved_by_attribution?reviewerMarkup(result.approved_by_attribution):'No attributed approval recorded',true)}${detail('Approval time (UTC)',utc(result.approved_at),true)}</dl>${invoiceFormatControls(result,id,lineFormat,lineCount)}${lineFormat==='ITEMIZED'?`<section class="invoice-movement-section"><h3>Billed movements</h3>${movementVatSummary(document,lineCount)}<div id="invoice-movement-preview" data-invoice-id="${esc(id)}" data-vat-policy="${esc(document.movement_vat?.policy || '')}">${movementPreviewMarkup(result.movement_details,id,document.movement_vat?.policy)}</div></section>`:''}<h3>Original source files</h3>${sourceReferences(result)}<h3>Email preview</h3><p class="small"><strong>Subject:</strong> ${esc(message.subject || result.subject || 'Not recorded')}</p><div class="email-preview">${esc(message.body || message.text || result.body || 'Message unavailable. Approval should remain blocked until the package is complete.')}</div><details><summary>Package fingerprint & immutable document data</summary><p class="mono">${esc(fingerprint || 'Unavailable')}</p>${json(document)}</details>
      ${canApprove?`<form id="approve-form" data-invoice-id="${esc(id)}" data-fingerprint="${esc(fingerprint)}" class="form-section"><div class="checkbox-line"><input id="approve-checked" type="checkbox" required><label for="approve-checked">I have opened the PDF and checked its totals, sender, recipients and email text. I approve this exact package for ${(state.summary?.provider || state.automationStatus?.mail?.provider || 'simulated')!=='simulated'?'dispatch (test mode: redirected to your connected address)':'simulated dispatch'}.</label></div><button id="approve-button" type="submit" class="primary" disabled>Approve exact package</button><p class="action-note">${esc(dispatchNote)}</p></form>`:''}
      ${status==='REJECTED'?`<div class="notice warning">This rejected package is on hold. It cannot be approved or automatically regenerated.</div><button data-reopen="${esc(id)}">Reopen rejected draft</button>`:''}${canDispatch?`<div class="form-section"><div class="section-actions"><button class="primary" data-dispatch="${esc(id)}">Queue now</button><button class="danger" data-revoke="${esc(id)}">Revoke approval</button></div><p class="action-note">${esc(dispatchNote)} The server rechecks the fingerprint and approval before queuing.</p></div>`:''}<p id="detail-error" class="error-text" role="alert" hidden></p>`;
      if(options.previewPdf && pdfReady)await loadInvoicePdf(id);
    }catch(error){if(dialogCurrent(generation))$('#detail-content').innerHTML=`<div class="notice error">${esc(errorText(error))}</div>`;}
  }
  function unloadInvoicePdf(){
    state.pdfController?.abort();state.pdfController=null;state.pdfLoadingId=null;state.pdfSequence=(state.pdfSequence || 0)+1;
    if(state.pdfObjectUrl)URL.revokeObjectURL(state.pdfObjectUrl);state.pdfObjectUrl=null;state.pdfPreviewMeta=null;
    const panel=$('#invoice-pdf-panel');if(panel){panel.replaceChildren();panel.hidden=true;}
  }
  async function loadInvoicePdf(id,pageNumber){
    const panel=$('#invoice-pdf-panel');if(!panel || state.detailId!==id)return;if(state.pdfLoadingId===id || (pageNumber===undefined && state.pdfObjectUrl)){panel.scrollIntoView({block:'nearest'});return;}
    const retained=state.pdfPreviewMeta?.invoice_id===id?state.pdfPreviewMeta:null;
    unloadInvoicePdf();const generation=state.dialogSequence,sequence=state.pdfSequence,controller=new AbortController();state.pdfController=controller;state.pdfLoadingId=id;
    const current=()=>dialogCurrent(generation) && state.detailId===id && sequence===state.pdfSequence && panel.isConnected;
    panel.hidden=false;panel.innerHTML='<p class="small muted" role="status">Loading a page from the exact invoice PDF…</p>';panel.scrollIntoView({block:'nearest'});
    try{
      const expected=state.detailPdfSha256;if(!/^[a-f0-9]{64}$/.test(expected || ''))throw new Error('The inspected package has no verified PDF hash. Refresh the package before previewing it.');
      const meta=retained || await api(`/api/invoices/${encodeURIComponent(id)}/preview`,undefined,undefined,{scope:`pdf:${sequence}`,signal:controller.signal});
      if(!current())return;if(meta.pdf_sha256!==expected)throw new Error('The preview PDF does not match the inspected package. Refresh the package explicitly.');
      if(!Number.isSafeInteger(meta.page_count) || meta.page_count<1)throw new Error('The preview returned an invalid page count. Use the exact PDF download.');
      const page=pageNumber ?? 1;if(!Number.isInteger(page) || page<1 || page>meta.page_count)throw new Error('This page is outside the retained PDF. Reload the preview.');
      state.pdfPreviewMeta={...meta,invoice_id:id};
      const response=await fetch(`/api/invoices/${encodeURIComponent(id)}/preview/pages/${page}?sha256=${encodeURIComponent(expected)}`,{credentials:'same-origin',signal:controller.signal});
      if(!response.ok){let detail;try{detail=(await response.json()).detail;}catch{}throw new Error(typeof detail==='string'?detail:`Page preview unavailable (${response.status}). You can still open the exact PDF.`);}
      if(response.headers.get('x-pdf-sha256')!==expected || response.headers.get('x-pdf-page')!==String(page))throw new Error('The returned page provenance does not match this PDF and page. Refresh the package.');
      const maxBytes=16*1024*1024,length=Number(response.headers.get('content-length'));if(length>maxBytes)throw new Error('This page exceeds the embedded preview limit. Open the exact PDF.');
      const chunks=[];let bytes=0;const reader=response.body.getReader();while(true){const part=await reader.read();if(part.done)break;bytes+=part.value.byteLength;if(bytes>maxBytes){await reader.cancel();throw new Error('This page exceeds the embedded preview limit. Open the exact PDF.');}chunks.push(part.value);}const blob=new Blob(chunks,{type:'image/png'}),header=await blob.slice(0,24).arrayBuffer(),signature=new Uint8Array(header);if(signature.length<24 || [137,80,78,71,13,10,26,10].some((v,i)=>signature[i]!==v))throw new Error('The server did not return a PNG page. Open the exact PDF or retry.');const dimensions=new DataView(header),width=dimensions.getUint32(16),height=dimensions.getUint32(20);if(!width || !height || width>1600 || height>1600)throw new Error('The returned page dimensions exceed the preview limit. Open the exact PDF.');
      if(!current())return;state.pdfObjectUrl=URL.createObjectURL(blob);const img=new Image();img.className='invoice-pdf-image';img.alt=`Invoice PDF page ${page} of ${meta.page_count}`;img.src=state.pdfObjectUrl;await img.decode();if(!current())return;
      if(!img.naturalWidth || !img.naturalHeight || img.naturalWidth>1600 || img.naturalHeight>1600)throw new Error('The returned page dimensions exceed the preview limit. Open the exact PDF.');
      panel.innerHTML=`<div class="section-actions"><strong class="small">Exact PDF · page preview</strong><button class="small" data-action="unload-pdf">Close preview</button><a href="/api/invoices/${encodeURIComponent(id)}/pdf" target="_blank" rel="noopener" class="small">Open exact PDF ↗</a></div><div class="pdf-page-nav"><button class="small" data-pdf-invoice="${esc(id)}" data-pdf-page="${page-1}" ${page>1?'':'disabled'}>← Previous page</button><span aria-live="polite">Page ${page} of ${meta.page_count}</span><button class="small" data-pdf-invoice="${esc(id)}" data-pdf-page="${page+1}" ${page<meta.page_count?'':'disabled'}>Next page →</button></div><div class="invoice-pdf-page"></div><p class="hint">Rendered from the frozen PDF · one page loaded at a time. The exact PDF remains available above.</p>`;$('.invoice-pdf-page',panel).append(img);
    }catch(error){if(current() && error.name!=='AbortError'){if(state.pdfObjectUrl)URL.revokeObjectURL(state.pdfObjectUrl);state.pdfObjectUrl=null;panel.innerHTML=`<div class="notice warning">${esc(errorText(error))}</div><div class="section-actions"><button data-load-pdf="${esc(id)}">Retry preview</button><a href="/api/invoices/${encodeURIComponent(id)}/pdf" target="_blank" rel="noopener">Open exact PDF ↗</a></div>`;}}
    finally{if(state.pdfController===controller){state.pdfController=null;state.pdfLoadingId=null;}}
  }
  function invoiceFormatLabel(value){return value==='ITEMIZED'?'Itemised · one line per movement':value==='SUMMARY'?'Summary':'Format unavailable';}
  const movementVatPolicy='line_half_up_reconciled_v1';
  const vatMoney=(value,currency)=>value==null || value===''?'<span class="muted">Unavailable</span>':amount(value,currency);
  function vatRate(value){
    const match=String(value ?? '').match(/^(\d+)(?:\.(\d+))?$/);if(!match)return '<span class="muted">Unavailable</span>';
    const fraction=(match[2] || '').padEnd(2,'0'),whole=(match[1]+fraction.slice(0,2)).replace(/^0+(?=\d)/,''),tail=fraction.slice(2).replace(/0+$/,'');return `${whole}${tail?'.'+tail:''}%`;
  }
  function movementVatSummary(document,lineCount){
    const vat=document.movement_vat,current=vat?.policy===movementVatPolicy;
    if(!current)return '<div class="notice warning invoice-vat-history"><strong>Historical net-only breakdown.</strong> Per-movement VAT, gross amounts and VAT reconciliation are unavailable in this frozen revision. The invoice-level totals above remain the recorded totals.</div>';
    return `<section class="invoice-vat-summary"><h4>Entire invoice · ${integer(lineCount)} movements</h4><p class="small muted">These totals cover all billed movements, not just the preview page. Line VAT is rounded half up to two decimal places. The rounding adjustment is applied once to reconcile the line amounts with the frozen invoice totals.</p><dl class="details-grid">${detail('Net subtotal · all movements',vatMoney(document.net_total,document.currency))}${detail('Recorded VAT rate',vatRate(vat.rate))}${detail('VAT subtotal · all movement lines',vatMoney(vat.line_vat_total,document.currency))}${detail('Gross subtotal · all movement lines',vatMoney(vat.line_gross_total,document.currency))}${detail('Invoice VAT rounding adjustment',vatMoney(vat.rounding_adjustment,document.currency),true)}${detail('Final invoice VAT',vatMoney(document.vat_total,document.currency))}${detail('Final invoice total',`<strong>${vatMoney(document.gross_total,document.currency)}</strong>`)}</dl><p class="hint">Line VAT subtotal + rounding adjustment = final invoice VAT. Line gross subtotal + rounding adjustment = final invoice total.</p></section>`;
  }
  function invoiceFormatControls(result,id,lineFormat,lineCount){
    if(result.status!=='DRAFT' || result.approved_at || !result.fingerprint)return '';
    const target=lineFormat==='ITEMIZED'?'SUMMARY':'ITEMIZED',pending=state.invoiceFormatPending?.has(id),exceeds=Number(lineCount)>10000,needsVat=lineFormat==='ITEMIZED' && !result.document?.movement_vat?.policy;
    const button=(format,label,vatUpgrade=false)=>`<button data-invoice-format="${format}" data-format-invoice-id="${esc(id)}" data-format-fingerprint="${esc(result.fingerprint)}" ${vatUpgrade?'data-vat-upgrade="true"':''} ${!canReview() || pending || (format==='ITEMIZED' && exceeds)?'disabled':''}>${pending?'Replacement being prepared…':label}</button>`;
    return `<section class="invoice-format-section"><h3>Invoice presentation</h3><p class="small muted">${lineFormat==='ITEMIZED'?'The PDF lists every billed movement.':'This PDF groups the billed movements into a summary.'} Changing presentation retains the current revision and creates a replacement PDF with unchanged billed totals. The replacement requires a fresh personal approval.</p>${exceeds && (target==='ITEMIZED' || needsVat)?'<div class="notice warning">This invoice exceeds the 10,000-movement itemised PDF limit. The system does not truncate an invoice to fit.</div>':''}${needsVat?'<p class="small muted">Add a per-movement VAT and gross breakdown with an explicit invoice rounding adjustment. The old net-only revision remains available.</p>':''}<div class="section-actions">${needsVat?button('ITEMIZED','Add VAT breakdown',true):''}${button(target,target==='ITEMIZED'?'Generate itemised replacement':'Generate summary replacement')}</div>${!canReview()?'<p class="hint">Sign in with a personal account to change invoice presentation.</p>':''}</section>`;
  }
  function movementPreviewMarkup(page,id,policy){
    if(!page || !Array.isArray(page.items))return '<p class="small muted">The movement preview is unavailable. Refresh this package or open its exact PDF.</p>';
    const items=page.items.slice(0,100),offset=Number(page.offset)||0,total=page.total,limit=Number(page.limit)||25,hasVat=policy===movementVatPolicy;
    if(!items.length)return '<p class="small muted">No itemised movement rows were returned for this package.</p>';
    return `<p class="small muted">This is a bounded preview. The itemised PDF contains every billed movement; no invoice rows are silently omitted. Scroll the table horizontally on a narrow screen to view every amount.</p><div class="table-wrap"><table class="invoice-movement-table"><thead><tr><th>Movement / event time</th><th>Description / supplier</th><th class="number">Net</th><th class="number">VAT rate</th><th class="number">VAT</th><th class="number">Gross</th></tr></thead><tbody>${items.map(row=>`<tr><td><strong class="cell-main mono">${esc(row.external_id || row.movement_id || 'Unavailable')}</strong><span class="secondary">Row ${integer(row.row_no)} · ${utc(row.event_at)}</span><details><summary>Recorded identifiers</summary><p class="small mono">Movement: ${esc(row.movement_id)}<br>Version: ${esc(row.version_id)}</p></details></td><td><span class="cell-main">${esc(row.description || 'No description recorded')}</span><span class="secondary">Supplier: ${esc(row.supplier_id || 'Unavailable')}</span></td><td class="number">${vatMoney(row.amount,row.currency)}</td><td class="number">${hasVat?vatRate(row.vat_rate):'<span class="muted">Unavailable</span>'}</td><td class="number">${vatMoney(hasVat?row.vat_amount:null,row.currency)}</td><td class="number">${vatMoney(hasVat?row.gross_amount:null,row.currency)}</td></tr>`).join('')}</tbody></table></div><div class="pagination"><span>${offset+1}–${offset+items.length} of ${integer(total)} movements</span><div><button data-invoice-movement-page="${esc(id)}" data-movement-offset="${Math.max(0,offset-limit)}" ${offset===0?'disabled':''}>← Previous</button><button data-invoice-movement-page="${esc(id)}" data-movement-offset="${offset+limit}" ${page.has_more?'':'disabled'}>Next →</button></div></div>`;
  }
  async function loadInvoiceMovements(id,offset){
    const target=$('#invoice-movement-preview');if(!target || target.dataset.invoiceId!==id)return;
    const generation=state.dialogSequence,sequence=(state.invoiceMovementSequence || 0)+1;state.invoiceMovementSequence=sequence;
    const current=()=>dialogCurrent(generation) && target.isConnected && $('#invoice-movement-preview')===target && state.invoiceMovementSequence===sequence;
    target.setAttribute('aria-busy','true');target.querySelectorAll('button').forEach(button=>button.disabled=true);
    try{const result=await api(`/api/invoices/${encodeURIComponent(id)}/movements?limit=25&offset=${offset}`);if(current())target.innerHTML=movementPreviewMarkup(result,id,target.dataset.vatPolicy);}
    catch(error){if(current())target.innerHTML=`<div class="notice warning">${esc(errorText(error))}</div><button data-invoice-movement-page="${esc(id)}" data-movement-offset="${offset}">Retry movement preview</button>`;}
    finally{if(current())target.setAttribute('aria-busy','false');}
  }
  function invoiceFormatForm(button){
    if(!canReview())return;const id=button.dataset.formatInvoiceId,target=button.dataset.invoiceFormat,fingerprint=button.dataset.formatFingerprint;
    if(state.invoiceFormatPending?.has(id)){toast('A format replacement is already being prepared for this invoice.');return;}
    if(!['ITEMIZED','SUMMARY'].includes(target) || !id || !fingerprint)return;
    const vatUpgrade=button.dataset.vatUpgrade==='true';openDialog('INVOICE REVISION',vatUpgrade?'Add VAT breakdown':target==='ITEMIZED'?'Generate itemised replacement':'Generate summary replacement');
    $('#detail-content').innerHTML=`<div class="notice">The current invoice revision and its PDF remain in the audit history. The server creates a replacement using the same billed movements and totals. It does not approve or send either package.</div>${vatUpgrade?'<div class="notice">This replacement adds VAT rate, rounded VAT and gross amount to each movement. An explicit rounding adjustment preserves the existing invoice totals.</div>':''}<p class="small muted">Requested format: <strong>${esc(invoiceFormatLabel(target))}</strong>. ${target==='ITEMIZED'?'The PDF lists all movements, up to 10,000; larger requests are rejected without truncation.':'The PDF groups the movements into summary lines.'} Inspect the replacement PDF before giving a fresh personal approval.</p><form id="invoice-format-form" data-invoice-id="${esc(id)}" data-fingerprint="${esc(fingerprint)}" data-line-format="${target}" class="stack"><div class="checkbox-line"><input id="invoice-format-confirm" type="checkbox" required><label for="invoice-format-confirm">Create this format replacement for the inspected package. I understand that the old revision is retained and the new PDF needs a separate approval.</label></div><button id="invoice-format-submit" type="submit" class="primary" disabled>Generate replacement PDF</button><p id="detail-error" class="error-text" role="alert" hidden></p></form>`;
  }
  async function saveInvoiceFormat(form,generation){
    if(!$('#invoice-format-confirm')?.checked)throw new Error('Confirm the replacement after reviewing its scope.');
    const id=form.dataset.invoiceId,payload={line_format:form.dataset.lineFormat,fingerprint:form.dataset.fingerprint};
    if(!state.invoiceFormatRequests)state.invoiceFormatRequests=new Map();if(!state.invoiceFormatPending)state.invoiceFormatPending=new Set();
    if(state.invoiceFormatPending.has(id))throw new Error('A replacement request for this invoice is already running.');
    const signature=JSON.stringify(payload);let request=state.invoiceFormatRequests.get(id);if(request?.signature!==signature){if(!request && state.invoiceFormatRequests.size>=20)throw new Error('Twenty replacement requests still need confirmation. Retry an existing request before starting another.');request={signature,id:uuid()};state.invoiceFormatRequests.set(id,request);}
    state.invoiceFormatPending.add(id);
    try{const result=await api(`/api/invoices/${encodeURIComponent(id)}/format`,{...payload,request_id:request.id});if(!result.id)throw new Error('Replacement confirmation did not include its invoice ID. Retry this same request to recover the result.');state.invoiceFormatRequests.delete(id);toast(`Replacement ${result.number || result.id} is being prepared. Inspect its PDF in Invoices before fresh approval.`);if(dialogCurrent(generation))await openInvoice(result.id);}
    finally{state.invoiceFormatPending.delete(id);}
  }
  function prepareInvoice(){
    openDialog('MONTHLY CLOSE','Prepare invoice');
    const periods=state.summary?.periods || [];
    $('#detail-content').innerHTML=`<p class="muted small">Choose a customer and period. The system verifies source coverage, unresolved cases and eligible movements before preparing a frozen invoice package.</p><form id="prepare-form" class="stack"><label for="prepare-customer">Customer</label><select name="customer_id" id="prepare-customer" required><option value="">Select a customer</option>${state.customers.map(c=>`<option value="${esc(c.id)}">${esc(c.legal_name)}</option>`).join('')}</select><label for="prepare-period">Billing period</label><input id="prepare-period" name="period" type="month" value="${esc([...periods].sort().at(-1) || '')}" required><label for="prepare-line-format">PDF format</label><select id="prepare-line-format" name="line_format"><option value="ITEMIZED" selected>Itemised · every billed movement</option><option value="SUMMARY">Summary · grouped totals</option></select><p class="hint">Itemised PDFs include every movement, up to 10,000. Requests above that limit are rejected; no rows are silently omitted.</p><button class="primary" type="submit">Check & prepare invoice</button><p id="detail-error" class="error-text" role="alert" hidden></p></form>`;
  }
  /* Insights: read-only statistical signals from /api/insights (docs/INSIGHTS.md).
     Every value shown comes from the API response; nothing is derived beyond
     display formatting, and missing values render as "—", never as zero. */
  const ratioText=value=>value===null || value===undefined || value===''?'—':`${(Number(value)*100).toFixed(1)}%`;
  const hoursText=value=>!Number.isFinite(Number(value)) || value===null?'—':Number(value)<48?`${Number(value).toFixed(1)} h`:`${(Number(value)/24).toFixed(1)} days`;
  const statText=(value,places=3)=>value===null || value===undefined || !Number.isFinite(Number(value))?'—':Number(value).toFixed(places);
  const periodLabel=value=>{const m=String(value || '').match(/^(\d{4})-(\d{2})$/);return m?new Date(Date.UTC(+m[1],+m[2]-1,1)).toLocaleDateString('en-GB',{month:'short',year:'numeric',timeZone:'UTC'}):String(value || '');};
  /* Colour follows the state, never its rank in the mix. */
  const insightStateTone=value=>({ELIGIBLE:'success',PENDING_MODEL:'info',REVIEW:'warning',BLOCKED:'danger',PENDING:'neutral'})[String(value || '').toUpperCase()] || 'accent';
  const insightPill=(text,kind)=>`<span class="badge ${kind}">${esc(text)}</span>`;
  const hhiKind=label=>label==='highly concentrated'?'bad':label==='moderately concentrated'?'warn':label?'good':'neutral';
  const conformityKind=label=>/^(close|acceptable)/.test(label || '')?'good':/^marginally/.test(label || '')?'warn':label==='nonconformity'?'bad':'neutral';
  function plainLimit(text){return String(text ?? '').replace(/total_net is NULL when the scope holds more than one currency;?\s*see net_by_currency/i,'With more than one currency, totals are shown per currency').replace(/customer_id/g,'customer').replace(/\bNULL\b/g,'empty').replace(/\bnet_by_currency\b/g,'per-currency totals');}
  function insightCard(id,title,subtitle,body,section,global,index,extra='',wide=false){
    const own=(section?.limitations || []).filter(text=>!global.includes(text));
    return `<section class="panel insight-card${wide?' is-wide':''}" id="insight-${id}" aria-labelledby="insight-${id}-title"${index===null?'':` style="--i:${index}"`}><div class="panel-header"><div><h2 id="insight-${id}-title">${esc(title)}</h2><p class="panel-subtitle">${esc(subtitle)}</p></div>${extra}</div><div class="panel-body">${body}${section?.method?`<details class="insight-method"><summary>Method</summary><p class="small muted">${esc(section.method)}</p></details>`:''}${own.length?`<p class="insight-footnote">${own.map(t=>esc(plainLimit(t))).join(' · ')}</p>`:''}</div></section>`;
  }
  function insightFind(movement,customer){
    const id=String(movement?.external_id || '');if(id.length<3 || id.length>100)return '';
    return `<button type="button" class="text-button small" data-insight-movement="${esc(id)}" data-insight-customer="${esc(customer || '')}" aria-label="Find review cases for movement ${esc(id)}">Find cases →</button>`;
  }
  function insightKpis(o,enter){
    const tile=(key,label,value,note,numericValue,kind='',i=0)=>`<div role="listitem" class="insight-kpi${kind?' '+kind:''}"${enter?` style="--i:${i}"`:''}><span class="metric-label">${esc(label)}</span><span class="metric-value" data-key="insights:${key}"${Number.isFinite(numericValue)?` data-value="${numericValue}"`:''}>${value}</span><span class="metric-note">${esc(note)}</span></div>`;
    const net=o.total_net!==null && o.total_net!==undefined?amount(o.total_net,o.net_by_currency?.[0]?.currency || 'GBP'):(o.net_by_currency || []).length?(o.net_by_currency || []).map(x=>amount(x.net_amount,x.currency)).join('<br>'):'—';
    const scores=o.model_scores || {};
    return `<div class="insight-kpis" role="list" aria-label="Period summary">${[
      tile('movements','Movements',integer(o.movements),`${integer(o.versioned_movements)} with an amount · ${integer(o.movements_without_version)} pending`,Number(o.movements),'',0),
      tile('net',(o.net_by_currency || []).length>1?'Net by currency':'Net amount',net,'Current versions, exact',NaN,'',1),
      tile('suppliers','Suppliers',integer(o.distinct_suppliers),`${integer(o.customers)} customers`,Number(o.distinct_suppliers),'',2),
      tile('flagged','Flagged rate',esc(ratioText(o.flagged_rate)),`${integer(o.flagged)} flagged · rule ${integer(o.rule_flagged)} · model ${integer(o.model_flagged)}`,NaN,'',3),
      tile('unavailable','Scores unavailable',integer(scores.unavailable),`${integer(scores.not_assessed)} not assessed · ${integer(scores.scored)} scored`,Number(scores.unavailable),Number(scores.unavailable)>0?'is-neutral':'',4)
    ].join('')}</div>`;
  }
  function insightSuppliers(s){
    const charts=window.MiniCharts,suppliers=s.top_suppliers || [],currencies=[...new Set(suppliers.map(x=>x.currency))];
    const lists=currencies.map(currency=>{const items=suppliers.filter(x=>x.currency===currency);return charts?charts.bar(items.map(x=>({label:x.supplier_id,value:Number(x.net_amount),display:amount(x.net_amount,x.currency).replace(/&amp;/g,'&'),tip:`${x.supplier_id} · ${amount(x.net_amount,x.currency).replace(/&amp;/g,'&')} · ${ratioText(x.share)} of ${currency} · ${integer(x.movements)} movements · ${integer(x.customers)} customers`})),{label:`Top suppliers by net amount (${currency})`,share:false,max:items.length+1,key:`insights:suppliers:${currency}`,summary:items.map(x=>`${x.supplier_id} ${ratioText(x.share)}`).join(', ')+'.'}):'';}).join('');
    const customers=s.customers || [];
    const table=customers.length?`<div class="table-wrap"><table class="insight-table"><caption class="sr-only">Supplier concentration (HHI) per customer and currency</caption><thead><tr><th scope="col">Customer</th><th scope="col" class="number">HHI</th><th scope="col" class="number">Top supplier</th><th scope="col">Classification</th></tr></thead><tbody>${customers.slice(0,8).map(c=>`<tr><td><span class="cell-main">${esc(customerName(c.customer_id))}</span><span class="secondary">${amount(c.net_amount,c.currency)} · ${integer(c.suppliers)} suppliers</span></td><td class="number">${esc(c.hhi ?? '—')}</td><td class="number">${esc(ratioText(c.top_supplier_share))}</td><td>${c.classification?insightPill(human(c.classification),hhiKind(c.classification)):'<span class="muted">Not evaluated</span>'}</td></tr>`).join('')}</tbody></table></div>`:'';
    return suppliers.length?`${lists}${table}`:empty('No supplier amounts','No current versions with a supplier_id in this scope.');
  }
  function insightHhiBadge(s,customer){
    const scoped=customer?(s.customers || [])[0]:null;
    if(scoped)return insightPill(`HHI ${scoped.hhi ?? '—'}${scoped.classification?' · '+scoped.classification:''}`,hhiKind(scoped.classification));
    return insightPill(`${integer(s.customers_flagged)} of ${integer(s.customers_evaluated)} customers HHI > ${s.hhi_threshold}`,Number(s.customers_flagged)>0?'warn':'good');
  }
  function insightDuplicates(d){
    const pairs=d.pairs || [];if(!pairs.length)return empty('No duplicate candidates',`No equal-amount pairs within ${integer(d.window_days)} days in this period.`);
    const side=m=>`<span class="cell-main mono">${esc(m.external_id || m.movement_id)}</span><span class="secondary">${utc(m.event_at)}</span>`;
    return `<p class="small muted">${integer(pairs.length)} of ${integer(d.total_pairs)} pairs shown, largest amounts first.</p><div class="table-wrap"><table class="insight-table"><caption class="sr-only">Duplicate candidate pairs</caption><thead><tr><th scope="col">Customer / supplier</th><th scope="col" class="number">Amount</th><th scope="col" class="number">Gap</th><th scope="col">First</th><th scope="col">Second</th><th scope="col">Cases</th></tr></thead><tbody>${pairs.map(p=>`<tr><td><span class="cell-main">${esc(customerName(p.customer_id))}</span><span class="secondary mono">${esc(p.supplier_id)}</span></td><td class="number">${amount(p.amount,p.currency)}</td><td class="number">${esc(hoursText(p.gap_hours))}</td><td>${side(p.first || {})}</td><td>${side(p.second || {})}</td><td>${insightFind(p.second,p.customer_id) || insightFind(p.first,p.customer_id) || '<span class="muted">—</span>'}</td></tr>`).join('')}</tbody></table></div>`;
  }
  function insightOutliers(o){
    const rows=(o.outliers || []).slice(0,10);
    const coverage=`<p class="small muted">${integer(o.groups_evaluated)} supplier groups evaluated (${integer(o.movements_evaluated)} movements); ${integer(o.groups_below_min_sample)} below the minimum sample of ${integer(o.min_sample)}.${rows.length?` ${integer(rows.length)} of ${integer(o.total_outliers)} shown, largest |z| first.`:''}</p>`;
    if(!rows.length)return coverage+empty('No amount outliers',`No movement has |z| above ${o.threshold} in an evaluated supplier group.`);
    return `${coverage}<div class="table-wrap"><table class="insight-table"><caption class="sr-only">Amount outliers by robust z-score</caption><thead><tr><th scope="col">Movement</th><th scope="col">Supplier</th><th scope="col" class="number">Amount</th><th scope="col" class="number">Supplier median</th><th scope="col" class="number">Robust z</th><th scope="col">Cases</th></tr></thead><tbody>${rows.map(r=>`<tr><td><span class="cell-main mono">${esc(r.external_id || r.movement_id)}</span><span class="secondary">${esc(customerName(r.customer_id))}</span></td><td><span class="mono">${esc(r.supplier_id)}</span><span class="secondary">${integer(r.supplier_movements)} movements · ${esc(r.dispersion)}</span></td><td class="number">${amount(r.amount,r.currency)}</td><td class="number">${amount(r.supplier_median,r.currency)}</td><td class="number"><span class="insight-z ${r.direction==='high'?'is-high':'is-low'}">${esc(statText(r.robust_z,2))}</span><span class="secondary">${esc(r.direction==='high'?'Above':'Below')} median</span></td><td>${insightFind(r,r.customer_id) || '<span class="muted">—</span>'}</td></tr>`).join('')}</tbody></table></div>`;
  }
  function insightBenford(b){
    const charts=window.MiniCharts,digits=b.digits || [],computed=b.status==='computed';
    const pill=computed?insightPill(human(b.conformity),conformityKind(b.conformity)):insightPill('Insufficient data','neutral');
    const chart=charts && Number(b.n)>0?charts.paired(digits.map(d=>({label:String(d.digit),value:Number(d.observed_share),reference:Number(d.expected_share),tip:`Digit ${d.digit} · observed ${ratioText(d.observed_share)} (${integer(d.observed)}) · expected ${ratioText(d.expected_share)}`})),{label:'First-digit shares, observed against Benford expected',key:'insights:benford',summary:digits.map(d=>`${d.digit}: ${ratioText(d.observed_share)} vs ${ratioText(d.expected_share)}`).join(', ')+'.'}):'';
    const stats=computed?`<dl class="insight-stats">${detail('Amounts tested',integer(b.n))}${detail('Average deviation (MAD)',esc(statText(b.mad,4)))}${detail('Fit statistic (chi-square, 8 df)',esc(statText(b.chi_square,2)))}${detail('p-value · chance of this pattern',esc(statText(b.p_value,4)))}</dl>`:`<div class="notice insight-insufficient"><strong>Insufficient data.</strong> ${integer(b.n)} amounts of at least 10 in scope; the test needs ${integer(b.min_n)}. Counts are shown without a conformity result.</div>`;
    return {pill,body:`${stats}${chart || (Number(b.n)>0?'':empty('No amounts to test','No current-version amounts of at least 10 in this scope.'))}<p class="small muted">${integer(b.excluded_below_minimum_amount)} amounts below 10 excluded.</p>`};
  }
  function insightTrends(t){
    const charts=window.MiniCharts,series=t.scope_series || [],currencies=[...new Set(series.map(x=>x.currency))];
    const change=value=>value===null || value===undefined?'no prior value':Math.abs(Number(value))>=1000?'new this period (previous period near zero)':`${Number(value)>0?'+':'−'}${Math.abs(Number(value))}%`;
    const blocks=currencies.map(currency=>{const points=series.filter(x=>x.currency===currency),last=points[points.length-1];
      const firstData=points.findIndex(p=>Number(p.net_amount)!==0 || Number(p.movements)>0),active=firstData<0?points:points.slice(firstData);const bars=charts?(charts.area?charts.area(active.map(p=>({label:periodLabel(p.period),value:Number(p.net_amount),display:amount(p.net_amount,currency).replace(/&amp;/g,'&'),note:`${integer(p.movements)} movements · ${change(p.net_change_pct)}`})),{label:`Net amount per period (${currency})`,key:`insights:trend:${currency}`,ranges:[['3M',3],['6M',6],['12M',12]],empty:`${integer(active.length)} of ${integer(points.length)} periods have data`}):charts.histogram(active.map(p=>({label:periodLabel(p.period),value:Number(p.net_amount),display:amount(p.net_amount,currency).replace(/&amp;/g,'&')})),{label:`Net amount per period (${currency})`,key:`insights:trend:${currency}`})):'';
      const spark=charts?charts.sparkline(active.map(p=>p.movements),{label:`Movements per period (${currency})`,interactive:true,labels:active.map(p=>periodLabel(p.period)),tips:active.map(p=>`${periodLabel(p.period)} · ${integer(p.movements)} movements · ${change(p.movement_change_pct)}`),key:`insights:spark:${currency}`}):'';
      const pct=Number(last?.net_change_pct),dir=last?.net_change_pct==null || Math.abs(pct)>=1000?'flat':pct>0?'up':pct<0?'down':'flat';return `<div class="insight-trend"><div class="quote-head"><strong class="quote-value">${amount(last?.net_amount,currency)}</strong><span class="delta-chip is-${dir}"><span aria-hidden="true">${dir==='up'?'▲':dir==='down'?'▼':'•'}</span> ${esc(change(last?.net_change_pct))}</span><span class="quote-sub muted">${esc(periodLabel(last?.period))} · against the previous period</span></div>${bars}<p class="small-label insight-spark-label">MOVEMENTS PER PERIOD</p>${spark}</div>`;}).join('');
    const movers=(t.top_movers || []).slice(0,5);
    const table=movers.length?`<div class="table-wrap"><table class="insight-table"><caption class="sr-only">Top movers against the previous period</caption><thead><tr><th scope="col">Customer</th><th scope="col" class="number">Previous</th><th scope="col" class="number">This period</th><th scope="col" class="number">Change</th></tr></thead><tbody>${movers.map(m=>`<tr><td>${esc(customerName(m.customer_id))}</td><td class="number">${amount(m.previous_net_amount,m.currency)}</td><td class="number">${amount(m.net_amount,m.currency)}</td><td class="number">${amount(m.net_change,m.currency)}<span class="secondary">${esc(m.net_change_pct===null?'new or from zero':m.net_change_pct+'%')}</span></td></tr>`).join('')}</tbody></table></div>`:'';
    return series.length?blocks+table:empty('No movement amounts','No current versions in the last 12 periods for this scope.');
  }
  function insightRisk(r){
    const charts=window.MiniCharts,hist=r.histogram || [],q=r.quantiles || {};
    const chart=charts && Number(r.scored)>0?charts.histogram(hist.map(b=>({label:`${b.lower.toFixed(1)}–${b.upper.toFixed(1)}`,value:b.versions,tip:`Score ${b.lower.toFixed(1)}–${b.upper.toFixed(1)} · ${integer(b.versions)} versions`})),{label:'Scored model results by score band',key:'insights:risk'}):empty('No scored versions','No model score was recorded for current versions in this scope.');
    /* All scored: one plain line instead of a bar with two zero segments. */
    const nulls=Number(r.scored)>0 && !Number(r.unavailable) && !Number(r.not_assessed)?`<p class="small muted">All ${integer(r.scored)} current versions have a model score.</p>`:charts?charts.stackedBar([{label:'Scored',value:r.scored,tone:'accent'},{label:'Score unavailable',value:r.unavailable,tone:'warning'},{label:'Not assessed',value:r.not_assessed,tone:'neutral'}],{label:'Model score availability',key:'insights:risk-null'}):'';
    return `${chart}<dl class="insight-stats">${detail('Mean (scored only)',esc(statText(r.mean_score)))}${detail('p50 / p90 / p99',esc(`${statText(q.p50)} / ${statText(q.p90)} / ${statText(q.p99)}`))}${detail('Unavailable (NULL)',integer(r.unavailable))}${detail('Not assessed',integer(r.not_assessed))}</dl>${nulls}<p class="small muted">${integer(r.unavailable)} unavailable and ${integer(r.not_assessed)} unassessed versions are counted here and never placed in a score band.</p>`;
  }
  function insightAgeing(a){
    const charts=window.MiniCharts,labels={'0-1d':'Under 1 day','1-3d':'1–3 days','3-7d':'3–7 days','>7d':'Over 7 days'},tones={'0-1d':'neutral','1-3d':'info','3-7d':'warning','>7d':'danger'};
    if(!Number(a.open_cases))return empty('No open review cases','Nothing is waiting for review in this scope.');
    const bar=charts?charts.stackedBar((a.by_age || []).map(b=>({label:labels[b.bucket] || b.bucket,value:b.cases,tone:tones[b.bucket]})),{label:'Open cases by age',key:'insights:ageing'}):'';
    const reasons=(a.by_reason || []).slice(0,6);
    return `<p class="small"><strong>${integer(a.open_cases)}</strong> open · oldest ${esc(hoursText(a.oldest_age_hours))}</p>${bar}${(a.by_severity || []).length?`<p class="insight-chips">${a.by_severity.map(s=>`<span class="badge ${/HIGH|CRITICAL/.test(s.severity)?'bad':/MEDIUM/.test(s.severity)?'warn':'neutral'}">${esc(human(s.severity))} ${integer(s.cases)}</span>`).join(' ')}</p>`:''}${reasons.length?`<ul class="insight-list">${reasons.map(r=>`<li><span>${esc(human(r.reason))} <span class="muted">· ${esc(human(r.severity))}</span></span><strong>${integer(r.cases)}</strong>${Number(r.older_than_7d)?`<span class="secondary">${integer(r.older_than_7d)} older than 7 days</span>`:''}</li>`).join('')}</ul>`:''}`;
  }
  function insightQuality(q){
    const charts=window.MiniCharts,fields=q.missing_optional_fields || [];
    const missing=charts && fields.length?charts.bar(fields.map(f=>({label:f.field,value:f.missing,display:`${integer(f.missing)} · ${ratioText(f.rate)}`,tip:`${f.field} missing in ${integer(f.missing)} of ${integer(q.versions)} versions (${ratioText(f.rate)})`})),{label:'Missing optional fields',share:false,max:fields.length+1,key:'insights:quality',tone:'warning'}):'';
    const list=(title,items,render)=>items.length?`<h3 class="insight-subhead">${esc(title)}</h3><ul class="insight-list">${items.map(render).join('')}</ul>`:'';
    return `<dl class="insight-stats">${detail('Current versions',integer(q.versions))}${detail('Corrected versions',integer(q.corrected_versions))}${detail('Rule failures',integer(q.rule_failures))}${detail('Without rule check',integer(q.versions_without_rule_assessment))}</dl>${missing}${list('Source mismatches',q.source_mismatches || [],m=>`<li><span>${esc(human(m.reason))}</span><strong>${integer(m.versions)}</strong></li>`)}${list('Source records',q.source_records || [],s=>`<li><span>Source ${esc(s.source)}</span><strong>${integer(s.records)}</strong><span class="secondary">${integer(s.movements)} movements</span></li>`)}${list('Quarantine',q.quarantine || [],r=>`<li><span>${esc(r.reason)} <span class="muted">· source ${esc(r.source)}</span></span><strong>${integer(r.rows)}</strong><span class="secondary">${integer(r.open)} open</span></li>`)}`;
  }
  async function insightsView(sequence){
    const customer=state.customerFilters.insights || '',scope=customer?{customer}:{};
    const enter=state.announceView===true,stagger=i=>enter?Math.min(i,12):null;
    let periods;
    try{periods=await viewApi('/api/insights/periods?'+new URLSearchParams(scope));}
    catch(error){if(error.name==='AbortError')throw error;if(sequence!==state.requestSequence)return null;return insightsError(error,customer);}
    if(sequence!==state.requestSequence)return null;
    const available=Array.isArray(periods.periods)?periods.periods:[];
    if(!available.length)return `${insightsFilters([],'',customer)}${empty('No movements yet',customer?'This customer has no recorded movements. Choose another customer or all customers.':'Insights appear once source movements have been ingested.','insight-empty')}`;
    const fallback=state.insightPeriod && !available.includes(state.insightPeriod);
    /* Open on the last complete month: a month in progress compared with a full
       month reads as a false collapse (-99%). It stays one click away. */
    const current=new Date().toISOString().slice(0,7),complete=available[0]===current && available.length>1?available[1]:available[0];
    const period=fallback || !state.insightPeriod?complete:state.insightPeriod;
    let report;
    try{report=await viewApi('/api/insights/report?'+new URLSearchParams({period,...scope}));}
    catch(error){if(error.name==='AbortError')throw error;if(sequence!==state.requestSequence)return null;return insightsFilters(available,period,customer)+insightsError(error,customer,true);}
    if(sequence!==state.requestSequence)return null;
    const s=report.sections || {},global=report.limitations || [],benford=insightBenford(s.benford || {});
    const notice=fallback?`<div class="notice">No movements in ${esc(periodLabel(state.insightPeriod))} for this scope; showing ${esc(periodLabel(period))}, the latest period with data.</div>`:'';
    const cards=[
      insightCard('suppliers','Supplier concentration','Share of net amount by supplier; HHI per customer and currency',insightSuppliers(s.supplier_concentration || {}),s.supplier_concentration,global,stagger(1),insightHhiBadge(s.supplier_concentration || {},customer)),
      insightCard('trends','Trend · last 12 periods',`${periodLabel((s.trends?.periods || [])[0])} to ${periodLabel(period)}`,insightTrends(s.trends || {}),s.trends,global,stagger(2)),
      insightCard('duplicates','Duplicate candidates',`Same customer, supplier, currency and amount within ${s.duplicate_candidates?.window_days ?? 7} days`,insightDuplicates(s.duplicate_candidates || {}),s.duplicate_candidates,global,stagger(3),insightPill(`${integer(s.duplicate_candidates?.total_pairs)} ${Number(s.duplicate_candidates?.total_pairs)===1?'pair':'pairs'}`,Number(s.duplicate_candidates?.total_pairs)>0?'warn':'good'),true),
      insightCard('outliers','Amount outliers',`Robust (modified) z-score per supplier and currency, |z| > ${s.amount_outliers?.threshold ?? 3.5}`,insightOutliers(s.amount_outliers || {}),s.amount_outliers,global,stagger(4),insightPill(`${integer(s.amount_outliers?.total_outliers)} ${Number(s.amount_outliers?.total_outliers)===1?'outlier':'outliers'}`,Number(s.amount_outliers?.total_outliers)>0?'warn':'good'),true),
      insightCard('benford','First-digit test (Benford)','Observed first significant digits against log10(1 + 1/d)',benford.body,s.benford,global,stagger(5),benford.pill),
      insightCard('risk','Model score distribution','Scored versions in ten bands; unavailable scores counted separately',insightRisk(s.risk_distribution || {}),s.risk_distribution,global,stagger(6)),
      insightCard('ageing','Review ageing','Open review cases by time since creation',insightAgeing(s.review_ageing || {}),s.review_ageing,global,stagger(7)),
      insightCard('quality','Data quality','Missing fields, rule failures, source coverage and quarantine',insightQuality(s.data_quality || {}),s.data_quality,global,stagger(8))
    ].join('');
    const overviewSection=s.overview || {},states=overviewSection.state_mix || [],charts=window.MiniCharts;
    const stateMix=charts && states.length?charts.stackedBar(states.map(x=>({label:human(x.state),value:x.movements,tone:insightStateTone(x.state)})),{label:'Movements by state',key:'insights:states'}):'';
    /* Show a caveat only when it applies to this snapshot (one currency, no quarantine: no note). */
    const overviewNotes=(overviewSection.limitations || []).filter(x=>!global.includes(x)).filter(x=>!(/^total_net is NULL/i.test(x) && overviewSection.total_net!=null) && !(/quarantine/i.test(x) && !(overviewSection.quarantine?.rows>0)));
    const head=`<section class="panel insight-card insight-summary" aria-labelledby="insight-summary-title"${enter?' style="--i:0"':''}><div class="panel-header"><div><h2 id="insight-summary-title">${esc(periodLabel(period))}${customer?` · ${esc(customerName(customer))}`:' · all customers'}</h2><p class="panel-subtitle">Snapshot ${utc(report.generated_at).replace(/(\d{2}:\d{2}:\d{2})\.\d+/,'$1')} · one read-only snapshot for every section</p></div></div><div class="panel-body">${insightKpis(overviewSection,enter)}${stateMix}${overviewNotes.length?`<p class="insight-footnote">${overviewNotes.map(t=>esc(plainLimit(t))).join(' · ')}</p>`:''}</div></section>`;
    return `${insightsFilters(available,period,customer)}${notice}${head}<div class="insight-grid">${cards}</div><p class="insight-footnote insight-global">${global.map(t=>esc(plainLimit(t))).join(' · ')}.</p>`;
  }
  function insightsFilters(periods,period,customer){
    return `<div class="filters insight-filters"><div><label for="insights-period">Period</label><select id="insights-period" data-insights-period ${periods.length?'':'disabled'}>${periods.map(p=>`<option value="${esc(p)}" ${p===period?'selected':''}>${esc(periodLabel(p))} (${esc(p)})${p===new Date().toISOString().slice(0,7)?' · month to date':''}</option>`).join('')}</select></div>${customerFilter('insights')}</div>`;
  }
  function insightsError(error,customer,hasFilters=false){
    return `${hasFilters?'':insightsFilters([],'',customer)}<div class="empty-state insight-error" role="alert"><strong>Insights are unavailable right now</strong><p>${esc(errorText(error))}</p><button type="button" data-insights-retry>Try again</button></div>`;
  }
  async function delivery(sequence){
    const items=rows(await viewApi(`/api/outbox?limit=${state.limit}&offset=${state.offset}${customerQuery('delivery')}`));
    const list=items.length?`<div class="table-wrap"><table><thead><tr><th>Message</th><th>Recipient</th><th>Submission</th><th>Delivery evidence</th><th>Original source files</th><th>Updated</th><th>Action</th></tr></thead><tbody>${items.map(item=>`<tr><td><span class="cell-main">${esc(item.kind || item.type || 'Message')}</span><span class="secondary mono">${esc(item.id)}</span><span class="secondary">${item.customer_id?esc(customerName(item.customer_id)):'Workspace message'}</span></td><td>${esc(item.recipient || item.to || item.payload?.to || 'Not recorded')}</td><td>${badge(item.status)}</td><td>${badge(item.delivery_status || item.delivery || 'UNKNOWN')}</td><td class="source-cell">${item.customer_id || item.source_files?.length || item.sources_url?sourceReferences(item,true):'<span class="small muted">Not linked to source files</span>'}</td><td>${date(item.updated_at || item.created_at)}</td><td><button class="table-action" data-outbox="${esc(item.id)}">Details →</button></td></tr>`).join('')}</tbody></table></div>`:empty('No outgoing messages','New flags create alert jobs. Approved invoices create dispatch jobs.');
    if(sequence!==state.requestSequence)return null;state.outboxRows=items;
    return `<p class="small muted">Workspace totals · all customers</p><div class="summary-grid">${metric('Queued',count('queued'),state.automationStatus?.mail?.ready===false && count('queued')>0?'Waiting for Gmail connection':'Waiting for a worker','delivery')}${metric('Provider accepted',count('accepted'),'Provider submission evidence','delivery')}${metric('Delivered',count('delivered'),'Recipient server evidence','delivery')}${metric('Uncertain',count('unknown_delivery'),'Needs reconciliation','delivery',true)}</div><section class="panel"><div class="panel-header"><div><h2>Message delivery</h2><p class="panel-subtitle">Invoice sends and exception alerts use the same auditable outbox.</p></div><a class="small" id="dispatch-export" href="/api/exports/dispatch.csv?${esc(new URLSearchParams(state.customerFilters.delivery?{customer_id:state.customerFilters.delivery}:{}).toString())}">Export filtered CSV ↗</a></div><div class="filters">${customerFilter('delivery')}</div>${list}${pagination(items.length)}</section><div class="notice">Provider acceptance is not delivery. Delivery is not proof of human reading. An open event is only a tracking signal; uncertain submissions are held for reconciliation.</div>${String(state.summary?.mode).toUpperCase().includes('SIM')?`<section class="panel"><div class="panel-header"><div><h2>Simulation controls</h2><p class="panel-subtitle">These controls affect the local fake provider only. No real email is sent.</p></div></div><div class="panel-body"><div class="section-actions"><label for="provider-outcome" class="small">Next provider outcome</label><select id="provider-outcome" style="width:auto"><option value="accepted">Accepted</option><option value="rejected">Rejected</option><option value="unknown">Unknown after submission</option></select><button data-action="set-provider">Set outcome</button><button data-action="worker-tick">Run one worker tick</button></div></div></section>`:''}`;
  }
  function openOutbox(id){
    const item=state.outboxRows?.find(row=>String(row.id)===String(id));if(!item)return;
    openDialog('DELIVERY EVIDENCE','Message details');
    const attempts=Array.isArray(item.attempts)?item.attempts:[];const attemptId=item.attempt_id || item.latest_attempt_id || attempts.at(-1)?.id;
    $('#detail-content').innerHTML=`<dl class="details-grid">${detail('Submission',badge(item.status))}${detail('Delivery',badge(item.delivery_status || 'UNKNOWN'))}${detail('Recipient',esc(item.recipient || item.to || 'Not recorded'),true)}${detail('Open signal',item.opened_signal?'Observed — not proof of reading':'Not observed')}</dl><h3>Original source files</h3>${sourceReferences(item)}<details><summary>Exact message, attempts & provider evidence</summary>${json(item)}</details>${attemptId && item.status==='UNKNOWN'?`<section class="form-section"><h3>Reconcile uncertain submission</h3><p class="muted small">Look up evidence for the existing attempt. This action does not resend the message. If no reliable evidence is available, its status stays unknown.</p><button class="primary" data-reconcile-attempt="${esc(attemptId)}">Check provider evidence</button></section>`:''}${attemptId && String(state.summary?.mode).toUpperCase().includes('SIM')?`<section class="form-section"><h3>Simulate a provider event</h3><p class="muted small">The event updates evidence for this exact attempt.</p><div class="section-actions"><button data-sim-event="delivered" data-attempt="${esc(attemptId)}">Delivered</button><button data-sim-event="bounced" data-attempt="${esc(attemptId)}">Bounced</button><button data-sim-event="opened" data-attempt="${esc(attemptId)}">Open signal</button></div></section>`:''}<p id="detail-error" class="error-text" role="alert" hidden></p>`;
  }
  function serviceChecks(){
    const value=key=>count(key)===null?'Unavailable':integer(count(key));
    const monthly=state.summary?.automation?.['scheduler.close.report'],dispatch=state.summary?.automation?.['scheduler.dispatch.report'];
    const policy=state.summary?.auto_monthly_preparation;
    const report=monthly?.report;
    return `<section class="panel" id="processing-checks"><div class="panel-header"><div><h2>Processing checks</h2><p class="panel-subtitle">Job queues and recorded automation outcomes.</p></div>${count('jobs_failed')>0?badge('FAILED JOBS'):''}</div><div class="panel-body"><div class="inline-stats"><div><span class="small-label">JOBS QUEUED</span><span class="verification-total" data-check="jobs-ready">${value('jobs_ready')}</span></div><div><span class="small-label">JOBS RUNNING</span><span class="verification-total" data-check="jobs-running">${value('jobs_running')}</span></div><div><span class="small-label">JOBS FAILED</span><span class="verification-total" data-check="jobs-failed">${value('jobs_failed')}</span></div></div><div class="status-line"><span>Automatic monthly preparation</span><strong>${policy===true?'Enabled':policy===false?'Disabled':'Unavailable'}</strong></div><div class="status-line"><span>Monthly report recorded</span><strong>${monthly?.checked_at?date(monthly.checked_at):'Unavailable'}</strong></div>${report?`<p class="action-note">Last recorded outcome: ${integer(report.attempted)} scopes checked, ${integer(report.prepared)} prepared, ${integer(report.blocked)} blocked, ${integer(report.errors)} errors.</p>`:'<p class="action-note">No monthly scheduler report is available yet.</p>'}<details><summary>Latest monthly scheduler report</summary>${monthly?json(monthly):'<p class="small muted">Unavailable</p>'}</details>${dispatch?`<details><summary>Latest approved-dispatch report</summary>${json(dispatch)}</details>`:''}<p class="action-note">These are queue counts and retained outcomes, not a worker heartbeat. A report timestamp may stay unchanged when its outcome is unchanged.</p></div></section>`;
  }
  async function dataView(sequence){
    const [batchResponse,model,auditResponse,trainingRuns]=await Promise.all([viewApi(`/api/batches?limit=${state.limit}&offset=${state.offset}${customerQuery('data')}`),viewApi('/api/model'),viewApi('/api/audit?limit=10&offset=0'),viewApi('/api/model/runs?limit=20')]);
    if(sequence!==state.requestSequence)return null;const batches=rows(batchResponse),audit=rows(auditResponse);state.batchRows=batches;
    const table=batches.length?`<div class="table-wrap"><table><thead><tr><th>Source / batch</th><th>Status</th><th class="number">Rows / expected</th><th class="number">Quarantined</th><th>Received</th><th>Action</th></tr></thead><tbody>${batches.map(b=>`<tr><td><span class="cell-main">Source ${esc(b.source)} · ${esc(b.period || '')}</span><span class="secondary mono">${esc(String(b.id).slice(0,16))}…</span><div class="source-references">${sourceFileList([{...b,download_url:b.download_url || b.file_url}],true)}</div></td><td>${badge(b.superseded_by?'SUPERSEDED':b.status || 'UNKNOWN')}${b.superseded_by?'<span class="secondary">Replacement recorded</span>':''}</td><td class="number">${integer(b.row_count ?? b.rows ?? b.total_rows)} / ${integer(b.expected_rows)}</td><td class="number">${integer(b.quarantined_count ?? b.rejected_count ?? b.quarantined)}</td><td>${date(b.created_at)}</td><td><button class="table-action" data-batch="${esc(b.id)}">Inspect →</button></td></tr>`).join('')}</tbody></table></div>`:empty('No source batches yet','Upload the synthetic A and B files to populate this workspace.');
    return `<section class="panel"><div class="panel-header"><div><h2>Source batch verification</h2><p class="panel-subtitle">Original inputs are retained. Duplicate retries never create extra movements.</p></div><button class="primary" data-action="upload-source">Upload source +</button></div><div class="filters">${customerFilter('data')}<span class="small muted">Shows relevant batches; downloads contain the whole original file, possibly multiple customers. Processing checks, model and audit remain workspace-wide.</span></div>${table}${pagination(batches.length)}</section>${serviceChecks()}${modelOperationsMarkup(model,rows(trainingRuns))}<section class="panel"><div class="panel-header"><div><h2>Recent audit trail</h2><p class="panel-subtitle">Latest 10 recorded actions</p></div></div><div class="panel-body">${audit.length?audit.map(a=>`<div class="review-item"><div><strong>${esc(human(a.action || a.event || a.kind))}</strong><small>${esc(a.actor_display_name || a.actor || 'System')}${/^legacy/.test(a.actor_identity_type || '')?' (local/system or legacy actor; no personal account)':''} · ${utc(a.created_at)}</small></div><span class="small muted mono">${esc(String(a.entity_id || a.target_id || '').slice(0,14))}</span></div>`).join(''):empty('No audit events','Actions will appear here as the system processes records.')}</div></section>`;
  }
  const workspaceEmailUrl=()=>`${(state.gmail?.workspace_url || `${location.origin}${location.pathname}`).replace(/\/$/,'')}/#email`;
  function gmailOriginMatches(){try{return new URL(state.gmail?.callback_url).origin===location.origin;}catch{return false;}}
  async function emailView(){
    const status=await viewApi('/api/mail/gmail');state.gmail=status;
    if(status.last_error)state.gmailLastError=status.last_error;
    if(status.connected || status.connection_result?.ok)state.gmailLastError=null;
    const configured=status.client_configured===true,connected=status.connected===true;
    const realGmail=String(status.delivery_provider || status.automatic_delivery || '').toLowerCase()==='gmail',redirect=status.test_redirect?.active===true;
    return `${realGmail?`<div class="notice${connected?'':' warning'}" id="gmail-simulation-notice"><strong>Approved invoices are sent through Gmail${connected?'':' once the account is connected'}.</strong> Nothing is sent before a person approves the exact package.${redirect?' <strong>Test mode:</strong> all emails are redirected to your connected address.':''}</div>`:`<div class="notice" id="gmail-simulation-notice"><strong>Email delivery remains simulated.</strong> Connecting Gmail stores an account connection only. It does not enable real dispatch or send an email.</div>`}
      ${gmailProgress(status)}
      ${state.gmailLastError?`<div class="notice error" role="alert"><strong>Connection needs attention.</strong> ${esc(state.gmailLastError)} <button class="text-button" data-action="gmail-dismiss-error">Dismiss</button></div>`:''}
      ${!gmailOriginMatches()?`<div class="notice warning"><strong>Open the configured workspace address first.</strong> This browser's address does not match the Google callback origin. Copy the local workspace URL, open that exact address in Chrome, Edge or Safari, then sign in and connect there. <button class="text-button" data-action="gmail-copy-workspace">Copy local workspace URL</button></div>`:''}
      <section class="panel"><div class="panel-header"><div><h2>Gmail connection</h2><p class="panel-subtitle">Account access and automatic delivery are separate settings.</p></div><span class="badge ${connected?'good':'warn'}">${connected?'CONNECTED':'NOT CONNECTED'}</span></div><div class="panel-body"><dl class="details-grid">${detail('Google client',configured?'Configured':'Configuration required')}${detail('Connected account',esc(status.email || 'Not connected'))}${detail('Expected account',esc(status.expected_email || 'Choose when connecting'))}${detail('Automatic delivery',status.automatic_delivery==='simulated'?'Simulated — no real emails':status.automatic_delivery==='gmail'?'Gmail (after approval)':status.automatic_delivery==='smtp'?'SMTP server (after approval)':esc(status.automatic_delivery || 'Unavailable'))}${status.test_redirect?detail('Test redirect',status.test_redirect.active?'On — every real message goes only to your connected Gmail address. The original recipient is noted in the message.':'Off — the simulator keeps every message on this computer.',true):''}</dl>${status.test_redirect?`<div class="section-actions"><button data-action="gmail-test" ${isAdmin()?'':'disabled title="An administrator account is required."'}>Send test email to my connected address</button><span class="small muted">Queues one message through the normal outbox${status.test_redirect.active?' and the redirect':' (simulator: nothing leaves this computer)'}.${status.test_redirect.ready===false && status.test_redirect.reason?' '+esc(status.test_redirect.reason):''}</span></div>`:''}${status.reason?`<p class="small muted"><strong>Connection status:</strong> ${esc(human(status.reason))}</p>`:''}${connected?`<div class="section-actions"><button class="danger" data-action="gmail-disconnect">Disconnect locally</button></div><details><summary>Granted permissions</summary>${Array.isArray(status.scopes) && status.scopes.length?`<ul class="scope-list">${status.scopes.map(scope=>`<li class="mono">${esc(scope)}</li>`).join('')}</ul>`:'<p class="small muted">Permission details unavailable.</p>'}</details>`:''}</div></section>
      <section class="panel"><div class="panel-header"><div><h2>Set up in three steps</h2><p class="panel-subtitle">Use the same Google Cloud project for the API, consent screen and OAuth client.</p></div></div><div class="panel-body setup-steps">
        <section class="setup-step"><span class="setup-number" aria-hidden="true">1</span><div><h3>Create a Google Web application client</h3><p class="small muted">Enable the Gmail API, configure the OAuth consent screen, then create an OAuth client with application type <strong>Web application</strong>. If the app is External and in Testing, add the expected account as a test user. Add this exact authorised redirect URI to the client:</p><div class="copy-field"><input id="gmail-callback-url" class="mono" readonly aria-label="Exact authorised redirect URI" value="${esc(status.callback_url || '')}"><button data-action="gmail-copy-callback" ${status.callback_url?'':'disabled'}>Copy URI</button></div><p class="hint">The hostname, port and path must match exactly. Download the client's JSON file and upload it locally below. Do not paste credentials into chat.</p><div class="section-actions setup-links"><a href="https://console.cloud.google.com/apis/library/gmail.googleapis.com" target="_blank" rel="noopener noreferrer">Enable Gmail API ↗</a><a href="https://developers.google.com/workspace/guides/create-credentials#oauth-client-id" target="_blank" rel="noopener noreferrer">Google client setup guide ↗</a></div><button data-action="gmail-config">${configured?'Replace client JSON':'Upload client JSON'}</button></div></section>
        <section class="setup-step"><span class="setup-number" aria-hidden="true">2</span><div><h3>Connect with Google</h3><p class="small muted">Authorise Gmail send access and account identity only. Inbox-reading permission is not requested. No message is sent when you connect.</p><div class="notice warning small">Open this local workspace in <strong>Chrome, Edge or Safari</strong> before connecting. Google may reject embedded browsers. Start and finish consent in the same browser so the local session can validate the callback.</div><div class="section-actions"><button class="primary" data-action="gmail-connect" ${configured && gmailOriginMatches()?'':'disabled'}>${connected?'Reconnect with Google':'Connect with Google'}</button><button data-action="gmail-copy-workspace">Copy local workspace URL</button></div>${!configured?'<p class="hint">Upload a valid Web application client JSON first.</p>':''}</div></section>
        <section class="setup-step"><span class="setup-number" aria-hidden="true">3</span><div><h3>Verify the connected account</h3><p class="small muted">After Google consent, this page shows the connected email and granted permissions. Confirm they belong to the intended account. Invoice delivery continues through the simulator.</p><p class="small muted">An External OAuth app in Testing that requests Gmail access has refresh tokens that expire after 7 days, so reconnection can be required. <a href="https://developers.google.com/identity/protocols/oauth2#expiration" target="_blank" rel="noopener noreferrer">Google token expiry guidance ↗</a></p></div></section>
      </div></section>`;
  }
  /* One idempotent test message; the request ID is reused until the server answers. */
  async function sendGmailTest(){
    if(!isAdmin())throw new Error('An administrator account is required.');
    state.gmailTestRequest=state.gmailTestRequest || uuid();
    const result=await api('/api/mail/gmail/test',{request_id:state.gmailTestRequest});state.gmailTestRequest=null;
    state.gmailTestSent=true;
    toast(result.replayed?'This test message was already queued.':result.ready===false?`Test message queued, but delivery is not ready: ${result.reason || 'check the connection'}.`:result.delivery_provider==='simulated'?'Test message queued in the simulator. No real email leaves this computer.':'Test message queued. It is redirected to your connected address.');
  }
  /* Four-step progress strip: where the Gmail setup stands at a glance. */
  function gmailProgress(status){
    const control=state.summary?.automation_control || {},tested=state.gmailTestSent===true;
    const steps=[['Google client uploaded',status.client_configured===true],['Account connected',status.connected===true],['Test email sent to yourself',tested],['Month-end automation on',control.monthly_preparation_enabled===true && control.email_delivery_enabled!==false && control.paused!==true]];
    const current=steps.findIndex(x=>!x[1]);
    return `<ol class="next-steps gmail-progress" aria-label="Email setup progress">${steps.map(([label,done],i)=>`<li class="step ${done?'is-done':i===current?'is-current':'is-todo'}"><span class="step-static"${i===current?' aria-current="step"':''}><span class="step-index" aria-hidden="true">${done?'✓':i+1}</span><span class="step-text"><span class="step-label">${label}</span><span class="step-note">${done?'Done':i===current?'Next':'Waiting'}</span></span></span></li>`).join('')}</ol>`;
  }
  async function copyEmailValue(value,input){
    if(!value)throw new Error('This value is not available yet. Refresh the connection status.');
    try{if(!navigator.clipboard?.writeText)throw new Error('Clipboard unavailable');await navigator.clipboard.writeText(value);toast('Copied to clipboard.');}
    catch{if(input){input.focus();input.select();toast('Select and copy the highlighted value with your keyboard.');}else{
      const content=`<p class="small muted">Copy this address into Chrome, Edge or Safari, sign in there, and start Connect with Google again. Copying only a Google consent link into another browser cannot validate the original local session.</p><input id="gmail-workspace-url" class="mono" readonly aria-label="Local workspace URL" value="${esc(value)}">`;
      if(!$('#detail-dialog').open){openDialog('OPEN IN YOUR BROWSER','Local workspace URL');$('#detail-content').innerHTML=content;}
      else if(!$('#gmail-workspace-url'))$('#detail-content').insertAdjacentHTML('beforeend',`<section class="form-section">${content}</section>`);
      $('#gmail-workspace-url').focus();$('#gmail-workspace-url').select();
    }}
  }
  function gmailConfig(){
    openDialog('GOOGLE CLIENT CONFIGURATION','Upload Web application client JSON');
    $('#detail-content').innerHTML=`<p class="small muted">Choose the JSON downloaded for a Google OAuth <strong>Web application</strong> client. The server validates it. The browser uploads the file directly without reading or displaying its contents.</p>${state.gmail?.client_configured?'<div class="notice warning">This replaces the local client configuration. You may need to reconnect the account afterwards.</div>':''}<form id="gmail-config-form" class="stack"><label for="gmail-config-file">Google OAuth client JSON · maximum 32 KiB</label><input id="gmail-config-file" name="file" type="file" accept=".json,application/json" required><p class="hint">Use a Web application client, with the exact redirect URI shown on Email setup. This is not a service-account key.</p><button type="submit" class="primary">Save client configuration</button><p id="detail-error" class="error-text" role="alert" hidden></p></form>`;
  }
  function gmailConnect(){
    if(!state.gmail?.client_configured){showError(new Error('Upload a Google Web application client JSON first.'));return;}
    if(!gmailOriginMatches()){showError(new Error('Open the exact configured workspace URL before starting Google consent.'));return;}
    openDialog('GOOGLE ACCOUNT CONSENT','Connect the intended Gmail account');
    const expected=state.gmail?.expected_email || '';
    $('#detail-content').innerHTML=`<p class="small muted">Use Chrome, Edge or Safari for this flow, starting from the local workspace. Stay in the same browser through Google's return to the workspace.</p><div class="notice">Requested access: <strong>Gmail send</strong> and <strong>account identity</strong>. Inbox reading is not requested. Connecting sends no email and keeps dispatch simulated.</div><form id="gmail-connect-form" class="stack"><label for="gmail-expected-email">Expected Google account${expected?'':' (optional)'}</label><input id="gmail-expected-email" name="expected_email" type="email" autocomplete="email" value="${esc(expected)}" ${expected?'readonly':''}><p class="hint">${expected?'This account is fixed by the workspace configuration. The server rejects a different account.':'If supplied, the server checks that the authorised account matches this email.'}</p><button type="submit" class="primary">Prepare Google consent</button><button type="button" data-action="gmail-copy-workspace">Copy local workspace URL</button><p id="detail-error" class="error-text" role="alert" hidden></p></form>`;
  }
  function showGoogleConsent(result){
    let target;try{target=new URL(result.authorization_url);}catch{throw new Error('The server returned an invalid Google consent URL.');}
    if(target.protocol!=='https:' || target.hostname!=='accounts.google.com' || target.port || target.username || target.password)throw new Error('The consent URL was rejected: only HTTPS accounts.google.com is allowed.');
    $('#detail-title').textContent='Continue to Google consent';
    $('#detail-content').innerHTML=`<div class="notice">Continue in this same browser. The next page is Google's consent screen. No email will be sent.</div><p class="small muted">If this workspace is open inside an embedded browser, copy the <strong>local workspace URL</strong> below into Chrome, Edge or Safari, sign in, then start Connect with Google again there. Moving only the consent link to another browser breaks the local session check.</p><div class="section-actions"><a id="gmail-consent-link" class="button-link primary" href="${esc(target.href)}" referrerpolicy="no-referrer">Continue with Google ↗</a><button data-action="gmail-copy-workspace">Copy local workspace URL</button></div><p class="action-note">The connection is complete only after Google returns you here and the account status confirms it.</p><p id="detail-error" class="error-text" role="alert" hidden></p>`;
  }
  function gmailDisconnect(){
    openDialog('LOCAL ACCOUNT CONNECTION','Disconnect Gmail locally');
    $('#detail-content').innerHTML=`<p class="small muted">Remove the stored Gmail connection for <strong>${esc(state.gmail?.email || 'the connected account')}</strong> from this workspace. This local action does not revoke permissions in your Google account. The demo stays in simulation.</p><form id="gmail-disconnect-form" class="stack"><div class="checkbox-line"><input id="gmail-disconnect-confirm" type="checkbox" required><label for="gmail-disconnect-confirm">I want to remove this workspace's stored Gmail account connection.</label></div><button id="gmail-disconnect-submit" type="submit" class="danger" disabled>Disconnect locally</button><p id="detail-error" class="error-text" role="alert" hidden></p></form>`;
  }
  function uploadSource(){
    openDialog('SOURCE INTAKE','Upload source file');
    const period=[...(state.summary?.periods || [])].sort().at(-1) || '';
    $('#detail-content').innerHTML=`<p class="muted small">Upload CSV, Parquet or XLSX using the agreed synthetic A/B source contract. Files are parsed by the server; source rows are not loaded into browser memory.</p><form id="ingest-form" class="stack"><div class="form-grid"><div><label for="source-side">Source feed</label><select id="source-side" name="source" required><option value="A">Source A</option><option value="B">Source B</option></select></div><div><label for="source-period">Accounting period</label><input id="source-period" name="period" type="month" value="${esc(period)}" required></div><div class="full"><label for="source-file">Source file · maximum 250 MiB</label><input id="source-file" name="file" type="file" accept=".csv,.parquet,.xlsx" required></div><div><label for="source-rows">Expected data rows</label><input id="source-rows" name="expected_rows" type="number" min="0" step="1" required placeholder="Exclude header rows"></div><div><label for="source-sheet">Worksheet name (XLSX only)</label><input id="source-sheet" name="sheet" placeholder="Required for XLSX" disabled></div></div><div class="checkbox-line"><input id="source-complete" name="complete" type="checkbox"><label for="source-complete">This file is the complete source delivery for the selected period. Row counts and coverage will still be checked by the server.</label></div><div class="notice">The completeness declaration affects monthly-close eligibility. Leave it unchecked for a partial delivery. For larger files, use the streaming ingestion CLI.</div><button type="submit" class="primary">Upload & verify source</button><p id="detail-error" class="error-text" role="alert" hidden></p></form>`;
  }
  async function openBatch(id){
    const generation=openDialog('SOURCE VERIFICATION','Loading source batch…');
    try{
    const result=await api(`/api/batches/${encodeURIComponent(id)}`);if(!dialogCurrent(generation))return;const batch=result.batch || result;
    if(pages[location.hash.slice(1)])history.pushState({ioDialog:true},'',`#batch/${encodeURIComponent(id)}`);else history.replaceState(history.state,'',`#batch/${encodeURIComponent(id)}`);$('#detail-title').textContent=`Source ${batch.source} · ${batch.period}`;
    const candidates=(state.batchRows || []).filter(b=>b.id!==id && b.source===batch.source && b.period===batch.period && !b.superseded_by && b.status==='COMPLETE' && b.declared_complete && b.rejected_count===0 && b.expected_rows===b.row_count);
    $('#detail-content').innerHTML=`<div class="section-actions">${badge(batch.superseded_by?'SUPERSEDED':batch.status)}${badge(batch.declared_complete?'COMPLETE DELIVERY':'PARTIAL DELIVERY')}</div><dl class="details-grid">${detail('Batch ID',`<span class="mono">${esc(batch.id)}</span>`,true)}${detail('Input file',esc(batch.original_name))}${detail('Observed / expected rows',`${integer(batch.row_count)} / ${integer(batch.expected_rows)}`)}${detail('Accepted / duplicate rows',`${integer(batch.accepted_count)} / ${integer(batch.duplicate_count)}`)}${detail('Quarantined rows',integer(batch.rejected_count))}${detail('SHA-256',`<span class="mono">${esc(batch.sha256)}</span>`,true)}${batch.superseded_by?detail('Superseded by',`<span class="mono">${esc(batch.superseded_by)}</span>`,true):''}</dl><h3>Original source file</h3>${sourceFileList([{...batch,download_url:batch.download_url || batch.file_url}])}${batch.error?`<div class="notice error">${esc(batch.error)}</div>`:''}<details><summary>Full batch verification record</summary>${json(batch)}</details>${!batch.superseded_by && ['FAILED','COMPLETE'].includes(batch.status)?`<details><summary>Register a verified replacement delivery</summary><p class="small muted">Upload the corrected source first. The replacement must preserve record coverage, use the same source and period, contain no quarantine, and satisfy exact row counts. Original evidence is retained.</p><form id="supersede-form" data-batch-id="${esc(id)}" class="stack"><label for="replacement-batch">Replacement batch ID</label><input id="replacement-batch" name="replacement_batch_id" list="replacement-options" required autocomplete="off" placeholder="Full batch ID"><datalist id="replacement-options">${candidates.map(b=>`<option value="${esc(b.id)}">${esc(b.original_name)}</option>`).join('')}</datalist><label for="replacement-reason">Verification evidence and reason</label><textarea id="replacement-reason" name="reason" required minlength="5" placeholder="Explain which input was corrected and how replacement coverage was checked."></textarea><div class="checkbox-line"><input id="replacement-verified" type="checkbox" required><label for="replacement-verified">I verified that this is the intended replacement. The server must independently validate its source, period, coverage and revisions.</label></div><button type="submit" class="primary">Register verified replacement</button></form></details>`:''}<p id="detail-error" class="error-text" role="alert" hidden></p>`;
    }catch(error){if(dialogCurrent(generation))$('#detail-content').innerHTML=`<div class="notice error">${esc(errorText(error))}</div>`;}
  }
  function importWorklist(){
    openDialog('REVIEW WORKLIST','Import reviewed corrections');
    $('#detail-content').innerHTML=`<p class="muted small">Export the review worklist, edit the proposed correction columns in Excel, then save it as CSV. Import a small, reviewed worklist of at most 1,000 rows and 5 MiB.</p><div class="notice">Only non-empty proposed amounts, description or expected amount are applied. Keep <strong>case_id</strong> and <strong>expected_version</strong> unchanged and supply <strong>review_reason</strong> for every changed row. The <strong>current_*</strong> columns are evidence, not proposed changes. This import cannot approve or dispatch invoices.</div><form id="worklist-form" class="stack"><label for="worklist-file">Reviewed CSV file</label><input id="worklist-file" name="file" type="file" accept=".csv" required><p id="worklist-file-info" class="hint">Choose a file to preview its first 16 KiB without loading the full worklist into browser memory.</p><details id="worklist-preview-section" open hidden><summary>Bounded CSV preview</summary><pre id="worklist-preview"></pre></details><div class="checkbox-line"><input id="worklist-reviewed" type="checkbox" required><label for="worklist-reviewed">I reviewed the proposed changes and evidence reasons. Apply the supplied corrections only where the server confirms that the movement version is still current.</label></div><button id="worklist-apply" type="submit" class="primary" disabled>Apply reviewed corrections</button><p class="hint">Each changed row is checked independently. Stale or invalid rows fail without overwriting newer evidence. Review the applied/failed result after import.</p><p id="detail-error" class="error-text" role="alert" hidden></p></form>`;
  }
  async function previewWorklist(input){
    const file=input.files?.[0],info=$('#worklist-file-info');$('#worklist-reviewed').checked=false;$('#worklist-apply').disabled=true;
    input.setCustomValidity(file && file.size>5*1024*1024?'This worklist exceeds 5 MiB. Export a smaller review worklist.':file && !/\.csv$/i.test(file.name)?'Choose a CSV worklist.':'');
    $('#worklist-preview-section').hidden=true;if(!file)return;
    if(!input.checkValidity()){input.reportValidity();return;}
    const preview=await file.slice(0,16*1024).text();if(input.files?.[0]!==file)return;
    $('#worklist-preview').textContent=preview;$('#worklist-preview-section').hidden=false;
    info.textContent=`${file.name} · ${integer(file.size)} bytes. ${file.size>16*1024?'Preview truncated at 16 KiB. ':''}The server validates the complete file and the 1,000-row limit before applying corrections.`;
  }
  function showImportResults(result,offset=0){
    state.importResult=result;state.importOffset=offset;const results=Array.isArray(result.results)?result.results:[],visible=results.slice(offset,offset+50);
    $('#detail-title').textContent='Correction import results';
    $('#detail-content').innerHTML=`<div class="inline-stats"><div><span class="small-label">APPLIED</span><span class="verification-total">${integer(result.applied)}</span></div><div><span class="small-label">FAILED</span><span class="verification-total">${integer(result.failed)}</span></div></div><div class="notice">Applied corrections create new versions and trigger reassessment. Failed rows retain their original state. Re-export current evidence before correcting a stale row; do not blindly replay the old file.</div>${visible.length?`<div class="table-wrap"><table><thead><tr><th>Row</th><th>Case</th><th>Result</th><th>Detail</th></tr></thead><tbody>${visible.map(r=>`<tr><td>${integer(r.row)}</td><td>${r.case_id?`<button class="table-action" data-case="${esc(r.case_id)}">Open case →</button>`:'—'}</td><td>${badge(r.status)}</td><td class="wrap-cell">${esc(r.error || 'Correction recorded')}</td></tr>`).join('')}</tbody></table></div><div class="pagination"><span>${offset+1}–${offset+visible.length} of ${results.length} results</span><div><button data-import-page="previous" ${offset===0?'disabled':''}>← Previous</button><button data-import-page="next" ${offset+50>=results.length?'disabled':''}>Next →</button></div></div>`:empty('No changed rows','No row results were returned. Check that proposed fields were populated.')}<p id="detail-error" class="error-text" role="alert" hidden></p>`;
  }
  async function saveCaseCommand(form,data,generation){
    const id=form.dataset.caseId,entry=state.workbench.get(id);if(!entry)throw new Error('Reopen this case before saving a decision.');if(entry.pending)throw new Error('A command for this case is already running.');if(entry.versionStale)throw new Error('A newer movement version exists. Refresh this case explicitly before reviewing it.');
    let endpoint='resolve',payload={expected_version:Number(form.dataset.version),outcome:data.get('outcome'),reason:data.get('reason'),request_id:uuid()};
    if(form.id==='correct-form'){const patch={};for(const field of ['source_a_amount','source_b_amount','description','expected_amount'])if(data.get(field)?.trim())patch[field]=data.get(field).trim();if(!Object.keys(patch).length)throw new Error('Enter at least one corrected value.');endpoint='correct';payload={expected_version:Number(form.dataset.version),patch,reason:data.get('reason'),request_id:uuid()};}
    captureCaseDraft();entry.pending=true;renderWorkbench();
    try{await api(`/api/cases/${encodeURIComponent(id)}/${endpoint}`,payload);entry.draft={};entry.dirty=false;entry.whatifDraft=null;entry.whatifDirty=false;entry.data=null;entry.explanation=null;entry.preview=null;entry.pending=false;entry.stale=true;toast(endpoint==='resolve'?'Review decision recorded.':'Correction saved as a new version. Reassessment queued.');if(dialogCurrent(generation) && state.activeCaseId===id)await openCase(id,{reload:true});}
    finally{entry.pending=false;renderWorkbench();if(state.activeCaseId===id && canReview())document.querySelectorAll('#resolve-form button[type=submit],#correct-form button[type=submit]').forEach(b=>b.disabled=false);}
  }
  function explanationMarkup(explanation,heading='Recorded model calculation'){
    if(explanation?.status!=='AVAILABLE')return `<div class="notice warning"><strong>Explanation unavailable.</strong> ${esc(explanation?.reason || 'No reconstructable frozen model evidence was returned.')} The recorded score is not replaced with zero.</div>`;
    const features=Array.isArray(explanation.features)?explanation.features:[],maximum=Math.max(1e-12,...features.map(f=>Math.abs(Number(f.contribution)||0)));
    return `<section class="model-calculation"><h3>${esc(heading)}</h3><dl class="details-grid">${detail('Model output',score(explanation.score)+`<span class="secondary mono">p = ${esc(numeric(explanation.score))}</span>`)}${detail('Flag threshold',score(explanation.threshold)+`<span class="secondary mono">p = ${esc(numeric(explanation.threshold))}</span>`)}${detail('Baseline log-odds',esc(numeric(explanation.baseline_log_odds ?? explanation.intercept)))}${detail('Baseline model output',score(explanation.baseline_score)+`<span class="secondary mono">p = ${esc(numeric(explanation.baseline_score))}</span>`)}${detail('Frozen model',`<span class="mono">${esc(explanation.model_version)}</span>`,true)}</dl><p class="small muted">${esc(explanation.baseline_definition || 'The intercept is the model baseline; feature contributions add to its log-odds.')}</p><div class="notice">Drivers are additive <strong>log-odds contributions</strong>, not probability-percentage contributions and not causal explanations. The output describes synthetic high-risk scenarios; it is not a verified probability of fraud.</div><div class="feature-drivers">${[...features].sort((a,b)=>Math.abs(b.contribution)-Math.abs(a.contribution)).map(f=>{const n=Number(f.contribution),direction=n>0?'increase':n<0?'decrease':'neutral';return `<div class="feature-driver"><div class="driver-label"><strong>${esc(f.label || human(f.feature))}</strong><span>${esc(numeric(f.contribution))} log-odds · ${esc(human(f.effect || direction))}</span></div><div class="driver-bar-track" aria-hidden="true"><span class="driver-bar ${direction}" style="width:${Math.min(100,Math.abs(n || 0)/maximum*100)}%"></span></div><p class="hint">${f.raw_value_status==='NOT_RETAINED'?'Raw input absent from assessment; see source snapshot':`Raw input: ${esc(f.raw_value!==null && typeof f.raw_value==='object'?JSON.stringify(f.raw_value):(f.raw_value ?? 'Unavailable'))}`} · Transformed: ${esc(numeric(f.transformed_value))}</p><details><summary>Transformation and coefficient</summary><p class="small muted">${esc(f.explanation || '')}</p>${json({feature:f.feature,training_mean:f.training_mean,training_std:f.training_std,scaling_divisor:f.scaling_divisor,standardized_value:f.standardized_value,coefficient:f.coefficient,contribution:f.contribution})}</details></div>`;}).join('')}</div><details><summary>Exact model calculation and reconstruction evidence</summary>${json(explanation)}</details></section>`;
  }
  function previewForm(entry,result){
    const inputs=result.editable_inputs;if(result.preview_available!==true || !inputs || !Object.keys(inputs).length || result.explanation?.status!=='AVAILABLE')return `<p class="small muted">Hypothetical preview unavailable: ${esc(result.preview_unavailable_reason || 'No previewable current-version model evidence.')}</p>`;
    const labels={amount:'Amount (GBP)',expected_amount:'Expected amount (GBP)',customer_age_days:'Customer age (days)',supplier_age_days:'Supplier age (days)',cross_border:'Cross-border indicator',event_at:'Event time (ISO with timezone)'};
    return `<section class="form-section"><h3>What-if sandbox</h3><div class="notice"><strong>Hypothetical preview only.</strong> This calculation never changes original inputs, business versions, case decisions or invoices. It cannot clear a rule failure. Changes are not applied automatically.</div><form id="model-preview-form" data-case-id="${esc(entry.id)}" data-version="${esc(result.version_no)}"><div class="form-grid">${Object.entries(labels).filter(([key])=>Object.prototype.hasOwnProperty.call(inputs,key)).map(([key,label])=>`<div><label for="whatif-${key}">${label}</label>${key==='cross_border'?`<select id="whatif-${key}" name="${key}"><option value="">Keep original (${esc(inputs[key] ?? 'unavailable')})</option><option value="0">0 · Domestic</option><option value="1">1 · Cross-border</option></select>`:`<input id="whatif-${key}" name="${key}" ${key.endsWith('_days')?'type="number" min="0" max="365250" step="1"':key==='event_at'?'type="text" maxlength="64"':'inputmode="decimal" maxlength="40" pattern="[0-9]+(\\.[0-9]{1,2})?"'} placeholder="${esc(inputs[key] ?? 'Unavailable')}" autocomplete="off">`}<p class="hint">Original: ${esc(inputs[key] ?? 'Unavailable')}</p></div>`).join('')}</div><div class="section-actions form-section"><button type="submit" class="primary">Calculate hypothetical score</button><button type="button" data-action="reset-preview">Reset hypothetical inputs</button></div><p class="hint">Leave a field empty to keep its original value. Read-only sessions can calculate this preview; no change is saved.</p><p id="preview-error" class="error-text" role="alert" hidden></p></form><div id="model-preview-result"></div></section>`;
  }
  function caseReasonMarkup(result){
    const c=result.case || {},payload=result.version?.payload || {},sides=payload.source_values || {};
    const explanations={
      MODEL_RISK:'The recorded model output crossed its review threshold. A human must check the source evidence before releasing this movement.',
      MODEL_UNAVAILABLE:'A usable model score was not recorded. Missing or unsupported inputs cannot be treated as a low-risk result.',
      AMOUNT_MISMATCH:'The source A and source B amounts do not match in this business snapshot.',
      MISSING_SOURCE_A:'The matching source A record is missing from this business snapshot.',
      MISSING_SOURCE_B:'The matching source B record is missing from this business snapshot.',
      AMBIGUOUS_SOURCE_KEYS:'The source keys are ambiguous, so a unique matching record cannot be established.',
      CURRENCY_MISMATCH:'The source records name different currencies.',
      SUPPLIER_ID_MISMATCH:'The source records name different suppliers.',
      PERIOD_MISMATCH:'The source records belong to different accounting periods.',
      EVENT_AT_MISMATCH:'The source records report different event times.',
      SOURCE_CHANGE_AFTER_ISSUE:'Source evidence changed after issuance. The issued snapshot is frozen; compare the latest observations before any authorised adjustment.'
    };
    const quarantine=!c.movement_id,ruleFailures=rows(result.assessments).filter(a=>a.kind==='RULE' && a.status==='FAIL').flatMap(a=>a.reasons || []),otherRules=[...new Set(ruleFailures)].filter(reason=>reason!==c.reason);
    const primary=quarantine?'This source row was quarantined because it could not be accepted as a valid movement. Corrected source evidence is required; a review decision cannot make this input billable.':explanations[c.reason] || `The recorded control raised: ${human(c.reason)}. Inspect the retained evidence before deciding.`;
    return `<p class="case-reason-summary">${esc(primary)}</p>${c.reason==='AMOUNT_MISMATCH'?`<div class="case-reason-facts"><span>Source A: <strong>${amount(sides.A?.amount,sides.A?.currency || payload.currency || 'GBP')}</strong></span><span>Source B: <strong>${amount(sides.B?.amount,sides.B?.currency || payload.currency || 'GBP')}</strong></span></div>`:''}${otherRules.length?`<div class="notice warning"><strong>Other source controls also failed.</strong><ul>${otherRules.map(reason=>`<li>${esc(explanations[reason] || human(reason))}</li>`).join('')}</ul>Model output does not clear these failures.</div>`:''}${!quarantine && c.reason!=='MODEL_RISK'?'<p class="hint">This case reason is separate from the model explanation below. A model score alone cannot clear a source-control failure.</p>':''}`;
  }
  function narrativeMarkup(narrative,{hypothetical=false}={}){
    if(!narrative || typeof narrative!=='object')return '<p class="small muted">A readable model summary is unavailable. The recorded controls and exact model evidence remain available below.</p>';
    const drivers=rows(narrative.drivers),mitigating=rows(narrative.mitigating_drivers);
    const items=values=>`<ul class="narrative-drivers">${values.map(driver=>`<li><strong>${esc(driver.label || human(driver.feature))}</strong><p>${esc(driver.text || 'Inspect the exact recorded contribution below.')}</p></li>`).join('')}</ul>`;
    return `<div class="model-narrative ${hypothetical?'hypothetical':''}"><p class="small-label">${hypothetical?'HYPOTHETICAL MODEL EXPLANATION':'RECORDED MODEL EXPLANATION'}</p>${narrative.headline?`<h4>${esc(narrative.headline)}</h4>`:''}${narrative.summary?`<p>${esc(narrative.summary)}</p>`:''}${drivers.length?`<p class="narrative-subheading">What increased this model output</p>${items(drivers.slice(0,3))}${drivers.length>3?`<details><summary>Show ${drivers.length-3} more increasing drivers</summary>${items(drivers.slice(3))}</details>`:''}`:''}${mitigating.length?`<details class="mitigating-drivers"><summary>What reduced this model output · ${mitigating.length} ${mitigating.length===1?'factor':'factors'}</summary>${items(mitigating)}</details>`:''}${narrative.caveat?`<p class="narrative-caveat">${esc(narrative.caveat)}</p>`:'<p class="narrative-caveat">These are synthetic model patterns, not proof of fraud or an invalid transaction. Contributions are model associations, not causal explanations.</p>'}${narrative.provenance?.raw_binding?`<p class="hint">Input provenance: ${esc(narrative.provenance.raw_binding==='VERIFIED'?(hypothetical?'hypothetical inputs match the explained features':'retained source inputs match the recorded features'):narrative.status==='AVAILABLE'?'recorded transformed features only':'unavailable')}${narrative.provenance.raw_binding_reason?' · '+esc(narrative.provenance.raw_binding_reason):''}</p>`:''}${hypothetical?'<p class="narrative-caveat"><strong>Only hypothetical inputs changed. Original evidence, decisions and invoices are unchanged.</strong></p>':''}</div>`;
  }
  async function caseExplanation(entry){
    const snapshot=entry.data;if(entry.explanation && entry.explanationSnapshot===snapshot)return entry.explanation;
    if(entry.explanationRequest?.snapshot===snapshot)return entry.explanationRequest.promise;
    const request={snapshot,promise:null};request.promise=api(`/api/cases/${encodeURIComponent(entry.id)}/model/explanation`).then(result=>{
      const expected=snapshot.version?.version_no;
      if(result.case_reason!==undefined && result.case_reason!==snapshot.case?.reason)throw new Error('The returned explanation has a different case reason. Refresh this case before continuing.');
      if(result.case_id!==undefined && result.case_id!==entry.id)throw new Error('The returned explanation belongs to another case. Refresh this case before continuing.');
      if(expected!==undefined && result.version_no!==expected)throw new Error('The case version changed. Refresh this case explicitly before inspecting or previewing its model.');
      if(state.workbench.get(entry.id)===entry && entry.data===snapshot){entry.explanation=result;entry.explanationSnapshot=snapshot;}return result;
    }).finally(()=>{if(entry.explanationRequest===request)entry.explanationRequest=null;});entry.explanationRequest=request;return request.promise;
  }
  async function loadCaseExplanation(){
    const entry=state.workbench.get(state.activeCaseId),output=$('#case-model-content'),narrativeOutput=$('#case-narrative-content');if(!entry?.data || !output || !narrativeOutput)return;
    const snapshot=entry.data,generation=state.dialogSequence,sequence=(entry.explanationSequence || 0)+1;entry.explanationSequence=sequence;
    const current=()=>dialogCurrent(generation) && state.activeCaseId===entry.id && state.workbench.get(entry.id)===entry && entry.data===snapshot && output.isConnected && narrativeOutput.isConnected && entry.explanationSequence===sequence;
    output.innerHTML='<p class="small muted">Reconstructing the recorded Logistic Regression calculation…</p>';
    try{
      const result=await caseExplanation(entry);if(!current())return;
      narrativeOutput.innerHTML=narrativeMarkup(result.narrative)+`<p class="hint">Snapshot version ${esc(result.version_no ?? 'unavailable')} · Recorded model <span class="mono">${esc(result.model_version || result.explanation?.model_version || 'Unavailable')}</span>. Exact calculations are expandable below.</p>`;
      output.innerHTML=`<p class="small muted">Assessment <span class="mono">${esc(result.assessment_id || 'Unavailable')}</span> · reviewed version ${esc(result.version_no ?? 'Unavailable')} · recorded score ${score(result.recorded_score)}</p>${explanationMarkup(result.explanation)}${previewForm(entry,result)}`;
      if($('#model-preview-form'))for(const [name,value] of Object.entries(entry.whatifDraft || {})){const input=$(`[name="${name}"]`,$('#model-preview-form'));if(input)input.value=value;}
      if(entry.preview && $('#model-preview-result'))$('#model-preview-result').innerHTML=previewResultMarkup(entry.preview);
    }catch(error){if(current()){const warning=`<div class="notice warning">${esc(errorText(error))}</div><button data-action="reload-explanation">Retry explanation</button>`;output.innerHTML=warning;narrativeOutput.innerHTML=warning;}}
  }
  function previewResultMarkup(result){return `<div class="notice warning"><strong>Hypothetical result — original data unchanged.</strong> Before: ${score(result.before?.score)} → after: ${score(result.after?.score)}. This output cannot be used as an automatic approval.</div>${narrativeMarkup(result.after_narrative,{hypothetical:true})}${explanationMarkup(result.after,'Hypothetical model calculation')}`;}
  async function calculatePreview(form){
    const entry=state.workbench.get(form.dataset.caseId);if(!entry || entry.id!==state.activeCaseId)return;
    const generation=state.dialogSequence,sequence=++entry.previewSequence,data=new FormData(form),patch={};for(const [key,value] of data){if(!String(value).trim())continue;patch[key]=key.endsWith('_days') || key==='cross_border'?Number(value):String(value).trim();}
    if(!Object.keys(patch).length)throw new Error('Change at least one hypothetical input before calculating.');
    if(patch.event_at && (!/(?:Z|[+-]\d{2}:\d{2})$/.test(patch.event_at) || !Number.isFinite(Date.parse(patch.event_at))))throw new Error('Use an ISO event time including Z or a timezone offset.');
    const result=await api(`/api/cases/${encodeURIComponent(entry.id)}/model/preview`,{expected_version:Number(form.dataset.version),patch});
    if(!dialogCurrent(generation) || state.activeCaseId!==entry.id || sequence!==entry.previewSequence || !form.isConnected)return;
    if(result.hypothetical!==true || result.persisted!==false)throw new Error('The server did not identify this response as hypothetical. It cannot be displayed as a valid preview.');
    if(result.version_no!==undefined && result.version_no!==Number(form.dataset.version))throw new Error('The case version changed during preview. Refresh the case to inspect current evidence.');
    entry.preview=result;$('#model-preview-result').innerHTML=previewResultMarkup(result);
  }
  function modelReadinessMarkup(model){
    const worker=model.worker || {};
    return `<dl class="details-grid">${detail('Artifact integrity',model.artifact_integrity_verified===true?'Verified by server':'Not verified')}${detail('Inference readiness',esc(human(model.status || 'UNAVAILABLE')))}${detail('Active model for future assessments',`<span class="mono">${esc(model.model_version || 'Unavailable')}</span>`,true)}${detail('Worker heartbeat',badge(worker.status || 'UNAVAILABLE'))}${detail('Heartbeat time (UTC)',utc(worker.at))}${detail('Heartbeat age',typeof worker.age_seconds==='number'?`${esc(worker.age_seconds)} seconds`:'Unavailable')}${detail('Risk threshold',score(model.threshold)+`<span class="secondary mono">p = ${esc(numeric(model.threshold))}</span>`)}</dl>${model.reason?`<div class="notice warning">${esc(model.reason)}</div>`:''}${worker.reason?`<div class="notice warning">${esc(worker.reason)}</div>`:''}<p class="hint">The heartbeat is server-reported freshness evidence, not proof that every job will succeed. Missing, stale and stopped are separate states. Status refreshes every 5 seconds while this view is visible.</p>`;
  }
  function stopModelMonitor(){
    clearTimeout(state.modelMonitorTimer);clearTimeout(state.modelMonitorTimeout);state.modelMonitorTimer=null;state.modelMonitorController?.abort();state.modelMonitorController=null;state.modelMonitorGeneration=(state.modelMonitorGeneration || 0)+1;
  }
  function startModelMonitor(){
    stopModelMonitor();if(document.hidden || !state.csrf || state.tab!=='data' || !$('#model-readiness'))return;
    const generation=state.modelMonitorGeneration,session=state.session;
    const current=()=>generation===state.modelMonitorGeneration && state.session===session && Boolean(state.csrf) && state.tab==='data' && !document.hidden;
    const schedule=()=>{if(current())state.modelMonitorTimer=setTimeout(poll,5000);};
    async function poll(){
      if(!current())return;const target=$('#model-readiness');if(!target)return;
      const controller=new AbortController();state.modelMonitorController=controller;const timeout=setTimeout(()=>controller.abort(),8000);state.modelMonitorTimeout=timeout;
      try{
        const response=await fetch('/api/model',{credentials:'same-origin',headers:{Accept:'application/json'},signal:controller.signal});
        if(!current() || $('#model-readiness')!==target)return;
        if(response.status===401){showLogin();return;}if(!response.ok)throw new Error(`Status request failed (${response.status})`);
        const model=await response.json();if(!current() || $('#model-readiness')!==target)return;
        target.innerHTML=modelReadinessMarkup(model);$('#model-readiness-badge').innerHTML=badge(model.status || 'UNAVAILABLE');
      }catch(error){
        if(current() && $('#model-readiness')===target){target.innerHTML='<div class="notice warning"><strong>Current model and worker status unavailable.</strong> The last status cannot be treated as current. Automatic status refresh will retry in 5 seconds.</div>';$('#model-readiness-badge').innerHTML=badge('UNAVAILABLE');}
      }finally{
        clearTimeout(timeout);if(state.modelMonitorTimeout===timeout)state.modelMonitorTimeout=null;if(state.modelMonitorController===controller)state.modelMonitorController=null;schedule();
      }
    }
    schedule();
  }
  function modelOperationsMarkup(model,runs){
    state.modelRuns=runs;
    const metrics=(label,values)=>`<tr><td>${label}</td><td>${integer(values?.rows)}</td><td>${integer(values?.positive_rows)}</td><td>${typeof values?.average_precision==='number'?esc(values.average_precision.toFixed(4)):'Unavailable'}</td><td>${typeof values?.roc_auc==='number'?esc(values.roc_auc.toFixed(4)):'Unavailable'}</td><td>${typeof values?.brier_score==='number'?esc(values.brier_score!==0 && Math.abs(values.brier_score)<1e-4?values.brier_score.toExponential(3):values.brier_score.toFixed(4)):'Unavailable'}</td></tr>`;
    const active=runs.some(r=>['QUEUED','RUNNING','ACTIVATING','ACTIVATION_UNCERTAIN'].includes(r.status));
    return `<section class="panel" id="model-operations"><div class="panel-header"><div><h2>Logistic Regression operations</h2><p class="panel-subtitle">Actual artifact readiness, held-out synthetic metrics and asynchronous training.</p></div><span id="model-readiness-badge">${badge(model.status || 'UNAVAILABLE')}</span></div><div class="panel-body"><div id="model-readiness">${modelReadinessMarkup(model)}</div><h3 class="history-heading">Chronological evaluation · synthetic data</h3><p class="small muted" id="evaluation-model">Evaluation for model <span class="mono">${esc(model.model_version || 'Unavailable')}</span>. Metrics describe this captured model; active readiness above may update independently.</p><div class="table-wrap"><table><thead><tr><th>Partition</th><th>Rows</th><th>Positive rows</th><th>Average precision</th><th>ROC AUC</th><th>Brier score</th></tr></thead><tbody>${metrics('Validation · threshold selection',model.validation_metrics)}${metrics('Held-out test',model.test_metrics)}</tbody></table></div><div class="notice warning">${esc(model.evaluation_scope || 'Synthetic fixtures demonstrate the pipeline. They do not establish real-world accuracy, calibration, fraud detection or compliance.')}</div><details><summary>Training recipe, split boundaries, costs and full metrics</summary>${json(model)}</details><div class="section-actions form-section"><button data-action="train-model" class="primary" ${isAdmin() && !active?'':'disabled'}>Train a synthetic model</button><span class="hint">${active?'A training run is queued, running or awaiting activation recovery.':isAdmin()?'Queued background work · 1,000–50,000 rows':'Personal administrator access required'}</span></div><h3>Recent training runs</h3>${runs.length?`<div class="training-runs">${runs.map(run=>`<details class="training-run"><summary><span>${badge(run.status)}</span><span class="mono">${esc(run.id)}</span><span>${utc(run.created_at)}</span></summary><dl class="details-grid">${detail('Requested by',reviewerMarkup(run.requester || {actor:run.actor}),true)}${detail('Synthetic rows',integer(run.parameters?.rows))}${detail('Seed',integer(run.parameters?.seed))}${detail('Serving period',esc(run.parameters?.period))}${detail('Job',`<span class="mono">${esc(run.job_id)}</span>`)}${detail('Resulting model',`<span class="mono">${esc(run.model_version || 'Not yet recorded')}</span>`,true)}</dl>${run.error?`<div class="notice error">${esc(run.error)}</div>`:''}<div class="section-actions">${safeApiUrl(run.training_download_url)?`<a href="${esc(safeApiUrl(run.training_download_url))}" download>Exact training Parquet ↗</a>`:''}${safeApiUrl(run.manifest_download_url)?`<a href="${esc(safeApiUrl(run.manifest_download_url))}" download>Training manifest ↗</a>`:''}${isAdmin() && ['ACTIVATING','ACTIVATION_UNCERTAIN'].includes(run.status)?`<button data-retry-training="${esc(run.id)}">Resume recorded activation</button>`:''}</div>${json(run)}</details>`).join('')}</div>`:'<p class="small muted">No UI training runs recorded. A pre-existing model can still be ready above.</p>'}<p class="hint">Newest 20 runs only. Updates arrive through the workspace event stream when this view is active.</p></div></section>`;
  }
  function trainingRecoveryForm(id){
    if(!isAdmin())return;const run=state.modelRuns?.find(r=>r.id===id);if(!run || !['ACTIVATING','ACTIVATION_UNCERTAIN'].includes(run.status))return;
    openDialog('RECORDED ACTIVATION','Resume model activation');$('#detail-content').innerHTML=`<div class="notice warning">Resume the stored activation intent for this exact training run. The worker reuses its recorded model target; this does not train a replacement model.</div><dl class="details-grid">${detail('Run',`<span class="mono">${esc(id)}</span>`,true)}${detail('Recorded model',`<span class="mono">${esc(run.model_version || 'Inspect retained run evidence')}</span>`,true)}${detail('Current run state',badge(run.status))}</dl><form id="model-recovery-form" data-run-id="${esc(id)}" class="stack"><div class="checkbox-line"><input id="recover-model-confirm" type="checkbox" required><label for="recover-model-confirm">I have checked this run and want to resume its recorded activation.</label></div><button type="submit" class="primary">Resume recorded activation</button><p id="detail-error" class="error-text" hidden></p></form>`;
  }
  function trainingForm(){
    if(!isAdmin())return;openDialog('ASYNCHRONOUS MODEL TRAINING','Train on synthetic data');const period=(state.summary?.periods || []).at(-1) || new Date().toISOString().slice(0,7);
    $('#detail-content').innerHTML=`<div class="notice warning">This creates synthetic training data and queues a Logistic Regression training job. Successful validation activates the model for <strong>future assessments only</strong>. Frozen historical scores, case reviews and invoices remain unchanged. Synthetic performance does not establish suitability for real customers.</div><form id="model-train-form" class="stack"><label for="train-rows">Synthetic rows · 1,000–50,000</label><input id="train-rows" name="rows" type="number" min="1000" max="50000" step="1" value="5000" required><label for="train-seed">Reproducible data seed · 0–2,147,483,647</label><input id="train-seed" name="seed" type="number" min="0" max="2147483647" step="1" value="42" required><label for="train-period">Serving period</label><input id="train-period" name="period" type="month" min="2000-01" max="2100-12" value="${esc(period)}" required><div class="checkbox-line"><input id="train-confirm" type="checkbox" required><label for="train-confirm">I understand this is synthetic training and a successful run changes the model for future assessments only.</label></div><button type="submit" class="primary">Queue training job</button><p class="hint">Training runs outside this request. Follow its status in Data & model.</p><p id="detail-error" class="error-text" hidden></p></form>`;
  }

  const caseDraftFields=['review-outcome','review-reason','correction-a','correction-b','correction-description','correction-expected','correction-reason'];
  function captureCaseDraft(){
    const entry=state.workbench.get(state.activeCaseId);if(!entry || !$('#detail-dialog').open)return;
    if($('#resolve-form')?.dataset.caseId!==entry.id && $('#correct-form')?.dataset.caseId!==entry.id)return;
    const draft={};for(const id of caseDraftFields){const input=$('#'+id);if(input)draft[id]=input.value;}
    entry.draft=draft;entry.dirty=Object.entries(draft).some(([id,value])=>id==='review-outcome'?value!=='UNRESOLVED':Boolean(value.trim()));
    entry.correctionOpen=$('#correct-form')?.closest('details')?.open || false;entry.scrollTop=$('#detail-dialog').scrollTop;
    const whatif=$('#model-preview-form');if(whatif?.dataset.caseId===entry.id){entry.whatifDraft=Object.fromEntries(new FormData(whatif));entry.whatifDirty=Object.values(entry.whatifDraft).some(v=>String(v).trim());}
    renderWorkbench();
  }
  function restoreCaseDraft(entry){
    for(const [id,value] of Object.entries(entry.draft || {})){const input=$('#'+id);if(input)input.value=value;}
    if(entry.correctionOpen && $('#correct-form'))$('#correct-form').closest('details').open=true;
    if(entry.pending)document.querySelectorAll('#resolve-form button,#correct-form button').forEach(b=>b.disabled=true);
  }
  function ensureWorkbenchEntry(id){
    let entry=state.workbench.get(id);if(entry)return entry;
    if(state.workbench.size>=8){toast('The workbench holds 8 cases. Close a case tab before opening another.');return null;}
    entry={id,data:null,draft:{},dirty:false,whatifDirty:false,loading:false,loadSequence:0,previewSequence:0,stale:false,pending:false};state.workbench.set(id,entry);renderWorkbench();return entry;
  }
  async function fetchWorkbenchCase(entry,reload=false){
    if(entry.data && !reload)return entry.data;if(entry.loading)return entry.promise;
    const sequence=++entry.loadSequence;entry.loading=true;entry.error=null;renderWorkbench();
    entry.promise=api(`/api/cases/${encodeURIComponent(entry.id)}`).then(result=>{if(state.workbench.get(entry.id)===entry && entry.loadSequence===sequence){entry.data=result;entry.stale=false;}return result;}).catch(error=>{if(state.workbench.get(entry.id)===entry)entry.error=errorText(error);throw error;}).finally(()=>{if(state.workbench.get(entry.id)===entry && entry.loadSequence===sequence){entry.loading=false;entry.promise=null;renderWorkbench();}});return entry.promise;
  }
  function caseTabLabel(entry){const c=entry.data?.case || {};return entry.data?.movement?.external_id || c.external_movement_id || (state.caseRows || []).find(r=>r.id===entry.id)?.external_movement_id || `${customerName(c.customer_id)} · ${String(entry.id).slice(0,8)}`;}
  function renderWorkbench(){
    const target=$('#case-workbench');if(!target)return;const entries=[...state.workbench.values()];target.hidden=!entries.length;
    const chip=e=>`<div class="case-chip ${state.activeCaseId===e.id?'active':''}"><input type="checkbox" data-compare-case="${esc(e.id)}" aria-label="Compare ${esc(caseTabLabel(e))}" ${state.compareCases.has(e.id)?'checked':''} ${!e.data?'disabled':''}><button data-workbench-case="${esc(e.id)}" title="${esc(e.id)}">${esc(caseTabLabel(e))}${e.dirty || e.whatifDirty?' · Unsaved':''}${e.loading?' · Loading':e.stale?' · Update available':''}${e.pending?' · Saving':''}</button><button data-close-case="${esc(e.id)}" aria-label="Close ${esc(caseTabLabel(e))}" ${e.pending?'disabled':''}>×</button></div>`;
    target.innerHTML=`<div class="workbench-heading"><div><strong>Case workbench · ${entries.length}/8</strong><span>Open cases and drafts stay in this browser tab's memory. This is separate from release selection.</span></div><button data-action="compare-cases" ${state.compareCases.size>=2 && state.compareCases.size<=3?'':'disabled'}>Compare ${state.compareCases.size || ''} cases</button></div><div class="case-chips">${entries.map(chip).join('')}</div>`;
    const tabs=$('#dialog-case-tabs');tabs.hidden=!state.activeCaseId;tabs.innerHTML=state.activeCaseId?`<div class="case-chips">${entries.map(e=>`<button class="case-switch ${e.id===state.activeCaseId?'active':''}" data-workbench-case="${esc(e.id)}" title="${esc(e.id)}">${esc(caseTabLabel(e))}${e.dirty || e.whatifDirty?' •':''}</button>`).join('')}</div><p class="hint">Switch cases without losing notes. Close this dialog to keep cases pinned.</p>`:'';
  }
  async function closeWorkbenchCase(id){
    captureCaseDraft();const entry=state.workbench.get(id);if(!entry)return;if(entry.pending){toast('Wait for the recorded decision before closing this case.');return;}
    if((entry.dirty || entry.whatifDirty) && (!await askConfirm({title:'Discard unsaved work?',message:'Discard the unsaved notes and hypothetical inputs for this case and close its tab?',confirmLabel:'Discard and close',danger:true}) || state.workbench.get(id)!==entry || entry.pending))return;
    entry.loadSequence++;entry.previewSequence++;entry.versionsController?.abort();entry.versionsSequence=(entry.versionsSequence || 0)+1;state.workbench.delete(id);state.compareCases.delete(id);
    if(state.activeCaseId===id){state.activeCaseId=null;const next=state.workbench.keys().next().value;if(next)openCase(next);else $('#detail-dialog').close();}renderWorkbench();
  }
  async function refreshWorkbenchCase(id){
    captureCaseDraft();const entry=state.workbench.get(id);if(!entry || entry.pending)return;
    if((entry.dirty || entry.whatifDirty) && (!await askConfirm({title:'Discard unsaved work?',message:'Discard this case’s unsaved notes and hypothetical inputs, then load the latest business version?',confirmLabel:'Discard and reload',danger:true}) || state.workbench.get(id)!==entry || entry.pending))return;
    entry.draft={};entry.dirty=false;entry.whatifDraft=null;entry.whatifDirty=false;entry.explanation=null;entry.preview=null;entry.versionsController?.abort();entry.versions=null;entry.versionsError=null;entry.versionStale=false;entry.versionsSequence=(entry.versionsSequence || 0)+1;entry.versionsLoading=false;entry.data=null;entry.previewSequence++;await openCase(id,{reload:true});
  }
  function updateInspectSelection(){
    const button=$('#open-inspected-cases');if(button){button.disabled=!state.inspectionSelection.size;button.textContent=`Open selected (${state.inspectionSelection.size}/8)`;}
    document.querySelectorAll('[data-inspect-case]').forEach(input=>input.checked=state.inspectionSelection.has(input.dataset.inspectCase));
    const all=$('#inspect-page-cases');if(all)all.checked=state.inspectionSelection.size>0 && (state.caseRows || []).slice(0,8).every(c=>state.inspectionSelection.has(c.id));
  }
  async function openInspectedCases(){
    if(state.loading || state.renderedReviewSequence!==state.requestSequence)return;
    const ids=[...state.inspectionSelection],fresh=ids.filter(id=>!state.workbench.has(id));if(state.workbench.size+fresh.length>8){toast(`Only ${8-state.workbench.size} workbench slots remain. Reduce the inspect selection or close a pinned case.`);return;}
    const entries=ids.map(ensureWorkbenchEntry).filter(Boolean);state.inspectionSelection.clear();updateInspectSelection();if(!entries.length)return;
    // Bounded at eight case requests, never source movement tables.
    const first=openCase(entries[0].id);await Promise.allSettled(entries.slice(1).map(entry=>fetchWorkbenchCase(entry)));await first;
  }
  function compareWorkbenchCases(){
    captureCaseDraft();const entries=[...state.compareCases].map(id=>state.workbench.get(id)).filter(e=>e?.data);if(entries.length<2 || entries.length>3)return;
    openDialog('READ-ONLY COMPARISON',`Compare ${entries.length} open cases`);
    $('#detail-content').innerHTML=`<div class="notice">Compare the pinned evidence snapshots. New source data may be available; each case keeps its own reviewed version. Comparison does not release, correct or approve anything.</div><div class="case-comparison">${entries.map(entry=>{const r=entry.data,c=r.case || {},v=r.version || {},payload=v.payload || {},model=rows(r.assessments).filter(a=>a.kind==='MODEL').at(-1);return `<article class="comparison-card"><h3>${esc(customerName(c.customer_id))}</h3><p class="mono">${esc(caseTabLabel(entry))}</p><div class="section-actions">${badge(c.status)}${entry.stale?badge('UPDATE AVAILABLE'):''}</div><dl class="details-grid">${detail('Version',esc(v.version_no ?? 'Unavailable'))}${detail('Amount',amount(payload.amount,payload.currency))}${detail('Risk score',score(c.score ?? model?.score)+`<span class="secondary mono">p = ${esc(numeric(c.score ?? model?.score))}</span><span class="secondary">Recorded model</span><span class="secondary mono">${esc(model?.model_version || 'Unavailable')}</span>`)}${detail('Period',esc(c.period))}${detail('Case reason',esc(human(c.reason)),true)}</dl><h3>Recorded checks</h3><ul class="comparison-reasons">${rows(r.assessments).map(a=>`<li>${esc(human(a.kind))}: ${esc(human(a.status))}${a.reasons?.length?' · '+a.reasons.map(human).map(esc).join(', '):''}</li>`).join('')}</ul><h3>Original source files</h3>${sourceReferences(r,true)}<button data-workbench-case="${esc(entry.id)}">Open this case</button></article>`;}).join('')}</div>`;
  }
  function caseQuery(includePaging=true){
    const query=new URLSearchParams();if(includePaging){query.set('limit',String(state.limit));query.set('offset',String(state.offset));}query.set('status',state.caseStatus);if(state.customerFilters.review)query.set('customer_id',state.customerFilters.review);
    for(const [key,value] of Object.entries(state.caseFilters)){if(value!=='')query.set(key,String(value));}return query;
  }
  function caseFilterControls(){return `<div class="filters case-advanced-filters"><div class="search-field"><label for="case-search">Movement ID starts with</label><input id="case-search" data-case-filter="q" value="${esc(state.caseFilters.q)}" minlength="3" maxlength="100" placeholder="At least 3 starting characters" autocomplete="off"></div><div><label for="case-reason">Exact reason</label><input id="case-reason" data-case-filter="reason" value="${esc(state.caseFilters.reason)}" maxlength="100" list="case-reasons" placeholder="All reasons"><datalist id="case-reasons">${['MODEL_RISK','MODEL_UNAVAILABLE','AMOUNT_MISMATCH','SOURCE_MISSING','SOURCE_CHANGE_AFTER_ISSUE','INVALID_INPUT'].map(v=>`<option value="${v}">`).join('')}</datalist></div><div><label for="case-severity">Severity</label><select id="case-severity" data-case-filter="severity">${[['','All severities'],['LOW','Low'],['MEDIUM','Medium'],['HIGH','High'],['CRITICAL','Critical']].map(([v,label])=>`<option value="${v}" ${state.caseFilters.severity===v?'selected':''}>${label}</option>`).join('')}</select></div><div class="risk-filter"><label for="case-min-score">Min risk %</label><input id="case-min-score" type="number" min="0" max="100" step="0.1" data-case-filter="min_score" value="${state.caseFilters.min_score===''?'':esc(Number(state.caseFilters.min_score)*100)}" placeholder="Any"></div><div class="risk-filter"><label for="case-max-score">Max risk %</label><input id="case-max-score" type="number" min="0" max="100" step="0.1" data-case-filter="max_score" value="${state.caseFilters.max_score===''?'':esc(Number(state.caseFilters.max_score)*100)}" placeholder="Any"></div><div><label for="case-sort">Sort</label><select id="case-sort" data-case-filter="sort">${[['newest','Newest first'],['oldest','Oldest first'],['score_desc','Highest risk first'],['score_asc','Lowest risk first'],['customer','Customer']].map(([v,label])=>`<option value="${v}" ${state.caseFilters.sort===v?'selected':''}>${label}</option>`).join('')}</select></div><button data-action="reset-case-filters">Reset advanced filters</button><span class="hint">Risk filters exclude unavailable scores. Prefix search needs 3 characters and is applied after a short pause.</span></div>`;}
  function scheduleCaseFilter(input,immediate=false){
    if(!input.checkValidity()){input.reportValidity();return;}const key=input.dataset.caseFilter;
    state.caseFilters[key]=['min_score','max_score'].includes(key) && input.value!==''?String(Number(input.value)/100):input.value;
    if(state.caseFilters.min_score!=='' && state.caseFilters.max_score!=='' && Number(state.caseFilters.min_score)>Number(state.caseFilters.max_score)){showError(new Error('Minimum risk must not exceed maximum risk.'));return;}
    clearTimeout(state.filterTimer);clearCaseSelection();state.inspectionSelection.clear();updateInspectSelection();state.offset=0;state.requestSequence++;state.renderedReviewSequence=null;
    document.querySelectorAll('#tab-content [data-inspect-case],#inspect-page-cases,#open-inspected-cases,#tab-content [data-select-case],#select-page-cases,#tab-content [data-case]').forEach(b=>b.disabled=true);document.querySelectorAll('#tab-content a').forEach(a=>a.setAttribute('aria-disabled','true'));
    state.filterTimer=setTimeout(()=>{state.filterTimer=null;if(state.tab==='review')refreshCurrent(false);},immediate?0:350);
  }

  function dialogCurrent(generation){return $('#detail-dialog').open && state.dialogSequence===generation;}
  function openDialog(eyebrow,title){cancelSwipeConfirmation();captureCaseDraft();unloadInvoicePdf();state.dialogAbort?.abort();state.dialogAbort=new AbortController();state.activeCaseId=null;renderWorkbench();const generation=++state.dialogSequence;$('#detail-eyebrow').textContent=eyebrow;$('#detail-title').textContent=title;$('#detail-content').innerHTML='<p class="muted">Loading…</p>';if(!$('#detail-dialog').open)$('#detail-dialog').showModal();return generation;}
  async function runAction(button,action){
    const generation=$('#detail-dialog').open?state.dialogSequence:null;button.disabled=true;const original=button.textContent;button.textContent='Working…';const target=$('#preview-error') || $('#detail-error');if(target)target.hidden=true;
    try{await action(generation);}
    catch(error){const detailError=$('#preview-error') || $('#detail-error');if(generation!==null && dialogCurrent(generation) && detailError){detailError.textContent=errorText(error);detailError.hidden=false;}else showError(error);}
    finally{if(button.isConnected){button.disabled=false;button.textContent=original;}}
  }
  document.addEventListener('click',event=>{
    const link=event.target.closest('#tab-content a');if(link && (state.loading || link.getAttribute('aria-disabled')==='true')){event.preventDefault();toast('Wait for the current view to load before opening its export or source file.');return;}
    /* Whole-row click opens the record (watchlist pattern); controls inside the row keep their own behaviour. */
    const rowHit=!event.target.closest('a,button,input,label,select,textarea,summary,details,[data-chart]') && event.target.closest('#tab-content tbody tr');if(rowHit && !String(window.getSelection?.() || '')){const opener=rowHit.querySelector('button[data-case]:not(:disabled),button[data-invoice]:not(:disabled),button[data-outbox]:not(:disabled)');if(opener){opener.click();return;}}
    const button=event.target.closest('button');if(!button || button.disabled)return;
    if(button.dataset.loginMode){setLoginMode(button.dataset.loginMode);return;}
    if(button.id==='operator-profile'){openProfile();return;}
    if(button.dataset.action==='profile-logout' || button.dataset.action==='switch-personal'){runAction(button,()=>signOut(button.dataset.action==='switch-personal'));return;}
    if(button.matches(mutationSelector) && !canReview()){toast('Sign in with a personal account to make changes.');return;}
    if(button.matches('[data-insights-retry]')){refreshCurrent(false);return;}
    if(button.dataset.insightMovement){const customer=button.dataset.insightCustomer;state.caseFilters={...state.caseFilters,q:button.dataset.insightMovement};state.caseStatus='';state.customerFilters.review=state.customers.some(c=>String(c.id)===customer)?customer:'';changeTab('review').catch(showError);return;}
    if(button.dataset.retryTraining){trainingRecoveryForm(button.dataset.retryTraining);return;}
    if(button.dataset.action==='train-model'){trainingForm();return;}
    if(button.dataset.action==='reload-explanation'){loadCaseExplanation();return;}
    if(button.dataset.action==='reset-preview'){const entry=state.workbench.get(state.activeCaseId);if(entry){entry.whatifDraft={};entry.whatifDirty=false;entry.preview=null;entry.previewSequence++;$('#model-preview-form')?.reset();if($('#model-preview-result'))$('#model-preview-result').innerHTML='';renderWorkbench();}return;}
    if(button.dataset.workbenchCase){openCase(button.dataset.workbenchCase);return;}
    if(button.dataset.closeCase){closeWorkbenchCase(button.dataset.closeCase);return;}
    if(button.dataset.refreshCase){refreshWorkbenchCase(button.dataset.refreshCase);return;}
    if(button.dataset.action==='open-inspected-cases'){openInspectedCases();return;}
    if(button.dataset.action==='compare-cases'){compareWorkbenchCases();return;}
    if(button.dataset.action==='reset-case-filters'){state.caseFilters={q:'',reason:'',severity:'',min_score:'',max_score:'',sort:'score_desc'};state.inspectionSelection.clear();clearCaseSelection();state.offset=0;refreshCurrent(false);return;}
    if(button.dataset.historyPage){historyPage(button);return;}
    if(button.dataset.action==='create-operator'){if(isAdmin())createOperatorForm();return;}
    if(button.dataset.operatorId){if(isAdmin())operatorStatusForm(button);return;}
    if(button.dataset.tab){changeTab(button.dataset.tab);return;}
    if(button.dataset.loadVersions){loadCaseVersions(button.dataset.loadVersions);return;}
    if(button.dataset.versionStep){stepCaseVersion(Number(button.dataset.versionStep));return;}
    if(button.dataset.case){openCase(button.dataset.case);return;}
    if(button.dataset.reviewLayout){setReviewLayout(button.dataset.reviewLayout);return;}
    if(button.dataset.invoiceStatus!==undefined && button.closest('#tab-content')){if(state.loading)return;state.invoiceStatus=button.dataset.invoiceStatus;state.offset=0;refreshCurrent(false);return;}
    if(button.dataset.invoiceLayout){setInvoiceLayout(button.dataset.invoiceLayout);return;}
    if(button.dataset.previewInvoice){openInvoice(button.dataset.previewInvoice,{previewPdf:true});return;}
    if(button.dataset.loadPdf){loadInvoicePdf(button.dataset.loadPdf);return;}
    if(button.dataset.pdfInvoice){loadInvoicePdf(button.dataset.pdfInvoice,Number(button.dataset.pdfPage));return;}
    if(button.dataset.action==='unload-pdf'){unloadInvoicePdf();return;}
    if(button.dataset.action==='automation-settings'){automationSettings();return;}
    if(button.dataset.action==='reload-automation'){if(!$('#automation-reason')?.value)automationSettings();else askConfirm({title:'Discard automation edits?',message:'Discard unsaved automation edits and load the current configuration?',confirmLabel:'Discard and reload',danger:true}).then(ok=>{if(ok && $('#detail-dialog').open)automationSettings();});return;}
    if(button.dataset.invoice){openInvoice(button.dataset.invoice);return;}
    if(button.dataset.invoiceFormat){invoiceFormatForm(button);return;}
    if(button.dataset.invoiceMovementPage){loadInvoiceMovements(button.dataset.invoiceMovementPage,Number(button.dataset.movementOffset));return;}
    if(button.dataset.outbox){openOutbox(button.dataset.outbox);return;}
    if(button.dataset.batch){openBatch(button.dataset.batch);return;}
    if(button.dataset.sourceUrl){openSources(button.dataset.sourceUrl,0,$('#detail-dialog').open?button.closest('.source-references'):null);return;}
    if(button.dataset.sourcePage){openSources(button.dataset.sourcePageUrl,Number(button.dataset.sourceOffset),button.closest('.source-references'));return;}
    if(button.dataset.page){state.inspectionSelection.clear();clearCaseSelection();state.offset=Math.max(0,state.offset+(button.dataset.page==='next'?state.limit:-state.limit));refreshCurrent(false);return;}
    if(button.dataset.action==='bulk-release'){openBulkRelease();return;}
    if(button.dataset.action==='bulk-results'){if(state.bulkResult){openDialog('HUMAN REVIEW','Bulk release results');showBulkResults(state.bulkResult);}return;}
    if(button.dataset.action==='clear-case-selection'){clearCaseSelection();if(state.pendingRefresh)refreshCurrent();return;}
    if(button.dataset.action==='prepare'){prepareInvoice();return;}
    if(button.dataset.action==='upload-source'){uploadSource();return;}
    if(button.dataset.action==='import-worklist'){importWorklist();return;}
    if(button.dataset.action==='gmail-config'){gmailConfig();return;}
    if(button.dataset.action==='gmail-connect'){gmailConnect();return;}
    if(button.dataset.action==='gmail-test'){runAction(button,sendGmailTest);return;}
    if(button.dataset.action==='gmail-disconnect'){gmailDisconnect();return;}
    if(button.dataset.action==='gmail-dismiss-error'){state.gmailLastError=null;refreshCurrent(false);return;}
    if(button.dataset.action==='gmail-copy-callback'){runAction(button,()=>copyEmailValue(state.gmail?.callback_url,$('#gmail-callback-url')));return;}
    if(button.dataset.action==='gmail-copy-workspace'){runAction(button,()=>copyEmailValue(workspaceEmailUrl()));return;}
    if(button.dataset.importPage){showImportResults(state.importResult,Math.max(0,state.importOffset+(button.dataset.importPage==='next'?50:-50)));return;}
    if(button.dataset.action==='set-provider')runAction(button,async()=>{await api('/api/simulation/provider',{outcome:$('#provider-outcome').value});toast('Simulation provider outcome updated.');});
    if(button.dataset.action==='worker-tick')runAction(button,async()=>{const result=await api('/api/worker/tick',{});toast(`Worker tick complete: ${JSON.stringify(result)}`);await refreshCurrent();});
    if(button.dataset.dispatch)runAction(button,async(generation)=>{await api(`/api/invoices/${encodeURIComponent(button.dataset.dispatch)}/dispatch`,{request_id:uuid()});toast('Approved invoice queued for simulated dispatch.');if(dialogCurrent(generation))$('#detail-dialog').close();await refreshCurrent();});
    if(button.dataset.reopen){runAction(button,async()=>{const item=(state.invoiceRows || []).find(row=>row.id===button.dataset.reopen) || await api(`/api/invoices/${encodeURIComponent(button.dataset.reopen)}/package`);const saved=await confirmSwipeAction({kind:'INVOICE',item,action:'reopen'});if(saved.accepted)await refreshCurrent();});return;}
    if(button.dataset.revoke){askConfirm({title:'Revoke approval',message:'The package returns to draft and needs a fresh approval before dispatch. The reason is recorded with your identity.',confirmLabel:'Revoke approval',danger:true,reason:'Reason for revoking this approval:'}).then(reason=>{if(typeof reason==='string' && reason.trim() && button.isConnected && !button.disabled)runAction(button,async(generation)=>{await api(`/api/invoices/${encodeURIComponent(button.dataset.revoke)}/revoke`,{reason:reason.trim(),request_id:uuid()});toast('Approval revoked.');if(dialogCurrent(generation))$('#detail-dialog').close();await refreshCurrent();});});}
    if(button.dataset.simEvent)runAction(button,async(generation)=>{await api('/api/simulation/events',{attempt_id:button.dataset.attempt,event:button.dataset.simEvent});toast('Simulated provider event recorded.');if(dialogCurrent(generation))$('#detail-dialog').close();await refreshCurrent();});
    if(button.dataset.reconcileAttempt)runAction(button,async(generation)=>{const result=await api(`/api/attempts/${encodeURIComponent(button.dataset.reconcileAttempt)}/reconcile`,undefined,'POST');toast(`Existing submission reconciled: ${human(result.status)}. No message was resent.`);if(dialogCurrent(generation))$('#detail-dialog').close();await refreshCurrent();});
  });
  let versionGesture=null;
  document.addEventListener('pointerdown',event=>{
    const surface=event.target.closest('.version-swipe-area');if(!surface || event.target.closest('a,button,input,textarea,select,summary,details'))return;
    versionGesture={id:event.pointerId,x:event.clientX,y:event.clientY,caseId:state.activeCaseId,generation:state.dialogSequence};
  });
  document.addEventListener('pointercancel',()=>{versionGesture=null;});
  document.addEventListener('pointerup',event=>{
    const gesture=versionGesture;versionGesture=null;if(!gesture || gesture.id!==event.pointerId || gesture.caseId!==state.activeCaseId || gesture.generation!==state.dialogSequence)return;
    const dx=event.clientX-gesture.x,dy=event.clientY-gesture.y;if(Math.abs(dx)>65 && Math.abs(dx)>Math.abs(dy)*1.5)stepCaseVersion(dx<0?1:-1);
  });
  document.addEventListener('keydown',event=>{if(event.target.matches('.version-swipe-area') && ['ArrowLeft','ArrowRight'].includes(event.key)){event.preventDefault();stepCaseVersion(event.key==='ArrowRight'?1:-1);}});
  document.addEventListener('input',event=>{if(event.target.dataset.caseFilter && event.target.tagName!=='SELECT')scheduleCaseFilter(event.target);if(event.target.closest('#model-preview-form')){const entry=state.workbench.get(state.activeCaseId);if(entry){entry.previewSequence++;entry.preview=null;if($('#model-preview-result'))$('#model-preview-result').innerHTML='<p class="hint">Inputs changed. Calculate again to view a current hypothetical result.</p>';}}if(caseDraftFields.includes(event.target.id) || event.target.closest('#model-preview-form'))captureCaseDraft();});
  document.addEventListener('change',event=>{
    if(event.target.dataset.caseFilter){if(event.target.tagName==='SELECT')scheduleCaseFilter(event.target,true);return;}
    if(event.target.dataset.inspectCase){if(state.loading || state.renderedReviewSequence!==state.requestSequence){event.target.checked=false;return;}const id=event.target.dataset.inspectCase;if(event.target.checked && state.inspectionSelection.size>=8){event.target.checked=false;toast('Select at most 8 cases to inspect.');return;}event.target.checked?state.inspectionSelection.add(id):state.inspectionSelection.delete(id);updateInspectSelection();return;}
    if(event.target.id==='inspect-page-cases'){if(state.loading || state.renderedReviewSequence!==state.requestSequence)return;state.inspectionSelection.clear();if(event.target.checked)(state.caseRows || []).slice(0,8).forEach(c=>state.inspectionSelection.add(c.id));updateInspectSelection();return;}
    if(event.target.dataset.compareCase){if(event.target.checked && state.compareCases.size>=3){event.target.checked=false;toast('Choose 2 or 3 pinned cases for comparison.');return;}event.target.checked?state.compareCases.add(event.target.dataset.compareCase):state.compareCases.delete(event.target.dataset.compareCase);renderWorkbench();return;}
    if(event.target.dataset.customerFilter){state.inspectionSelection.clear();clearCaseSelection();state.customerFilters[event.target.dataset.customerFilter]=event.target.value;state.offset=0;refreshCurrent(false);}
    if(event.target.matches('[data-insights-period]')){state.insightPeriod=event.target.value;refreshCurrent(false);return;}
    if(event.target.id==='case-status'){state.inspectionSelection.clear();clearCaseSelection();state.caseStatus=event.target.value;state.offset=0;refreshCurrent(false);}
    if(event.target.dataset.selectCase){if(state.loading || state.renderedReviewSequence!==state.requestSequence){event.target.checked=false;return;}const row=(state.caseRows || []).find(c=>c.id===event.target.dataset.selectCase);if(row && releaseEligible(row)){event.target.checked?state.selectedCases.set(row.id,{...row}):state.selectedCases.delete(row.id);updateCaseSelection();}}
    if(event.target.id==='select-page-cases'){if(state.loading || state.renderedReviewSequence!==state.requestSequence){event.target.checked=false;return;}if(event.target.checked)(state.caseRows || []).filter(releaseEligible).forEach(row=>state.selectedCases.set(row.id,{...row}));else state.selectedCases.clear();updateCaseSelection();}
    if(event.target.id==='bulk-release-confirm')$('#bulk-release-submit').disabled=!event.target.checked;
    if(event.target.id==='invoice-format-confirm')$('#invoice-format-submit').disabled=!event.target.checked;
    if(event.target.id==='approve-checked')$('#approve-button').disabled=!event.target.checked;
    if(event.target.id==='gmail-disconnect-confirm')$('#gmail-disconnect-submit').disabled=!event.target.checked;
    if(event.target.id==='gmail-config-file'){
      const file=event.target.files?.[0];event.target.setCustomValidity(file && file.size>32*1024?'Google client JSON must be at most 32 KiB.':file && !/\.json$/i.test(file.name)?'Choose the downloaded Google client JSON file.':'');
      if(file && !event.target.checkValidity())event.target.reportValidity();
    }
    if(event.target.id==='worklist-file')previewWorklist(event.target).catch(error=>{$('#detail-error').textContent=errorText(error);$('#detail-error').hidden=false;});
    if(event.target.id==='worklist-reviewed')$('#worklist-apply').disabled=!event.target.checked || !$('#worklist-file').files?.[0] || !$('#worklist-file').checkValidity();
    if(event.target.id==='source-file'){
      const file=event.target.files?.[0],xlsx=file?.name.toLowerCase().endsWith('.xlsx');
      $('#source-sheet').disabled=!xlsx;$('#source-sheet').required=Boolean(xlsx);if(!xlsx)$('#source-sheet').value='';
      event.target.setCustomValidity(file && file.size>250*1024*1024?'File exceeds the 250 MiB browser upload limit. Use the streaming ingestion CLI.':'');
      if(file && file.size>250*1024*1024)event.target.reportValidity();
    }
  });
  document.addEventListener('submit',event=>{
    const form=event.target;if(!['automation-form','resolve-form','correct-form','approve-form','prepare-form','invoice-format-form','ingest-form','supersede-form','worklist-form','gmail-config-form','gmail-connect-form','gmail-disconnect-form','bulk-release-form','operator-create-form','operator-status-form','model-preview-form','model-train-form','model-recovery-form'].includes(form.id))return;event.preventDefault();const button=$('button[type="submit"]',form),data=new FormData(form),generation=state.dialogSequence;
    if(form.id==='automation-form' && state.automationPending)return;
    if(form.id==='invoice-format-form' && state.invoiceFormatPending?.has(form.dataset.invoiceId))return;
    runAction(button,async()=>{
      if(!canReview() && form.id!=='model-preview-form')throw new Error('Shared access is read-only. Sign in with your personal account.');
      if(form.id==='model-preview-form'){await calculatePreview(form);return;}
      if(form.id==='model-recovery-form'){
        if(!isAdmin() || !$('#recover-model-confirm').checked)throw new Error('Confirm resuming this recorded model activation.');
        const id=form.dataset.runId;if(!state.recoveryRequests)state.recoveryRequests=new Map();if(!state.recoveryRequests.has(id))state.recoveryRequests.set(id,uuid());
        await api(`/api/model/runs/${encodeURIComponent(id)}/retry`,{request_id:state.recoveryRequests.get(id)});state.recoveryRequests.delete(id);toast('Recorded activation queued for recovery. No new training was requested.');if(dialogCurrent(generation)){$('#detail-dialog').close();await changeTab('data');}return;
      }
      if(form.id==='model-train-form'){
        if(!isAdmin() || !$('#train-confirm').checked)throw new Error('Administrator access and explicit training confirmation are required.');
        const payload={rows:Number(data.get('rows')),seed:Number(data.get('seed')),period:data.get('period')};const signature=JSON.stringify(payload);if(state.trainingRequest?.signature!==signature)state.trainingRequest={signature,id:uuid()};
        const result=await api('/api/model/train',{...payload,request_id:state.trainingRequest.id});state.trainingRequest=null;toast(`Training run ${result.id || ''} queued. Follow its status in Data & model.`);if(dialogCurrent(generation)){$('#detail-dialog').close();await changeTab('data');}return;
      }
      if(form.id==='operator-create-form'){
        if(!isAdmin())throw new Error('Administrator access is required.');
        await api('/api/operators',{username:String(data.get('username') || '').trim(),display_name:String(data.get('display_name') || '').trim(),password:data.get('password'),role:data.get('role')});
        if(dialogCurrent(generation))$('#detail-dialog').close();toast('Personal account created. Share credentials through your approved secure channel.');
      }
      if(form.id==='operator-status-form'){
        if(!isAdmin() || !$('#operator-status-confirm').checked)throw new Error('Confirm the account status change.');
        await api('/api/operators/'+encodeURIComponent(form.dataset.operatorId),{active:form.dataset.active==='true'},'PATCH');
        if(dialogCurrent(generation))$('#detail-dialog').close();toast('Account status updated.');
      }
      if(form.id==='bulk-release-form'){
        if(!$('#bulk-release-confirm').checked)throw new Error('Confirm that every selected case and its source evidence were reviewed.');
        const selected=state.bulkSnapshot || [],reason=String(data.get('reason') || '').trim();if(!selected.length || selected.length>state.limit)throw new Error('Select between 1 and 50 cases from the current page.');
        if(reason.length<5)throw new Error('Give an evidence reason of at least 5 characters.');
        const payload={items:selected.map(c=>({case_id:c.id,expected_version:c.current_version})),reason};
        const signature=JSON.stringify(payload);if(state.bulkRequest?.signature!==signature)state.bulkRequest={signature,id:uuid()};
        if(state.bulkPending)throw new Error('A bulk release request is already running.');state.bulkPending=true;
        try{
          const result=await api('/api/cases/bulk-release',{...payload,request_id:state.bulkRequest.id});
          for(const item of selected)if(state.selectedCases.get(item.id)?.current_version===item.current_version)state.selectedCases.delete(item.id);
          updateCaseSelection();state.bulkSnapshot=null;state.bulkRequest=null;state.bulkResult=result;
          if($('#bulk-results-open'))$('#bulk-results-open').hidden=false;
          if(dialogCurrent(generation))showBulkResults(result);
          toast(`${integer(result.counts?.released)} movements released; ${integer(result.counts?.failed)} cases failed. See Last release results.`);
        }finally{state.bulkPending=false;}
      }
      if(form.id==='resolve-form' || form.id==='correct-form')await saveCaseCommand(form,data,generation);
      if(form.id==='approve-form'){
        if(!$('#approve-checked').checked)throw new Error('Check the approval confirmation after inspecting the package.');
        await api(`/api/invoices/${encodeURIComponent(form.dataset.invoiceId)}/approve`,{fingerprint:form.dataset.fingerprint,request_id:uuid()});toast('Exact invoice package approved.');if(dialogCurrent(generation))await openInvoice(form.dataset.invoiceId);
      }
      if(form.id==='automation-form')await saveAutomation(form,generation);
      if(form.id==='invoice-format-form')await saveInvoiceFormat(form,generation);
      if(form.id==='prepare-form'){
        const result=await api('/api/close',{customer_id:data.get('customer_id'),period:data.get('period'),line_format:data.get('line_format') || 'ITEMIZED',request_id:uuid()});toast('Monthly close checks passed. Invoice package prepared.');if(dialogCurrent(generation)){if(result.id || result.invoice_id)await openInvoice(result.id || result.invoice_id);else $('#detail-dialog').close();}
      }
      if(form.id==='ingest-form'){
        const file=data.get('file');if(!file || !file.size)throw new Error('Choose a non-empty source file.');
        if(file.size>250*1024*1024)throw new Error('File exceeds 250 MiB. Use the streaming ingestion CLI.');
        if(!/\.(csv|parquet|xlsx)$/i.test(file.name))throw new Error('Choose a CSV, Parquet or XLSX source file.');
        data.set('complete',$('#source-complete').checked?'true':'false');
        if(!String(data.get('sheet') || '').trim())data.delete('sheet');
        await api('/api/ingest',data);toast('Source intake finished. Inspect the batch counts and quarantine findings.');if(dialogCurrent(generation))$('#detail-dialog').close();
      }
      if(form.id==='supersede-form'){
        const replacement=String(data.get('replacement_batch_id') || '').trim();if(replacement===form.dataset.batchId)throw new Error('Select a different replacement batch.');
        await api(`/api/batches/${encodeURIComponent(form.dataset.batchId)}/supersede`,{replacement_batch_id:replacement,reason:data.get('reason'),request_id:uuid()});toast('Replacement verified and recorded. Original evidence retained.');if(dialogCurrent(generation))$('#detail-dialog').close();
      }
      if(form.id==='worklist-form'){
        const file=data.get('file');if(!file?.size || !/\.csv$/i.test(file.name))throw new Error('Choose a non-empty CSV worklist.');
        if(file.size>5*1024*1024)throw new Error('Worklists are capped at 5 MiB and 1,000 rows. Export a smaller worklist.');
        if(!$('#worklist-reviewed').checked)throw new Error('Review the proposed values and reasons, then confirm the checkbox.');
        const result=await api('/api/review/import',data);if(dialogCurrent(generation))showImportResults(result);toast(`Correction import: ${integer(result.applied)} applied, ${integer(result.failed)} failed.`);
      }
      if(form.id==='gmail-config-form'){
        const file=data.get('file');if(!file?.size || !/\.json$/i.test(file.name))throw new Error('Choose a non-empty Google client JSON file.');
        if(file.size>32*1024)throw new Error('Google client JSON must be at most 32 KiB.');
        await api('/api/mail/gmail/config',data);toast('Client configuration saved. Connect the intended Google account next.');if(dialogCurrent(generation))$('#detail-dialog').close();
      }
      if(form.id==='gmail-connect-form'){
        const expected=String(data.get('expected_email') || '').trim();
        const result=await api('/api/mail/gmail/connect',expected?{expected_email:expected}:{});if(dialogCurrent(generation))showGoogleConsent(result);
      }
      if(form.id==='gmail-disconnect-form'){
        if(!$('#gmail-disconnect-confirm').checked)throw new Error('Confirm that you want to remove the local connection.');
        await api('/api/mail/gmail/disconnect',{});toast('Gmail disconnected locally. Delivery remains simulated.');if(dialogCurrent(generation))$('#detail-dialog').close();
      }
      await refreshCurrent();
    });
  });
  for(const id of ['personal-login-form','login-form','bootstrap-form'])$('#'+id).addEventListener('submit',async event=>{
    event.preventDefault();const form=event.target,button=$('button[type="submit"]',form);button.disabled=true;$('#login-error').hidden=true;
    try{const data=new FormData(form);if(id==='bootstrap-form' && data.get('password')!==$('#setup-password-confirm').value)throw new Error('The personal passwords do not match.');
      const endpoint=id==='bootstrap-form'?'/api/operators/bootstrap':'/api/login',payload=Object.fromEntries(data.entries());
      const session=await api(endpoint,payload);if(id==='bootstrap-form')state.setupRequired=false;await enterWorkspace(session);
    }catch(error){$('#login-error').textContent=errorText(error);$('#login-error').hidden=false;}finally{button.disabled=false;}
  });
  $('#logout').addEventListener('click',()=>runAction($('#logout'),()=>signOut()));
  new MutationObserver(applyIdentityPermissions).observe($('#detail-content'),{childList:true,subtree:true});
  new MutationObserver(applyIdentityPermissions).observe($('#tab-content'),{childList:true,subtree:true});
  function loadLazyDetails(details){if(details.id==='case-model-explanation' && !details.dataset.loaded){details.dataset.loaded='true';loadCaseExplanation();}if(details.id==='case-history' && !details.dataset.loaded){details.dataset.loaded='true';state.historyPages={caseId:details.dataset.caseId,modern:[],legacy:[],attempts:[]};loadCaseHistory();}}
  document.addEventListener('toggle',event=>{if(event.target.open)loadLazyDetails(event.target);},true);
  /* Start the lazy request on the summary activation itself (click covers Enter/Space):
     the toggle event is queued behind the next frame, which is costly under glass blur. */
  document.addEventListener('click',event=>{const summary=event.target.closest?.('#case-model-explanation > summary, #case-history > summary');if(summary && !summary.parentElement.open && !event.defaultPrevented)loadLazyDetails(summary.parentElement);});
  $('#refresh').addEventListener('click',()=>refreshCurrent());$('#global-error').addEventListener('click',event=>{if(event.target.closest('[data-dismiss-error]'))$('#global-error').hidden=true;});$('#toast-stack')?.addEventListener('click',event=>{const dismiss=event.target.closest('.toast-dismiss');if(dismiss)dismissToast(dismiss.closest('.toast'));});$('#toast-stack')?.addEventListener('pointerenter',()=>{if(!$('#toast').hidden && $('#toast').dataset.kind!=='error' && Date.now()-state.toastShown<9000)clearTimeout(state.toastTimer);});$('#toast-stack')?.addEventListener('pointerleave',()=>{if(!$('#toast').hidden && $('#toast').dataset.kind!=='error'){clearTimeout(state.toastTimer);state.toastTimer=setTimeout(()=>$('#toast').hidden=true,3000);}});$('#load-update').addEventListener('click',()=>{clearCaseSelection();refreshCurrent();});
  $('#close-detail').addEventListener('click',()=>{captureCaseDraft();unloadInvoicePdf();$('#detail-dialog').close();});
  $('#detail-dialog').addEventListener('cancel',()=>{captureCaseDraft();unloadInvoicePdf();});
  $('#detail-dialog').addEventListener('close',()=>{if($('#detail-dialog').open)return;cancelSwipeConfirmation();unloadInvoicePdf();state.dialogAbort?.abort();state.activeCaseId=null;renderWorkbench();state.dialogSequence++;state.detailId=null;state.importResult=null;if(/^#(case|batch)\//.test(location.hash) && history.state?.ioDialog)history.back();else history.replaceState(null,'',`#${state.tab}`);if(state.pendingRefresh && !state.loading && !swipeViewActive())refreshCurrent();});
  window.addEventListener('hashchange',()=>{if(state.csrf && location.hash!==`#${state.tab}`)openHashRoute().catch(showError);});window.addEventListener('popstate',()=>{if($('#detail-dialog').open && !/^#(case|batch)\//.test(location.hash)){captureCaseDraft();unloadInvoicePdf();$('#detail-dialog').close();}});
  window.addEventListener('resize',revealActiveNavigation);
  document.addEventListener('visibilitychange',()=>{if(document.hidden){state.swipe?.deactivate();cancelSwipeConfirmation();clearTimeout(state.viewRefreshTimer);state.pendingViewRefresh=false;state.viewAbort?.abort();state.requestSequence++;state.loading=false;unloadInvoicePdf();disconnect();stopModelMonitor();connection('Paused · tab hidden');}else if(state.csrf){connect();if(!$('#detail-dialog').open)refreshCurrent();else startModelMonitor();}});
  window.addEventListener('beforeunload',event=>{captureCaseDraft();disconnect();stopModelMonitor();if([...state.workbench.values()].some(e=>e.dirty || e.whatifDirty)){event.preventDefault();event.returnValue='';}});
  (async()=>{try{const status=await api('/api/auth/status');state.setupRequired=status.setup_required===true;setLoginMode(state.setupRequired?'setup':'personal');await enterWorkspace(await api('/api/session'));}catch(error){showLogin();if(!errorText(error).includes('expired')){$('#login-error').textContent=errorText(error);$('#login-error').hidden=false;}}})();
})();
