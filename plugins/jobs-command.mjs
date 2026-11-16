import { spawnSync as nodeSpawnSync } from "node:child_process";
import { fileURLToPath } from "node:url";
import path from "node:path";

const PLUGIN_DIR = path.dirname(fileURLToPath(import.meta.url));
const PROJECTION_SCRIPT = path.resolve(PLUGIN_DIR, "../scripts/projection.py");
const AUTH_CONTEXT_ERROR =
  "OpenClaw did not provide the supported authorization state. Install the verified 2026.10.1 command API.";

export function validatePrivateDashboardUrl(value) {
  if (typeof value !== "string" || value.trim() === "" || value.length > 2048) return null;
  let url;
  try {
    url = new URL(value.trim());
  } catch {
    return null;
  }
  if (url.protocol !== "https:" || url.username || url.password || url.search || url.hash) {
    return null;
  }
  const host = url.hostname.toLowerCase().replace(/^\[|\]$/g, "").replace(/\.$/, "");
  const allowedPaths = new Set(["", "/", "/jobs", "/jobs/", "/index.html", "/jobs/index.html"]);
  if (!allowedPaths.has(url.pathname)) return null;

  const ipv4 = host.split(".").map((part) => (/^\d+$/.test(part) ? Number(part) : NaN));
  const isIpv4 = ipv4.length === 4 && ipv4.every((part) => Number.isInteger(part) && part >= 0 && part <= 255);
  const privateIpv4 = isIpv4 && (
    ipv4[0] === 10 || ipv4[0] === 127 ||
    (ipv4[0] === 172 && ipv4[1] >= 16 && ipv4[1] <= 31) ||
    (ipv4[0] === 192 && ipv4[1] === 168) ||
    (ipv4[0] === 100 && ipv4[1] >= 64 && ipv4[1] <= 127)
  );
  const isPrivateName = host === "localhost" || host.endsWith(".localhost") ||
    host === "home.arpa" || host.endsWith(".home.arpa") ||
    host === "ts.net" || host.endsWith(".ts.net");
  const isPrivateIpv6 = host === "::1" || /^f[cd][0-9a-f:]*$/i.test(host);
  return privateIpv4 || isPrivateIpv6 || isPrivateName ? url.toString() : null;
}

export function readProjectionCompact({ spawnSync = nodeSpawnSync, env = process.env,
  scriptPath = PROJECTION_SCRIPT } = {}) {
  const executable = typeof env.JOBSEARCH_PYTHON === "string" && env.JOBSEARCH_PYTHON.trim()
    ? env.JOBSEARCH_PYTHON.trim()
    : "python3";
  let result;
  const childEnv = {};
  for (const key of ["PATH", "HOME", "USERPROFILE", "SystemRoot", "WINDIR", "TMPDIR", "TEMP",
    "JOBSEARCH_PROJECTION_DB", "JOBSEARCH_TIMEZONE"]) {
    if (typeof env[key] === "string") childEnv[key] = env[key];
  }
  childEnv.PYTHONIOENCODING = "utf-8";
  try {
    result = spawnSync(executable, [scriptPath, "report", "compact"], {
      encoding: "utf8",
      env: childEnv,
      windowsHide: true,
      timeout: 15000,
      maxBuffer: 128 * 1024,
    });
  } catch {
    throw new Error("cached projection could not be read");
  }
  if (!result || result.error || result.status !== 0 || typeof result.stdout !== "string") {
    throw new Error("cached projection could not be read");
  }
  const text = result.stdout.trimEnd();
  if (!text) throw new Error("cached projection could not be read");
  return text;
}

export function registerJobsCommand(api, {
  readCompact = () => readProjectionCompact(),
  dashboardUrl = () => process.env.JOBSEARCH_DASHBOARD_URL,
} = {}) {
  if (!api || typeof api.registerCommand !== "function") {
    throw new Error("Unsupported OpenClaw setup: api.registerCommand is required (verified with 2026.10.1).");
  }

  api.registerCommand({
    name: "jobs",
    description: "Show the cached jobs summary and private dashboard link.",
    acceptsArgs: false,
    requireAuth: true,
    handler: async (ctx) => {
      if (!ctx || ctx.isAuthorizedSender !== true) {
        return { text: ctx && ctx.isAuthorizedSender === false
          ? "This command is not authorized for this sender."
          : AUTH_CONTEXT_ERROR };
      }
      let compact;
      try {
        compact = readCompact();
      } catch {
        return { text: "Jobs snapshot unavailable: the committed projection could not be read. Collect it separately, then retry." };
      }
      if (typeof compact !== "string" || !compact.trim()) {
        return { text: "Jobs snapshot unavailable: the committed projection could not be read. Collect it separately, then retry." };
      }

      let configured;
      try {
        configured = dashboardUrl();
      } catch {
        configured = null;
      }
      const link = validatePrivateDashboardUrl(configured);
      const dashboardLine = link
        ? `Private dashboard: ${link}`
        : configured == null || String(configured).trim() === ""
          ? "Private dashboard: not configured (set JOBSEARCH_DASHBOARD_URL)."
          : "Private dashboard: invalid configuration; use private HTTPS without credentials, query, or fragment.";
      return { text: `${compact.trimEnd()}\n${dashboardLine}` };
    },
  });
}

const plugin = {
  id: "jobs-command",
  name: "Jobs command",
  description: "Authenticated, deterministic jobs status from the cached projection.",
  register(api) {
    registerJobsCommand(api);
  },
};

export default plugin;
