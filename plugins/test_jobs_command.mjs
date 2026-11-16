import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import plugin, { registerJobsCommand, readProjectionCompact, validatePrivateDashboardUrl } from "./jobs-command.mjs";

const manifest = JSON.parse(readFileSync(new URL("./openclaw.plugin.json", import.meta.url), "utf8"));
const packageInfo = JSON.parse(readFileSync(new URL("./package.json", import.meta.url), "utf8"));
assert.equal(manifest.id, "jobs-command");
assert.equal(packageInfo.openclaw.extensions[0], "./jobs-command.mjs");
assert.equal(plugin.id, "jobs-command");

const compact = [
  "Jobs snapshot synthetic-snapshot · as of 2026-11-16 · generated 2026-11-16T06:00:00Z",
  "Daily goal (Asia/Singapore): 1 / 1 confirmed · coverage=complete · ready queue=1/1",
  "Queue: queued 1",
  "Attention: stale in progress 0 · outcome unknown 0 · sync pending/error 0 · snapshot gaps 0",
  "Sources: queue=fresh age=0s, tracker=stale age=172800s",
].join("\n");

function makeSdk() {
  const registered = [];
  const calls = { model: 0, transport: 0 };
  const api = {
    registerCommand(definition) { registered.push(definition); },
    invokeModel() { calls.model += 1; throw new Error("unexpected model invocation"); },
    sendMessage() { calls.transport += 1; throw new Error("unexpected transport invocation"); },
  };
  return { api, registered, calls };
}

async function main() {
  const pluginSdk = makeSdk();
  plugin.register(pluginSdk.api);
  assert.equal(pluginSdk.registered.length, 1);

  const sdk = makeSdk();
  let reads = 0;
  registerJobsCommand(sdk.api, {
    readCompact() { reads += 1; return compact; },
    dashboardUrl: () => "https://jobs.tailnet.ts.net/jobs",
  });
  assert.equal(sdk.registered.length, 1);
  const command = sdk.registered[0];
  assert.equal(command.name, "jobs");
  assert.equal(command.acceptsArgs, false);
  assert.equal(command.requireAuth, true);
  assert.equal(await command.handler({ isAuthorizedSender: false }).then((x) => x.text),
    "This command is not authorized for this sender.");
  assert.equal(reads, 0, "unauthorized calls do not read the cached index");
  assert.match((await command.handler({}).then((x) => x.text)), /authorization state/);
  assert.match((await command.handler({ senderId: "synthetic-user" }).then((x) => x.text)),
    /authorization state/, "senderId is not treated as an authorization field");
  assert.equal(reads, 0, "unsupported auth context fails closed");
  const response = await command.handler({ isAuthorizedSender: true });
  assert.equal(response.text, `${compact}\nPrivate dashboard: https://jobs.tailnet.ts.net/jobs`);
  assert.equal(reads, 1);
  assert.match(response.text, /tracker=stale/);
  assert.equal(sdk.calls.model, 0);
  assert.equal(sdk.calls.transport, 0);

  assert.throws(() => registerJobsCommand({}), /api\.registerCommand.*2026\.10\.1/);
  for (const accepted of [
    "https://dashboard.example.com/jobs",
    "http://jobs.ts.net/jobs",
    "https://user:pass@jobs.ts.net/jobs",
    "https://jobs.ts.net/jobs?token=synthetic",
    "https://jobs.ts.net/jobs#synthetic",
    "https://jobs.ts.net/token/secret-value",
    "https://jobs.ts.net/custom/private-path",
  ]) assert.equal(validatePrivateDashboardUrl(accepted), null, accepted);
  assert.equal(validatePrivateDashboardUrl("https://jobs.tailnet.ts.net/jobs"),
    "https://jobs.tailnet.ts.net/jobs");
  assert.equal(validatePrivateDashboardUrl("https://192.168.1.4/jobs"),
    "https://192.168.1.4/jobs");

  const noLink = makeSdk();
  registerJobsCommand(noLink.api, { readCompact: () => compact, dashboardUrl: () => undefined });
  assert.match((await noLink.registered[0].handler({ isAuthorizedSender: true })).text,
    /Private dashboard: not configured/);

  const invalidLink = makeSdk();
  registerJobsCommand(invalidLink.api, {
    readCompact: () => compact,
    dashboardUrl: () => "https://public.example.com/?token=synthetic",
  });
  const invalidText = (await invalidLink.registered[0].handler({ isAuthorizedSender: true })).text;
  assert.match(invalidText, /invalid configuration/);
  assert.doesNotMatch(invalidText, /public\.example|token=synthetic/);

  const failing = makeSdk();
  registerJobsCommand(failing.api, {
    readCompact() { throw new Error("C:\\private\\index.sqlite3 is unreadable"); },
    dashboardUrl: () => "https://jobs.tailnet.ts.net/jobs",
  });
  const failure = (await failing.registered[0].handler({ isAuthorizedSender: true })).text;
  assert.match(failure, /Jobs snapshot unavailable/);
  assert.doesNotMatch(failure, /private\\index|sqlite3|Error/);

  const invoked = [];
  const output = readProjectionCompact({
    env: { JOBSEARCH_PYTHON: "python3", JOBSEARCH_PROJECTION_DB: "synthetic.sqlite3",
      OPENCLAW_TOKEN: "synthetic-do-not-forward" },
    scriptPath: "scripts/projection.py",
    spawnSync(executable, args, options) {
      invoked.push({ executable, args, options });
      return { status: 0, stdout: `${compact}\n`, stderr: "" };
    },
  });
  assert.equal(output, compact, "the command keeps the shared CLI formatter byte-for-byte");
  assert.equal(invoked.length, 1);
  assert.deepEqual(invoked[0].args, ["scripts/projection.py", "report", "compact"]);
  assert.equal(invoked[0].options.timeout, 15000);
  assert.equal(invoked[0].options.env.JOBSEARCH_PROJECTION_DB, "synthetic.sqlite3");
  assert.equal(invoked[0].options.env.OPENCLAW_TOKEN, undefined);
  assert.throws(() => readProjectionCompact({
    env: {}, scriptPath: "projection.py",
    spawnSync: () => ({ status: 1, stdout: "", stderr: "private path" }),
  }), /cached projection could not be read/);

  console.log("test_jobs_command: all assertions passed");
}

await main();
