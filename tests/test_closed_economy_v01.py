from dataclasses import dataclass

@dataclass
class Economy:
    bank_loans: int = 0
    bank_deposits: int = 0
    firm_deposit: int = 0
    household_deposit: int = 0
    firm_debt: int = 0
    wage_income: int = 0
    firm_wage_expense: int = 0
    firm_sales: int = 0
    household_consumption: int = 0
    output: int = 0
    inventory: int = 0

    def check(self):
        assert self.bank_loans == self.firm_debt, 'Loan asset/liability mismatch'
        assert self.bank_deposits == self.firm_deposit + self.household_deposit, 'Deposit mismatch'
        assert self.bank_loans == self.bank_deposits, 'Bank balance sheet mismatch'
        assert self.firm_deposit >= 0 and self.household_deposit >= 0
        assert self.inventory == self.output - self.firm_sales
        assert self.inventory >= 0

    def loan(self, n):
        assert n >= 0
        self.bank_loans += n; self.firm_debt += n
        self.bank_deposits += n; self.firm_deposit += n
        self.check()

    def produce(self, n):
        assert n >= 0
        self.output += n; self.inventory += n
        self.check()

    def wages(self, n):
        assert 0 <= n <= self.firm_deposit
        self.firm_deposit -= n; self.household_deposit += n
        self.wage_income += n; self.firm_wage_expense += n
        self.check()

    def buy(self, n):
        assert 0 <= n <= self.household_deposit and n <= self.inventory
        self.household_deposit -= n; self.firm_deposit += n
        self.household_consumption += n; self.firm_sales += n
        self.inventory -= n
        self.check()

    def repay(self, n):
        assert 0 <= n <= self.firm_debt and n <= self.firm_deposit
        self.firm_deposit -= n; self.bank_deposits -= n
        self.firm_debt -= n; self.bank_loans -= n
        self.check()

    def close(self):
        assert self.output == self.household_consumption + self.inventory
        gdp_production = self.output
        gdp_income = self.wage_income + (self.firm_sales - self.firm_wage_expense + self.inventory)
        gdp_expenditure = self.household_consumption + self.inventory
        assert gdp_production == gdp_income == gdp_expenditure
        return gdp_production, gdp_income, gdp_expenditure

def run():
    e=Economy(); e.check()
    e.loan(100); e.produce(100); e.wages(100); e.buy(100); e.repay(100)
    assert e.close() == (100,100,100)
    assert (e.bank_loans,e.bank_deposits,e.firm_deposit,e.household_deposit,e.firm_debt,e.inventory)==(0,)*6
    print('PASS: full production/income/expenditure and double-entry circuit')
    e=Economy(); e.loan(100); e.produce(100); e.wages(100); e.buy(90)
    assert e.close() == (100,100,100) and e.inventory == 10
    print('PASS: 10 units unsold are inventory investment')
    e=Economy(); e.loan(100); e.produce(100); e.wages(100)
    e.bank_deposits += 1
    try: e.check()
    except AssertionError: print('PASS: unpaired financial entry rejected')
    else: raise AssertionError('missing counterpart undetected')
    e=Economy(); e.loan(100); e.produce(100); e.wages(100)
    try: e.buy(101)
    except AssertionError: print('PASS: impossible purchase rejected')
    else: raise AssertionError('overspending undetected')

if __name__=='__main__': run()
