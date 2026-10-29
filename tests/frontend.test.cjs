const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const source = readFileSync(path.join(__dirname, "..", "static", "app.js"), "utf8");

function setup(session = { contextId: "page-session", csrfToken: "fresh-token", authenticated: true }) {
  const requests = [];
  const nodes = {
    notice: { hidden: true },
    "reload-session": { hidden: true, addEventListener() {} },
  };
  const hiddenInput = { value: "old-token" };
  const context = vm.createContext({
    FormData,
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
