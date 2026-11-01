/* Page-scoped orchestration. Financial confirmation remains owned by the host. */
(() => {
  'use strict';
  const KINDS = new Set(['CASE', 'INVOICE']);
  const MAX_ITEMS = 100;
  function create(options) {
    if (typeof options?.api !== 'function' || typeof options?.confirmAction !== 'function') throw new TypeError('SwipeWorkspace requires api and confirmAction.');
    const states = new Map();
    let visual = null, visualSequence = 0;
    const epoch = () => options.getEpoch?.() ?? 0;
    const personal = () => options.canReview?.() === true;
    const notify = text => { if (typeof options.onMessage === 'function') { try { options.onMessage(text); } catch { /* Notifications do not determine a recorded result. */ } } };
    const requestId = () => {
      if (typeof crypto.randomUUID === 'function') return crypto.randomUUID();
      const b = crypto.getRandomValues(new Uint8Array(16)); b[6] = (b[6] & 15) | 64; b[8] = (b[8] & 63) | 128;
      const hex = Array.from(b, n => n.toString(16).padStart(2, '0')).join('');
      return `${hex.slice(0,8)}-${hex.slice(8,12)}-${hex.slice(12,16)}-${hex.slice(16,20)}-${hex.slice(20)}`;
    };
    const binding = (item, getToken) => {
      const value = getToken(item) ?? null;
      if (value !== null && !['string', 'number', 'boolean'].includes(typeof value)) throw new TypeError('A swipe snapshot token must be a primitive version or fingerprint.');
      return value;
    };
    const recordKey = (item, getToken) => JSON.stringify([item.id, binding(item, getToken)]);
    const counts = state => {
      const value = {pending:0, deferred:0, confirmed:0, rejected:0};
      for (const record of state.records) value[record.disposition]++;
      return value;
    };
    const queue = state => state.records.filter(record => record.disposition === state.view).map(record => record.item);
    function lookup(state, item) { return state.records.find(record => record.key === recordKey(item, state.getToken)); }
    function currentRecord(state) { return state.records.find(record => record.disposition === state.view) || null; }
    function isCurrent(state) { return states.get(state.kind) === state && state.epoch === epoch(); }
    function isVisible(state) { return isCurrent(state) && visual?.state === state && visual.shell.isConnected; }
    function currentCase(item) { return Number.isInteger(item.version_no) && item.version_no > 0 && item.current_version === item.version_no; }
    function quarantine(item) { return item.movement_id == null && item.version_no == null && item.current_version == null; }
    function availability(state, item, action) {
      if (!item) return {disabled:true, reason:'No card is available in this view.'};
      if (state.pending) return {disabled:true, reason:'Wait for the current action to finish.'};
      if (!isCurrent(state)) return {disabled:true, reason:'Your session changed. Refresh this worklist.'};
      if (action === 'later' && state.view === 'deferred') return {disabled:true, reason:'This card is already in Later. Use Return this card to queue.'};
      if (!personal()) return action === 'later' || action === 'resume' ? {disabled:false, reason:''} : {disabled:true, reason:'Sign in with your personal account to record a decision.'};
      if (state.stale) return {disabled:true, reason:'New data is available. Refresh the page before recording another action.'};
      if (state.kind === 'CASE') {
        if (item.status !== 'OPEN') return {disabled:true, reason:'Only an open case can be actioned.'};
        if (action === 'later' || action === 'resume') {
          if (currentCase(item) || quarantine(item)) return {disabled:false, reason:''};
          return {disabled:true, reason:'This case belongs to an older or unavailable movement version. Refresh its evidence.'};
        }
        if (!currentCase(item)) return {disabled:true, reason:quarantine(item) ? 'Quarantined evidence requires corrected source data before a financial review decision.' : 'This case does not match the current movement version. Open its evidence and refresh.'};
        if (action === 'review' && item.release_eligible !== true) return {disabled:true, reason:'This case is not eligible for release. Open its evidence to resolve the source or rule issue.'};
        return {disabled:false, reason:''};
      }
      if (typeof item.fingerprint !== 'string' || !item.fingerprint) return {disabled:true, reason:'The package fingerprint is unavailable. Refresh this invoice.'};
      if (action === 'review') return item.status === 'DRAFT' && item.pdf_ready === true ? {disabled:false, reason:''} : {disabled:true, reason:'Only a prepared draft PDF can be reviewed for approval.'};
      return ['DRAFT','APPROVED'].includes(item.status) ? {disabled:false, reason:''} : {disabled:true, reason:'Only an unissued draft or approved package can be actioned.'};
    }
    function payload(state, item, action, id) {
      return {kind:state.kind, id:item.id, expected_version:state.kind === 'CASE' ? item.version_no ?? null : null,
        fingerprint:state.kind === 'INVOICE' ? item.fingerprint ?? null : null,
        reason:action === 'later' ? 'Deferred from the swipe worklist for 24 hours.' : 'Returned to the review queue from the swipe worklist.', request_id:id};
    }
    function matchingBinding(state, item, value) {
      if (!value || typeof value !== 'object') return false;
      return state.kind === 'CASE' ? value.case_id === item.id && value.version_no === (item.version_no ?? null) && value.version_id === (item.version_id ?? null)
        : value.invoice_id === item.id && value.fingerprint === item.fingerprint;
    }
    function renderToolbar(state) {
      if (!isVisible(state)) return;
      const shell = visual.shell, value = counts(state), busy = Boolean(state.pending || visual.deck.isBusy);
      shell.querySelectorAll('[data-swipe-view]').forEach(button => {
        const selected = button.dataset.swipeView === state.view;
        button.setAttribute('aria-pressed', String(selected)); button.classList.toggle('is-active', selected);
        button.disabled = busy;
        button.textContent = `${button.dataset.swipeView === 'pending' ? 'Pending' : 'Later'} (${value[button.dataset.swipeView]})`;
      });
      const back = shell.querySelector('[data-swipe-return]'), record = currentRecord(state);
      back.hidden = state.view !== 'deferred';
      const status = availability(state, record?.item, 'resume'); back.disabled = busy || status.disabled; back.title = status.reason;
      const warning = shell.querySelector('[data-swipe-stale]'); warning.hidden = !state.stale;
      const note = shell.querySelector('[data-swipe-note]');
      note.textContent = personal() ? 'Later postpones this exact version for your account for 24 hours. Review and Reject always require a separate personal confirmation.' : 'Shared access is read-only. Later only moves a card within this local worklist; it does not save a postponement or a review decision.';
      if (state.kind === 'INVOICE') note.textContent += ' Later only changes your worklist; it does not pause approved invoice dispatch.';
      const deferred = shell.querySelector('[data-swipe-deferred-info]'); deferred.hidden = state.view !== 'deferred' || !record;
      if (!deferred.hidden) {
        const stamp = record.postponement?.until;
        const validDate = stamp && Number.isFinite(Date.parse(stamp));
        deferred.textContent = record.local ? 'Deferred in this local session only.' : validDate ? `Personal postponement until ${new Date(stamp).toISOString().replace('T',' ').replace('.000Z',' UTC')}.` : 'Personal postponement recorded for this exact snapshot.';
      }
    }
    function updateVisible(state, message) {
      if (!isVisible(state)) return;
      renderToolbar(state);
      visual.deck.update({items:queue(state), counts:counts(state), message});
    }
    function validOperation(state, operation) { return isCurrent(state) && state.pending === operation && state.records.includes(operation.record); }
    async function perform(state, action, item, signal, owner) {
      const available = availability(state, item, action);
      if (available.disabled) return {accepted:false, message:available.reason};
      const record = lookup(state, item);
      if (!record || record.disposition !== state.view) return {accepted:false, message:'This card is no longer in the current worklist.'};
      record.requests ||= {};
      record.requests[action] ||= requestId();
      const operation = {record, action, requestId:record.requests[action]}; state.pending = operation;
      renderToolbar(state);
      if (action === 'resume' && isVisible(state)) visual.deck.update({message:'Returning this card to the queue…'});
      let success = false, message = '';
      try {
        if (action === 'later' || action === 'resume') {
          const isPersonal = personal();
          if (isPersonal) {
            // A submitted command is never cancelled by navigation. The frozen response is
            // applied only to this same session and page snapshot when it returns.
            const response = await options.api(`/api/review/${action === 'later' ? 'postpone' : 'resume'}`, payload(state,item,action,operation.requestId));
            if (!validOperation(state, operation)) return {accepted:false, message:'The worklist changed. Refresh to inspect the recorded result.'};
            const expected = action === 'later' ? 'POSTPONED' : 'RESUMED';
            if (response?.status !== expected || response?.id !== item.id || response?.kind !== state.kind) throw new Error('The recorded result did not match this card. Refresh before retrying.');
            if (!matchingBinding(state,item,response.binding)) throw new Error('The recorded result did not match this version. Refresh before retrying.');
            if (action === 'later' && (!matchingBinding(state,item,response.personal_postponement?.binding) || !Number.isFinite(Date.parse(response.personal_postponement?.until)))) throw new Error('The postponement receipt was incomplete. Refresh before retrying.');
            record.postponement = response.personal_postponement || null; record.local = false;
          } else { record.postponement = null; record.local = action === 'later'; }
          if (!validOperation(state, operation)) return {accepted:false, message:'The worklist changed. Refresh to inspect the recorded result.'};
          record.disposition = action === 'later' ? 'deferred' : 'pending';
          message = action === 'later' ? isPersonal ? 'Postponed for your account for 24 hours. No financial decision was recorded.' : 'Moved to Later in this local worklist only.' : isPersonal ? 'Returned to your queue. No financial decision was recorded.' : 'Returned to the local queue.';
          success = true;
        } else {
          const result = await options.confirmAction({kind:state.kind, item, action, signal});
          if (!validOperation(state, operation)) return {accepted:false, message:'The worklist changed. Refresh to inspect any recorded result.'};
          if (result?.accepted !== true) { message = result?.message || 'No decision recorded. The card remains in this worklist.'; return {accepted:false, message}; }
          record.disposition = action === 'review' ? 'confirmed' : 'rejected'; record.postponement = null; record.local = false;
          message = result.message || (action === 'review' ? 'Personal confirmation recorded.' : state.kind === 'CASE' ? 'Issue recorded. The case remains open for remediation; its movement is blocked.' : 'Invoice rejected and held from delivery.');
          success = true;
        }
        delete record.requests[action];
        return {accepted:true, items:queue(state), counts:counts(state), message};
      } catch (error) {
        message = error?.message || 'The action could not be completed. The card remains in this worklist.';
        return {accepted:false, message};
      } finally {
        if (state.pending === operation) state.pending = null;
        if (isCurrent(state)) {
          renderToolbar(state);
          // The original CardDeck consumes its own callback result. A newly mounted
          // visual needs the retained result explicitly, without changing other views.
          if (isVisible(state) && visual.sequence !== owner) updateVisible(state, message);
          if (success) notify(message);
        }
      }
    }
    function deactivate() {
      visualSequence++;
      if (!visual) return;
      const old = visual; visual = null;
      old.lifecycle.abort(); old.deck.destroy(); old.shell.remove();
    }
    function mount(container, input) {
      if (!(container instanceof Element) || !KINDS.has(input?.kind) || !Array.isArray(input.items) || input.items.length > MAX_ITEMS || typeof input.renderCard !== 'function') throw new TypeError('SwipeWorkspace requires a container, kind, bounded items and renderCard.');
      const getToken = input.getToken || (item => input.kind === 'CASE' ? item.version_no ?? null : item.fingerprint ?? null);
      const keys = input.items.map(item => { if (typeof item?.id !== 'string' || !item.id) throw new TypeError('Every swipe item requires a stable id.'); return recordKey(item,getToken); });
      if (new Set(keys).size !== keys.length || new Set(input.items.map(item => item.id)).size !== keys.length) throw new TypeError('A swipe page must not repeat items.');
      const signature = JSON.stringify(keys), old = states.get(input.kind);
      let state = old;
      if (!state || state.epoch !== epoch() || state.scope !== input.scope || state.signature !== signature) {
        state = {kind:input.kind, scope:input.scope, signature, epoch:epoch(), view:'pending', stale:false, pending:null, getToken,
          records:input.items.map(item => {
            const frozen = structuredClone(item), postponement = frozen.personal_postponement;
            const deferred = personal() && postponement && matchingBinding({kind:input.kind},frozen,postponement.binding) && Number.isFinite(Date.parse(postponement.until)) && Date.parse(postponement.until) > Date.now();
            return {key:recordKey(frozen,getToken), item:frozen, disposition:deferred ? 'deferred' : 'pending', postponement:deferred ? postponement : null, local:false};
          })};
        states.set(input.kind,state);
      }
      deactivate();
      const lifecycle = new AbortController(), shell = document.createElement('section'), sequence = ++visualSequence;
      shell.className = 'swipe-workspace'; shell.dataset.swipeKind = input.kind;
      shell.innerHTML = '<div class="swipe-workspace-toolbar"><div class="swipe-workspace-views" role="group" aria-label="Swipe worklist view"><button type="button" data-swipe-view="pending"></button><button type="button" data-swipe-view="deferred"></button></div><button type="button" data-swipe-return hidden>Return this card to queue</button></div><p class="swipe-workspace-note" data-swipe-note></p><p class="swipe-workspace-stale" data-swipe-stale role="status" hidden>New data is available. This card and its notes are preserved. Use Refresh to load the current evidence before another saved action.</p><p class="swipe-workspace-note" data-swipe-deferred-info hidden></p><div class="swipe-workspace-deck"></div>';
      container.replaceChildren(shell);
      const deck = window.CardDeck.mount(shell.querySelector('.swipe-workspace-deck'), {
        items:queue(state), counts:counts(state), renderCard:input.renderCard, onActiveCard:input.onActiveCard, getKey:item=>item.id, getToken:state.getToken,
        scopeLabel:'This page · current worklist', label:input.kind === 'CASE' ? 'Movement review cards' : 'Invoice review cards',
        emptyMessage:'Switch between Pending and Later, or refresh to load another page. Confirmed and rejected counts describe actions in this worklist.',
        getActionState:(item,action)=>availability(state,item,action),
        onAction:({action,item,signal})=>perform(state,action,item,signal,sequence),
        onBusyChange:()=>renderToolbar(state)
      });
      visual = {state, sequence, shell, deck, lifecycle};
      shell.addEventListener('click', event => {
        const tab = event.target.closest('[data-swipe-view]');
        if (tab && !tab.disabled && !state.pending && !deck.isBusy && tab.dataset.swipeView !== state.view) {
          state.view = tab.dataset.swipeView; updateVisible(state, `${state.view === 'pending' ? 'Pending' : 'Later'} worklist selected.`);
        }
        const back = event.target.closest('[data-swipe-return]');
        if (back && !back.disabled && !state.pending && !deck.isBusy) {
          const item = currentRecord(state)?.item; if (!item) return;
          perform(state,'resume',item,lifecycle.signal,sequence).then(result => {
            if (isVisible(state) && visual.sequence === sequence) updateVisible(state, result.message);
          });
        }
      }, {signal:lifecycle.signal});
      renderToolbar(state);
    }
    function invalidate(kind) {
      const state = states.get(kind); if (!state || !isCurrent(state)) return;
      state.stale = true; renderToolbar(state);
      // A successful command may emit its audit event before its HTTP response.
      // Invalidation must not discard that response or cancel its saved result.
      if (isVisible(state) && !state.pending && !visual.deck.isBusy) updateVisible(state,'New data is available. Refresh before recording another action.');
    }
    function reset() { deactivate(); states.clear(); }
    function getSnapshot(kind) {
      const state = states.get(kind); if (!state || !isCurrent(state)) return null;
      return {kind, scope:state.scope, view:state.view, stale:state.stale, busy:Boolean(state.pending), counts:counts(state),
        items:state.records.map(record=>({id:record.item.id, token:binding(record.item,state.getToken), disposition:record.disposition}))};
    }
    return {mount, deactivate, reset, invalidate, getSnapshot};
  }
  window.SwipeWorkspace = Object.freeze({create});
})();
