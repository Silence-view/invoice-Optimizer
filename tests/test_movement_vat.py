"""Integer-penny references and edge cases for explicit per-movement VAT."""
from decimal import Decimal, localcontext

import pytest

from invoice_optimizer.movement_vat import MOVEMENT_VAT_POLICY, calculate_movement_vat


def _money(pennies):
    return f'{pennies // 100}.{pennies % 100:02d}'


@pytest.mark.parametrize('amounts,net,vat,gross,line_vat,line_gross,adjustment', [
    (['0.03','0.03'], '0.06','0.01','0.07','0.02','0.08','-0.01'),
    (['0.02','0.02'], '0.04','0.01','0.05','0.00','0.04','0.01'),
    (['10.10','20.20'], '30.30','6.06','36.36','6.06','36.36','0.00'),
    (['0.00'], '0.00','0.00','0.00','0.00','0.00','0.00'),
])
def test_positive_negative_and_zero_adjustments_are_explicit(amounts, net, vat, gross, line_vat, line_gross, adjustment):
    original = list(amounts)
    rows, receipt = calculate_movement_vat(amounts, invoice_net_total=net, invoice_vat_total=vat, invoice_gross_total=gross)
    assert amounts == original
    assert receipt == {'policy':MOVEMENT_VAT_POLICY, 'rate':'0.20', 'line_vat_total':line_vat,
                       'line_gross_total':line_gross, 'rounding_adjustment':adjustment}
    assert sum(Decimal(row['vat_amount']) for row in rows) + Decimal(adjustment) == Decimal(vat)
    assert sum(Decimal(row['gross_amount']) for row in rows) + Decimal(adjustment) == Decimal(gross)
    assert all(Decimal(amount) + Decimal(row['vat_amount']) == Decimal(row['gross_amount'])
               for amount, row in zip(amounts, rows))


@pytest.mark.parametrize('percent', [0, 5, 20, 50, 100])
def test_each_rounding_result_matches_independent_integer_penny_reference(percent):
    pennies = list(range(501))
    rate = '1.00' if percent == 100 else f'0.{percent:02d}'
    expected_line_vat = [(amount * percent + 50) // 100 for amount in pennies]
    net = sum(pennies)
    vat = (net * percent + 50) // 100
    rows, receipt = calculate_movement_vat([_money(amount) for amount in pennies], rate=rate,
        invoice_net_total=_money(net), invoice_vat_total=_money(vat), invoice_gross_total=_money(net + vat))
    assert [row['vat_amount'] for row in rows] == [_money(value) for value in expected_line_vat]
    assert [row['gross_amount'] for row in rows] == [_money(amount + row_vat) for amount, row_vat in zip(pennies, expected_line_vat)]
    assert Decimal(receipt['rounding_adjustment']) == Decimal(vat - sum(expected_line_vat)) / 100


def test_ten_thousand_rows_remain_exact_and_are_never_invisibly_adjusted():
    rows, receipt = calculate_movement_vat(['0.03'] * 10000,
        invoice_net_total='300.00', invoice_vat_total='60.00', invoice_gross_total='360.00')
    assert len(rows) == 10000
    assert all(row['vat_amount'] == '0.01' and row['gross_amount'] == '0.04' for row in rows)
    assert receipt['line_vat_total'] == '100.00'
    assert receipt['line_gross_total'] == '400.00'
    assert receipt['rounding_adjustment'] == '-40.00'
    with pytest.raises(ValueError, match='1 to 10000'):
        calculate_movement_vat(['not-read'] * 10001,
            invoice_net_total='0.00', invoice_vat_total='0.00', invoice_gross_total='0.00')


def test_high_precision_does_not_depend_on_global_decimal_context():
    with localcontext() as context:
        context.prec = 4
        rows, receipt = calculate_movement_vat(['49999999999999.99','49999999999999.99'],
            invoice_net_total='99999999999999.98', invoice_vat_total='20000000000000.00',
            invoice_gross_total='119999999999999.98')
    assert [row['vat_amount'] for row in rows] == ['10000000000000.00'] * 2
    assert receipt['line_gross_total'] == '119999999999999.98'
    assert receipt['rounding_adjustment'] == '0.00'


@pytest.mark.parametrize('value', [0.03, True, None, 'NaN', 'Infinity', '-0.01', '0.001', '1000000000000000000.00'])
def test_inexact_nonfinite_negative_or_overscale_money_is_rejected(value):
    with pytest.raises(ValueError):
        calculate_movement_vat([value], invoice_net_total='0.03', invoice_vat_total='0.01', invoice_gross_total='0.04')


@pytest.mark.parametrize('changes,match', [
    ({'invoice_vat_total':'0.02'}, 'invoice-level HALF_UP'),
    ({'invoice_gross_total':'0.05'}, 'gross total'),
    ({'invoice_net_total':'0.04','invoice_vat_total':'0.01','invoice_gross_total':'0.05'}, 'exactly cover'),
    ({'rate':'1.01'}, 'rate must be'),
    ({'rate':'0.2000001'}, 'rate must be'),
])
def test_invoice_or_rate_tampering_cannot_be_hidden_as_rounding(changes, match):
    args = {'invoice_net_total':'0.03', 'invoice_vat_total':'0.01', 'invoice_gross_total':'0.04', **changes}
    with pytest.raises(ValueError, match=match):
        calculate_movement_vat(['0.03'], **args)
