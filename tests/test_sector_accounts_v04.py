"""Four-sector signed-position accounts with explicit net worth; Decimal toy model."""
from dataclasses import dataclass, field
from decimal import Decimal
D = lambda x: Decimal(str(x))
SECTORS = ("firm", "household", "bank", "government")

@dataclass
class Economy:
    positions: dict = field(default_factory=dict)
    inventory: Decimal = D(0)
    produced: Decimal = D(0)
    household_consumed: Decimal = D(0)
    government_consumed: Decimal = D(0)
    history: list = field(default_factory=list)

    def get(self, sector, account):
        return self.positions.get((sector, account), D(0))

    def worth(self, sector):
        financial = sum((v for (s, _), v in self.positions.items() if s == sector), D(0))
        return financial + (self.inventory if sector == "firm" else D(0))

    def check(self):
        assert sum(self.positions.values(), D(0)) == 0
        assert self.get("bank", "loan") == -self.get("firm", "debt")
        assert self.get("bank", "bond") == -self.get("government", "bond_debt")
        assert self.get("bank", "deposit_liability") == -sum(
            (self.get(s, "deposit") for s in SECTORS if s != "bank"), D(0))
        assert self.inventory >= 0
        assert self.inventory == self.produced - self.household_consumed - self.government_consumed
        assert all(self.get(s, "deposit") >= 0 for s in ("firm", "household", "government"))
        assert sum((self.worth(s) for s in SECTORS), D(0)) == self.inventory

    def post(self, name, changes, *, inventory=0, output=0, household_use=0, government_use=0):
        changes = {k: D(v) for k, v in changes.items()}
        if sum(changes.values(), D(0)) != 0:
            raise ValueError("unbalanced postings")
        old = (self.positions.copy(), self.inventory, self.produced,
               self.household_consumed, self.government_consumed)
        try:
            for k, v in changes.items():
                self.positions[k] = self.positions.get(k, D(0)) + v
            self.inventory += D(inventory)
            self.produced += D(output)
            self.household_consumed += D(household_use)
            self.government_consumed += D(government_use)
            self.check()
        except Exception:
            (self.positions, self.inventory, self.produced,
             self.household_consumed, self.government_consumed) = old
            raise
        self.history.append(name)

    def loan(self, n):
        n = D(n)
        assert n >= 0
        self.post("loan", {("bank","loan"):n, ("firm","debt"):-n,
                           ("firm","deposit"):n, ("bank","deposit_liability"):-n})

    def produce(self, n):
        n = D(n)
        assert n >= 0
        self.post("production", {}, inventory=n, output=n)

    def transfer(self, name, payer, receiver, n):
        n = D(n)
        assert n >= 0 and payer != receiver
        self.post(name, {(payer,"deposit"):-n, (receiver,"deposit"):n})

    def buy(self, buyer, n):
        n = D(n)
        assert buyer in ("household", "government") and n >= 0
        self.post("final consumption", {(buyer,"deposit"):-n, ("firm","deposit"):n},
                  inventory=-n, household_use=n if buyer == "household" else 0,
                  government_use=n if buyer == "government" else 0)

    def bond(self, n):
        n = D(n)
        assert n >= 0
        self.post("bond issuance", {("bank","bond"):n, ("government","bond_debt"):-n,
                                    ("government","deposit"):n,
                                    ("bank","deposit_liability"):-n})

    def interest(self, n):
        n = D(n)
        assert n >= 0
        self.post("interest", {("firm","deposit"):-n,
                               ("bank","deposit_liability"):n})

    def repay(self, n):
        n = D(n)
        assert 0 <= n <= self.get("bank", "loan")
        self.post("repayment", {("firm","deposit"):-n,
                                ("bank","deposit_liability"):n,
                                ("bank","loan"):-n, ("firm","debt"):n})

def test_baseline():
    e = Economy()
    e.loan(100); e.produce(100)
    e.transfer("wages","firm","household",100)
    e.buy("household",70)
    e.transfer("direct levy","firm","government",30)
    e.transfer("social transfer","government","household",10)
    e.buy("government",15); e.interest(5); e.repay(50)
    assert e.inventory == 15
    assert {s:e.worth(s) for s in SECTORS} == {
        "firm":D(-35), "household":D(40), "bank":D(5), "government":D(5)}
    assert e.get("bank","loan") == 50
    assert e.produced == e.household_consumed + e.government_consumed + e.inventory

def test_debt_financed_deficit():
    e = Economy()
    e.loan(100); e.bond(20); e.produce(100)
    e.transfer("wages","firm","household",100)
    e.buy("household",70)
    e.transfer("levy","firm","government",5)
    e.transfer("transfer","government","household",10)
    e.buy("government",15); e.repay(80)
    assert e.get("government","deposit") == 0
    assert e.get("government","bond_debt") == -20
    assert sum((e.worth(s) for s in SECTORS), D(0)) == 15

def test_atomic_rollback():
    e = Economy(); e.loan(100); e.produce(100)
    e.transfer("wages","firm","household",100)
    old = (e.positions.copy(), e.inventory, e.produced, list(e.history))
    try: e.buy("household",101)
    except AssertionError: pass
    else: raise AssertionError("overspending accepted")
    assert old == (e.positions, e.inventory, e.produced, e.history)

def test_unbalanced_rejected():
    e = Economy()
    try: e.post("bad", {("firm","deposit"):1})
    except ValueError: pass
    else: raise AssertionError("unbalanced posting accepted")
    assert not e.positions and not e.history

def test_decimal():
    e = Economy(); e.loan("10.25"); e.produce("10.25")
    e.transfer("wages","firm","household","10.25")
    e.buy("household","4.10")
    assert e.inventory == D("6.15")
    assert sum((e.worth(s) for s in SECTORS), D(0)) == D("6.15")

if __name__ == "__main__":
    for test in (test_baseline, test_debt_financed_deficit,
                 test_atomic_rollback, test_unbalanced_rejected, test_decimal):
        test()
        print("PASS", test.__name__)
