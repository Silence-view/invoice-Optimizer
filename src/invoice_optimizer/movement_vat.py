"""Exact per-movement VAT presentation with an explicit rounding reconciliation.

The original invoice remains the accounting authority. Independently rounding
each displayed movement can differ from rounding VAT once on the invoice net.
The difference is reported explicitly, never assigned invisibly to a movement.
"""
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP, localcontext

MOVEMENT_VAT_POLICY = 'line_half_up_reconciled_v1'
MAX_MOVEMENT_VAT_ROWS = 10000
PENNY = Decimal('0.01')
MAX_AMOUNT = Decimal('999999999999999999.99')


def _decimal(value, name, *, money=False):
    if not isinstance(value, (str, Decimal)):
        raise ValueError(name + ' must be an exact decimal string or Decimal; floats are not accepted')
    try:
        number = Decimal(value)
    except InvalidOperation:
        raise ValueError(name + ' must be a finite decimal') from None
    if not number.is_finite() or number < 0 or number > MAX_AMOUNT:
        raise ValueError(name + ' must be a bounded, nonnegative finite decimal')
    if money and number.as_tuple().exponent < -2:
        raise ValueError(name + ' must have no more than two decimal places')
    return number.copy_abs() if number == 0 else number


def calculate_movement_vat(amounts, *, invoice_net_total, invoice_vat_total,
                           invoice_gross_total, rate='0.20'):
    """Return ``(per_row_vat_fields, reconciliation)`` without mutating inputs.

    All sums and arithmetic use Decimal precision 64 and explicit HALF_UP penny
    rounding. A bounded list/tuple is required so a caller cannot accidentally
    materialize an unbounded stream. Mixed rates are outside this contract.
    """
    if not isinstance(amounts, (list, tuple)) or not 1 <= len(amounts) <= MAX_MOVEMENT_VAT_ROWS:
        raise ValueError(f'Movement VAT requires 1 to {MAX_MOVEMENT_VAT_ROWS} amounts; truncation is not allowed')
    with localcontext() as context:
        context.prec = 64
        context.rounding = ROUND_HALF_UP
        vat_rate = _decimal(rate, 'rate')
        if vat_rate > 1 or vat_rate.as_tuple().exponent < -6:
            raise ValueError('rate must be from zero to one with at most six decimal places')
        net = _decimal(invoice_net_total, 'invoice_net_total', money=True)
        vat = _decimal(invoice_vat_total, 'invoice_vat_total', money=True)
        gross = _decimal(invoice_gross_total, 'invoice_gross_total', money=True)
        if (net * vat_rate).quantize(PENNY, rounding=ROUND_HALF_UP) != vat:
            raise ValueError('Invoice VAT does not match its unchanged invoice-level HALF_UP policy')
        if net + vat != gross:
            raise ValueError('Invoice net plus VAT does not match its unchanged gross total')
        rows = []
        net_sum = Decimal('0.00')
        vat_sum = Decimal('0.00')
        gross_sum = Decimal('0.00')
        rate_text = format(vat_rate, 'f')
        for index, value in enumerate(amounts):
            amount = _decimal(value, f'amounts[{index}]', money=True)
            row_vat = (amount * vat_rate).quantize(PENNY, rounding=ROUND_HALF_UP)
            row_gross = amount + row_vat
            rows.append({'vat_rate': rate_text, 'vat_amount': format(row_vat, '.2f'),
                         'gross_amount': format(row_gross, '.2f')})
            net_sum += amount
            vat_sum += row_vat
            gross_sum += row_gross
        if net_sum != net:
            raise ValueError('Movement net amounts must exactly cover the unchanged invoice net total')
        adjustment = vat - vat_sum
        if gross_sum + adjustment != gross:
            raise ValueError('Movement VAT reconciliation does not match the unchanged invoice gross total')
        return rows, {'policy': MOVEMENT_VAT_POLICY, 'rate': rate_text,
                      'line_vat_total': format(vat_sum, '.2f'),
                      'line_gross_total': format(gross_sum, '.2f'),
                      'rounding_adjustment': format(adjustment, '.2f')}
