const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const source = readFileSync(path.join(__dirname, "..", "static", "app.js"), "utf8");

function setup(session = { contextId: "page-session", csrfToken: "fresh-token", authenticated: true }, extraNodes = {}) {
  const requests = [];
  const nodes = {
    notice: { hidden: true },
    "reload-session": { hidden: true, addEventListener() {} },
    ...extraNodes,
  };
  const hiddenInput = { value: "old-token" };
  const context = vm.createContext({
    FormData,
    AbortSignal,
    document: {
      getElementById: (id) => nodes[id] || null,
      querySelector: (selector) => ({
        content: selector.includes("session-context") ? "page-session" : "old-token",
      }),
      querySelectorAll: (selector) => selector.includes("csrf_token") ? [hiddenInput] : [],
    },
    window: { location: { reload() {} } },
    fetch: async (url, options) => {
      requests.push({ url, options });
      return {
        ok: true,
        json: async () => url === "/auth/session" ? session : { count: 1 },
      };
    },
  });
  vm.runInContext(source, context);
  return { context, requests, nodes, hiddenInput };
}

function exploreSetup() {
  function element(tag = "") {
    let value = "";
    return {
      tag, children: [], events: {}, textContent: "", disabled: false, isConnected: true,
      get value() { return value; },
      set value(next) { value = next; },
      get options() { return this.children; },
      appendChild(child) { this.children.push(child); },
      replaceChildren() { this.children = []; value = ""; },
      insertRow() { const row = element("tr"); this.children.push(row); return row; },
      addEventListener(name, callback) { this.events[name] = callback; },
      scrollIntoView() {},
    };
  }
  const nodes = Object.fromEntries([
    "explore-search", "explore-workspace", "explore-workspace-count",
    "artifact-title", "item-count", "items", "artifacts-panel",
  ].map((id) => [id, element()]));
  const result = setup(undefined, nodes);
  result.context.document.createElement = element;
  result.context.document.querySelectorAll = () => Object.values(nodes);
  vm.runInContext(`workspaceRows = [
    { id: "alpha-id", name: "Sales Alpha", state: "Active" },
    { id: "beta-id", name: "Marketing Beta", state: "Active" },
    { id: "inactive-id", name: "Archived", state: "Deleted" },
    { id: "denied-id", name: "Restricted", error: true }
  ];`, result.context);
  result.context.renderExploreWorkspaces();
  return result;
}

test("Explore lists imported workspaces and filters by name or ID without API calls", () => {
  const { context, nodes, requests } = exploreSetup();
  const select = nodes["explore-workspace"];
  assert.equal(select.options.length, 5);
  assert.equal(select.options[3].disabled, true);
  assert.equal(select.options[4].disabled, true);
  nodes["explore-search"].value = "  SALES  ";
  nodes["explore-search"].events.input();
  assert.deepEqual(select.options.map((option) => option.value), ["", "alpha-id"]);
  nodes["explore-search"].value = "BETA-ID";
  nodes["explore-search"].events.input();
  assert.deepEqual(select.options.map((option) => option.value), ["", "beta-id"]);
  assert.equal(nodes["explore-workspace-count"].textContent, "1 of 4 imported workspaces shown.");
  assert.equal(requests.length, 0);
});

test("Explore dropdown loads only the selected workspace's artifacts and clears stale results on failure", async () => {
  const { context, nodes, requests } = exploreSetup();
  context.fetch = async (url, options) => {
    requests.push({ url, options });
    return { ok: true, json: async () => ({ items: [{ id: "item-id", name: "Notebook", type: "Notebook" }] }) };
  };
  nodes["explore-workspace"].value = "alpha-id";
  await nodes["explore-workspace"].events.change({ target: nodes["explore-workspace"] });
  assert.equal(nodes["artifact-title"].textContent, "Artifacts / Sales Alpha");
  assert.equal(nodes["item-count"].textContent, 1);
  context.renderExploreWorkspaces();
  assert.equal(nodes["explore-workspace"].value, "alpha-id");
  assert.equal(nodes["item-count"].textContent, 1);
  assert.deepEqual(requests.map((request) => request.url), ["/api/workspaces/alpha-id/items"]);
  context.fetch = async () => ({ ok: false, status: 403, json: async () => ({ error: "Access denied" }) });
  await assert.rejects(context.loadWorkspaceArtifacts("beta-id"), /Access denied/);
  assert.equal(nodes["explore-workspace"].value, "beta-id");
  assert.equal(nodes["item-count"].textContent, "0");
  assert.equal(nodes.items.children[0].children[0].textContent, "No artifacts loaded for this workspace.");
});

test("Explore filter clears hidden selections and stale artifact results; empty results disable the list", async () => {
  const { context, nodes } = exploreSetup();
  context.fetch = async () => ({ ok: true, json: async () => ({ items: [] }) });
  await context.loadWorkspaceArtifacts("alpha-id");
  nodes["explore-search"].value = "no match";
  nodes["explore-search"].events.input();
  assert.equal(nodes["explore-workspace"].value, "");
  assert.equal(nodes["explore-workspace"].disabled, true);
  assert.equal(nodes["artifact-title"].textContent, "Workspace artifacts");
  assert.equal(nodes["item-count"].textContent, "0");
  assert.equal(nodes["explore-workspace-count"].textContent, "0 of 4 imported workspaces shown.");
  await context.loadWorkspaceArtifacts("beta-id");
  assert.equal(nodes["explore-search"].value, "");
  assert.equal(nodes["explore-workspace"].value, "beta-id");
  assert.equal(nodes["explore-workspace"].disabled, false);
});

test("Explore rejects unavailable workspaces and restores dropdown state after operations", async () => {
  const { context, nodes, requests } = exploreSetup();
  await assert.rejects(context.loadWorkspaceArtifacts("inactive-id"), /available workspace/);
  await assert.rejects(context.loadWorkspaceArtifacts("denied-id"), /available workspace/);
  assert.equal(requests.length, 0);
  nodes["explore-workspace"].disabled = true;
  await context.operation("Refresh...", async () => {
    context.renderExploreWorkspaces();
    assert.equal(nodes["explore-workspace"].disabled, true);
  });
  assert.equal(nodes["explore-workspace"].disabled, false);
  nodes["explore-search"].value = "Archived";
  nodes["explore-search"].events.input();
  await context.operation("Other operation...", async () => {});
  assert.equal(nodes["explore-workspace"].disabled, true);
});

test("CSV upload obtains a fresh session token and keeps the original multipart body", async () => {
  const { context, requests, hiddenInput } = setup();
  const form = new FormData();
  form.append("file", new Blob(["workspace_id\n11111111-1111-4111-8111-111111111111\n"]), "workspaces.csv");
  await context.api("/api/import", { method: "POST", body: form });
  assert.deepEqual(requests.map((request) => request.url), ["/auth/session", "/api/import"]);
  assert.equal(requests[1].options.headers["X-CSRF-Token"], "fresh-token");
  assert.equal(requests[1].options.body, form);
  assert.equal(requests[1].options.headers["Content-Type"], undefined);
  assert.equal(requests[1].options.credentials, "same-origin");
  assert.equal(requests[0].options.cache, "no-store");
  assert.equal(hiddenInput.value, "fresh-token");
});

test("changed or expired sessions block the write before sending uploaded data", async () => {
  for (const authenticated of [true, false]) {
    const { context, requests } = setup({
      contextId: "different-session", csrfToken: "new-token", authenticated,
    });
    await assert.rejects(
      context.api("/api/import", { method: "POST", body: new FormData() }),
      (error) => error.reload === true && error.message.includes("no action was performed"),
    );
    assert.equal(requests.length, 1);
    assert.equal(requests[0].url, "/auth/session");
  }
});

test("assignment JSON is encoded once without changing the caller's request object", async () => {
  const { context, requests } = setup();
  const body = { workspaceIds: ["workspace"], primary: "capacity" };
  const options = { method: "POST", body };
  await context.api("/api/assign", options);
  assert.equal(options.body, body);
  assert.deepEqual(JSON.parse(requests[1].options.body), body);
});

test("CSRF rejection exposes reload recovery and never replays a mutation", async () => {
  const { context, requests } = setup();
  const fetch = context.fetch;
  context.fetch = async (url, options) => {
    if (url === "/auth/session") return fetch(url, options);
    requests.push({ url, options });
    return {
      ok: false, status: 403,
      json: async () => ({ error: "Token is out of date", code: "csrf_mismatch" }),
    };
  };
  await assert.rejects(
    context.api("/api/assign", { method: "POST", body: {} }),
    (error) => error.reload === true,
  );
  assert.equal(requests.length, 2);
});

test("GET requests do not introduce a token refresh or extra request", async () => {
  const { context, requests } = setup();
  await context.api("/api/inventory");
  assert.equal(requests.length, 1);
  assert.equal(requests[0].url, "/api/inventory");
});

test("metadata ZIP download refreshes CSRF and makes partial exports visible", async () => {
  for (const status of ["complete", "partial"]) {
    const { context, requests, nodes } = setup();
    const fetch = context.fetch;
    let clicked = false;
    let removed = false;
    let revoked = false;
    context.URL = {
      createObjectURL: () => "blob:backup",
      revokeObjectURL: (url) => { assert.equal(url, "blob:backup"); revoked = true; },
    };
    context.window.setTimeout = (callback) => callback();
    const link = { click: () => { clicked = true; }, remove: () => { removed = true; } };
    context.document.createElement = () => link;
    context.document.body = { appendChild() {} };
    context.fetch = async (url, options) => {
      if (url === "/auth/session") return fetch(url, options);
      requests.push({ url, options });
      return {
        ok: true,
        headers: { get: (name) => name === "Content-Type" ? "application/zip" : status },
        blob: async () => new Blob(["zip"]),
      };
    };
    await context.downloadMetadataBackup(["workspace"]);
    assert.deepEqual(requests.map((request) => request.url), ["/auth/session", "/api/backup"]);
    assert.equal(requests[1].options.headers["X-CSRF-Token"], "fresh-token");
    assert.deepEqual(JSON.parse(requests[1].options.body), { workspaceIds: ["workspace"] });
    assert.equal(link.download, "fabric-metadata-backup.zip");
    assert.ok(clicked && removed && revoked);
    assert.equal(nodes.notice.className, status === "partial" ? "error" : "");
    assert.match(nodes.notice.textContent, /manifest.json/);
  }
});

test("metadata backup errors are not downloaded or retried", async () => {
  const { context, requests } = setup();
  const fetch = context.fetch;
  context.fetch = async (url, options) => {
    if (url === "/auth/session") return fetch(url, options);
    requests.push({ url, options });
    return {
      ok: false, status: 403,
      json: async () => ({ error: "Token expired", code: "csrf_mismatch" }),
    };
  };
  await assert.rejects(context.downloadMetadataBackup(["workspace"]), (error) => error.reload === true);
  assert.equal(requests.length, 2);
  await assert.rejects(context.downloadMetadataBackup([]), /Select at least one/);
  assert.equal(requests.length, 2);
});

test("a stale-session error displays the explicit reload button", async () => {
  const { context, nodes } = setup();
  await context.operation("Uploading...", async () => {
    const error = new Error("Session changed");
    error.reload = true;
    throw error;
  });
  assert.equal(nodes.notice.textContent, "Session changed");
  assert.equal(nodes["reload-session"].hidden, false);
});

test("completed sign-in navigates to the management page immediately", async () => {
  const { context, requests } = setup();
  let destination;
  context.window.location.replace = (value) => { destination = value; };
  context.fetch = async (url, options) => {
    requests.push({ url, options });
    return { ok: true, json: async () => ({ status: "authenticated" }) };
  };
  await context.waitForSignIn();
  assert.equal(destination, "/");
  assert.equal(requests.length, 1);
  assert.equal(requests[0].url, "/auth/status");
  assert.ok(requests[0].options.signal instanceof AbortSignal);
});

test("a hung status request shows explicit reload recovery rather than waiting forever", async () => {
  const { context } = setup();
  context.fetch = async () => {
    const error = new Error("Status timeout");
    error.name = "TimeoutError";
    throw error;
  };
  await assert.rejects(
    context.waitForSignIn(),
    (error) => error.reload === true && error.message.includes("did not respond"),
  );
});

test("a timeout reading the status body preserves explicit reload recovery", async () => {
  const { context } = setup();
  context.fetch = async () => ({
    ok: true,
    json: async () => {
      const error = new Error("Status body timeout");
      error.name = "AbortError";
      throw error;
    },
  });
  await assert.rejects(
    context.waitForSignIn(),
    (error) => error.reload === true && error.message.includes("did not respond"),
  );
});

test("pending sign-in polling stops at its deadline without starting another login", async () => {
  const { context, requests } = setup();
  const times = [0, 0, 400000];
  context.Date = { now: () => times.shift() };
  context.window.setTimeout = (resolve) => resolve();
  context.fetch = async (url, options) => {
    requests.push({ url, options });
    return { ok: true, json: async () => ({ status: "pending" }) };
  };
  await assert.rejects(
    context.waitForSignIn(),
    (error) => error.reload === true && error.message.includes("not confirmed"),
  );
  assert.deepEqual(requests.map((request) => request.url), ["/auth/status"]);
});
