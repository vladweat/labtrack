import { defineConfig, devices } from '@playwright/test';

const publicDemoOrigin = 'https://labtrack.wvzl.site';
const configuredOrigin = process.env.LABTRACK_PUBLIC_DEMO_URL ?? publicDemoOrigin;

if (configuredOrigin !== publicDemoOrigin) {
  throw new Error(`Public demo smoke is restricted to ${publicDemoOrigin}.`);
}

export default defineConfig({
  testDir: './external-smoke',
  testMatch: 'public-demo.spec.ts',
  outputDir: 'test-results/public-demo-smoke',
  fullyParallel: false,
  forbidOnly: true,
  retries: 0,
  workers: 1,
  timeout: 45_000,
  expect: { timeout: 10_000 },
  reporter: 'list',
  use: {
    ...devices['Desktop Chrome'],
    baseURL: publicDemoOrigin,
    ignoreHTTPSErrors: false,
    serviceWorkers: 'block',
    trace: 'off',
    screenshot: 'off',
    video: 'off',
  },
});
