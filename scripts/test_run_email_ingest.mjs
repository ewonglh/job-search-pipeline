import assert from 'node:assert/strict';
import {EventEmitter} from 'node:events';
import {mkdtempSync, readdirSync, rmdirSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {Writable} from 'node:stream';
import {run} from './run-email-ingest.mjs';

class Child extends EventEmitter {
  constructor() {
    super();
    this.stderr = new EventEmitter();
    this.input = '';
    this.stdin = new Writable({write: (chunk, _encoding, done) => {
      this.input += chunk.toString();
      done();
    }});
  }
  kill() {}
}

const secret = ['pipe', 'only', 'runtime', 'value'].join('-');
const child = new Child();
const temp = mkdtempSync(join(tmpdir(), 'email-ingest-pipe-'));
let argv;
let childEnv;
process.env.SYNTHETIC_SECRET_CANARY = secret;
let logs = '';
const oldError = process.stderr.write;
process.stderr.write = function (chunk, ...rest) { logs += String(chunk); return oldError.call(this, chunk, ...rest); };
try {
  const exit = run(['--mode', 'initial'], {
    readConfig: async () => ({plugins: {entries: {imap: {config: {accounts: {
      synthetic: {host: 'mail.example.invalid', port: 993,
        user: 'user@example.invalid', password: {source: 'secret-ref'}}
    }}}}}}),
    resolveSecret: async () => secret,
    spawn: (_program, args, options) => {
      argv = args;
      childEnv = options.env;
      assert.deepEqual(options.stdio, ['pipe', 'inherit', 'pipe']);
      process.nextTick(() => child.emit('exit', 0));
      return child;
    },
    deadlineMs: 100,
  });
  assert.equal(await exit, 0);
  assert.equal(JSON.parse(child.input).password, secret);
  assert.equal(argv.includes(secret), false);
  assert.equal(Object.values(childEnv).includes(secret), false);
  assert.equal(Object.hasOwn(childEnv, 'SYNTHETIC_SECRET_CANARY'), false);
  assert.equal(argv.includes('--mode'), true);
  assert.equal(logs.includes(secret), false);
  assert.deepEqual(readdirSync(temp), []);

  await assert.rejects(run(['--password', secret], {
    readConfig: async () => { throw new Error(secret); },
  }), /managed_email_ingestion_unavailable/);
  assert.equal(logs.includes(secret), false);
} finally {
  process.stderr.write = oldError;
  delete process.env.SYNTHETIC_SECRET_CANARY;
  rmdirSync(temp);
}
console.log('test_run_email_ingest: all assertions passed');
