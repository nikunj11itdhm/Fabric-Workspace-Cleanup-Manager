const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const source = readFileSync(path.join(__dirname, "..", "static", "dashboard.js"), "utf8");

function setup() {
  function element() {
    return {
      children: [], attributes: {}, dataset: {}, hidden: false, textContent: "",
      appendChild(child) { this.children.push(child); },
      replaceChildren() { this.children = []; this.textContent = ""; },
      insertRow() { const row = element(); this.children.push(row); return row; },
      setAttribute(name, value) { this.attributes[name] = value; },
      removeAttribute(name) { delete this.attributes[name]; },
      classList: { active: false, toggle(name, value) { this[name] = value; } },
    };
  }
  const nodes = Object.fromEntries([
    "metric-workspaces", "metric-workspaces-detail", "metric-trial", "metric-findings",
    "metric-items", "metric-score", "metric-score-detail", "scan-status", "report-status",
    "artifact-coverage", "check-item-descriptions", "dashboard-types", "health-bars",
    "recent-activity", "recommended-actions", "cleanup-findings", "report-workspaces",
    "scan-tenant", "import-count",
    "view-home", "view-inventory", "view-cleanup", "view-backup", "view-reports",
    "view-settings", "view-about",
  ].map((id) => [id, element()]));
  const panels = Object.entries(nodes).filter(([id]) => id.startsWith("view-")).map(([, node]) => node);
  const navigation = ["home", "inventory", "cleanup", "backup", "reports", "settings", "about"].map((name) => {
    const node = element(); node.dataset.route = name; return node;
  });
  const requests = [];
  const notices = [];
  const downloads = [];
  const context = vm.createContext({
    $: (id) => nodes[id] || null,
    document: {
      createElement: element,
      querySelectorAll: (selector) => selector === ".view-panel" ? panels : navigation,
    },
    cell: (row, value) => { const node = element(); node.textContent = value; row.appendChild(node); return node; },
    api: async (url, options, responseType) => {
      requests.push({ url, options, responseType });
      throw new Error("Unconfigured test API");
    },
    notice: (...args) => notices.push(args),
    saveDownload: (...args) => downloads.push(args),
    loadInventory: async (inventory) => { context.loadedInventory = inventory; },
  });
  vm.runInContext(source, context);
  return { context, nodes, navigation, requests, notices, downloads };
}

function data() {
  return {
    importedCount: 2, workspaceCount: 2, workspaceSource: "imported", tenantId: null,
    scan: null, summary: null, activity: [],
    settings: { includeItemDescriptions: true },
  };
}

function scanned() {
  return {
    ...data(),
    scan: {
      id: "scan-id", scope: "imported", startedAtUtc: "2026-11-04T06:00:00Z", staleReason: "Cleanup was attempted.",
      workspaces: [{
        id: "workspace", name: "<script>Unsafe name</script>", status: "Trial", health: "review", scanStatus: "scanned",
        inventoryComplete: true, items: [{ id: "item", name: "Report", type: "Report" }],
        findings: [{ code: "missingItemDescription", itemId: "item", message: "Add a description." }],
        errors: [],
      }, {
        id: "pending", status: "Unknown", scanStatus: "pending",
        items: [], findings: [], errors: [],
      }],
    },
    summary: {
      activeWorkspaces: 1, cleanupReviewFindings: 1, itemCount: 1,
      itemCountComplete: false, healthScore: 80, passedChecks: 4, totalChecks: 5, knownChecks: 5,
      status: "running", assessedWorkspaces: 1, workspaceCount: 2,
      artifactTypes: [{ type: "Report", count: 1 }],
      health: { healthy: 0, review: 1, errors: 0, unknown: 1 },
      findings: { missingItemDescription: 1 },
    },
  };
}

test("dashboard starts with no invented score or counts and switches sidebar views", () => {
  const { context, nodes, navigation, requests } = setup();
  context.renderDashboard(data());
  assert.equal(nodes["metric-score"].textContent, "--");
  assert.equal(nodes["metric-items"].textContent, "Not scanned");
  assert.equal(nodes["metric-workspaces"].textContent, 2);
  context.navigateView("reports");
  assert.equal(nodes["view-home"].hidden, true);
  assert.equal(nodes["view-reports"].hidden, false);
  assert.equal(navigation.find((node) => node.dataset.route === "reports").attributes["aria-current"], "page");
  context.navigateView("invalid");
  assert.equal(nodes["view-reports"].hidden, false);
  assert.equal(requests.length, 0);
});

test("dashboard renders partial/stale counts, safe text, health bars and metadata-only recommendations", () => {
  const { context, nodes } = setup();
  const state = scanned();
  state.activity = [{ atUtc: "2026-11-04T06:10:00Z", message: "<img onerror=alert(1)>", kind: "scan" }];
  context.renderDashboard(state);
  assert.equal(nodes["metric-score"].textContent, "80%");
  assert.match(nodes["metric-score-detail"].textContent, /4\/5 passed/);
  assert.match(nodes["scan-status"].textContent, /STALE: Cleanup was attempted/);
  assert.match(nodes["artifact-coverage"].textContent, /lower bounds/);
  assert.equal(nodes["dashboard-types"].children[0].children[2].textContent, "Not measured");
  assert.equal(nodes["health-bars"].children.length, 4);
  assert.equal(nodes["report-workspaces"].children[0].children[0].textContent, "<script>Unsafe name</script> / workspace");
  assert.equal(nodes["recent-activity"].children[0].textContent, "<img onerror=alert(1)>");
  assert.match(nodes["cleanup-findings"].children[0].children[0].textContent, /Report/);
  assert.ok(nodes["recommended-actions"].children.some((node) => node.textContent.includes("Document")));
  assert.ok(nodes["recommended-actions"].children.some((node) => node.textContent.includes("Rescan")));
});

test("scan runs workspace reads sequentially with matching scan ID and stops on a backend stop reason", async () => {
  const { context, requests, notices } = setup();
  const start = scanned();
  start.scan.scope = "tenant";
  start.inventory = { workspaces: [{ id: "workspace" }, { id: "pending" }], capacities: [] };
  start.scan.staleReason = null;
  const stopped = structuredClone(start);
  stopped.scan.stopReason = "Throttled";
  stopped.summary.status = "partial";
  context.api = async (url, options) => {
    requests.push({ url, options });
    return requests.length === 1 ? start : stopped;
  };
  await context.runDashboardScan();
  assert.deepEqual(requests.map((request) => request.url), ["/api/dashboard/scan", "/api/dashboard/scan/workspace"]);
  assert.equal(requests[1].options.body.scanId, "scan-id");
  assert.equal(requests[0].options.body.scope, "tenant");
  assert.equal(context.loadedInventory, start.inventory);
  assert.match(notices[0][0], /Scan partial/);
  assert.equal(notices[0][1], "error");
  requests.length = 0;
  start.scan.scope = "imported";
  stopped.scan.scope = "imported";
  await context.runDashboardScan("imported");
  assert.equal(requests[0].options.body.scope, "imported");
});

test("tenant scans need no imported IDs and display an explicit empty result", async () => {
  const { context, nodes, requests } = setup();
  const response = {
    ...data(), importedCount: 0, workspaceCount: 0, workspaceSource: "tenant", tenantId: "destination-tenant",
    scan: { id: "empty", scope: "tenant", startedAtUtc: "2026-11-04T06:00:00Z", workspaces: [] },
    summary: { activeWorkspaces: 0, cleanupReviewFindings: 0, itemCount: 0, itemCountComplete: true, healthScore: null,
      passedChecks: 0, totalChecks: 0, knownChecks: 0, status: "complete", assessedWorkspaces: 0, workspaceCount: 0,
      artifactTypes: [], health: { healthy: 0, review: 0, errors: 0, unknown: 0 }, findings: {} },
    inventory: { workspaces: [], capacities: [] },
  };
  context.api = async (url, options) => { requests.push({ url, options }); return response; };
  await context.runDashboardScan();
  assert.equal(requests[0].options.body.scope, "tenant");
  assert.equal(requests.length, 1);
  assert.equal(nodes["scan-tenant"].textContent, "destination-tenant");
  assert.match(nodes["import-count"].textContent, /optional/);
  assert.match(nodes["scan-status"].textContent, /Tenant/);
  assert.match(nodes["report-workspaces"].children[0].children[0].textContent, /No workspaces were returned/);
});
test("report download requires a successful report response and uses the JSON filename", async () => {
  const { context, requests, downloads } = setup();
  const blob = { report: "blob" };
  context.api = async (url, options, responseType) => {
    requests.push({ url, options, responseType });
    return { headers: { get: () => "application/json" }, blob: async () => blob };
  };
  await context.downloadScanReport();
  assert.equal(requests[0].url, "/api/dashboard/report");
  assert.equal(requests[0].options.method, "POST");
  assert.equal(requests[0].responseType, "download");
  assert.deepEqual(downloads, [[blob, "fabric-workspace-scan-report.json"]]);
  context.api = async () => { throw new Error("Report unavailable"); };
  await assert.rejects(context.downloadScanReport(), /Report unavailable/);
  assert.equal(downloads.length, 1);
});
