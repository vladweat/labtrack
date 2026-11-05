import { expect, test } from '@playwright/test';

test('the app shell and sign-in screen use Russian', async ({ page }) => {
  await page.goto('/');

  await expect(page.locator('html')).toHaveAttribute('lang', 'ru');
  await expect(page).toHaveTitle('Курьер LabTrack');
  await expect(page.getByRole('heading', { name: 'Вход в LabTrack' })).toBeVisible();
  await expect(page.getByLabel('Электронная почта')).toBeVisible();
  await expect(page.getByLabel('Пароль')).toBeVisible();
  await expect(page.getByRole('button', { name: 'Войти' })).toBeVisible();
});

test('shows API sign-in failures in Russian', async ({ page }) => {
  await page.route('**/api/v1/auth/token', (route) => route.fulfill({
    status: 401,
    json: { detail: 'Invalid email or password' },
  }));
  await page.goto('/');
  await page.getByLabel('Электронная почта').fill('viewer@example.test');
  await page.getByLabel('Пароль').fill('wrong-password');
  await page.getByRole('button', { name: 'Войти' }).click();
  await expect(page.getByRole('alert')).toHaveText('Неверный адрес электронной почты или пароль.');
});
