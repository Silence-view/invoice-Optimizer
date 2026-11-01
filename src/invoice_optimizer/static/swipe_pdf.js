/* One verified page of the active invoice; no PDF data on the cards underneath. */
(() => {
  'use strict';
  let instance = 0;
  const MAX_BYTES = 16 * 1024 * 1024, MAX_DIMENSION = 1600;
  const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));

  function mount(panel, {item, api, onUnauthorized}) {
    const id = item.id, expected = item.pdf_sha256, scope = `swipe-pdf:${++instance}`;
    const lifecycle = new AbortController();
    let destroyed = false, sequence = 0, controller = null, objectUrl = null, metadata = null, shownPage = 1;
    const pdfLink = `<a href="/api/invoices/${encodeURIComponent(id)}/pdf" target="_blank" rel="noopener">Open exact PDF ↗</a>`;
    const title = `<div class="swipe-pdf-title"><strong>Exact PDF preview</strong>${pdfLink}</div>`;

    function clearImage() {
      const image = panel.querySelector('.swipe-pdf-image');
      if (image) { image.removeAttribute('src'); image.remove(); }
      if (objectUrl) URL.revokeObjectURL(objectUrl);
      objectUrl = null;
    }
    function destroy() {
      if (destroyed) return;
      destroyed = true; sequence++; controller?.abort(); controller = null;
      lifecycle.abort(); clearImage(); metadata = null; panel.replaceChildren();
    }
    function navigation(page, total, loading = false) {
      return `<div class="swipe-pdf-navigation"><button type="button" data-swipe-pdf-page="${page-1}" ${loading || page<=1?'disabled':''}>← Previous page</button><span aria-live="polite">${total?`Page ${page} of ${total}`:'Loading PDF…'}</span><button type="button" data-swipe-pdf-page="${page+1}" ${loading || !total || page>=total?'disabled':''}>Next page →</button></div>`;
    }
    async function load(page) {
      if (destroyed || !Number.isSafeInteger(page) || page < 1) return;
      const request = ++sequence; controller?.abort(); controller = new AbortController();
      const activeController = controller;
      const current = () => !destroyed && sequence === request && panel.isConnected && !activeController.signal.aborted;
      clearImage();
      panel.setAttribute('aria-busy', 'true');
      panel.innerHTML = title + navigation(page, metadata?.page_count, true) + '<div class="swipe-pdf-viewport swipe-pdf-loading" role="status">Loading the exact PDF page…</div>';
      try {
        if (item.pdf_ready !== true) throw new Error('The PDF is still being prepared. Refresh the worklist when it is ready.');
        if (!/^[a-f0-9]{64}$/.test(expected || '')) throw new Error('This card has no verified PDF identity. Refresh the worklist before previewing it.');
        const meta = metadata || await api(`/api/invoices/${encodeURIComponent(id)}/preview`, undefined, undefined, {scope, signal:activeController.signal});
        if (!current()) return;
        if (meta.pdf_sha256 !== expected) throw new Error('The preview does not match this card’s PDF. Refresh the worklist to inspect the current package.');
        if (!Number.isSafeInteger(meta.page_count) || meta.page_count < 1 || page > meta.page_count) throw new Error('Invalid PDF page count. Open the exact PDF or retry.');
        metadata = meta;
        const response = await fetch(`/api/invoices/${encodeURIComponent(id)}/preview/pages/${page}?sha256=${expected}`, {credentials:'same-origin', signal:activeController.signal});
        if (!current()) return;
        if (response.status === 401) { onUnauthorized?.(); return; }
        if (!response.ok) {
          let detail; try { detail = (await response.json()).detail; } catch { /* Keep bounded status fallback. */ }
          throw new Error(typeof detail === 'string' ? detail : `PDF preview unavailable (${response.status}). Retry or open the exact PDF.`);
        }
        if (response.headers.get('x-pdf-sha256') !== expected || response.headers.get('x-pdf-page') !== String(page)) throw new Error('The returned page does not match this invoice and page number. Refresh the worklist.');
        if (Number(response.headers.get('content-length')) > MAX_BYTES) throw new Error('This page exceeds the preview size limit. Open the exact PDF.');
        if (!response.body) throw new Error('The PDF page response is empty. Retry the preview.');
        const reader = response.body.getReader(), chunks = []; let bytes = 0;
        while (true) {
          const part = await reader.read(); if (part.done) break;
          bytes += part.value.byteLength;
          if (bytes > MAX_BYTES) { await reader.cancel(); throw new Error('This page exceeds the preview size limit. Open the exact PDF.'); }
          chunks.push(part.value);
        }
        const blob = new Blob(chunks, {type:'image/png'}), header = await blob.slice(0,24).arrayBuffer(), signature = new Uint8Array(header);
        if (signature.length < 24 || [137,80,78,71,13,10,26,10,0,0,0,13,73,72,68,82].some((value,index) => signature[index] !== value)) throw new Error('The server did not return a valid PNG page. Retry or open the exact PDF.');
        const dimensions = new DataView(header), width = dimensions.getUint32(16), height = dimensions.getUint32(20);
        if (!width || !height || width > MAX_DIMENSION || height > MAX_DIMENSION) throw new Error('The PDF page dimensions exceed the preview limit. Open the exact PDF.');
        if (!current()) return;
        objectUrl = URL.createObjectURL(blob);
        const image = new Image(); image.className = 'swipe-pdf-image'; image.draggable = false;
        image.alt = `${item.number || id} — exact PDF page ${page} of ${meta.page_count}`;
        image.dataset.pdfSha256 = expected; image.dataset.pdfPage = String(page); image.src = objectUrl;
        await image.decode();
        if (!current()) return;
        if (image.naturalWidth !== width || image.naturalHeight !== height) throw new Error('Decoded PDF page dimensions do not match its image header.');
        shownPage = page;
        panel.innerHTML = title + navigation(page, meta.page_count) + '<div class="swipe-pdf-viewport"></div><p class="swipe-pdf-note">Exact retained PDF · one page at a time. Use Enlarge PDF for a larger view.</p>';
        panel.querySelector('.swipe-pdf-viewport').append(image);
      } catch (error) {
        if (current() && error.name !== 'AbortError') {
          clearImage();
          panel.innerHTML = title + `<div class="swipe-pdf-error" role="status">${esc(error.message || 'The PDF preview is unavailable.')}</div><button type="button" data-swipe-pdf-retry="${page}">Retry preview</button>`;
        }
      } finally {
        if (current()) { panel.setAttribute('aria-busy','false'); controller = null; }
      }
    }
    panel.addEventListener('click', event => {
      const button = event.target.closest('[data-swipe-pdf-page],[data-swipe-pdf-retry]');
      if (!button || button.disabled) return;
      event.preventDefault(); event.stopPropagation();
      void load(Number(button.dataset.swipePdfPage ?? button.dataset.swipePdfRetry ?? shownPage));
    }, {signal:lifecycle.signal});
    void load(1);
    return destroy;
  }
  window.SwipePdf = Object.freeze({mount});
})();
