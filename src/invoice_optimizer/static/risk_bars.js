/* Recorded logistic feature evidence. Percentages are contribution shares. */
(() => {
  'use strict';
  const esc=value=>String(value ?? '').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const exact=value=>Number.isFinite(value)?String(value):'Unavailable';
  function raw(feature,currency){
    const value=feature.raw_value;
    if(feature.raw_value_status==='NOT_RETAINED' || value==null)return 'Raw input unavailable';
    if(feature.feature==='log_amount')return `${currency || ''} ${value}`.trim();
    if(feature.feature==='amount_to_expected')return `${value.amount} / ${value.expected_amount} ${currency || ''}`.trim();
    if(feature.feature==='night_hour')return `${value.hour_utc}:00 UTC · ${value.event_at}`;
    if(feature.feature.includes('age_days'))return `${value} days`;
    if(feature.feature==='cross_border')return value===1?'Yes (1)':'No (0)';
    return typeof value==='object'?JSON.stringify(value):String(value);
  }
  function markup(result){
    if(!result)return '<div class="risk-drivers-loading" role="status">Loading recorded model parameters…</div>';
    if(result.status!=='AVAILABLE')return `<div class="risk-drivers-unavailable"><strong>Model parameters unavailable</strong><p>${esc(result.reason || 'No reconstructable recorded assessment.')}</p><span>No risk bars can be inferred from missing evidence.</span></div>`;
    const features=Array.isArray(result.features)?result.features:[];
    return `<div class="risk-driver-box"><div class="risk-driver-heading"><strong>Logistic regression · parameters</strong><span>Contribution share</span></div><p class="risk-driver-legend"><span class="risk-up">↑ Raises score</span><span class="risk-down">↓ Reduces score</span></p>${result.raw_binding!=='VERIFIED_SOURCE_SNAPSHOT'?`<p class="risk-binding-warning">Raw values could not be verified: ${esc(result.raw_binding_reason || 'Retained features only.')}</p>`:''}<div class="risk-driver-list">${features.map((feature,index)=>{
      const share=typeof feature.share_percent==='number' && Number.isFinite(feature.share_percent)?Math.max(0,Math.min(100,feature.share_percent)):null;
      const effect=feature.effect==='INCREASE'?'up':feature.effect==='DECREASE'?'down':'neutral';
      const direction=effect==='up'?'Raises':effect==='down'?'Reduces':'Does not change';
      const value=raw(feature,result.currency),percent=share==null?'Unavailable':share.toFixed(1)+'%';
      const explanation=`${direction} model log-odds by ${exact(feature.contribution)}. ${percent} of total absolute feature contribution; not a risk probability. Raw input: ${value}. Transformed input: ${exact(feature.transformed_value)}.`;
      return `<button type="button" class="risk-driver risk-${effect}" data-risk-feature="${esc(feature.feature)}" aria-label="${esc(feature.label+'. '+explanation)}"><span class="risk-driver-title"><span>${esc(feature.label)}</span><span class="risk-driver-direction" aria-hidden="true">${effect==='up'?'↑':effect==='down'?'↓':'—'}</span></span><span class="risk-driver-value">${esc(value)}</span><span class="risk-driver-track" aria-hidden="true"><span class="risk-driver-fill" style="width:${share ?? 0}%;--driver-delay:${index*25}ms"></span></span><span class="risk-driver-tooltip" role="tooltip"><strong>${esc(percent)} contribution share</strong><span>${esc(direction)} score · ${esc(exact(feature.contribution))} log-odds</span><span>Model input: ${esc(exact(feature.transformed_value))}</span><span>${esc(feature.transformation)}</span></span></button>`;
    }).join('')}</div><p class="risk-driver-note">Bars show each parameter’s share of absolute log-odds contributions, not a separate fraud probability. Hover, focus or tap a parameter for its percentage.</p></div>`;
  }
  window.RiskBars=Object.freeze({markup});
})();
