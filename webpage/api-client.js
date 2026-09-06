import {
  QuantApiContractError,
  quantApiErrorMessage,
  validateQuantApiPayload,
} from "./api-contracts.js";

const DEFAULT_CACHE_LIMIT = 48;

function cacheTtlFor(url, method) {
  if (method !== "GET") return 0;
  const path = new URL(url, window.location.origin).pathname;
  if (path.endsWith("/events") || path.endsWith("/markets")) return 15_000;
  if (path.endsWith("/backtest-runs")) return 4_000;
  if (path.endsWith("/health")) return 2_000;
  return 0;
}

function clone(value) {
  return typeof structuredClone === "function"
    ? structuredClone(value)
    : JSON.parse(JSON.stringify(value));
}

export function createQuantApiClient({ cacheLimit = DEFAULT_CACHE_LIMIT } = {}) {
  const cache = new Map();
  const inFlight = new Map();

  function setCache(key, value, ttlMs) {
    cache.delete(key);
    cache.set(key, { expiresAt: Date.now() + ttlMs, value: clone(value) });
    while (cache.size > cacheLimit) cache.delete(cache.keys().next().value);
  }

  async function request(url, {
    signal,
    timeoutMs = 12_000,
    cacheTtlMs,
    validate = true,
    ...options
  } = {}) {
    const method = String(options.method || "GET").toUpperCase();
    const key = `${method}:${url}`;
    const ttlMs = Number.isFinite(cacheTtlMs) ? Math.max(0, cacheTtlMs) : cacheTtlFor(url, method);
    const cached = cache.get(key);
    if (cached && cached.expiresAt > Date.now()) return clone(cached.value);
    if (cached) cache.delete(key);
    if (method === "GET" && inFlight.has(key)) return clone(await inFlight.get(key));

    const task = (async () => {
      const controller = new AbortController();
      const timer = setTimeout(() => controller.abort("timeout"), timeoutMs);
      const abort = () => controller.abort(signal?.reason || "upstream abort");
      signal?.addEventListener?.("abort", abort, { once: true });
      try {
        const response = await fetch(url, {
          cache: "no-store",
          ...options,
          method,
          signal: controller.signal,
        });
        const payload = await response.json().catch(() => ({}));
        if (!response.ok) throw new Error(quantApiErrorMessage(payload, response));
        const result = validate ? validateQuantApiPayload(url, payload, { method }) : payload;
        if (ttlMs > 0) setCache(key, result, ttlMs);
        return result;
      } catch (error) {
        if (controller.signal.aborted && !signal?.aborted) {
          throw new Error(`Request timed out after ${timeoutMs}ms`);
        }
        throw error;
      } finally {
        clearTimeout(timer);
        signal?.removeEventListener?.("abort", abort);
      }
    })();

    if (method === "GET") inFlight.set(key, task);
    try {
      return clone(await task);
    } finally {
      if (inFlight.get(key) === task) inFlight.delete(key);
    }
  }

  return {
    request,
    clearCache: () => cache.clear(),
    debugState: () => ({ cacheEntries: cache.size, inFlight: inFlight.size }),
    ContractError: QuantApiContractError,
  };
}
