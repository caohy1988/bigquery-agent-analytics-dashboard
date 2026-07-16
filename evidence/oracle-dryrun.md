# Oracle dry-run compilation evidence (M0)

Date: 2026-07-16 · project: test-project-0728-467323 · tool commit: see git log

All 37 generated oracle queries (`oracle/queries/`) dry-run compile with
date parameters (`@start_date=2026-06-15`, `@end_date=2026-07-15`) against
each candidate-profile fixture dataset:

| Profile | Dataset | Result |
|---|---|---|
| ADK 1.27.0 | `bqaa_fixture_adk_1_27_0` (15 views) | 37/37 validated |
| ADK 1.36.1 | `bqaa_fixture_adk_1_36_1` (17 views) | 37/37 validated |
| ADK 2.4.0 | `bqaa_fixture_adk_2_4_0` (21 views) | 37/37 validated |

The queries reference only the 15-view static intersection, so identical
compilation across supersets is the expected contract behavior — now
observed, not assumed. Expected result sets per seeded scenario and the
independent review pass are the remaining oracle deliverables.
