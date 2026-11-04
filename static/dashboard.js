"use strict";

let dashboardData = null;
const healthLabels = { healthy: "Healthy", review: "Needs review", errors: "Scan errors", unknown: "Not assessed / unknown" };

function navigateView(name) {
  const target = $(`view-${name}`);
  if (!target) return;
  document.querySelectorAll(".view-panel").forEach((panel) => { panel.hidden = panel !== target; });
  document.querySelectorAll(".sidebar [data-route]").forEach((button) => {
    const active = button.dataset.route === name;
    button.classList.toggle("active", active);
    if (active) button.setAttribute("aria-current", "page");
    else button.removeAttribute("aria-current");
  });
}

function emptyRow(id, columns, message) {
  const row = $(id).insertRow();
  const td = cell(row, message);
  td.colSpan = columns;
  td.className = "empty";
}

function renderDashboard(data) {
  dashboardData = data;
  const scan = data.scan;
  const summary = data.summary;
  $("metric-workspaces").textContent = data.workspaceCount;
  const scopeLabel = scan?.scope === "tenant" ? "Tenant" : scan?.scope === "imported" ? "CSV" : "Loaded";
  $("metric-workspaces-detail").textContent = summary ? `${summary.activeWorkspaces} active; ${scopeLabel.toLowerCase()} scope` : "Scan tenant workspaces; CSV is optional";
  $("scan-tenant").textContent = data.tenantId || "Tenant from current Entra sign-in";
  $("import-count").textContent = data.importedCount ? `${data.importedCount} saved CSV workspace IDs (optional scope).` : "CSV import is optional; no CSV loaded.";
  $("metric-trial").textContent = scan ? scan.workspaces.filter((row) => row.status === "Trial").length : "--";
  $("metric-findings").textContent = summary ? summary.cleanupReviewFindings : "--";
  $("metric-items").textContent = summary ? `${summary.itemCount} observed items` : "Not scanned";
  $("metric-score").textContent = summary?.healthScore == null ? "--" : `${summary.healthScore}%`;
  $("metric-score-detail").textContent = summary
    ? `${summary.passedChecks}/${summary.totalChecks} passed; ${summary.totalChecks - summary.knownChecks} unknown. Assessed scope only.`
    : "Not a governance certification";
  const stateText = scan
    ? `${summary.status.toUpperCase()} (${scopeLabel}): ${summary.assessedWorkspaces}/${summary.workspaceCount} workspaces assessed. Started ${new Date(scan.startedAtUtc).toLocaleString()}.${scan.staleReason ? ` STALE: ${scan.staleReason}` : ""}${scan.stopReason ? ` STOPPED: ${scan.stopReason}` : ""}`
    : `${data.workspaceCount} workspaces loaded. Scan all workspaces in the signed-in tenant, or import an optional CSV for a smaller scope.`;
  $("scan-status").textContent = stateText;
  $("report-status").textContent = stateText;
  $("artifact-coverage").textContent = summary
    ? `${summary.itemCountComplete ? "Inventory sources succeeded for every workspace." : "Partial inventory: counts are observed lower bounds, not complete totals."} Data sizes and usage are not measured.`
    : "Counts require a scan. Data sizes and usage are not measured.";
  $("check-item-descriptions").checked = data.settings.includeItemDescriptions;
  $("dashboard-types").replaceChildren();
  for (const item of summary?.artifactTypes || []) {
    const row = $("dashboard-types").insertRow();
    cell(row, item.type);
    cell(row, item.count);
    cell(row, "Not measured");
  }
  if (!summary?.artifactTypes.length) emptyRow("dashboard-types", 3, summary ? "No artifacts observed; inspect scan coverage." : "Run a scan to load artifact totals.");
  $("health-bars").replaceChildren();
  if (summary) {
    for (const [kind, label] of Object.entries(healthLabels)) {
      const row = document.createElement("div");
      row.className = `health-row health-${kind}`;
      const text = document.createElement("span");
      text.textContent = label;
      const meter = document.createElement("meter");
      meter.min = 0;
      meter.max = Math.max(1, summary.workspaceCount);
      meter.value = summary.health[kind];
      meter.setAttribute("aria-label", `${label}: ${summary.health[kind]} workspaces`);
      const count = document.createElement("strong");
      count.textContent = summary.health[kind];
      row.appendChild(text);
      row.appendChild(meter);
      row.appendChild(count);
      $("health-bars").appendChild(row);
    }
  } else {
    $("health-bars").textContent = "No scan available.";
  }
  $("recent-activity").replaceChildren();
  for (const event of data.activity.slice(0, 8)) {
    const row = document.createElement("li");
    row.textContent = event.message;
    const time = document.createElement("time");
    time.dateTime = event.atUtc;
    time.textContent = new Date(event.atUtc).toLocaleString();
    row.appendChild(time);
    $("recent-activity").appendChild(row);
  }
  if (!data.activity.length) {
    const row = document.createElement("li");
    row.textContent = "No activity in this session.";
    $("recent-activity").appendChild(row);
  }
  $("recommended-actions").replaceChildren();
  const recommendations = [];
  const findings = summary?.findings || {};
  if (!summary) recommendations.push("Scan tenant workspaces directly, or optionally import a CSV for a smaller scan.");
  if (summary?.workspaceCount === 0) recommendations.push("No workspaces were returned for this tenant. Verify the signed-in tenant and Admin API scope.");
  if (scan?.staleReason) recommendations.push("Rescan: operations occurred after this snapshot.");
  if (summary && (summary.status !== "complete" || summary.health.unknown)) recommendations.push("Resolve scan/access errors and unknown evidence; review coverage before using totals or scores.");
  if (findings.emptyWorkspace) recommendations.push(`Review ${findings.emptyWorkspace} empty workspace(s). Do not delete based on emptiness alone.`);
  if (findings.missingWorkspaceDescription || findings.missingItemDescription) recommendations.push(`Document ${findings.missingWorkspaceDescription || 0} workspace(s) and ${findings.missingItemDescription || 0} artifact(s) with missing descriptions.`);
  if (findings.unknownCapacity) recommendations.push(`Validate capacity metadata/access for ${findings.unknownCapacity} workspace(s).`);
  if (!recommendations.length) recommendations.push("No findings under the enabled metadata rules. Usage, ownership and recovery readiness remain unassessed.");
  for (const message of recommendations) {
    const li = document.createElement("li");
    li.textContent = message;
    $("recommended-actions").appendChild(li);
  }
  $("cleanup-findings").replaceChildren();
  $("report-workspaces").replaceChildren();
  let findingCount = 0;
  for (const workspace of scan?.workspaces || []) {
    const row = $("report-workspaces").insertRow();
    cell(row, `${workspace.name || workspace.id} / ${workspace.id}`);
    cell(row, healthLabels[workspace.health || "unknown"]);
    cell(row, workspace.scanStatus === "pending" ? "Not scanned" : `${workspace.items.length}${workspace.inventoryComplete ? "" : " (partial)"}`);
    cell(row, `${workspace.findings.length} findings; ${workspace.errors.length} errors`);
    const itemNames = new Map(workspace.items.map((item) => [item.id, item.name]));
    for (const finding of [...workspace.findings, ...workspace.errors.map((error) => ({ code: error.stage, message: error.message }))]) {
      const findingRow = $("cleanup-findings").insertRow();
      cell(findingRow, `${workspace.name || workspace.id}${finding.itemId ? ` / ${itemNames.get(finding.itemId) || finding.itemId}` : ""}`);
      cell(findingRow, finding.code);
      cell(findingRow, finding.message);
      findingCount += 1;
    }
  }
  if (!findingCount) emptyRow("cleanup-findings", 3, scan ? "No findings observed; review scan coverage before taking action." : "Run a dashboard scan first.");
  if (!scan) emptyRow("report-workspaces", 4, "No scan available.");
  else if (!scan.workspaces.length) emptyRow("report-workspaces", 4, "No workspaces were returned by the Admin API for this tenant.");
}

async function refreshDashboardState() {
  renderDashboard(await api("/api/dashboard"));
}

async function runDashboardScan(scope = "tenant") {
  let data = await api("/api/dashboard/scan", { method: "POST", body: { scope } });
  renderDashboard(data);
  await loadInventory(data.inventory);
  const scanId = data.scan.id;
  for (const workspace of data.scan.workspaces) {
    data = await api(`/api/dashboard/scan/${workspace.id}`, {
      method: "POST", body: { scanId },
    });
    renderDashboard(data);
    if (data.scan.stopReason) break;
  }
  notice(`Scan ${data.summary.status}: ${data.summary.assessedWorkspaces}/${data.summary.workspaceCount} workspaces assessed. Review scope, coverage and findings.`,
    data.summary.status === "complete" ? "" : "error");
}

async function downloadScanReport() {
  const response = await api("/api/dashboard/report", { method: "POST" }, "download");
  if (!(response.headers.get("Content-Type") || "").startsWith("application/json")) {
    throw new Error("The server did not return a scan report. No report was downloaded.");
  }
  saveDownload(await response.blob(), "fabric-workspace-scan-report.json");
  notice("Scan report download started. Check timestamps, coverage and stale warnings before using it.");
}

if ($("dashboard-scan")) {
  document.querySelectorAll("[data-route]").forEach((button) => {
    button.addEventListener("click", () => navigateView(button.dataset.route));
  });
  $("dashboard-scan").addEventListener("click", () => operation("Loading all workspaces in the signed-in tenant and scanning metadata...", () => runDashboardScan("tenant")));
  $("dashboard-scan-csv").addEventListener("click", () => operation("Scanning the optional CSV workspace scope...", () => runDashboardScan("imported")));
  $("download-report").addEventListener("click", () => operation("Generating scan report...", downloadScanReport));
  $("dashboard-settings").addEventListener("submit", (event) => {
    event.preventDefault();
    const enabled = $("check-item-descriptions").checked;
    operation("Saving session scan settings...", async () => {
      renderDashboard(await api("/api/dashboard/settings", {
        method: "POST", body: { includeItemDescriptions: enabled },
      }));
      notice("Scan settings saved for this session. Run a new scan.");
    });
  });
  $("cleanup-preview").addEventListener("click", () => {
    const id = $("cleanup-workspace").value;
    operation("Preparing deletion preview...", () => previewCleanup(id));
  });
  $("backup-from-view").addEventListener("click", () => {
    const ids = [...document.querySelectorAll(".workspace-select:checked")].map((el) => el.value);
    operation("Exporting selected workspace metadata...", () => downloadMetadataBackup(ids));
  });
  operation("Loading dashboard session...", async () => {
    await refreshDashboardState();
    if (dashboardData.workspaceSource !== "none") await loadInventory();
    notice("Dashboard ready. Scans are explicit and read-only; cleanup always requires confirmation.");
  });
}
