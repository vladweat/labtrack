import { defineConfig, devices } from '@playwright/test';

if (process.env.LABTRACK_REAL_ACCEPTANCE !== '1') {
  throw new Error(
    'Real local acceptance is disabled. Run scripts/run_stage_i_acceptance.sh to provision a fresh disposable database.',
  );
}

const webPort = Number(process.env.LABTRACK_ACCEPTANCE_WEB_PORT ?? 18417);
const apiProxyTarget = process.env.API_PROXY_TARGET ?? 'http://127.0.0.1:18000';
const baseURL = `http://127.0.0.1:${webPort}`;
const proxyUrl = new URL(apiProxyTarget);
const outputDir = process.env.LABTRACK_ACCEPTANCE_OUTPUT_DIR;
const acceptanceReporter = process.env.LABTRACK_ACCEPTANCE_REPORTER ?? 'list';

if (!Number.isInteger(webPort) || webPort < 1 || webPort > 65535) {
  throw new Error('LABTRACK_ACCEPTANCE_WEB_PORT must be a valid TCP port.');
}
if (proxyUrl.protocol !== 'http:' || !['127.0.0.1', 'localhost', '::1'].includes(proxyUrl.hostname)) {
  throw new Error('The real local acceptance API proxy must target loopback over HTTP.');
}
if (!outputDir || !outputDir.startsWith('/')) {
  throw new Error('LABTRACK_ACCEPTANCE_OUTPUT_DIR must point to an absolute temporary directory.');
}
if (!['list', 'json'].includes(acceptanceReporter)) {
  throw new Error('LABTRACK_ACCEPTANCE_REPORTER must be list or json.');
}

export default defineConfig({
  testDir: './e2e',
  testMatch: ['local_real_acceptance.spec.ts'],
  outputDir,
  fullyParallel: false,
  forbidOnly: true,
  retries: 0,
  workers: 1,
  reporter: acceptanceReporter,
  use: {
    ...devices['Desktop Chrome'],
    baseURL,
    serviceWorkers: 'allow',
    trace: 'off',
    screenshot: 'off',
    video: 'off',
  },
  webServer: {
    command: `npm run dev -- --host 127.0.0.1 --port ${webPort} --strictPort`,
    url: baseURL,
    reuseExistingServer: false,
    timeout: 30_000,
    env: { API_PROXY_TARGET: apiProxyTarget },
  },
});
