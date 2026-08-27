/**
 * Окружение smoke-тестов рендера.
 *
 * localStorage: jsdom этой версии его не отдаёт (а Node ругается, что своё
 * хранилище требует --localstorage-file), тогда как витрина читает оттуда тему,
 * watchlist и настройки фильтров прямо в теле компонентов. Без хранилища первый
 * же рендер падал бы на getItem — то есть тест ловил бы собственную обвязку
 * вместо кода приложения. Кладём простое хранилище в памяти.
 */
function memoryStorage() {
  let map = new Map();
  return {
    getItem: (k) => (map.has(String(k)) ? map.get(String(k)) : null),
    setItem: (k, v) => { map.set(String(k), String(v)); },
    removeItem: (k) => { map.delete(String(k)); },
    clear: () => { map = new Map(); },
    key: (i) => [...map.keys()][i] ?? null,
    get length() { return map.size; },
  };
}

for (const target of [globalThis, globalThis.window]) {
  if (target && !target.localStorage) {
    Object.defineProperty(target, "localStorage", {
      value: memoryStorage(), configurable: true, writable: true,
    });
  }
}

// Наблюдатели размеров: витрина меряет ими контейнеры графиков, в jsdom их нет.
if (!globalThis.ResizeObserver) {
  globalThis.ResizeObserver = class {
    observe() {} unobserve() {} disconnect() {}
  };
}
if (!globalThis.matchMedia) {
  globalThis.matchMedia = () => ({
    matches: false, addListener() {}, removeListener() {},
    addEventListener() {}, removeEventListener() {},
  });
}
