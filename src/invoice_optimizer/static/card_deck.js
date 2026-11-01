/* Bounded presentation only. The host owns snapshots, permissions and every business decision. */
(() => {
  'use strict';
  const MAX_ITEMS = 100;
  const ACTIONS = ['later', 'review', 'reject'];
  const LABELS = {later: 'Later', review: 'Review & confirm', reject: 'Reject'};
  const COUNTS = {pending: 'Pending', deferred: 'Later', confirmed: 'Confirmed', rejected: 'Rejected'};
  const INTERACTIVE = 'a,button,input,textarea,select,label,summary,[contenteditable]:not([contenteditable="false"]),[role="button"],[role="slider"],[role="textbox"]';
  let instanceNumber = 0;

  function mount(container, options) {
    if (!(container instanceof Element)) throw new TypeError('CardDeck requires a container element.');
    if (typeof options?.renderCard !== 'function' || typeof options?.onAction !== 'function') {
      throw new TypeError('CardDeck requires renderCard and onAction callbacks.');
    }
    const getKey = options.getKey || (item => item.id);
    const getToken = options.getToken || (() => null);
    const id = `card-deck-${++instanceNumber}`;
    const lifecycle = new AbortController();
    const media = window.matchMedia('(prefers-reduced-motion: reduce)');
    let items = [], counts = {}, generation = 0, destroyed = false, busy = false;
    let gesture = null, pointerCapture = null, frame = null, resetTimer = null;
    let animationTimer = null, finishAnimation = null, actionController = null;
    let pendingTransform = null, restoreFocus = false, suppressClickUntil = 0, activeCleanup = null, renderedItems = [];
    const shell = document.createElement('section');
    shell.className = 'card-deck';
    shell.dataset.deckId = id;
    shell.setAttribute('aria-label', options.label || 'Focused review worklist');
    shell.innerHTML = '<div class="card-deck-header"><p class="card-deck-scope"></p><div class="card-deck-counts" aria-label="Page worklist counters" aria-live="polite" aria-atomic="true"></div></div><div class="card-deck-stage" tabindex="0" role="group" aria-label="Current card. Use the action buttons, left and right arrow keys, or drag horizontally."><div class="card-deck-dropzone" data-direction="left" aria-hidden="true"><span>Later</span></div><div class="card-deck-dropzone" data-direction="right" aria-hidden="true"><span>Review &amp; confirm</span></div><div class="card-deck-layers"></div></div><div class="card-deck-actions"></div><p class="card-deck-hint">Drag left to review later. Drag right to open review and confirmation. No decision is saved by dragging alone.</p><p class="card-deck-live" role="status" aria-live="polite" aria-atomic="true"></p>';
    const stage = shell.querySelector('.card-deck-stage');
    const layers = shell.querySelector('.card-deck-layers');
    const actionBar = shell.querySelector('.card-deck-actions');
    const live = shell.querySelector('.card-deck-live');
    shell.querySelector('.card-deck-scope').textContent = options.scopeLabel || 'This page · session worklist';
    shell.querySelector('.card-deck-hint').textContent = 'Drag left for Later. Drag right to open review and confirmation. A right swipe never confirms a decision.';
    container.replaceChildren(shell);

    function validateItems(value) {
      if (!Array.isArray(value) || value.length > MAX_ITEMS) throw new TypeError('CardDeck requires a bounded page of at most 100 items.');
      const keys = value.map(item => getKey(item));
      if (keys.some(key => key == null) || new Set(keys).size !== keys.length) throw new TypeError('Each card must have a distinct stable key.');
      return value.slice();
    }

    function permission(item, action) {
      if (!item) return {disabled: true, reason: 'No card is available.'};
      try {
        const result = options.getActionState?.(item, action);
        if (typeof result === 'boolean') return {disabled: result, reason: ''};
        return {disabled: result?.disabled === true, reason: typeof result?.reason === 'string' ? result.reason : ''};
      } catch {
        return {disabled: true, reason: 'Action availability is unavailable. Refresh the worklist.'};
      }
    }

    function announce(message) { if (!destroyed) live.textContent = message || ''; }
    function notify(callback, value) { if (typeof callback === 'function') { try { callback(value); } catch { /* Host observers must not break lifecycle cleanup. */ } } }
    function currentCard() { return layers.querySelector('[data-layer="0"]'); }
    function releaseCapture() {
      const capture = pointerCapture; pointerCapture = null;
      if (capture != null && stage.hasPointerCapture?.(capture)) { try { stage.releasePointerCapture(capture); } catch { /* Already released by the browser. */ } }
    }
    function cancelFrame() { if (frame != null) cancelAnimationFrame(frame); frame = null; pendingTransform = null; }
    function clearResetTimer() { if (resetTimer != null) clearTimeout(resetTimer); resetTimer = null; }
    function clearAnimation() {
      if (animationTimer != null) clearTimeout(animationTimer);
      animationTimer = null;
      if (finishAnimation) { const resolve = finishAnimation; finishAnimation = null; resolve(); }
    }
    function clearDirection() {
      shell.classList.remove('is-left', 'is-right', 'is-ready', 'is-dragging');
      shell.querySelectorAll('.card-deck-dropzone').forEach(zone => zone.classList.remove('is-unavailable'));
    }
    function resetPosition(animated = true) {
      cancelFrame(); clearResetTimer(); clearDirection();
      const card = currentCard(); if (!card) return;
      card.classList.remove('is-leaving');
      card.classList.toggle('is-snapping', animated && !media.matches);
      card.style.removeProperty('transform');
      if (animated && !media.matches) resetTimer = setTimeout(() => { resetTimer = null; card.classList.remove('is-snapping'); }, 180);
      else card.classList.remove('is-snapping');
    }
    function cancelGesture(animated = true) {
      gesture = null; releaseCapture(); resetPosition(animated);
    }
    function setBusy(value) {
      if (busy === value) return;
      busy = value; shell.classList.toggle('is-busy', value); shell.setAttribute('aria-busy', String(value));
      currentCard()?.toggleAttribute('inert', value);
      refreshButtons(); notify(options.onBusyChange, value);
    }
    function refreshButtons() {
      actionBar.querySelectorAll('[data-deck-action]').forEach(button => {
        const state = permission(items[0], button.dataset.deckAction);
        button.disabled = busy || state.disabled;
        button.title = state.reason;
        shell.querySelector(`#${button.getAttribute('aria-describedby')}`).textContent = state.reason;
      });
    }
    function renderCounts() {
      const node = shell.querySelector('.card-deck-counts'); node.replaceChildren();
      for (const [key, label] of Object.entries(COUNTS)) {
        const value = counts[key], entry = document.createElement('div');
        const number = document.createElement('strong'); number.dataset.deckCount = key;
        number.textContent = Number.isSafeInteger(value) && value >= 0 ? value.toLocaleString('en-GB') : '—';
        const caption = document.createElement('span'); caption.textContent = label;
        entry.append(number, caption); node.append(entry);
      }
    }
    function clearActiveCard() {
      const cleanup = activeCleanup; activeCleanup = null;
      if (typeof cleanup === 'function') cleanup();
    }
    function render() {
      clearActiveCard();
      layers.replaceChildren(); renderCounts(); actionBar.replaceChildren();
      shell.classList.toggle('is-empty', items.length === 0);
      const shown = items.slice(0, 3);
      for (let layer = shown.length - 1; layer >= 0; layer--) {
        const item = shown[layer], card = document.createElement('div');
        card.className = 'card-deck-layer'; card.dataset.layer = String(layer); card.dataset.cardKey = String(getKey(item));
        if (layer !== 0) { card.inert = true; card.setAttribute('aria-hidden', 'true'); }
        else card.inert = busy;
        const content = options.renderCard(item, {active: layer === 0, layer});
        if (typeof content === 'string') card.innerHTML = content;
        else if (content instanceof Node) card.append(content);
        else throw new TypeError('renderCard must return an HTML string or DOM node.');
        layers.append(card);
      }
      if (!shown.length) {
        const empty = document.createElement('div'); empty.className = 'card-deck-empty';
        const title = document.createElement('strong'); title.textContent = 'No cards left in this worklist';
        const text = document.createElement('p'); text.textContent = options.emptyMessage || 'Use the page controls to return to deferred cards or load another page.';
        empty.append(title, text); layers.append(empty);
      }
      for (const action of ACTIONS) {
        const wrap = document.createElement('div'), button = document.createElement('button'), reason = document.createElement('span');
        button.type = 'button'; button.dataset.deckAction = action; button.textContent = LABELS[action];
        button.className = action === 'review' ? 'primary' : action === 'reject' ? 'danger' : '';
        reason.id = `${id}-reason-${action}`; reason.className = 'card-deck-sr-only';
        button.setAttribute('aria-describedby', reason.id); wrap.append(button, reason); actionBar.append(wrap);
      }
      refreshButtons();
      if (shown.length && typeof options.onActiveCard === 'function') {
        activeCleanup = options.onActiveCard({item:items[0], element:currentCard()});
      }
      renderedItems = items.map(item => ({item, key:getKey(item), token:getToken(item)}));
    }
    function focusStage() {
      if (destroyed || !restoreFocus || !stage.isConnected) return;
      if (document.activeElement === document.body || shell.contains(document.activeElement)) stage.focus({preventScroll: true});
    }
    function animateAccepted(action) {
      if (media.matches || !currentCard()) return Promise.resolve();
      const card = currentCard(), direction = action === 'later' ? -1 : action === 'review' ? 1 : 0;
      card.classList.add('is-leaving');
      card.style.transform = direction ? `translate3d(${direction * Math.max(stage.clientWidth * 1.1, 360)}px, -10px, 0) rotate(${direction * 12}deg)` : 'translate3d(0, 16px, 0) scale(.97)';
      return new Promise(resolve => {
        finishAnimation = resolve;
        animationTimer = setTimeout(() => { animationTimer = null; finishAnimation = null; resolve(); }, 180);
      });
    }
    async function act(action, expected) {
      if (destroyed || busy || !items.length || !ACTIONS.includes(action)) return;
      const item = items[0], key = getKey(item), token = getToken(item);
      if (expected && (expected.key !== key || !Object.is(expected.token, token) || expected.generation !== generation)) { cancelGesture(); announce('This card changed. Review the current snapshot.'); return; }
      const state = permission(item, action);
      if (state.disabled) { cancelGesture(); announce(state.reason || 'This action is unavailable for the current card.'); return; }
      const actionGeneration = generation, controller = new AbortController(); actionController = controller;
      restoreFocus = document.activeElement === document.body || shell.contains(document.activeElement);
      cancelGesture(false); setBusy(true); announce(action === 'later' ? 'Processing Later…' : 'Complete the review and confirmation. The card remains here until the result is recorded.');
      notify(options.onInteraction, {type: 'action', action, key});
      try {
        // The host may open confirmation UI. A gesture itself has no authority to save a decision.
        const result = await options.onAction({action, item, key, token, signal: controller.signal});
        if (destroyed || generation !== actionGeneration || controller.signal.aborted) return;
        if (result?.accepted !== true) { announce(result?.message || 'No change recorded. This card remains in the worklist.'); return; }
        const next = validateItems(result.items);
        if (!result.counts || typeof result.counts !== 'object') throw new TypeError('An accepted action must return the host worklist and counts.');
        await animateAccepted(action);
        if (destroyed || generation !== actionGeneration || controller.signal.aborted) return;
        items = next; counts = {...result.counts}; generation++; render();
        announce(result.message || `${LABELS[action]} completed. Worklist counters updated.`);
      } catch (error) {
        if (!destroyed && generation === actionGeneration && !controller.signal.aborted) announce(error?.message || 'The action did not complete. Check the recorded state before retrying.');
      } finally {
        if (actionController === controller) { actionController = null; if (!destroyed) { setBusy(false); resetPosition(false); focusStage(); } }
      }
    }
    function threshold() { return Math.max(70, Math.min(140, stage.clientWidth * .25)); }
    function paintGesture() {
      frame = null; if (!gesture?.horizontal || !pendingTransform || destroyed) return;
      const {x, y} = pendingTransform, card = currentCard(); if (!card) return;
      const direction = x < 0 ? 'left' : 'right', state = permission(items[0], x < 0 ? 'later' : 'review');
      shell.classList.add('is-dragging'); shell.classList.toggle('is-left', x < 0); shell.classList.toggle('is-right', x >= 0);
      shell.classList.toggle('is-ready', Math.abs(x) >= threshold() && !state.disabled);
      shell.querySelectorAll('.card-deck-dropzone').forEach(zone => zone.classList.toggle('is-unavailable', zone.dataset.direction === direction && state.disabled));
      if (!media.matches) card.style.transform = `translate3d(${x}px, ${Math.max(-18, Math.min(18, y * .12))}px, 0) rotate(${Math.max(-11, Math.min(11, x / Math.max(stage.clientWidth, 1) * 14))}deg)`;
    }
    function onDown(event) {
      if (destroyed || busy || !items.length || gesture || event.isPrimary === false || event.button !== 0 || event.target.closest(INTERACTIVE)) return;
      if (!currentCard()?.contains(event.target) && event.target !== stage && event.target !== layers) return;
      clearResetTimer(); currentCard()?.classList.remove('is-snapping');
      gesture = {pointerId: event.pointerId, x: event.clientX, y: event.clientY, horizontal: false, key: getKey(items[0]), token: getToken(items[0]), generation};
    }
    function onMove(event) {
      if (!gesture || event.pointerId !== gesture.pointerId || busy) return;
      const x = event.clientX - gesture.x, y = event.clientY - gesture.y;
      if (!gesture.horizontal) {
        if (Math.abs(y) > 10 && Math.abs(y) >= Math.abs(x)) { cancelGesture(false); return; }
        if (Math.abs(x) < 10 || Math.abs(x) <= Math.abs(y) * 1.25) return;
        gesture.horizontal = true;
        stage.focus({preventScroll: true});
        try { stage.setPointerCapture(event.pointerId); pointerCapture = event.pointerId; } catch { /* Synthetic events or a released native pointer. */ }
        notify(options.onInteraction, {type: 'drag', key: gesture.key});
      }
      if (event.cancelable) event.preventDefault();
      pendingTransform = {x, y}; if (frame == null) frame = requestAnimationFrame(paintGesture);
    }
    function onUp(event) {
      if (!gesture || event.pointerId !== gesture.pointerId) return;
      const captured = gesture, x = event.clientX - captured.x;
      gesture = null; releaseCapture(); cancelFrame();
      if (!captured.horizontal) { resetPosition(false); return; }
      suppressClickUntil = performance.now() + 300;
      if (Math.abs(x) < threshold()) { resetPosition(); announce('Drag cancelled. This card has not changed.'); return; }
      act(x < 0 ? 'later' : 'review', captured);
    }
    function onCancel(event) { if (gesture && event.pointerId === gesture.pointerId) { cancelGesture(); announce('Drag cancelled. This card has not changed.'); } }
    function onKey(event) {
      if (event.target.closest(INTERACTIVE) || event.altKey || event.ctrlKey || event.metaKey || event.shiftKey || event.repeat) return;
      if (event.key === 'Escape' && gesture) { event.preventDefault(); cancelGesture(); announce('Drag cancelled.'); return; }
      if (!['ArrowLeft', 'ArrowRight'].includes(event.key)) return;
      event.preventDefault(); act(event.key === 'ArrowLeft' ? 'later' : 'review');
    }
    stage.addEventListener('pointerdown', onDown, {signal: lifecycle.signal});
    stage.addEventListener('pointermove', onMove, {signal: lifecycle.signal, passive: false});
    stage.addEventListener('pointerup', onUp, {signal: lifecycle.signal});
    stage.addEventListener('pointercancel', onCancel, {signal: lifecycle.signal});
    stage.addEventListener('lostpointercapture', onCancel, {signal: lifecycle.signal});
    stage.addEventListener('pointerleave', event => { if (gesture && !gesture.horizontal && event.pointerId === gesture.pointerId) cancelGesture(false); }, {signal: lifecycle.signal});
    stage.addEventListener('keydown', onKey, {signal: lifecycle.signal});
    stage.addEventListener('click', event => { if (performance.now() < suppressClickUntil || busy) { event.preventDefault(); event.stopPropagation(); } }, {signal: lifecycle.signal, capture: true});
    actionBar.addEventListener('click', event => { const button = event.target.closest('[data-deck-action]'); if (button && !button.disabled) act(button.dataset.deckAction); }, {signal: lifecycle.signal});
    const onMotion = () => { cancelGesture(false); if (media.matches) clearAnimation(); };
    media.addEventListener('change', onMotion);

    function update(value) {
      if (destroyed) return;
      const next = value?.items === undefined ? items : validateItems(value.items);
      const sameItems = next.length === renderedItems.length && next.every((item,index) => {
        const previous = renderedItems[index];
        return item === previous.item && getKey(item) === previous.key && Object.is(getToken(item), previous.token);
      });
      generation++; actionController?.abort(); actionController = null; clearAnimation(); cancelGesture(false); setBusy(false);
      items = next; if (value?.counts !== undefined) counts = {...value.counts};
      if (sameItems) { renderCounts(); refreshButtons(); } else render();
      announce(value?.message || 'Worklist updated. Review the current card before choosing an action.');
    }
    function destroy() {
      if (destroyed) return;
      destroyed = true; generation++; actionController?.abort(); actionController = null;
      lifecycle.abort(); media.removeEventListener('change', onMotion); clearAnimation(); cancelGesture(false); clearResetTimer();
      items = []; counts = {}; if (busy) { busy = false; notify(options.onBusyChange, false); }
      clearActiveCard(); shell.remove();
    }
    try { items = validateItems(options.items || []); counts = {...options.counts}; render(); }
    catch (error) { destroy(); throw error; }
    return {update, destroy, getCurrent: () => items[0] || null, getCounts: () => ({...counts}), get isBusy() { return busy; }};
  }
  window.CardDeck = Object.freeze({mount});
})();
