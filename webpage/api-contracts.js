/**
 * @typedef {Object} QuantApiEnvelope
 * @property {unknown[]=} items
 * @property {unknown=} item
 * @property {string=} error
 * @property {string=} status
 * @property {string|number=} requestId
 */

/**
 * @typedef {Object} PriceWindowEnvelope
 * @property {unknown[]} outcomes
 * @property {Object=} event
 * @property {Object=} dataQuality
 * @property {Object=} chartQuality
 * @property {string=} requestId
 */

/**
 * @typedef {Object} RunProgressEnvelope
 * @property {number} runId
 * @property {string} status
 * @property {number=} progress
 * @property {string=} phase
 * @property {number=} currentX
 * @property {number=} rowsProcessed
 * @property {number=} totalRows
 * @property {string=} error
 */

export class QuantApiContractError extends Error {
  constructor(message, { url = "", payload = null } = {}) {
    super(message);
    this.name = "QuantApiContractError";
    this.url = url;
    this.payload = payload;
  }
}

function assertObject(payload, url) {
  if (!payload || typeof payload !== "object" || Array.isArray(payload)) {
    throw new QuantApiContractError("Quant API returned a non-object payload", { url, payload });
  }
}

/**
 * Validate the stable outer contract without inventing missing domain data.
 * Endpoint-specific payloads remain available unchanged after validation.
 *
 * @param {string} url
 * @param {unknown} payload
 * @param {{method?: string}} options
 * @returns {QuantApiEnvelope|PriceWindowEnvelope|RunProgressEnvelope}
 */
export function validateQuantApiPayload(url, payload, { method = "GET" } = {}) {
  assertObject(payload, url);
  const path = new URL(url, window.location.origin).pathname;
  const requestMethod = String(method).toUpperCase();
  if (path.endsWith("/price-window") || path.endsWith("/event-price-tile")) {
    if (!Array.isArray(payload.outcomes)) {
      throw new QuantApiContractError("Price-window payload is missing outcomes[]", { url, payload });
    }
  }
  if (/\/backtest-runs\/\d+\/progress$/.test(path)) {
    const item = payload.item;
    assertObject(item, url);
    if (!Number.isInteger(Number(item.runId ?? item.run_id)) || !String(item.status || "")) {
      throw new QuantApiContractError("Run progress payload is missing runId or status", { url, payload });
    }
  }
  if (
    requestMethod === "GET"
    && (path.endsWith("/events") || path.endsWith("/markets") || path.endsWith("/backtest-runs"))
  ) {
    if (!Array.isArray(payload.items)) {
      throw new QuantApiContractError("Collection payload is missing items[]", { url, payload });
    }
  }
  return payload;
}

export function quantApiErrorMessage(payload, response) {
  return String(
    payload?.error
    || payload?.message
    || `API ${response.status}: ${response.statusText || "request failed"}`,
  );
}
