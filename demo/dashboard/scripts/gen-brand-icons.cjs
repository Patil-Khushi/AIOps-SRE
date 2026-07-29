const si = require('simple-icons');
const fs = require('fs');
const MAP = [
  ['Prometheus','siPrometheus'],
  ['Grafana','siGrafana'],
  ['OpenTelemetry','siOpentelemetry'],
  ['Jaeger','siJaeger'],
  ['Jira','siJira'],
  ['PagerDuty','siPagerduty'],
  ['SQLite','siSqlite'],
  ['pgvector','siPostgresql'],
  ['Qdrant','siQdrant'],
  ['Redis','siRedis'],
  ['Kubernetes','siKubernetes'],
  ['GitHub Actions','siGithubactions'],
  ['sentence-transformers','siHuggingface'],
];
let out = `// AUTO-GENERATED from the \'simple-icons\' package (CC0-1.0), then vendored so the
// dashboard carries no runtime dependency and builds fully offline. Brand names and
// marks remain trademarks of their respective owners; they appear here solely to
// identify the integrations this platform supports (nominative use).
//
// To refresh: npm i -D simple-icons && re-run scripts/gen-brand-icons.cjs, or hand-edit.
// viewBox for every path below is "0 0 24 24".

export interface BrandIcon {
  /** SVG path data, 24x24 viewBox. */
  path: string;
  /** Official brand hex, without the leading #. */
  hex: string;
}

export const BRAND_ICONS: Record<string, BrandIcon> = {\n`;
for (const [name, key] of MAP) {
  const i = si[key];
  if (!i) { console.error('MISSING ' + key); process.exit(1); }
  out += `  ${JSON.stringify(name)}: {\n    hex: ${JSON.stringify(i.hex)},\n    path: ${JSON.stringify(i.path)},\n  },\n`;
}
out += '};\n';
fs.writeFileSync('src/data/brandIcons.ts', out, 'utf8');
console.log('wrote src/data/brandIcons.ts  bytes=' + out.length + '  icons=' + MAP.length);

