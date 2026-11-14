Synthetic MRIO fixtures for tests/test_mrio_data.py (AMFE P2 data track, docs/121).
NOT third-party data: every number is invented for the tests and internally consistent.

good/         2 countries x 3 industries, 2020, USD_mn; row balance x = Z*1 + y,
              column balance x = 1'*Z + va, sum(va) = sum(y) = 120; P2 scale gate FAILS
              on purpose (2x3) -> tests pass --no-p2-gate / enforce_p2_scale=False.
bad_balance/  same table with x[0] = 50 -> accounting identities violated.
bad_negative/ Z[0][0] = -5 -> non-negativity violated (schema error).
bad_shape/    Z has 5 rows instead of 6 -> dimension error (schema error).
bad_units/    units = EUR_mn -> the canonical base is USD_mn (schema error).
bad_duplicate/ countries = ['AA','AA'] -> duplicate labels (schema error).

The 10+ x 30+ P2-scale fixture is generated programmatically in the test suite
(tests/test_mrio_data.py::p2_scale_document) to keep the repository small.
