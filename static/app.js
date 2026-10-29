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

async function requestJson(path, options = {}) {
  const response = await fetch(path, { ...options, credentials: "same-origin", cache: "no-store" });
  let data;
  try {
    data = await response.json();
  } catch {
    throw new Error("The server returned an unreadable response. Check the server terminal.");
  }
  if (!response.ok) {
    const error = new Error(data.error || `Request failed (${response.status}).`);
    error.reload = data.code === "csrf_mismatch";
    throw error;
  }
  return data;
}

async function api(path, options = {}) {
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
  return requestJson(path, { ...options, headers, body });
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
  document.querySelectorAll("button, input").forEach((el) => {
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

function itemTable(id, items) {
  const tbody = $(id);
  tbody.replaceChildren();
  if (!items.length) {
    const row = tbody.insertRow();
    const td = cell(row, "No items found.");
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
  while (true) {
    const result = await api("/auth/status");
    if (result.status === "authenticated") {
      window.location.replace("/");
      return;
    }
    if (result.status !== "pending") throw new Error("Sign-in is no longer active. Try again.");
    await new Promise((resolve) => window.setTimeout(resolve, 2000));
  }
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
      view.addEventListener("click", () => operation("Fetching all artifact pages...", async () => {
        const result = await api(`/api/workspaces/${workspace.id}/items`);
        $("artifact-title").textContent = `Artifacts / ${workspace.name}`;
        $("item-count").textContent = result.items.length;
        itemTable("items", result.items);
        notice(`${result.items.length} artifacts loaded.`);
        $("artifacts-panel").scrollIntoView({ behavior: "smooth" });
      }));
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
      $("workspaces").replaceChildren();
      $("workspace-count").textContent = imported.count;
      $("import-count").textContent = `${imported.count} workspace IDs imported.`;
      $("items").replaceChildren();
      $("item-count").textContent = "0";
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
        $("artifact-title").textContent = `Remaining artifacts / ${target.name}`;
        $("item-count").textContent = result.remaining.length;
        itemTable("items", result.remaining);
      }
      notice(result.message, result.complete ? "" : "error");
    });
  });
}
