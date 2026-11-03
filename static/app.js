"use strict";

const $ = (id) => document.getElementById(id);
let csrf = document.querySelector('meta[name="csrf-token"]').content;
const pageContext = document.querySelector('meta[name="session-context"]').content;
let workspaceRows = [];
let deletion = null;
let busy = false;

function notice(message, kind = "") {
  const box = $("notice");
  box.textContent = message;
  box.className = kind;
  box.hidden = false;
  $("reload-session").hidden = true;
}

async function requestJson(path, options = {}, responseType = "json") {
  const response = await fetch(path, { ...options, credentials: "same-origin", cache: "no-store" });
  if (response.ok && responseType === "zip") return response;
  let data;
  try {
    data = await response.json();
  } catch (error) {
    if (error.name === "TimeoutError" || error.name === "AbortError") throw error;
    throw new Error("The server returned an unreadable response. Check the server terminal.");
  }
  if (!response.ok) {
    const error = new Error(data.error || `Request failed (${response.status}).`);
    error.reload = data.code === "csrf_mismatch";
    throw error;
  }
  return data;
}

async function api(path, options = {}, responseType = "json") {
  const headers = { Accept: "application/json", ...options.headers };
  const method = (options.method || "GET").toUpperCase();
  if (!["GET", "HEAD", "OPTIONS"].includes(method)) {
    const current = await requestJson("/auth/session");
    if (current.contextId !== pageContext) {
      const error = new Error("Your app session changed or expired, possibly after a server restart or another sign-in. Reload the page before retrying; no action was performed.");
      error.reload = true;
      throw error;
    }
    if (typeof current.csrfToken !== "string" || !current.csrfToken) {
      throw new Error("Could not refresh the session token. Reload the page.");
    }
    csrf = current.csrfToken;
    document.querySelectorAll('input[name="csrf_token"]').forEach((input) => { input.value = csrf; });
    headers["X-CSRF-Token"] = csrf;
  }
  let body = options.body;
  if (body && !(body instanceof FormData)) {
    headers["Content-Type"] = "application/json";
    body = JSON.stringify(body);
  }
  return requestJson(path, { ...options, headers, body }, responseType);
}

async function downloadMetadataBackup(ids) {
  if (!ids.length) throw new Error("Select at least one workspace to back up.");
  const response = await api("/api/backup", {
    method: "POST", body: { workspaceIds: ids },
  }, "zip");
  if (!(response.headers.get("Content-Type") || "").startsWith("application/zip")) {
    throw new Error("The server did not return a metadata ZIP. No backup was downloaded.");
  }
  const blob = await response.blob();
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = "fabric-metadata-backup.zip";
  document.body.appendChild(link);
  try {
    link.click();
  } finally {
    link.remove();
    window.setTimeout(() => URL.revokeObjectURL(url), 1000);
  }
  const partial = response.headers.get("X-Backup-Status") !== "complete";
  notice(partial
    ? "Partial metadata ZIP download started. Some inventory or definitions could not be exported. Review manifest.json before relying on this backup."
    : "Metadata ZIP download started. Review manifest.json. This does not back up actual data or guarantee restoration.",
  partial ? "error" : "");
}

$("reload-session").addEventListener("click", () => window.location.reload());

if ($("logout-form")) {
  $("logout-form").addEventListener("submit", (event) => {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    operation("Signing out...", async () => {
      await api("/auth/logout", { method: "POST", body: form });
      window.location.replace("/");
    });
  });
}

async function operation(message, action) {
  if (busy) return;
  busy = true;
  const disabled = new Map();
  document.querySelectorAll("button, input, select").forEach((el) => {
    disabled.set(el, el.disabled);
    el.disabled = true;
  });
  notice(message, "busy");
  try {
    await action();
  } catch (error) {
    notice(error.message, "error");
    $("reload-session").hidden = !error.reload;
  } finally {
    busy = false;
    disabled.forEach((value, el) => { if (el.isConnected) el.disabled = value; });
    if ($("explore-workspace")) {
      $("explore-workspace").disabled = ![...$("explore-workspace").options]
        .some((option) => option.value && !option.disabled);
    }
    if ($("confirm-delete")) {
      $("confirm-delete").disabled = !deletion || !deletion.items.length
        || $("delete-confirmation").value !== deletion.confirmation;
    }
  }
}

function cell(row, value, tag = "td") {
  const element = document.createElement(tag);
  element.textContent = value == null ? "" : String(value);
  row.appendChild(element);
  return element;
}

function badge(td, value, kind = "") {
  const span = document.createElement("span");
  span.className = `status ${kind}`;
  span.textContent = value;
  td.appendChild(span);
}

function itemTable(id, items, emptyMessage = "No items found.") {
  const tbody = $(id);
  tbody.replaceChildren();
  if (!items.length) {
    const row = tbody.insertRow();
    const td = cell(row, emptyMessage);
    td.colSpan = id === "delete-items" ? 2 : 3;
    td.className = "empty";
  }
  for (const item of items) {
    const row = tbody.insertRow();
    cell(row, item.name);
    cell(row, item.type);
    if (id !== "delete-items") cell(row, item.id);
  }
}

function clearExploreArtifacts() {
  $("artifact-title").textContent = "Workspace artifacts";
  $("item-count").textContent = "0";
  itemTable("items", [], "No workspace selected.");
}

function renderExploreWorkspaces() {
  const select = $("explore-workspace");
  const selected = select.value;
  const search = $("explore-search").value.trim().toLowerCase();
  const matches = workspaceRows.filter((workspace) =>
    `${workspace.name} ${workspace.id}`.toLowerCase().includes(search));
  select.replaceChildren();
  const placeholder = document.createElement("option");
  placeholder.value = "";
  placeholder.textContent = matches.length ? "Choose a workspace..." : "No matching workspaces.";
  select.appendChild(placeholder);
  for (const workspace of matches) {
    const option = document.createElement("option");
    option.value = workspace.id;
    option.disabled = Boolean(workspace.error || workspace.state !== "Active");
    option.textContent = `${workspace.name} | ${workspace.id}${option.disabled ? " (unavailable)" : ""}`;
    select.appendChild(option);
  }
  const keepSelection = matches.some((workspace) =>
    workspace.id === selected && !workspace.error && workspace.state === "Active");
  select.value = keepSelection ? selected : "";
  if (!keepSelection) clearExploreArtifacts();
  select.disabled = busy || !matches.some((workspace) => !workspace.error && workspace.state === "Active");
  $("explore-workspace-count").textContent = `${matches.length} of ${workspaceRows.length} imported workspaces shown.`;
}

function selectExploreWorkspace(workspaceId) {
  if (![...$("explore-workspace").options].some((option) => option.value === workspaceId)) {
    $("explore-search").value = "";
    renderExploreWorkspaces();
  }
  $("explore-workspace").value = workspaceId;
}

async function loadWorkspaceArtifacts(workspaceId) {
  const workspace = workspaceRows.find((row) => row.id === workspaceId);
  if (!workspace || workspace.error || workspace.state !== "Active") {
    throw new Error("Choose an available workspace from the imported inventory.");
  }
  selectExploreWorkspace(workspaceId);
  $("artifact-title").textContent = `Artifacts / ${workspace.name}`;
  $("item-count").textContent = "0";
  itemTable("items", [], "No artifacts loaded for this workspace.");
  const result = await api(`/api/workspaces/${workspace.id}/items`);
  $("item-count").textContent = result.items.length;
  itemTable("items", result.items);
  notice(`${result.items.length} artifacts loaded.`);
  $("artifacts-panel").scrollIntoView({ behavior: "smooth" });
}

if ($("explore-workspace")) {
  $("explore-search").addEventListener("input", renderExploreWorkspaces);
  $("explore-workspace").addEventListener("change", (event) => {
    const id = event.target.value;
    if (!id) {
      clearExploreArtifacts();
      return;
    }
    return operation("Fetching all artifact pages...", () => loadWorkspaceArtifacts(id));
  });
}

function showResults(rows, summary) {
  $("results-panel").hidden = false;
  $("result-summary").textContent = summary;
  $("results").replaceChildren();
  for (const result of rows) {
    const row = $("results").insertRow();
    cell(row, result.name || result.workspaceId || result.id);
    const kind = ["assigned", "skipped", "deleted", "absent"].includes(result.status) ? "good" : "bad";
    badge(cell(row, ""), result.status, kind);
    const attempts = (result.attempts || [])
      .map((attempt) => `${attempt.capacityId}: ${attempt.message}`).join(" | ");
    cell(row, [result.message, attempts].filter(Boolean).join(" "));
  }
}

async function waitForSignIn() {
  notice("Complete Microsoft Entra sign-in in your browser, then return here. Your existing app session will continue automatically; no terminal is needed.", "busy");
  const deadline = Date.now() + 300000;
  while (Date.now() < deadline) {
    let result;
    try {
      result = await api("/auth/status", { signal: AbortSignal.timeout(15000) });
    } catch (error) {
      if (error.name === "TimeoutError" || error.name === "AbortError") {
        const timeout = new Error("The local server did not respond to the sign-in status check. Reload this page to check the existing session; do not start another Microsoft login.");
        timeout.reload = true;
        throw timeout;
      }
      throw error;
    }
    if (result.status === "authenticated") {
      window.location.replace("/");
      return;
    }
    if (result.status !== "pending") throw new Error("Sign-in is no longer active. Try again.");
    await new Promise((resolve) => window.setTimeout(resolve, 2000));
  }
  const error = new Error("Sign-in completion was not confirmed within five minutes. Reload this page to check your session. If the Microsoft tab still shows sign-in or consent, finish that step first.");
  error.reload = true;
  throw error;
}

if ($("login-form")) {
  $("login-form").addEventListener("submit", (event) => {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    operation("Opening Microsoft sign-in...", async () => {
      await api("/auth/login", { method: "POST", body: form });
      await waitForSignIn();
    });
  });
  if ($("login-form").dataset.pending === "true") {
    operation("Waiting for Microsoft sign-in...", waitForSignIn);
  }
}

async function loadInventory() {
  const data = await api("/api/inventory");
  workspaceRows = data.workspaces;
  renderExploreWorkspaces();
  $("workspace-count").textContent = workspaceRows.length;
  $("import-count").textContent = `${workspaceRows.length} unique workspace IDs imported.`;
  $("select-all").checked = false;
  const tbody = $("workspaces");
  tbody.replaceChildren();
  for (const workspace of workspaceRows) {
    const row = tbody.insertRow();
    const selectCell = cell(row, "");
    const checkbox = document.createElement("input");
    checkbox.type = "checkbox";
    checkbox.value = workspace.id;
    checkbox.className = "workspace-select";
    checkbox.disabled = Boolean(workspace.error || workspace.state !== "Active");
    checkbox.setAttribute("aria-label", `Select ${workspace.name}`);
    selectCell.appendChild(checkbox);
    const nameCell = cell(row, workspace.name);
    const id = document.createElement("small");
    id.textContent = workspace.id;
    nameCell.appendChild(id);
    const capacityCell = cell(row, workspace.capacityName || workspace.capacityId || "Unassigned");
    if (workspace.sku) {
      const sku = document.createElement("small");
      sku.textContent = workspace.sku;
      capacityCell.appendChild(sku);
    }
    badge(cell(row, ""), workspace.status, workspace.status === "Trial" ? "good" : "");
    const actions = cell(row, "");
    if (!workspace.error && workspace.state === "Active") {
      const view = document.createElement("button");
      view.type = "button";
      view.className = "secondary";
      view.textContent = "Artifacts";
      view.addEventListener("click", () => operation("Fetching all artifact pages...", () => loadWorkspaceArtifacts(workspace.id)));
      actions.appendChild(view);
      const remove = document.createElement("button");
      remove.type = "button";
      remove.className = "secondary danger-text";
      remove.textContent = "Delete items";
      remove.addEventListener("click", () => operation("Preparing deletion preview...", async () => {
        deletion = await api(`/api/workspaces/${workspace.id}/delete-preview`, { method: "POST" });
        $("delete-workspace").textContent = `${deletion.name} / ${deletion.items.length} items`;
        $("delete-phrase").textContent = deletion.confirmation;
        $("delete-confirmation").value = "";
        $("confirm-delete").disabled = true;
        itemTable("delete-items", deletion.items);
        $("delete-dialog").showModal();
        notice("Review the item list and confirm only if you intend to delete these items.");
      }));
      actions.appendChild(remove);
    }
  }
  const capacities = $("capacity-list");
  capacities.replaceChildren();
  for (const capacity of data.capacities) {
    const p = document.createElement("p");
    p.textContent = `${capacity.displayName} | ${capacity.id} | ${capacity.sku} | ${capacity.state}`;
    capacities.appendChild(p);
  }
  if (!data.capacities.length) capacities.textContent = "No accessible capacities were returned.";
}

if ($("import-form")) {
  $("import-form").addEventListener("submit", (event) => {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    operation("Importing CSV and fetching tenant inventory...", async () => {
      const imported = await api("/api/import", { method: "POST", body: form });
      workspaceRows = [];
      $("explore-search").value = "";
      renderExploreWorkspaces();
      $("workspaces").replaceChildren();
      $("workspace-count").textContent = imported.count;
      $("import-count").textContent = `${imported.count} workspace IDs imported.`;
      await loadInventory();
      notice("Inventory loaded. Select workspaces to assign, or browse their artifacts.");
    });
  });
  $("refresh").addEventListener("click", () => operation("Refreshing tenant inventory...", async () => {
    await loadInventory();
    notice("Inventory refreshed.");
  }));
  $("select-all").addEventListener("change", (event) => {
    document.querySelectorAll(".workspace-select:not(:disabled)")
      .forEach((checkbox) => { checkbox.checked = event.target.checked; });
  });
  $("backup").addEventListener("click", () => {
    const ids = [...document.querySelectorAll(".workspace-select:checked")].map((el) => el.value);
    operation("Exporting metadata and supported definitions. This may take several minutes; keep this page open...", async () => {
      await downloadMetadataBackup(ids);
    });
  });
  $("assign").addEventListener("click", () => {
    const ids = [...document.querySelectorAll(".workspace-select:checked")].map((el) => el.value);
    const primary = $("primary").value.trim();
    const secondary = $("secondary").value.trim();
    if (!ids.length || !primary) {
      notice("Select at least one workspace and enter your primary trial capacity ID.", "error");
      return;
    }
    if (!window.confirm(`Assign ${ids.length} selected workspace(s) to your trial capacity? Non-trial paid assignments will also be changed. Existing trial assignments are preserved.`)) return;
    operation("Assigning and verifying workspaces. This may take several minutes...", async () => {
      const result = await api("/api/assign", {
        method: "POST", body: { workspaceIds: ids, primary, secondary },
      });
      const assigned = result.results.filter((r) => r.status === "assigned").length;
      const summary = `${assigned} assigned; ${result.results.length - assigned} skipped, pending, or failed. Review each result.`;
      showResults(result.results, summary);
      notice(summary, result.results.some((r) => ["failed", "unknown", "pending"].includes(r.status)) ? "error" : "");
    });
  });
  $("cancel-delete").addEventListener("click", () => {
    deletion = null;
    $("delete-dialog").close();
  });
  $("delete-dialog").addEventListener("cancel", () => { deletion = null; });
  $("delete-confirmation").addEventListener("input", (event) => {
    $("confirm-delete").disabled = !deletion || !deletion.items.length
      || event.target.value !== deletion.confirmation;
  });
  $("confirm-delete").addEventListener("click", () => {
    if (!deletion) return;
    const target = deletion;
    const confirmation = $("delete-confirmation").value;
    deletion = null;
    $("delete-dialog").close();
    operation("Deleting confirmed items and verifying remaining contents...", async () => {
      const result = await api(`/api/workspaces/${target.workspaceId}/delete`, {
        method: "POST", body: { nonce: target.nonce, confirmation },
      });
      showResults(result.results, result.message);
      if (result.remaining !== null) {
        selectExploreWorkspace(target.workspaceId);
        $("artifact-title").textContent = `Remaining artifacts / ${target.name}`;
        $("item-count").textContent = result.remaining.length;
        itemTable("items", result.remaining);
      }
      notice(result.message, result.complete ? "" : "error");
    });
  });
}
