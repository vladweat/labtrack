const SHIPMENT_STATUS_LABELS: Record<string, string> = {
  DRAFT: 'Черновик',
  READY_FOR_PICKUP: 'Готово к забору',
  PICKUP_PENDING: 'Ожидает забора',
  IN_TRANSIT: 'В пути',
  DELIVERY_PENDING: 'Ожидает доставки',
  DELIVERY_BLOCKED: 'Доставка заблокирована',
  PARTIALLY_DELIVERED: 'Доставлено частично',
  RECEIVING: 'Приёмка',
  COMPLETED: 'Завершено',
  COMPLETED_WITH_ISSUES: 'Завершено с замечаниями',
  CLOSED_UNDELIVERED: 'Закрыто без доставки',
  CANCELLED: 'Отменено',
};

const CONTAINER_STATUS_LABELS: Record<string, string> = {
  PREPARED: 'Подготовлен',
  IN_TRANSIT: 'В пути',
  DELIVERY_PENDING: 'Ожидает доставки',
  DELIVERY_BLOCKED: 'Доставка заблокирована',
  RECEIVED: 'Принят',
  CLOSED_UNDELIVERED: 'Закрыт без доставки',
};

const COMMAND_STATE_LABELS: Record<string, string> = {
  PENDING_SYNC: 'Ожидает синхронизации',
  SYNCED: 'Синхронизировано',
  REJECTED: 'Отклонено',
  REVIEW_REQUIRED: 'Требуется проверка',
};

const RECEIPT_RESULT_LABELS: Record<string, string> = {
  ACCEPTED: 'Принят',
  MISSING: 'Отсутствует',
  REJECTED: 'Отклонён',
};

const MATERIAL_TYPE_LABELS: Record<string, string> = {
  BLOOD: 'Кровь',
  URINE: 'Моча',
  OTHER: 'Другое',
};

const ISSUE_REASON_LABELS: Record<string, string> = {
  SAMPLE_MISSING: 'Образец отсутствует',
  SAMPLE_REJECTED: 'Образец отклонён',
  CONTAINER_MISSING: 'Контейнер отсутствует',
  CONTAINER_REJECTED: 'Контейнер отклонён',
};

const API_ERROR_LABELS: Record<string, string> = {
  'Invalid email or password': 'Неверный адрес электронной почты или пароль.',
  'Too many failed login attempts. Try again later.': 'Слишком много неудачных попыток входа. Попробуйте позже.',
  'Invalid or expired access token': 'Срок действия сеанса истёк. Войдите снова.',
  'Not authenticated': 'Для продолжения войдите в систему.',
  'Insufficient role': 'Для этого действия недостаточно прав.',
  'Resource not found': 'Запись не найдена или недоступна.',
  'from must precede to': 'Начальная дата должна быть раньше конечной.',
  'region_id is required for multiple authorized regions': 'Выберите регион для просмотра данных.',
  'Shipment must be sealed before Pickup': 'Перед забором необходимо опечатать отправку.',
  'Container codes must match the complete sealed manifest': 'Укажите точный набор кодов опечатанных контейнеров.',
  'Whole-Shipment Pickup must be confirmed first': 'Сначала подтвердите забор всей отправки.',
  'Container is not in transit': 'Контейнер ещё не передан в доставку.',
  'Container code does not match the sealed manifest': 'Код контейнера не совпадает с опечатанным манифестом.',
  'Every item in a received container must have a result': 'Укажите результат для каждого образца в принятом контейнере.',
  'Reason code does not match the result': 'Выбранная причина не соответствует результату.',
  'Receiving session is not open': 'Сеанс приёмки закрыт. Откройте его повторно.',
  'Shipment is not receiving': 'Отправка сейчас не находится на приёмке.',
  'Every container must be received or closed as undelivered': 'Примите каждый контейнер или закройте его без доставки.',
  'At least one container must be received': 'Для завершения приёмки нужно принять хотя бы один контейнер.',
  'Configured destination and future delivery time are required': 'Укажите лабораторию-получателя и будущую дату доставки.',
  'Sender and configured destination laboratory must share a region': 'Организация отправителя и лаборатория-получатель должны находиться в одном регионе.',
  'Shipment must contain at least one container': 'Добавьте в отправку хотя бы один контейнер.',
  'Shipment is no longer a draft': 'Черновик отправки уже изменён. Обновите данные.',
};

const COMMAND_ERROR_LABELS: Record<string, string> = {
  OFFLINE_SNAPSHOT_EXPIRED: 'Снимок старше 24 часов. Требуется проверка и получение нового снимка.',
  SNAPSHOT_EXPIRED: 'Снимок старше 24 часов. Требуется проверка и получение нового снимка.',
  INVALID_SNAPSHOT: 'Снимок недействителен для этой учётной записи. Загрузите новый снимок.',
  OFFLINE_COMMAND_STALE: 'Данные изменились после создания команды. Загрузите новый снимок.',
  INVALID_DEPENDENCY: 'Не удалось связать доставку с командой забора.',
  COMMAND_REJECTED: 'Сервер отклонил команду. Проверьте данные отправки и повторите действие.',
};

const RUNTIME_ERROR_LABELS: Record<string, string> = {
  'The active account changed; the local write was rejected.': 'Учётная запись изменилась в другой вкладке. Войдите снова.',
  'The active account changed; local data was not purged.': 'Учётная запись изменилась в другой вкладке. Данные не удалены.',
  'The queued command status is missing from local storage.': 'Не удалось обновить состояние команды. Обновите страницу и повторите попытку.',
  'A replay acknowledgement is missing its original outcome.': 'Не удалось восстановить ответ сервера для повторной команды.',
};

const DATE_TIME_FORMAT = new Intl.DateTimeFormat('ru-RU', {
  dateStyle: 'medium',
  timeStyle: 'short',
});

function isRussianTextWithoutLatin(message: string): boolean {
  return /[А-Яа-яЁё]/.test(message) && !/[A-Za-z]/.test(message);
}

export function shipmentStatusLabel(status: string): string {
  return SHIPMENT_STATUS_LABELS[status] ?? 'Другой статус';
}

export function containerStatusLabel(status: string): string {
  return CONTAINER_STATUS_LABELS[status] ?? 'Другой статус';
}

export function commandStateLabel(state: string): string {
  return COMMAND_STATE_LABELS[state] ?? 'Неизвестное состояние';
}

export function receiptResultLabel(result: string | null): string {
  return result ? RECEIPT_RESULT_LABELS[result] ?? 'Другой результат' : 'Не указан';
}

export function materialTypeLabel(materialType: string): string {
  return MATERIAL_TYPE_LABELS[materialType] ?? 'Другой материал';
}

export function issueReasonLabel(code: string): string {
  return ISSUE_REASON_LABELS[code] ?? 'Другая причина';
}

export function receivingSessionStatusLabel(status: string): string {
  if (status === 'OPEN') return 'Открыта';
  if (status === 'CLOSED') return 'Закрыта';
  return 'Неизвестное состояние';
}

export function localizeApiError(message: string, status: number): string {
  const translated = API_ERROR_LABELS[message.trim()];
  if (translated) return translated;
  if (isRussianTextWithoutLatin(message)) return message;
  if (status === 401) return 'Срок действия сеанса истёк или данные входа неверны. Проверьте их и повторите попытку.';
  if (status === 403) return 'Для этого действия недостаточно прав.';
  if (status === 404) return 'Запись не найдена или недоступна.';
  if (status === 409) return 'Действие невозможно в текущем состоянии. Обновите данные и повторите попытку.';
  if (status === 422) return 'Проверьте введённые данные и повторите попытку.';
  if (status === 429) return 'Слишком много запросов. Подождите и повторите попытку.';
  if (status >= 500) return 'Сервер временно не может выполнить запрос. Попробуйте позже.';
  return `Не удалось выполнить запрос (код ${status}). Повторите попытку.`;
}

export function localizeCommandError(code: string | undefined, message: string | undefined): string {
  if (code && COMMAND_ERROR_LABELS[code]) return COMMAND_ERROR_LABELS[code];
  if (message && isRussianTextWithoutLatin(message)) return message;
  return 'Сервер не применил команду. Загрузите актуальный снимок и повторите синхронизацию.';
}

export function safeErrorMessage(error: unknown, fallback: string): string {
  if (error instanceof Error) {
    const translated = RUNTIME_ERROR_LABELS[error.message.trim()];
    if (translated) return translated;
    if (isRussianTextWithoutLatin(error.message)) return error.message;
  }
  return fallback;
}

export function formatDateTime(value: string): string {
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : DATE_TIME_FORMAT.format(date);
}
