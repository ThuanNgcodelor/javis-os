import { workflow, node, links } from '@n8n-as-code/transformer';

// <workflow-map>
// Workflow : AMIS CRM Full Warm — Redis Sync Every 1h
// Nodes   : 6  |  Connections: 5
//
// NODE INDEX
// ──────────────────────────────────────────────────────────────────
// Property name                    Node type (short)         Flags
// ManualTrigger                      manualTrigger
// ScheduleTrigger                    scheduleTrigger
// FetchAmisData                      code
// ValidateSourceCounts               code
// WarmRedisViaFastapi                httpRequest
// ValidateSyncResult                 code
//
// ROUTING MAP
// ──────────────────────────────────────────────────────────────────
// ManualTrigger
//    → FetchAmisData
//      → ValidateSourceCounts
//        → WarmRedisViaFastapi
//          → ValidateSyncResult
// ScheduleTrigger
//    → FetchAmisData (↩ loop)
// </workflow-map>

// =====================================================================
// METADATA DU WORKFLOW
// =====================================================================

@workflow({
    id: 'QLY0cLK5tqcx3KY6',
    name: 'AMIS CRM Full Warm — Redis Sync Every 1h',
    active: false,
    isArchived: false,
    settings: { timezone: 'Asia/Ho_Chi_Minh', executionOrder: 'v1', binaryMode: 'separate' },
})
export class AmisCrmFullWarmRedisSyncEvery1hWorkflow {
    // =====================================================================
    // CONFIGURATION DES NOEUDS
    // =====================================================================

    @node({
        id: 'a1b2c3d4-e5f6-7890-abcd-ef1234560001',
        name: 'Manual Trigger',
        type: 'n8n-nodes-base.manualTrigger',
        version: 1,
        position: [0, 144],
    })
    ManualTrigger = {};

    @node({
        id: 'a1b2c3d4-e5f6-7890-abcd-ef1234560002',
        name: 'Schedule Trigger',
        type: 'n8n-nodes-base.scheduleTrigger',
        version: 1.3,
        position: [0, 352],
    })
    ScheduleTrigger = {
        rule: {
            interval: [
                {
                    field: 'hours',
                },
            ],
        },
    };

    @node({
        id: 'a1b2c3d4-e5f6-7890-abcd-ef1234560003',
        name: 'Fetch AMIS Data',
        type: 'n8n-nodes-base.code',
        version: 2,
        position: [320, 240],
    })
    FetchAmisData = {
        jsCode: `
const BASE_URL = "https://crmconnect.misa.vn/api/v2";
const CLIENT_ID = "JavisCFCChatbot";
const CLIENT_SECRET = "Jb2wUAbsVytJpiaYAWAyaK8dKWBGsC7QB/cwvT62ZBQ=";

if (!CLIENT_SECRET) {
  throw new Error(
    "[AMIS Warm] AMIS_CLIENT_SECRET is not set. " +
    "Add it in n8n Settings > Environment Variables."
  );
}

const helpers = this.helpers;

let tokenRaw;
try {
  tokenRaw = await helpers.httpRequest({
    method: "POST",
    url: BASE_URL + "/Account",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ client_id: CLIENT_ID, client_secret: CLIENT_SECRET }),
  });
} catch (err) {
  throw new Error("[AMIS Warm] Auth failed: " + String(err.message || err));
}

const tokenPayload = (typeof tokenRaw === "string") ? JSON.parse(tokenRaw) : tokenRaw;
let token = "";
if (typeof tokenPayload.data === "string") {
  token = tokenPayload.data;
} else if (tokenPayload.data && typeof tokenPayload.data.access_token === "string") {
  token = tokenPayload.data.access_token;
}
if (!token) {
  throw new Error("[AMIS Warm] Empty token. AMIS response: " + JSON.stringify(tokenPayload).slice(0, 300));
}

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

const AUTH_HEADERS = {
  "Authorization": "Bearer " + token,
  "Clientid": CLIENT_ID,
  "Accept": "application/json",
  "Connection": "close",
};

async function fetchAllPages(resource, maxPages) {
  const all = [];
  const PAGE_SIZE = 100;
  const seen = new Set();
  maxPages = maxPages || 300;

  for (let page = 0; page < maxPages; page++) {
    const url = BASE_URL + "/" + resource +
      "?page=" + page + "&pageSize=" + PAGE_SIZE +
      "&orderBy=modified_date&isDescending=true";

    let raw = null;
    let lastError = null;

    // Retry up to 3 times on socket reset (ECONNRESET) / network glitch
    for (let attempt = 0; attempt < 3; attempt++) {
      try {
        raw = await helpers.httpRequest({
          method: "GET",
          url: url,
          headers: AUTH_HEADERS,
          timeout: 20000,
        });
        break;
      } catch (err) {
        lastError = err;
        const msg = String(err.message || err);
        if (msg.indexOf("401") !== -1 && page > 0) return all;
        // Pause and retry on ECONNRESET, timeout or 502/503/429
        await sleep(500 * (attempt + 1));
      }
    }

    if (!raw && lastError) {
      // If beyond first page, stop gracefully with what we have
      if (page > 0) break;
      throw new Error("[AMIS Warm] GET " + resource + " page " + page + " failed: " + String(lastError.message || lastError));
    }

    const payload = (typeof raw === "string") ? JSON.parse(raw) : raw;
    let records = null;
    if (Array.isArray(payload)) {
      records = payload;
    } else if (Array.isArray(payload.data)) {
      records = payload.data;
    } else if (payload.data && Array.isArray(payload.data.items)) {
      records = payload.data.items;
    } else if (Array.isArray(payload.items)) {
      records = payload.items;
    } else if (Array.isArray(payload.records)) {
      records = payload.records;
    }

    if (!records || records.length === 0) break;

    const fp = resource + ":p" + page + ":n" + records.length + ":" + JSON.stringify(records[0]).slice(0, 60);
    if (seen.has(fp)) break;
    seen.add(fp);

    all.push.apply(all, records);

    const total = (
      payload.total !== undefined ? payload.total :
      payload.totalCount !== undefined ? payload.totalCount :
      (payload.data && payload.data.total !== undefined) ? payload.data.total : null
    );
    if (total !== null && typeof total === "number" && all.length >= total) break;
    if (records.length < PAGE_SIZE) break;

    // Rate-limit buffer (100ms) to avoid tripping MISA AMIS firewall
    await sleep(100);
  }
  return all;
}

const allCustomers = await fetchAllPages("Customers", 50);
const customers = allCustomers.filter(function(c) {
  if (c.inactive === true) return false;
  if (!c.purchase_date_first) return false;
  var t = c.account_type;
  if (!t) return false;
  var types = Array.isArray(t) ? t : [t];
  return types.some(function(v) {
    var s = String(v);
    return s === "KH001" || s === "KH002" || s.indexOf("001") !== -1 || s.indexOf("002") !== -1;
  });
});

const products = await fetchAllPages("Products", 20);

const rawOrders = await fetchAllPages("SaleOrders", 300);
const saleOrders = rawOrders.filter(function(o) {
  if (o.is_invoiced !== true) return false;
  var s = String(o.status || "").toLowerCase();
  return s.indexOf("huy") === -1 && s.indexOf("tu choi") === -1 &&
    s.indexOf("cancel") === -1 && s.indexOf("reject") === -1;
});

return [{
  json: {
    customers: customers,
    products: products,
    sale_orders: saleOrders,
    fetched_at: new Date().toISOString(),
    counts: {
      customers_raw: allCustomers.length,
      customers_eligible: customers.length,
      products: products.length,
      sale_orders_raw: rawOrders.length,
      sale_orders_invoiced: saleOrders.length,
    },
  },
}];
`,
    };

    @node({
        id: 'a1b2c3d4-e5f6-7890-abcd-ef1234560004',
        name: 'Validate Source Counts',
        type: 'n8n-nodes-base.code',
        version: 2,
        position: [624, 240],
    })
    ValidateSourceCounts = {
        jsCode: `
const data = $input.first().json;
const c = data.counts || {};

if (!c.customers_eligible || c.customers_eligible < 5) {
  throw new Error(
    "[AMIS Warm] Too few eligible customers: " + (c.customers_eligible || 0) +
    " (raw fetched: " + (c.customers_raw || 0) + "). " +
    "Check KH001/KH002 customers have purchase_date_first set in AMIS."
  );
}
if (!c.products || c.products < 1) {
  throw new Error("[AMIS Warm] No products fetched. Check AMIS Products API.");
}
if (!c.sale_orders_invoiced || c.sale_orders_invoiced < 1) {
  throw new Error(
    "[AMIS Warm] No invoiced sale orders (raw=" + (c.sale_orders_raw || 0) + "). " +
    "Python projection needs sale orders to determine dealer brand scopes."
  );
}

console.log("[AMIS Warm] Source OK — customers=" + c.customers_eligible +
  " products=" + c.products + " orders=" + c.sale_orders_invoiced);

return $input.all();
`,
    };

    @node({
        id: 'a1b2c3d4-e5f6-7890-abcd-ef1234560005',
        name: 'Warm Redis via FastAPI',
        type: 'n8n-nodes-base.httpRequest',
        version: 4.4,
        position: [928, 240],
    })
    WarmRedisViaFastapi = {
        method: 'POST',
        url: 'http://127.0.0.1:7777/admin/amis/warm',
        sendBody: true,
        specifyBody: 'json',
        jsonBody: '={{ $json }}',
        options: {
            timeout: 300000,
        },
    };

    @node({
        id: 'a1b2c3d4-e5f6-7890-abcd-ef1234560006',
        name: 'Validate Sync Result',
        type: 'n8n-nodes-base.code',
        version: 2,
        position: [1232, 240],
    })
    ValidateSyncResult = {
        jsCode: `
const result = $input.first().json || {};

if (result.status !== "ok") {
  const reasons = (result.gate && result.gate.reasons)
    ? result.gate.reasons.join(", ")
    : "unknown gate failure";
  throw new Error(
    "[AMIS Warm] Sync blocked: " + reasons +
    ". Status=" + String(result.status || "unknown") +
    ". Ensure AMIS_PILOT_APPROVE_ALL=true on FastAPI and dealers have GPS."
  );
}
if (result.written !== true) {
  throw new Error("[AMIS Warm] Did not write to Redis. dry_run=" + String(result.dry_run));
}

const locationCount = (result.snapshots && result.snapshots.locations)
  ? (result.snapshots.locations.record_count || 0) : 0;
const productCount = (result.snapshots && result.snapshots.products)
  ? (result.snapshots.products.record_count || 0) : 0;
const withCoords = (result.metrics && result.metrics.locations)
  ? (result.metrics.locations.with_coordinates_count || 0) : 0;

if (locationCount < 1) {
  throw new Error("[AMIS Warm] Zero locations in snapshot! " +
    "Set AMIS_PILOT_APPROVE_ALL=true and ensure dealers have GPS coordinates in AMIS.");
}

console.log("[AMIS Warm] SUCCESS — locations=" + locationCount +
  " gps=" + withCoords + " products=" + productCount +
  " at=" + result.synced_at);

return [{
  json: {
    status: "ok",
    source: "amis_crm",
    synced_at: result.synced_at,
    location_count: locationCount,
    location_with_coordinates_count: withCoords,
    product_count: productCount,
    locations_snapshot_hash: (result.snapshots && result.snapshots.locations)
      ? result.snapshots.locations.snapshot_hash : "",
    products_snapshot_hash: (result.snapshots && result.snapshots.products)
      ? result.snapshots.products.snapshot_hash : "",
  },
}];
`,
    };

    // =====================================================================
    // ROUTAGE ET CONNEXIONS
    // =====================================================================

    @links()
    defineRouting() {
        this.ManualTrigger.out(0).to(this.FetchAmisData.in(0));
        this.ScheduleTrigger.out(0).to(this.FetchAmisData.in(0));
        this.FetchAmisData.out(0).to(this.ValidateSourceCounts.in(0));
        this.ValidateSourceCounts.out(0).to(this.WarmRedisViaFastapi.in(0));
        this.WarmRedisViaFastapi.out(0).to(this.ValidateSyncResult.in(0));
    }
}
