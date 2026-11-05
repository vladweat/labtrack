import { describe, expect, it } from 'vitest';
import {
  localizeApiError,
  localizeCommandError,
  safeErrorMessage,
  shipmentStatusLabel,
} from './localization';

describe('Russian UI localization fallbacks', () => {
  it('does not expose unknown English API errors', () => {
    expect(localizeApiError('Unexpected server error', 409)).toBe(
      'Действие невозможно в текущем состоянии. Обновите данные и повторите попытку.',
    );
  });

  it('does not expose mixed-language command errors', () => {
    expect(localizeCommandError(undefined, 'Ошибка while syncing')).toBe(
      'Сервер не применил команду. Загрузите актуальный снимок и повторите синхронизацию.',
    );
  });

  it('does not expose mixed-language runtime errors', () => {
    expect(safeErrorMessage(new Error('Ошибка: NetworkError'), 'Повторите попытку.')).toBe(
      'Повторите попытку.',
    );
  });

  it('translates known account-isolation errors into Russian', () => {
    expect(safeErrorMessage(
      new Error('The active account changed; the local write was rejected.'),
      'Повторите попытку.',
    )).toBe('Учётная запись изменилась в другой вкладке. Войдите снова.');
  });

  it('does not expose unknown status codes', () => {
    expect(shipmentStatusLabel('FUTURE_STATUS')).toBe('Другой статус');
  });
});
