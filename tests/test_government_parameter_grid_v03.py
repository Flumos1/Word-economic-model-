"""Deterministic 10,000-case regression of the existing four-sector toy model.

This does not reproduce the previously reported 10,626-case experimental grid.
It independently verifies a documented, reproducible grid with the code in GitHub.
"""
from test_government_economy_v02 import scenario


def run():
    checked = 0
    for sales in range(50, 90):  # 40 values
        for tax in range(25):  # 25 values, always <= sales
            for public_purchase in range(10):  # 10 values
                transfer = tax // 2
                borrowing = max(0, transfer + public_purchase - tax)
                ledger, inventory, repaid = scenario(
                    sales=sales,
                    tax=tax,
                    transfer=transfer,
                    public_purchase=public_purchase,
                    interest=0,
                    government_borrowing=borrowing,
                )
                assert inventory == 100 - sales - public_purchase
                assert 0 <= repaid <= 100
                assert ledger.bal("government", "cash") >= 0
                checked += 1
    assert checked == 10000
    print(f"PASS: {checked} reproducible four-sector parameter combinations")


if __name__ == "__main__":
    run()
