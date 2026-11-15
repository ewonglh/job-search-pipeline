// Optional OpenClaw adapter. SecretRef resolution stays in memory; the worker
// receives the result only over an anonymous stdin pipe.
import {pathToFileURL, fileURLToPath} from 'node:url';
import {dirname, join, resolve} from 'node:path';
import {spawn} from 'node:child_process';

const SDK_CONFIG = 'dist/plugin-sdk/runtime-config-snapshot.js';
const SDK_SECRET = 'dist/plugin-sdk/secret-input-runtime.js';
const PYTHON_OPTIONS = new Set([
  '--mode', '--since', '--until', '--repair-name', '--max-messages',
  '--max-seconds', '--max-body-bytes',
]);

function selectOptions(args) {
  const out = [];
  for (let i = 0; i < args.length; i += 1) {
    const key = args[i];
    if (!PYTHON_OPTIONS.has(key) || i + 1 >= args.length) {
      throw new Error('invalid_ingestion_option');
    }
    const value = String(args[++i]);
    if (!value || value.length > 120 || /[\0\r\n]/.test(value)) {
      throw new Error('invalid_ingestion_option');
    }
    if (key.startsWith('--max-') && !/^\d{1,4}$/.test(value)) {
      throw new Error('invalid_ingestion_option');
    }
    if ((key === '--since' || key === '--until') && !/^\d{4}-\d{2}-\d{2}$/.test(value)) {
      throw new Error('invalid_ingestion_option');
    }
    if (key === '--mode' && !['initial', 'backfill', 'repair'].includes(value)) {
      throw new Error('invalid_ingestion_option');
    }
    out.push(key, value);
  }
  return out;
}

function safeAccountId(value) {
  return typeof value === 'string' && /^[A-Za-z0-9_.@+-]{1,120}$/.test(value);
}

function safeHost(value) {
  return typeof value === 'string' && value.length <= 253
    && /^[A-Za-z0-9.-]+$/.test(value) && !value.startsWith('.')
    && !value.endsWith('.') && !value.includes('..');
}

function workerEnvironment(source) {
  const allowed = new Set([
    'PATH', 'HOME', 'USERPROFILE', 'HOMEDRIVE', 'HOMEPATH', 'SYSTEMROOT',
    'WINDIR', 'TMP', 'TEMP', 'TMPDIR', 'LANG', 'LC_ALL', 'PYTHONUTF8',
    'JOBSEARCH_TRACKER', 'JOBSEARCH_RECRUITMENT_LEDGER',
    'JOBSEARCH_INGEST_STATE', 'JOBSEARCH_EMAIL_ADDRESS', 'JOBSEARCH_EMAIL_FOLDERS',
  ]);
  return Object.fromEntries(Object.entries(source).filter(([key, value]) =>
    allowed.has(key) && typeof value === 'string'));
}

export async function run(args = [], deps = {}) {
  try {
    const flag = args.indexOf('--account');
    let accountId = flag >= 0 ? args[flag + 1] : undefined;
    if (flag >= 0 && !safeAccountId(accountId)) {
      throw new Error('managed_account_unavailable');
    }
    const optionArgs = flag < 0 ? args : args.filter((_value, index) =>
      index !== flag && index !== flag + 1);
    const options = selectOptions(optionArgs);
    const packageRoot = process.env.OPENCLAW_PACKAGE_ROOT;
    if (!packageRoot && (!deps.readConfig || !deps.resolveSecret)) {
      throw new Error('managed_adapter_unavailable');
    }
    const root = packageRoot ? resolve(packageRoot) : '';
    const readConfig = deps.readConfig || (await import(pathToFileURL(
      join(root, SDK_CONFIG)))).getRuntimeConfig;
    const resolveSecret = deps.resolveSecret || (await import(pathToFileURL(
      join(root, SDK_SECRET)))).resolveRequiredConfiguredSecretRefInputString;
    if (typeof readConfig !== 'function' || typeof resolveSecret !== 'function') {
      throw new Error('managed_adapter_unavailable');
    }
    const config = await readConfig();
    const accounts = config?.plugins?.entries?.imap?.config?.accounts || {};
    if (!accountId && Object.keys(accounts).length === 1) accountId = Object.keys(accounts)[0];
    const account = accounts[accountId];
    const port = account?.port == null ? 993 : Number(account.port);
    if (!safeAccountId(accountId) || !account || account.secure === false
        || !safeHost(account.host) || !Number.isInteger(port)
        || port < 1 || port > 65535
        || typeof account.user !== 'string' || account.user.length > 254
        || /[\0\r\n]/.test(account.user)
        || !account.password || typeof account.password !== 'object') {
      throw new Error('managed_account_unavailable');
    }
    const password = await resolveSecret({
      config,
      env: process.env,
      value: account.password,
      path: `plugins.entries.imap.config.accounts.${accountId}.password`,
      unresolvedReasonStyle: 'generic',
    });
    if (typeof password !== 'string' || !password || password.length > 16384
        || password.includes('\0')) {
      throw new Error('managed_secret_unavailable');
    }
    const credential = JSON.stringify({
      account: accountId,
      host: account.host,
      port,
      user: account.user,
      password,
    });
    const worker = deps.worker || join(dirname(fileURLToPath(import.meta.url)), 'email_ingest.py');
    const python = deps.python || process.env.JOBSEARCH_PYTHON
      || (process.platform === 'win32' ? 'python' : 'python3');
    const child = (deps.spawn || spawn)(python,
      [worker, 'managed-scan', '--credential-stdin', ...options],
      {stdio: ['pipe', 'inherit', 'pipe'], env: workerEnvironment(process.env)});
    // Library or server errors may include auth data; never forward stderr.
    child.stderr?.on('data', () => {});
    child.stdin?.on('error', () => {});
    child.stdin.end(credential);
    return await new Promise((resolveExit, rejectExit) => {
      let killTimer;
      const deadline = setTimeout(() => {
        child.kill('SIGTERM');
        killTimer = setTimeout(() => child.kill('SIGKILL'), 5000);
        killTimer.unref?.();
      }, deps.deadlineMs || 240000);
      deadline.unref?.();
      const cleanup = () => { clearTimeout(deadline); clearTimeout(killTimer); };
      child.on('error', () => { cleanup(); rejectExit(new Error('ingest_process_failed')); });
      child.on('exit', code => { cleanup(); resolveExit(code ?? 1); });
    });
  } catch (_) {
    // Do not expose SDK resolution or child-process errors to logs or callers.
    throw new Error('managed_email_ingestion_unavailable');
  }
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  try {
    const args = process.argv.slice(2);
    const filtered = [];
    for (let i = 0; i < args.length; i += 1) {
      if (args[i] === '--account' && i + 1 < args.length) {
        filtered.push(args[i], args[++i]);
      } else {
        filtered.push(args[i]);
      }
    }
    process.exitCode = await run(filtered);
  } catch (_) {
    process.stderr.write('Recruitment ingestion unavailable; diagnostics were suppressed.\n');
    process.exitCode = 2;
  }
}
