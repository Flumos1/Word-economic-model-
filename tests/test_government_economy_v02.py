from dataclasses import dataclass, field

@dataclass
class Ledger:
    """Signed financial positions: assets +, liabilities -. Not a full accrual ledger."""
    balances: dict = field(default_factory=dict)
    entries: list = field(default_factory=list)

    def post(self, name, postings):
        assert sum(postings.values()) == 0, (name, postings)
        new = self.balances.copy()
        for account, amount in postings.items():
            new[account] = new.get(account, 0) + amount
        self.balances = new
        self.entries.append((name, postings.copy()))

    def bal(self, sector, account):
        return self.balances.get((sector, account), 0)

    def check(self):
        assert sum(self.balances.values()) == 0
        for name, postings in self.entries:
            assert sum(postings.values()) == 0, name


def scenario(sales=70, tax=30, transfer=10, public_purchase=15,
             interest=5, government_borrowing=0):
    """One country, one good, price=1; no intermediates or product taxes.

    The government tax is a tax on firm income, not a tax on products.
    All flows are nominal integer amounts in one period.
    """
    l = Ledger()

    def transfer_cash(name, payer, receiver, n):
        assert n >= 0 and l.bal(payer, 'cash') >= n
        l.post(name, {(payer, 'cash'): -n, (receiver, 'cash'): n})

    l.post('bank loan', {
        ('bank', 'loan_asset'): 100, ('firm', 'loan_liability'): -100,
        ('firm', 'cash'): 100, ('bank', 'deposit_liability'): -100
    })
    if government_borrowing:
        assert government_borrowing > 0
        l.post('government borrowing', {
            ('bank', 'government_bond_asset'): government_borrowing,
            ('government', 'bond_liability'): -government_borrowing,
            ('government', 'cash'): government_borrowing,
            ('bank', 'deposit_liability'): -government_borrowing
        })

    transfer_cash('wages', 'firm', 'household', 100)
    assert 0 <= sales <= 100
    transfer_cash('household consumption', 'household', 'firm', sales)
    assert tax >= 0 and transfer >= 0 and public_purchase >= 0
    transfer_cash('firm income tax', 'firm', 'government', tax)
    transfer_cash('government transfer', 'government', 'household', transfer)
    assert sales + public_purchase <= 100
    transfer_cash('government purchase', 'government', 'firm', public_purchase)

    if interest:
        assert interest >= 0 and l.bal('firm', 'cash') >= interest
        l.post('interest payment', {
            ('firm', 'cash'): -interest,
            ('bank', 'deposit_liability'): interest
        })

    repay = min(100, l.bal('firm', 'cash'))
    if repay:
        l.post('loan repayment', {
            ('firm', 'cash'): -repay, ('bank', 'deposit_liability'): repay,
            ('bank', 'loan_asset'): -repay, ('firm', 'loan_liability'): repay
        })

    l.check()
    inventory = 100 - sales - public_purchase
    assert inventory >= 0
    # GDP by production: output 100; by expenditure: consumption + government
    # final purchases + inventory investment. The wages and consolidated
    # operating surplus (including bank interest income) sum to 100.
    assert 100 == sales + public_purchase + inventory
    assert l.bal('government', 'cash') == (
        government_borrowing + tax - transfer - public_purchase)
    assert l.bal('bank', 'loan_asset') == -l.bal('firm', 'loan_liability')
    assert l.bal('bank', 'government_bond_asset') == -l.bal('government', 'bond_liability')
    assert -l.bal('bank', 'deposit_liability') == sum(
        l.bal(s, 'cash') for s in ('firm', 'household', 'government'))
    assert (l.bal('bank', 'loan_asset') + l.bal('bank', 'government_bond_asset')
            == -l.bal('bank', 'deposit_liability') + interest)
    return l, inventory, repay


def test_full():
    l, inventory, repay = scenario(sales=70, tax=30, transfer=10,
                                    public_purchase=15, interest=5)
    assert inventory == 15 and repay == 50
    assert l.bal('government', 'cash') == 5


def test_inventory():
    l, inventory, repay = scenario(sales=60, tax=25, transfer=5,
                                    public_purchase=10, interest=0)
    assert inventory == 30 and repay == 45


def test_government_debt():
    l, inventory, repay = scenario(sales=70, tax=5, transfer=10,
                                    public_purchase=15, interest=0,
                                    government_borrowing=20)
    assert l.bal('government', 'cash') == 0
    assert l.bal('government', 'bond_liability') == -20


def test_impossible_purchase():
    try:
        scenario(sales=90, tax=10, transfer=10, public_purchase=15)
    except AssertionError:
        return
    raise AssertionError('overselling was not rejected')


def test_unbalanced_entry():
    try:
        Ledger().post('unbalanced', {('bank', 'cash'): 1})
    except AssertionError:
        return
    raise AssertionError('unbalanced entry was not rejected')


if __name__ == '__main__':
    for test in (test_full, test_inventory, test_government_debt,
                 test_impossible_purchase, test_unbalanced_entry):
        test()
        print('PASS', test.__name__)
