export type JsonObject = Record<string, unknown>;

export interface Page<T> {
  items: T[];
  next_cursor: string | null;
}

export interface PaperAccount extends JsonObject {
  account_id: string;
  default_strategy_id: string;
  name: string;
  status: string;
}

export interface PaperOrder extends JsonObject {
  intent_id: number;
  account_id: string;
  asset_id: string;
  status: string;
}

export interface PaperPerformance extends JsonObject {
  account: PaperAccount;
  summary: JsonObject;
  current: JsonObject | null;
  history: JsonObject[];
  next_cursor: string | null;
  data_quality: JsonObject;
}

export interface PaperOrderAudit extends JsonObject {
  order: PaperOrder;
  timeline: JsonObject[];
  fills: JsonObject[];
  ledger: JsonObject[];
  quality: JsonObject;
}

export interface ReplaySession extends JsonObject {
  replay_session_id: string;
  account_id: string;
  parent_replay_session_id?: string | null;
  name: string;
  status: "CREATED" | "RUNNING" | "PAUSED" | "COMPLETED" | "FAILED" | "CANCELLED";
  event_count: number;
  cursor_event_index: number;
  data_hash: string;
  artifact_hash?: string | null;
}

export interface CreateReplaySession extends JsonObject {
  account_id: string;
  strategy_id?: string;
  name: string;
  start_ts: string;
  end_ts: string;
  speed?: string;
  seed?: number;
  strategy_version: string;
  execution_model: string;
  benchmark?: "CASH" | "CONSERVATIVE_NAV";
}

export interface ReplayReport extends JsonObject {
  performance: JsonObject;
  risk: JsonObject;
  benchmark: JsonObject;
  data_quality: JsonObject;
  limitations: string[];
  report_hash: string;
}

export interface ScenarioRun extends JsonObject {
  scenario_run_id: string;
  account_id: string;
  scenario_type: "SCENARIO";
  status: string;
  result: JsonObject;
}

export interface ConditionalOrder extends JsonObject {
  conditional_order_id: string;
  account_id: string;
  order_type: string;
  status: string;
  child_order: JsonObject;
}

export interface AdminDashboard extends JsonObject {
  tenant: JsonObject;
  active_freezes: JsonObject[];
}

export interface EvidenceBundle extends JsonObject {
  bundle_id: string;
  content_sha256: string;
  byte_count: number;
}

export class PaperApiError extends Error {
  constructor(
    message: string,
    public readonly code: string,
    public readonly statusCode?: number,
    public readonly requestId?: string,
    public readonly details: JsonObject = {},
  ) {
    super(message);
    this.name = "PaperApiError";
  }
}

export class PaperApiClient {
  constructor(
    private readonly baseUrl: string,
    private readonly apiKey: string,
  ) {
    if (!baseUrl || !apiKey) throw new Error("baseUrl and apiKey are required");
  }

  private async request<T>(
    method: string,
    path: string,
    options: { body?: JsonObject; query?: Record<string, string | number | undefined>; idempotencyKey?: string } = {},
  ): Promise<T> {
    const url = new URL(path, this.baseUrl.endsWith("/") ? this.baseUrl : `${this.baseUrl}/`);
    for (const [key, value] of Object.entries(options.query ?? {})) {
      if (value !== undefined) url.searchParams.set(key, String(value));
    }
    const response = await fetch(url, {
      method,
      headers: {
        Accept: "application/json",
        Authorization: `Bearer ${this.apiKey}`,
        ...(options.body ? { "Content-Type": "application/json" } : {}),
        ...(options.idempotencyKey ? { "Idempotency-Key": options.idempotencyKey } : {}),
      },
      body: options.body ? JSON.stringify(options.body) : undefined,
    });
    const payload = (await response.json()) as JsonObject;
    if (!response.ok) {
      const error = (payload.error ?? {}) as JsonObject;
      throw new PaperApiError(
        String(error.message ?? `Paper API returned HTTP ${response.status}`),
        String(error.code ?? "PAPER_HTTP_ERROR"),
        response.status,
        error.request_id ? String(error.request_id) : undefined,
        (error.details ?? {}) as JsonObject,
      );
    }
    if (!("data" in payload)) throw new PaperApiError("Invalid response envelope", "PAPER_PROTOCOL_ERROR");
    return payload.data as T;
  }

  private key(value?: string): string {
    return value ?? crypto.randomUUID();
  }

  listAccounts(limit = 50, cursor?: string): Promise<Page<PaperAccount>> {
    return this.request("GET", "/v1/paper/accounts", { query: { limit, cursor } });
  }

  getAccount(accountId: string): Promise<PaperAccount> {
    return this.request("GET", `/v1/paper/accounts/${accountId}`);
  }

  createAccount(name: string, initialCash: string, idempotencyKey?: string): Promise<PaperAccount> {
    return this.request("POST", "/v1/paper/accounts", {
      body: { name, initial_cash: initialCash },
      idempotencyKey: this.key(idempotencyKey),
    });
  }

  forkAccount(accountId: string, name: string, idempotencyKey?: string): Promise<PaperAccount> {
    return this.request("POST", `/v1/paper/accounts/${accountId}/fork`, {
      body: { name }, idempotencyKey: this.key(idempotencyKey),
    });
  }

  listOrders(accountId: string, limit = 50, cursor?: string): Promise<Page<PaperOrder>> {
    return this.request("GET", "/v1/paper/orders", { query: { account_id: accountId, limit, cursor } });
  }

  getOrder(orderId: number): Promise<PaperOrder> {
    return this.request("GET", `/v1/paper/orders/${orderId}`);
  }

  getOrderAudit(orderId: number): Promise<PaperOrderAudit> {
    return this.request("GET", `/v1/paper/audit/${orderId}`);
  }

  createOrder(order: JsonObject, idempotencyKey?: string): Promise<PaperOrder> {
    return this.request("POST", "/v1/paper/orders", { body: order, idempotencyKey: this.key(idempotencyKey) });
  }

  cancelOrder(orderId: number, idempotencyKey?: string): Promise<PaperOrder> {
    return this.request("DELETE", `/v1/paper/orders/${orderId}`, { idempotencyKey: this.key(idempotencyKey) });
  }

  cancelOrders(orderIds: number[], idempotencyKey?: string): Promise<JsonObject> {
    return this.request("POST", "/v1/paper/orders/cancel", {
      body: { order_ids: orderIds },
      idempotencyKey: this.key(idempotencyKey),
    });
  }

  cancelMarketOrders(
    accountId: string,
    filters: { assetId?: string; conditionId?: string },
    idempotencyKey?: string,
  ): Promise<JsonObject> {
    return this.request("POST", "/v1/paper/orders/cancel-market", {
      body: {
        account_id: accountId,
        asset_id: filters.assetId,
        condition_id: filters.conditionId,
      },
      idempotencyKey: this.key(idempotencyKey),
    });
  }

  cancelAllOrders(accountId: string, idempotencyKey?: string): Promise<JsonObject> {
    return this.request("POST", "/v1/paper/orders/cancel-all", {
      body: { account_id: accountId },
      idempotencyKey: this.key(idempotencyKey),
    });
  }

  replaceOrder(orderId: number, limitPrice: string, size: string, idempotencyKey?: string): Promise<PaperOrder> {
    return this.request("POST", `/v1/paper/orders/${orderId}/replace`, {
      body: { limit_price: limitPrice, size }, idempotencyKey: this.key(idempotencyKey),
    });
  }

  listPositions(accountId: string, limit = 50, cursor?: string): Promise<Page<JsonObject>> {
    return this.request("GET", `/v1/paper/accounts/${accountId}/positions`, { query: { limit, cursor } });
  }

  listFills(accountId: string, limit = 50, cursor?: string): Promise<Page<JsonObject>> {
    return this.request("GET", `/v1/paper/accounts/${accountId}/fills`, { query: { limit, cursor } });
  }

  listLedger(accountId: string, limit = 50, cursor?: string): Promise<Page<JsonObject>> {
    return this.request("GET", `/v1/paper/accounts/${accountId}/ledger`, { query: { limit, cursor } });
  }

  listJournal(accountId: string, limit = 50, cursor?: string): Promise<Page<JsonObject>> {
    return this.request("GET", `/v1/paper/accounts/${accountId}/journal`, { query: { limit, cursor } });
  }

  listTca(accountId: string, limit = 50, cursor?: string): Promise<Page<JsonObject>> {
    return this.request("GET", `/v1/paper/accounts/${accountId}/tca`, { query: { limit, cursor } });
  }

  getPerformance(accountId: string, limit = 100, cursor?: string): Promise<PaperPerformance> {
    return this.request("GET", `/v1/paper/accounts/${accountId}/performance`, { query: { limit, cursor } });
  }

  listReplaySessions(limit = 50, cursor?: string): Promise<Page<ReplaySession>> {
    return this.request("GET", "/v1/paper/replays", { query: { limit, cursor } });
  }

  createReplaySession(replay: CreateReplaySession, idempotencyKey?: string): Promise<ReplaySession> {
    return this.request("POST", "/v1/paper/replays", {
      body: replay,
      idempotencyKey: this.key(idempotencyKey),
    });
  }

  getReplaySession(replaySessionId: string): Promise<ReplaySession> {
    return this.request("GET", `/v1/paper/replays/${replaySessionId}`);
  }

  pauseReplaySession(replaySessionId: string, idempotencyKey?: string): Promise<ReplaySession> {
    return this.request("POST", `/v1/paper/replays/${replaySessionId}/pause`, {
      idempotencyKey: this.key(idempotencyKey),
    });
  }

  resumeReplaySession(
    replaySessionId: string,
    maxEvents = 1000,
    idempotencyKey?: string,
  ): Promise<ReplaySession> {
    return this.request("POST", `/v1/paper/replays/${replaySessionId}/resume`, {
      body: { max_events: maxEvents },
      idempotencyKey: this.key(idempotencyKey),
    });
  }

  forkReplaySession(
    replaySessionId: string,
    fork: JsonObject,
    idempotencyKey?: string,
  ): Promise<ReplaySession> {
    return this.request("POST", `/v1/paper/replays/${replaySessionId}/fork`, {
      body: fork,
      idempotencyKey: this.key(idempotencyKey),
    });
  }

  listReplayEvents(replaySessionId: string, limit = 50, cursor?: string): Promise<Page<JsonObject>> {
    return this.request("GET", `/v1/paper/replays/${replaySessionId}/events`, {
      query: { limit, cursor },
    });
  }

  getReplayReport(replaySessionId: string): Promise<ReplayReport> {
    return this.request("GET", `/v1/paper/replays/${replaySessionId}/report`);
  }

  listScenarioRuns(accountId: string, limit = 100): Promise<Page<ScenarioRun>> {
    return this.request("GET", "/v1/paper/scenarios", { query: { account_id: accountId, limit } });
  }

  createScenarioRun(scenario: JsonObject, idempotencyKey?: string): Promise<ScenarioRun> {
    return this.request("POST", "/v1/paper/scenarios", {
      body: scenario, idempotencyKey: this.key(idempotencyKey),
    });
  }

  getScenarioRun(scenarioRunId: string): Promise<ScenarioRun> {
    return this.request("GET", `/v1/paper/scenarios/${scenarioRunId}`);
  }

  listConditionalOrders(accountId: string, limit = 100): Promise<Page<ConditionalOrder>> {
    return this.request("GET", "/v1/paper/conditional-orders", { query: { account_id: accountId, limit } });
  }

  createConditionalOrder(order: JsonObject, idempotencyKey?: string): Promise<ConditionalOrder> {
    return this.request("POST", "/v1/paper/conditional-orders", {
      body: order, idempotencyKey: this.key(idempotencyKey),
    });
  }

  getConditionalOrder(conditionalOrderId: string): Promise<ConditionalOrder> {
    return this.request("GET", `/v1/paper/conditional-orders/${conditionalOrderId}`);
  }

  cancelConditionalOrder(conditionalOrderId: string, reason = "sdk_cancel", idempotencyKey?: string): Promise<ConditionalOrder> {
    return this.request("DELETE", `/v1/paper/conditional-orders/${conditionalOrderId}`, {
      body: { reason }, idempotencyKey: this.key(idempotencyKey),
    });
  }

  getAdminDashboard(): Promise<AdminDashboard> {
    return this.request("GET", "/v1/paper/admin/dashboard");
  }

  setTenantFrozen(frozen: boolean, reason: string, idempotencyKey?: string): Promise<JsonObject> {
    const action = frozen ? "freeze" : "unfreeze";
    return this.request("POST", `/v1/paper/admin/tenant/${action}`, {
      body: { reason }, idempotencyKey: this.key(idempotencyKey),
    });
  }

  setAccountFrozen(
    accountId: string,
    frozen: boolean,
    reason: string,
    idempotencyKey?: string,
  ): Promise<PaperAccount> {
    const action = frozen ? "freeze" : "unfreeze";
    return this.request("POST", `/v1/paper/admin/accounts/${accountId}/${action}`, {
      body: { reason }, idempotencyKey: this.key(idempotencyKey),
    });
  }

  killAccount(accountId: string, reason: string, idempotencyKey?: string): Promise<JsonObject> {
    return this.request("POST", `/v1/paper/admin/accounts/${accountId}/kill`, {
      body: { reason }, idempotencyKey: this.key(idempotencyKey),
    });
  }

  reconcileAccount(
    accountId: string,
    mode: "DRY_RUN" | "APPLY" = "DRY_RUN",
    idempotencyKey?: string,
  ): Promise<JsonObject> {
    return this.request("POST", `/v1/paper/admin/accounts/${accountId}/reconcile`, {
      body: { mode }, idempotencyKey: this.key(idempotencyKey),
    });
  }

  listAdminJobs(limit = 100): Promise<{ items: JsonObject[] }> {
    return this.request("GET", "/v1/paper/admin/jobs", { query: { limit } });
  }

  listDlqEvents(limit = 100): Promise<{ items: JsonObject[] }> {
    return this.request("GET", "/v1/paper/admin/dlq", { query: { limit } });
  }

  replayDlqEvent(dlqEventId: string, idempotencyKey?: string): Promise<JsonObject> {
    return this.request("POST", `/v1/paper/admin/dlq/${dlqEventId}/replay`, {
      body: {}, idempotencyKey: this.key(idempotencyKey),
    });
  }

  createNotice(notice: JsonObject, idempotencyKey?: string): Promise<JsonObject> {
    return this.request("POST", "/v1/paper/admin/notices", {
      body: notice, idempotencyKey: this.key(idempotencyKey),
    });
  }

  createIncident(incident: JsonObject, idempotencyKey?: string): Promise<JsonObject> {
    return this.request("POST", "/v1/paper/admin/incidents", {
      body: incident, idempotencyKey: this.key(idempotencyKey),
    });
  }

  listRetentionPolicies(): Promise<{ items: JsonObject[] }> {
    return this.request("GET", "/v1/paper/admin/retention");
  }

  upsertRetentionPolicy(
    resourceType: string,
    retentionDays: number,
    legalHold = false,
    idempotencyKey?: string,
  ): Promise<JsonObject> {
    return this.request("POST", `/v1/paper/admin/retention/${resourceType}`, {
      body: { retention_days: retentionDays, legal_hold: legalHold },
      idempotencyKey: this.key(idempotencyKey),
    });
  }

  createEvidenceBundle(
    accountId?: string,
    incidentId?: string,
    idempotencyKey?: string,
  ): Promise<EvidenceBundle> {
    return this.request("POST", "/v1/paper/admin/evidence-bundles", {
      body: { account_id: accountId, incident_id: incidentId },
      idempotencyKey: this.key(idempotencyKey),
    });
  }

  async downloadEvidenceBundle(bundleId: string): Promise<Blob> {
    const url = new URL(
      `/v1/paper/admin/evidence-bundles/${bundleId}/download`,
      this.baseUrl.endsWith("/") ? this.baseUrl : `${this.baseUrl}/`,
    );
    const response = await fetch(url, {
      headers: { Authorization: `Bearer ${this.apiKey}`, Accept: "application/zip" },
    });
    if (!response.ok) throw new PaperApiError(
      `Paper API returned HTTP ${response.status}`,
      "PAPER_HTTP_ERROR",
      response.status,
    );
    return response.blob();
  }

  async exportAccount(
    accountId: string,
    resource: "orders" | "positions" | "fills" | "ledger" | "journal" | "nav" | "tca",
    format: "csv" | "jsonl" | "parquet" = "csv",
  ): Promise<Blob> {
    const url = new URL(
      `/v1/paper/accounts/${accountId}/export`,
      this.baseUrl.endsWith("/") ? this.baseUrl : `${this.baseUrl}/`,
    );
    url.searchParams.set("resource", resource);
    url.searchParams.set("format", format);
    const response = await fetch(url, {
      headers: { Authorization: `Bearer ${this.apiKey}` },
    });
    if (!response.ok) {
      const payload = (await response.json().catch(() => ({}))) as JsonObject;
      const error = (payload.error ?? {}) as JsonObject;
      throw new PaperApiError(
        String(error.message ?? `Paper API returned HTTP ${response.status}`),
        String(error.code ?? "PAPER_HTTP_ERROR"),
        response.status,
        error.request_id ? String(error.request_id) : undefined,
      );
    }
    return response.blob();
  }
}
