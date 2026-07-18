# Parity oracle

The oracle is the independent expected-results implementation used by M4 to
certify dashboard parity (governing contract, "Parity oracle" section).

**Independence boundary (frozen):** oracle queries may read the
BQAA-generated fixture views and independently translate the pinned LookML
measure/filter/join semantics, but they must NOT import or render
`events_v1.sql.tmpl`, reuse the dashboard's calculated fields, or share the
production transformation implementation. A translation defect must not be
able to appear identically in the implementation and its expected results.

Deliverables (M0, per the bootstrap contract):

- `queries/<chart-id>.sql` — one versioned query per manifest chart ID
  (37 total), written against the generated fixture views directly;
- `expected/<scenario>/<chart-id>.json` — deterministic expected result sets
  per seeded scenario (produced with `tools/seed_events.py` seeds);
- `runner.py` — executes every oracle query against each applicable frozen
  fixture profile and compares report results: exact for counts/distinct
  counts/sums/averages, declared tolerance for approximate percentiles;
- coverage of tile filters, sorting, limits, current/previous periods, and
  the frozen `call_row_policy`.

Status: 37 queries generated and mapped via spec/overrides.yaml; expected results + cross-profile compare receipts are produced under the enforced-provenance runner across three scenarios (base, filtered f1, trace/span-filtered f2) and validated end-to-end by tools/validate_evidence.py in CI. Remaining: independent human review of the 37 translations against the pinned LookML.