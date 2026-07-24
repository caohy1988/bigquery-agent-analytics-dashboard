import { REPORT_CONFIG } from "./report-config.mjs";

export const PROJECT_RE = /^[a-z][a-z0-9-]{4,28}[a-z0-9]$/;
export const BIGQUERY_ID_RE = /^[A-Za-z_][A-Za-z0-9_]{0,1023}$/;

function requireValue(label, value, pattern) {
  const normalized = String(value ?? "").trim();
  if (!pattern.test(normalized)) {
    throw new Error(`Enter a valid BigQuery ${label}.`);
  }
  return normalized;
}

export function validateConfiguration(input) {
  return Object.freeze({
    project: requireValue("project ID", input.project, PROJECT_RE),
    dataset: requireValue("dataset ID", input.dataset, BIGQUERY_ID_RE),
    table: requireValue("table ID", input.table, BIGQUERY_ID_RE),
  });
}

export function buildDashboardUrl(input, config = REPORT_CONFIG) {
  const values = validateConfiguration(input);
  const alias = config.dataSourceAlias;
  const replacements = [
    config.sentinels.project,
    values.project,
    config.sentinels.dataset,
    values.dataset,
    config.sentinels.viewPrefix,
    config.defaultViewPrefix,
  ];
  const params = new URLSearchParams({
    "c.reportId": config.reportId,
    "c.mode": "view",
    "r.reportName": `BigQuery Agent Analytics — ${values.dataset}.${values.table}`,
    [`ds.${alias}.datasourceName`]:
      `BQAA — ${values.project}.${values.dataset}.${values.table}`,
    [`ds.${alias}.billingProjectId`]: values.project,
    [`ds.${alias}.sqlReplace`]: replacements.join(","),
    [`ds.${alias}.refreshFields`]: "false",
  });
  return `https://lookerstudio.google.com/reporting/create?${params.toString()}`;
}

export function buildSetupUrl(input, pageUrl) {
  const values = validateConfiguration(input);
  const url = new URL(pageUrl);
  url.search = new URLSearchParams(values).toString();
  url.hash = "";
  return url.toString();
}
