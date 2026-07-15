# Security and publication safety

## Reporting

Report suspected vulnerabilities or accidentally committed sensitive data by
opening a private security advisory on this repository.

## Data handling rules (from the governing contract)

- All fixtures are synthetic. Production project IDs, credentials,
  service-account keys, trace/user identifiers, prompts, tool
  arguments/results, and error payloads must never be committed.
- Benchmark raw results must sanitize project and principal identifiers
  while retaining the job statistics needed by the cost gates.
- The canonical Looker Studio template connects only to synthetic fixture
  data via the sentinel bindings.
- Hydrated data sources must be verified as **Viewer's Credentials** before
  any sharing; Owner's Credentials is prohibited for shared copies.
- CI runs secret scanning (gitleaks) on every push and pull request.
