"""Complete per-movement PDF detail retains the original accounting summary."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
import json
from pathlib import Path

from pypdf import PdfReader
import pytest

from invoice_optimizer.documents import (InvoiceValidationError,MAX_MOVEMENT_DETAILS,
    _validated_context,render_invoice)
from test_documents import sample_document,compiler


def itemized_document(count=3,amounts=None):
    document=sample_document()
    amounts=amounts if amounts is not None else [format(Decimal(index+1)/100,'.2f') for index in range(count)]
    assert len(amounts)==count
    net=sum((Decimal(value) for value in amounts),Decimal('0.00'))
    vat=(net*Decimal('.20')).quantize(Decimal('.01'),rounding=ROUND_HALF_UP)
    document.update(line_format='ITEMIZED',movement_count=count,
        lines=[{'description':f'Monthly accounting summary for {count} movements','quantity':'1',
            'unit_price':str(net),'net_amount':str(net),'vat_rate':'0.20','vat_amount':str(vat),'gross_amount':str(net+vat)}],
        net_total=str(net),vat_total=str(vat),gross_total=str(net+vat))
    start=datetime(2026,9,2,10,20,30,tzinfo=timezone.utc)
    document['movement_detail']=[{
        'row_no':index+1,'movement_id':f'movement-{index+1:05d}','version_id':f'version-{index+1:05d}',
        'external_id':f'SRC-MOV-{index+1:05d}','event_at':(start+timedelta(minutes=index)).isoformat(),
        'description':f'Purchased service for movement {index+1:05d}',
        'supplier_id':f'SUPPLIER-{index+1:05d}','amount':amount,'currency':'GBP'}
        for index,amount in enumerate(amounts)]
    return document


def pdf_text(result):
    pdf=PdfReader(result['pdf_path'])
    return pdf,'\n'.join(page.extract_text() for page in pdf.pages)


def with_movement_vat(document,rate='0.20'):
    """Independent fixture arithmetic, without using production VAT helpers."""
    vat_total=Decimal('0.00');gross_total=Decimal('0.00')
    for row in document['movement_detail']:
        net=Decimal(row['amount'])
        vat=(net*Decimal(rate)).quantize(Decimal('.01'),rounding=ROUND_HALF_UP)
        row.update(vat_rate=rate,vat_amount=format(vat,'.2f'),gross_amount=format(net+vat,'.2f'))
        vat_total+=vat;gross_total+=net+vat
    document['movement_vat']={'policy':'line_half_up_reconciled_v1','rate':rate,
        'line_vat_total':format(vat_total,'.2f'),'line_gross_total':format(gross_total,'.2f'),
        'rounding_adjustment':format(Decimal(document['vat_total'])-vat_total,'.2f')}
    return document


@pytest.mark.parametrize('amounts,expected_adjustment,subtotal_vat,subtotal_gross',[
    (['0.03']*3,'-0.01','0.03','0.12'),
    (['0.01']*3,'0.01','0.00','0.03'),
    (['1.00']*3,'0.00','0.60','3.60'),
])
def test_per_movement_vat_shows_exact_rows_and_signed_reconciliation_without_changing_invoice(
        tmp_path,compiler,amounts,expected_adjustment,subtotal_vat,subtotal_gross):
    document=itemized_document(3,amounts)
    original_accounting={key:deepcopy(document[key]) for key in ('lines','net_total','vat_total','gross_total')}
    with_movement_vat(document)
    before=deepcopy(document)
    assert document['movement_vat']['rounding_adjustment']==expected_adjustment
    assert document['movement_vat']['line_vat_total']==subtotal_vat
    assert document['movement_vat']['line_gross_total']==subtotal_gross
    result=render_invoice(document,tmp_path,compiler)
    pdf,text=pdf_text(result)
    assert len(pdf.pages)==2
    detail=pdf.pages[1].extract_text()
    words=' '.join(detail.split())
    for label in ('Net (GBP)','VAT %','VAT (GBP)','Gross (GBP)','Movement subtotal','Reconciled invoice totals'):
        assert label in words
    assert 'breakdown of the same monthly charge' in words
    assert 'Invoice totals are unchanged' in words
    assert ('Invoice VAT rounding adjustment' in detail)==(expected_adjustment!='0.00')
    context=_validated_context(document)
    if expected_adjustment!='0.00':
        signed=format(Decimal(expected_adjustment),'+.2f')
        assert detail.count(signed)==2 and context['movement_vat_adjustment']==signed
    for row,rendered in zip(document['movement_detail'],context['movement_rows']):
        assert rendered['vat_rate']=='20'
        assert rendered['vat_amount']==row['vat_amount'] and rendered['gross_amount']==row['gross_amount']
        assert ''.join(detail.split()).count(row['external_id'])==1
    # Full rendered table cells contain exact independently computed values.
    tex=Path(result['tex_path']).read_text()
    for row in document['movement_detail']:
        expected=f"\\fitnum{{19mm}}{{{row['amount']}}} & 20 & \\fitnum{{19mm}}{{{row['vat_amount']}}} & \\fitnum{{19mm}}{{{row['gross_amount']}}}"
        assert expected in tex
    assert {key:document[key] for key in original_accounting}==original_accounting
    assert json.loads(Path(result['json_path']).read_text())==before and document==before
    log=(Path(result['pdf_path']).parent/'invoice.log').read_text()
    assert 'Overfull \\hbox' not in log and 'Overfull \\vbox' not in log


def test_vat_detail_paginates_with_all_source_text_headers_and_no_repeated_financial_rows(tmp_path,compiler):
    document=with_movement_vat(itemized_document(60))
    document['movement_detail'][0].update(description='R&D 50% '+('Full service narrative. '*40),
        external_id='EXTERNAL_'+('W'*150),supplier_id='SUP_'+('W'*150))
    result=render_invoice(document,tmp_path,compiler)
    pdf,text=pdf_text(result)
    assert len(pdf.pages)>=4
    compact=''.join(text.split())
    for row in document['movement_detail'][1:]:
        assert compact.count(row['external_id'])==1 and compact.count(row['supplier_id'])==1
    assert text.count('Movement subtotal')==1 and text.count('Reconciled invoice totals')==1
    for number,page in enumerate(pdf.pages,start=1):
        words=page.extract_text()
        assert f'Page {number}' in words and 'SYNTHETIC DEMO / NOT A TAX INVOICE' in words
        if number>1 and ('Ref:' in words or 'continued' in words):
            assert all(label in words for label in ('Net (GBP)','VAT (GBP)','Gross (GBP)'))
    context=_validated_context(document)
    first=[row for row in context['movement_rows'] if row['row_no']==1]
    for key in ('description','external_id','supplier_id'):
        assert ''.join(row[key] for row in first)==document['movement_detail'][0][key]
    assert sum(not row['continuation'] for row in context['movement_rows'])==60
    assert json.loads(Path(result['json_path']).read_text())==document
    log=(Path(result['pdf_path']).parent/'invoice.log').read_text()
    assert 'Overfull \\hbox' not in log and 'Overfull \\vbox' not in log


@pytest.mark.parametrize('mutation',[
    lambda d:d.pop('movement_vat'),
    lambda d:d.update(movement_vat=None),
    lambda d:d['movement_vat'].update(policy='arbitrary-policy'),
    lambda d:d['movement_vat'].pop('rate'),
    lambda d:d['movement_vat'].update(rate='0.05'),
    lambda d:d['movement_detail'][0].pop('vat_rate'),
    lambda d:d['movement_detail'][0].pop('vat_amount'),
    lambda d:d['movement_detail'][0].pop('gross_amount'),
    lambda d:d['movement_detail'][0].update(vat_rate='0.05'),
    lambda d:d['movement_detail'][0].update(vat_amount='0.00'),
    lambda d:d['movement_detail'][0].update(vat_amount='0.001'),
    lambda d:d['movement_detail'][0].update(vat_amount=0.01),
    lambda d:d['movement_detail'][0].update(gross_amount='0.03'),
    lambda d:d['movement_vat'].update(line_vat_total='0.02'),
    lambda d:d['movement_vat'].update(line_gross_total='0.11'),
    lambda d:d['movement_vat'].pop('rounding_adjustment'),
    lambda d:d['movement_vat'].update(rounding_adjustment='0.01'),
    lambda d:d['movement_vat'].update(rounding_adjustment='0.00'),
    lambda d:d['movement_vat'].update(rounding_adjustment='-0.010'),
    lambda d:d['movement_vat'].update(rounding_adjustment=-0.01),
])
def test_partial_or_tampered_movement_vat_rejected_before_render(tmp_path,mutation):
    document=with_movement_vat(itemized_document(3,['0.03']*3));mutation(document)
    with pytest.raises(InvoiceValidationError):render_invoice(document,tmp_path/'uncreated','compiler-must-not-run')
    assert not (tmp_path/'uncreated').exists()


def test_zero_rate_is_explicit_and_never_inferred_from_purchase_sources(tmp_path,compiler):
    document=itemized_document(1,['1.05'])
    document.update(vat_enabled=False,vat_total='0.00',gross_total='1.05')
    document['seller']['vat_number']=''
    document['lines'][0].update(vat_rate='0.00',vat_amount='0.00',gross_amount='1.05')
    with_movement_vat(document,rate='0.00')
    result=render_invoice(document,tmp_path,compiler)
    _,text=pdf_text(result)
    assert 'VAT INVOICE' not in text and 'Invoice VAT rounding adjustment' not in text
    assert 'No VAT is charged because the supplier is not VAT registered' in ' '.join(text.split())
    assert 'declared 0% rate' not in text
    assert _validated_context(document)['movement_rows'][0]['vat_amount']=='0.00'


def test_movement_policy_rejects_accounting_line_vat_that_differs_from_invoice_level_rounding(tmp_path):
    document=itemized_document(3,['0.03']*3)
    document['lines']=[dict(description='Separately rounded accounting line',quantity='1',unit_price='0.03',
        net_amount='0.03',vat_rate='0.20',vat_amount='0.01',gross_amount='0.04') for _ in range(3)]
    document.update(vat_total='0.03',gross_total='0.12')
    # This is legitimate historical accounting, but not the newly declared
    # invoice-level rounding policy: HALF_UP(0.09 * 0.20) is 0.02.
    assert _validated_context(document)['totals']['vat']=='0.03'
    with_movement_vat(document)
    with pytest.raises(InvoiceValidationError,match='invoice-level HALF_UP'):
        render_invoice(document,tmp_path/'uncreated','compiler-must-not-run')
    assert not (tmp_path/'uncreated').exists()


def test_movement_vat_exact_half_penny_rounds_up_at_explicit_rate():
    document=itemized_document(1,['0.10'])
    document.update(vat_total='0.01',gross_total='0.11')
    document['lines'][0].update(vat_rate='0.05',vat_amount='0.01',gross_amount='0.11')
    with_movement_vat(document,rate='0.05')
    context=_validated_context(document)
    assert context['movement_rows'][0]['vat_amount']=='0.01'
    assert context['movement_rows'][0]['vat_rate']=='5'
    document['movement_detail'][0].update(vat_amount='0.00',gross_amount='0.10')
    with pytest.raises(InvoiceValidationError,match='HALF_UP penny rounding'):_validated_context(document)


def test_complete_detail_is_in_same_pdf_and_all_accounting_values_stay_unchanged(tmp_path,compiler):
    document=itemized_document(amounts=['0.03']*3)
    before=deepcopy(document)
    result=render_invoice(document,tmp_path,compiler)
    pdf,text=pdf_text(result)
    assert len(pdf.pages)==2 and 'ITEMISED MOVEMENT DETAIL' in pdf.pages[1].extract_text()
    assert text.count('Amount due')==1 and text.count('VAT SUMMARY')==1
    assert document['net_total']=='0.09' and document['vat_total']=='0.02' and document['gross_total']=='0.11'
    summary=pdf.pages[0].extract_text()
    assert 'Itemised movement detail: 3 movements, listed on the following pages.' in summary
    assert 'GBP 0.11' in summary and 'GBP 0.02' in summary and 'GBP 0.09' in summary
    detail=pdf.pages[1].extract_text()
    assert detail.count('0.03')==3
    assert 'VAT %' not in detail and 'VAT (GBP)' not in detail
    compact=''.join(text.split())
    for item in document['movement_detail']:
        assert compact.count(item['external_id'])==1
        assert compact.count(item['supplier_id'])==1
        assert ''.join(item['description'].split()) in compact
    assert json.loads(Path(result['json_path']).read_text())==before and document==before
    original_bytes=Path(result['pdf_path']).read_bytes()
    assert render_invoice(document,tmp_path,compiler)==result
    assert Path(result['pdf_path']).read_bytes()==original_bytes


def test_multi_page_detail_has_every_movement_repeated_headers_and_page_numbers(tmp_path,compiler):
    document=itemized_document(count=150)
    result=render_invoice(document,tmp_path,compiler)
    pdf,text=pdf_text(result)
    assert len(pdf.pages)>=5
    compact=''.join(text.split())
    for item in document['movement_detail']:
        assert compact.count(item['external_id'])==1
        assert compact.count(item['supplier_id'])==1
    for number,page in enumerate(pdf.pages,start=1):
        words=page.extract_text()
        assert f'Page {number}' in words
        assert 'DEMO-2026-000001' in words and 'SYNTHETIC DEMO / NOT A TAX INVOICE' in words
        if number>=2:
            assert 'Movement reference' in words
            assert 'Net (GBP)' in words and 'Supplier' in words
    assert text.count('Amount due')==1
    log=(Path(result['pdf_path']).parent/'invoice.log').read_text()
    assert 'Overfull \\hbox' not in log and 'Overfull \\vbox' not in log


def test_all_long_text_is_preserved_escaped_and_continued_without_repeating_amount(tmp_path,compiler):
    document=itemized_document(1,amounts=['123.45'])
    row=document['movement_detail'][0]
    row.update(description=(r'R&D 50% _discount_ #42 $5 {braces} ~tilde ^caret \input{secret.txt} '+('Full service narrative. '*38)),
        external_id='EXTERNAL_'+('W'*150),supplier_id='SUP_'+('W'*150),
        event_at='2026-09-02T00:45:12.123456+01:00')
    result=render_invoice(document,tmp_path,compiler)
    pdf,text=pdf_text(result)
    detail='\n'.join(page.extract_text() for page in pdf.pages[1:])
    assert detail.count('123.45')==1
    assert '(continued)' in detail and 'secret.txt' in detail and '50%' in detail
    assert '2026-09-01' in ''.join(detail.split())
    assert '23:45:12.123456' in ''.join(detail.split())
    # Extract each PDF column separately: reading order interleaves columns when
    # an identifier continues on another physical row. Compare every character,
    # including escaped LaTeX syntax, without assuming a single unbroken line.
    def column_text(left,right):
        parts=[]
        def collect(value,cm,tm,font,size):
            if left<=tm[4]<right and 50<tm[5]<670:parts.append(value)
        for page in pdf.pages[1:]:page.extract_text(visitor_text=collect)
        return ''.join(''.join(parts).replace('(continued)','').split())
    assert column_text(150,240)==''.join(row['external_id'].split())
    assert column_text(420,500)==''.join(row['supplier_id'].split())
    assert column_text(244,420)==''.join(row['description'].split())
    tex=Path(result['tex_path']).read_text()
    assert r'\input{secret.txt}' not in tex
    assert r'\textbackslash{}input\{secret.txt\}' in tex.replace(r'\hspace{0pt}','')
    assert json.loads(Path(result['json_path']).read_text())==document
    log=(Path(result['pdf_path']).parent/'invoice.log').read_text()
    assert 'Overfull \\hbox' not in log and 'Overfull \\vbox' not in log


def test_many_explicit_newlines_cannot_overflow_a_single_unsplittable_cell(tmp_path,compiler):
    document=itemized_document(1,amounts=['34.56'])
    row=document['movement_detail'][0]
    row.update(description='\n'.join(f'Text part {index:03d}' for index in range(60)),
               external_id='\n'.join('ID'+str(index) for index in range(25)),
               supplier_id='\n'.join('S'+str(index) for index in range(25)))
    result=render_invoice(document,tmp_path,compiler)
    pdf,text=pdf_text(result)
    assert all(f'Text part {index:03d}' in text for index in range(60))
    assert text.count('34.56')>=1
    log=(Path(result['pdf_path']).parent/'invoice.log').read_text()
    assert 'Overfull \\hbox' not in log and 'Overfull \\vbox' not in log


@pytest.mark.parametrize('mutation',[
    lambda d:d.update(line_format='OTHER'),
    lambda d:d.update(movement_count=None),
    lambda d:d.update(movement_count=True),
    lambda d:d.update(movement_count=2),
    lambda d:d.update(movement_detail=[]),
    lambda d:d['movement_detail'][1].update(row_no=1),
    lambda d:d['movement_detail'][0].update(row_no=True),
    lambda d:d['movement_detail'][1].update(version_id=d['movement_detail'][0]['version_id']),
    lambda d:d['movement_detail'][1].update(movement_id=d['movement_detail'][0]['movement_id']),
    lambda d:d['movement_detail'][0].update(amount='0.02'),
    lambda d:d['movement_detail'][0].update(amount=0.01),
    lambda d:d['movement_detail'][0].update(amount='0.010'),
    lambda d:d['movement_detail'][0].update(currency='EUR'),
    lambda d:d['movement_detail'][0].update(event_at='2026-09-02T10:20:30'),
    lambda d:d['movement_detail'][0].update(event_at='2026-09-01T00:30:00+01:00'),
    lambda d:d['movement_detail'][0].update(event_at='not-a-date'),
    lambda d:d['movement_detail'][0].update(description=''),
    lambda d:d['movement_detail'][0].update(supplier_id=''),
    lambda d:d['movement_detail'][0].update(external_id=''),
])
def test_incomplete_or_changed_detail_is_rejected_before_compiler(tmp_path,mutation):
    document=itemized_document();mutation(document)
    with pytest.raises(InvoiceValidationError):render_invoice(document,tmp_path/'uncreated','compiler-must-not-run')
    assert not (tmp_path/'uncreated').exists()


def test_explicit_cap_is_complete_or_rejected_never_truncated(tmp_path):
    document=itemized_document(MAX_MOVEMENT_DETAILS)
    context=_validated_context(document)
    assert context['movement_count']==MAX_MOVEMENT_DETAILS
    assert len(context['movement_rows'])==MAX_MOVEMENT_DETAILS
    document['movement_detail'].append(deepcopy(document['movement_detail'][-1]))
    document['movement_count']+=1
    with pytest.raises(InvoiceValidationError,match='10000.*truncation'):
        render_invoice(document,tmp_path/'uncreated','compiler-must-not-run')
    assert not (tmp_path/'uncreated').exists()


def test_legacy_summary_and_explicit_summary_stay_without_item_detail(tmp_path,compiler):
    legacy=sample_document()
    first=render_invoice(legacy,tmp_path,compiler)
    pdf,text=pdf_text(first)
    assert len(pdf.pages)==1 and 'ITEMISED MOVEMENT DETAIL' not in text
    summary={**legacy,'line_format':'SUMMARY','movement_count':1200}
    result=render_invoice(summary,tmp_path,compiler)
    pdf,text=pdf_text(result)
    assert len(pdf.pages)==1 and 'ITEMISED MOVEMENT DETAIL' not in text
    # A supplied detail array may not silently disappear under SUMMARY mode.
    summary['movement_detail']=[]
    with pytest.raises(InvoiceValidationError,match='silently omitted'):
        render_invoice(summary,tmp_path/'uncreated','compiler-must-not-run')
