import { expect, test } from '@playwright/test';

test('public synthetic demo renders its sign-in view and reports readiness', async ({
  context,
  page,
  request,
  baseURL,
}) => {
  const pageErrors: string[] = [];
  const blockedRequests: string[] = [];
  const expectedOrigin = 'https://labtrack.wvzl.site';
  expect(baseURL).toBe(expectedOrigin);

  page.on('pageerror', (error) => pageErrors.push(error.message));
  await context.route('**/*', async (route) => {
    const outgoing = route.request();
    const origin = new URL(outgoing.url()).origin;
    if (outgoing.method() !== 'GET' || origin !== expectedOrigin) {
      blockedRequests.push(`${outgoing.method()} ${origin}`);
      await route.abort('blockedbyclient');
      return;
    }
    await route.continue();
  });

  const response = await page.goto('/', { waitUntil: 'domcontentloaded' });
  expect(response?.status()).toBe(200);
  expect(new URL(page.url()).origin).toBe(expectedOrigin);
  await expect(page.getByRole('heading', { name: 'Sign in to continue' })).toBeVisible();
  await expect(page.getByLabel('Email', { exact: true })).toBeVisible();
  await expect(page.getByLabel('Password', { exact: true })).toBeVisible();
  await expect(page.getByRole('button', { name: 'Sign in', exact: true })).toBeVisible();
  await expect(page.getByRole('note', { name: 'Shared demo notice' })).toContainText(
    'Demo accounts and data are shared and synthetic.',
  );

  const readiness = await request.get('/health/ready', { maxRedirects: 0, timeout: 15_000 });
  expect(readiness.status()).toBe(200);
  expect(await readiness.json()).toEqual({ status: 'ok' });
  expect(blockedRequests).toEqual([]);
  expect(pageErrors).toEqual([]);
});
