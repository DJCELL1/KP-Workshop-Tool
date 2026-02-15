"""
HDL Workshop Board
==================
Standalone drag-and-drop Kanban board for workshop staff to move kickplate
jobs between stages without logging into Cin7.

Run: python workshop.py
Then open: http://localhost:5000

For production: pip install waitress
Then run: waitress-serve --port=5000 workshop:app

Author: HDL Engineering
"""

import os
import sys
import logging
from datetime import datetime, timedelta
from flask import Flask, jsonify, request, send_file
import requests
from requests.auth import HTTPBasicAuth
import pytz

# Python 3.11+ has tomllib built-in
try:
    import tomllib
except ModuleNotFoundError:
    try:
        import tomli as tomllib
    except ModuleNotFoundError:
        print("ERROR: Python 3.11+ required (for tomllib), or install 'tomli' package")
        sys.exit(1)

# =============================================================================
# CONFIGURATION
# =============================================================================

KICKPLATE_STAGES = [
    'Kickplate - New',
    'Kickplate - Processing',
    'Kickplate - Job Complete',
    'Kickplate - To Collect'
]
KICKPLATE_STAGE_PREFIX = "Kickplate - "
CIN7_WEB_URL_BASE = "https://go.cin7.com/Cloud/TransactionEntry/TransactionEntry.aspx"
CIN7_CUSTOMER_APPS_LINK = "767392"
TIMEZONE_DISPLAY = 'Pacific/Auckland'
DUE_SOON_DAYS = 7
API_TIMEOUT = 30
API_MAX_RETRIES = 3
API_RETRY_DELAY = 1.0
AUTO_REFRESH_MINUTES = 3

SALES_ORDER_FIELDS = [
    "Id", "Reference", "ProjectName", "Company", "FirstName",
    "CreatedDate", "EstimatedDeliveryDate",
    "Stage", "IsVoid", "LineItems"
]

# Stages to exclude from API query
EXCLUDED_STAGES = [
    'Fully Dispatched', 'Dispatched', 'Cancelled', 'Declined',
    'To Call', 'Awaiting PO', 'Awaiting Payment',
    'Release To Pick', 'Partially Picked', 'Fully Picked',
    'Fully Picked - Hold', 'On Hold', 'Ready to Invoice',
    'Release To Pick - WMS', 'Ready To Pack - WMS'
]

# Logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
log = logging.getLogger('workshop')

# =============================================================================
# CREDENTIALS
# =============================================================================

def load_credentials():
    """Load Cin7 API credentials from .streamlit/secrets.toml."""
    secrets_path = os.path.join(
        os.path.dirname(os.path.abspath(__file__)),
        '.streamlit', 'secrets.toml'
    )
    if not os.path.exists(secrets_path):
        log.error(f"Secrets file not found: {secrets_path}")
        sys.exit(1)

    with open(secrets_path, 'rb') as f:
        secrets = tomllib.load(f)

    return (
        secrets.get('CIN7_API_BASE', 'https://api.cin7.com/api/v1'),
        secrets['CIN7_USERNAME'],
        secrets['CIN7_KEY']
    )


BASE_URL, USERNAME, API_KEY = load_credentials()
AUTH = HTTPBasicAuth(USERNAME, API_KEY)

# =============================================================================
# CIN7 API FUNCTIONS
# =============================================================================

def cin7_get(path, params=None):
    """Make an authenticated GET request to the Cin7 API with retries."""
    url = f"{BASE_URL.rstrip('/')}/{path.lstrip('/')}"
    last_error = None

    for attempt in range(API_MAX_RETRIES):
        try:
            resp = requests.get(
                url, params=params, auth=AUTH,
                timeout=API_TIMEOUT,
                headers={"Accept": "application/json"}
            )
            if resp.status_code == 200:
                return resp.json()
            if resp.status_code == 429:
                import time
                time.sleep(API_RETRY_DELAY * (attempt + 1) * 2)
                continue
            last_error = f"HTTP {resp.status_code}: {resp.text[:300]}"
        except requests.exceptions.Timeout:
            last_error = "Request timed out"
        except requests.exceptions.ConnectionError:
            last_error = "Connection error"
        except requests.exceptions.RequestException as e:
            last_error = str(e)

        if attempt < API_MAX_RETRIES - 1:
            import time
            time.sleep(API_RETRY_DELAY * (attempt + 1))

    log.error(f"Cin7 GET {path} failed after {API_MAX_RETRIES} attempts: {last_error}")
    return None


def cin7_put(path, data):
    """Make an authenticated PUT request to the Cin7 API.

    Cin7 Omni expects PUT data as an array with lowercase field names.
    """
    url = f"{BASE_URL.rstrip('/')}/{path.lstrip('/')}"
    last_error = None

    # Cin7 Omni API expects an array of objects with lowercase keys
    payload = [data] if isinstance(data, dict) else data

    for attempt in range(API_MAX_RETRIES):
        try:
            resp = requests.put(
                url, json=payload, auth=AUTH,
                timeout=API_TIMEOUT,
                headers={
                    "Accept": "application/json",
                    "Content-Type": "application/json"
                }
            )
            if resp.status_code in (200, 201, 204):
                # Check Cin7 response for per-item success
                try:
                    result = resp.json()
                    if isinstance(result, list) and result:
                        item = result[0]
                        if item.get("success") is False:
                            errors = item.get("errors", [])
                            last_error = "; ".join(errors) if errors else "Cin7 reported failure"
                            return {"success": False, "error": last_error}
                except (ValueError, KeyError):
                    pass
                return {"success": True}
            if resp.status_code == 429:
                import time
                time.sleep(API_RETRY_DELAY * (attempt + 1) * 2)
                continue
            last_error = f"HTTP {resp.status_code}: {resp.text[:300]}"
        except requests.exceptions.RequestException as e:
            last_error = str(e)

        if attempt < API_MAX_RETRIES - 1:
            import time
            time.sleep(API_RETRY_DELAY * (attempt + 1))

    log.error(f"Cin7 PUT {path} failed: {last_error}")
    return {"success": False, "error": last_error}


def parse_date(date_value):
    """Parse various date formats into datetime."""
    if date_value is None or not isinstance(date_value, str):
        return None
    date_formats = [
        "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%S.%f",
        "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S.%fZ",
        "%Y-%m-%d", "%d/%m/%Y", "%m/%d/%Y",
    ]
    cleaned = date_value.replace("+00:00", "").rstrip("Z")
    for fmt in date_formats:
        try:
            return datetime.strptime(cleaned, fmt.rstrip("Z"))
        except ValueError:
            continue
    return None


def fetch_kickplate_orders():
    """Fetch all kickplate orders from Cin7 API."""
    fields = ",".join(SALES_ORDER_FIELDS)
    where_parts = [f"Stage<>'{s}'" for s in EXCLUDED_STAGES]
    where = " AND ".join(where_parts)

    all_orders = []
    page = 1

    while page <= 100:
        params = {
            "fields": fields,
            "order": "EstimatedDeliveryDate ASC, CreatedDate ASC",
            "page": page,
            "rows": 250
        }
        if where:
            params["where"] = where

        response = cin7_get("/SalesOrders", params)
        if response is None:
            break

        if isinstance(response, list):
            orders = response
        elif isinstance(response, dict):
            orders = response.get("data", response.get("Data", []))
            if not isinstance(orders, list):
                orders = [response] if response.get("Id") else []
        else:
            break

        if not orders:
            break

        all_orders.extend(orders)
        if len(orders) < 250:
            break
        page += 1

    # Process orders
    tz = pytz.timezone(TIMEZONE_DISPLAY)
    today = datetime.now(tz).date()
    processed = []

    for order in all_orders:
        # Skip void
        if order.get("IsVoid") or order.get("isVoid"):
            continue

        # Get field helper (PascalCase / camelCase)
        def gf(name):
            return order.get(name) or order.get(name[0].lower() + name[1:])

        stage = gf("Stage") or ""
        if not stage.startswith(KICKPLATE_STAGE_PREFIX):
            continue

        # Parse dates
        etd = parse_date(gf("EstimatedDeliveryDate"))
        created = parse_date(gf("CreatedDate"))

        # Calculate KP quantity
        line_items = gf("LineItems") or []
        qty_total = 0
        for item in (line_items if isinstance(line_items, list) else []):
            qty = item.get("Qty") or item.get("qty") or item.get("UomQtyOrdered") or item.get("uomQtyOrdered") or 0
            try:
                qty_total += float(qty)
            except (TypeError, ValueError):
                pass

        # Overdue / due soon
        is_overdue = False
        is_due_soon = False
        is_on_track = False
        days_overdue = 0

        if etd:
            est_date = etd.date() if hasattr(etd, 'date') else etd
            days_until = (est_date - today).days
            if days_until < 0:
                is_overdue = True
                days_overdue = abs(days_until)
            elif days_until <= DUE_SOON_DAYS:
                is_due_soon = True
            else:
                is_on_track = True

        order_id = gf("Id")
        cin7_url = f"{CIN7_WEB_URL_BASE}?idCustomerAppsLink={CIN7_CUSTOMER_APPS_LINK}&OrderId={order_id}" if order_id else "#"

        import html
        processed.append({
            "id": order_id,
            "reference": html.escape(str(gf("Reference") or "No Ref")),
            "projectName": html.escape(str(gf("ProjectName") or "")),
            "firstName": html.escape(str(gf("FirstName") or "")),
            "stage": stage,
            "createdDate": created.strftime("%d %b %Y") if created else "",
            "etd": etd.strftime("%d %b %Y") if etd else "",
            "qtyTotal": int(qty_total),
            "isOverdue": is_overdue,
            "isDueSoon": is_due_soon,
            "isOnTrack": is_on_track,
            "daysOverdue": days_overdue,
            "cin7Url": cin7_url
        })

    return processed


def update_order_stage(order_id, new_stage):
    """Update a sales order's stage in Cin7."""
    if new_stage not in KICKPLATE_STAGES:
        return {"success": False, "error": f"Invalid stage: {new_stage}"}

    result = cin7_put("/SalesOrders", {"id": order_id, "stage": new_stage})
    if result.get("success"):
        log.info(f"Stage updated: Order {order_id} -> {new_stage}")
    else:
        log.error(f"Stage update failed: Order {order_id} -> {new_stage}: {result.get('error')}")
    return result


# =============================================================================
# FLASK APP
# =============================================================================

app = Flask(__name__)


@app.route('/')
def index():
    """Serve the workshop board HTML page."""
    return HTML_TEMPLATE


@app.route('/api/jobs')
def api_jobs():
    """Return all kickplate jobs grouped by stage."""
    try:
        orders = fetch_kickplate_orders()
        grouped = {stage: [] for stage in KICKPLATE_STAGES}
        for order in orders:
            stage = order["stage"]
            if stage in grouped:
                grouped[stage].append(order)

        tz = pytz.timezone(TIMEZONE_DISPLAY)
        now = datetime.now(tz)

        return jsonify({
            "jobs": grouped,
            "timestamp": now.strftime("%d %b %Y %H:%M"),
            "totalCount": len(orders)
        })
    except Exception as e:
        log.exception("Error fetching jobs")
        return jsonify({"error": str(e)}), 500


@app.route('/api/jobs/<int:order_id>/stage', methods=['POST'])
def api_update_stage(order_id):
    """Update a job's stage."""
    data = request.get_json()
    if not data or 'stage' not in data:
        return jsonify({"success": False, "error": "Missing 'stage' in request body"}), 400

    new_stage = data['stage']
    result = update_order_stage(order_id, new_stage)

    if result.get("success"):
        return jsonify({"success": True, "orderId": order_id, "newStage": new_stage})
    else:
        return jsonify({"success": False, "error": result.get("error", "Unknown error"), "orderId": order_id}), 500


@app.route('/api/logo')
def api_logo():
    """Serve the HDL logo."""
    logo_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'Logos-01.jpg')
    if os.path.exists(logo_path):
        return send_file(logo_path, mimetype='image/jpeg')
    return '', 404


# =============================================================================
# HTML TEMPLATE
# =============================================================================

HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, user-scalable=no">
<title>Kickplate Workshop Stages | Hardware Direct</title>
<script src="https://cdnjs.cloudflare.com/ajax/libs/Sortable/1.15.6/Sortable.min.js"></script>
<style>
    * { margin: 0; padding: 0; box-sizing: border-box; }

    body {
        font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
        background: #F5F7FA;
        color: #11222c;
        min-height: 100vh;
        display: flex;
        flex-direction: column;
    }

    /* Header */
    .header {
        background: linear-gradient(135deg, #f69000 0%, #f6c624 100%);
        padding: 1rem 1.5rem;
        display: flex;
        align-items: center;
        justify-content: space-between;
        box-shadow: 0 4px 15px rgba(244, 121, 32, 0.3);
    }

    .header-left {
        display: flex;
        align-items: center;
        gap: 1rem;
    }

    .header-logo {
        height: 50px;
        border-radius: 6px;
    }

    .header-title {
        color: white;
        font-size: 1.5rem;
        font-weight: 700;
    }

    .header-right {
        display: flex;
        align-items: center;
        gap: 1rem;
        color: white;
    }

    .header-time {
        font-size: 0.9rem;
        opacity: 0.9;
    }

    .btn-refresh {
        background: rgba(255,255,255,0.2);
        color: white;
        border: 2px solid rgba(255,255,255,0.5);
        padding: 0.5rem 1.25rem;
        border-radius: 8px;
        font-size: 0.95rem;
        font-weight: 600;
        cursor: pointer;
        transition: background 0.2s;
    }

    .btn-refresh:hover {
        background: rgba(255,255,255,0.35);
    }

    /* Board */
    .board {
        display: grid;
        grid-template-columns: repeat(4, 1fr);
        gap: 1rem;
        padding: 1rem;
        flex: 1;
        min-height: 0;
    }

    /* Columns */
    .column {
        background: white;
        border-radius: 12px;
        display: flex;
        flex-direction: column;
        box-shadow: 0 2px 8px rgba(0,0,0,0.08);
        overflow: hidden;
        min-height: 300px;
    }

    .column-header {
        padding: 0.75rem 1rem;
        display: flex;
        align-items: center;
        justify-content: space-between;
        flex-shrink: 0;
    }

    .column-header.new       { background: linear-gradient(135deg, #f69000, #e08000); }
    .column-header.processing { background: linear-gradient(135deg, #53b1b1, #3d8a8a); }
    .column-header.complete   { background: linear-gradient(135deg, #11222c, #1a3a4a); }
    .column-header.collect    { background: linear-gradient(135deg, #6f42c1, #5a32a3); }

    .column-title {
        color: white;
        font-weight: 700;
        font-size: 1.1rem;
    }

    .column-count {
        background: rgba(255,255,255,0.25);
        color: white;
        padding: 0.2rem 0.7rem;
        border-radius: 20px;
        font-weight: 700;
        font-size: 0.95rem;
    }

    /* Card list - drop zone */
    .card-list {
        flex: 1;
        padding: 0.75rem;
        overflow-y: auto;
        min-height: 100px;
    }

    .card-list:empty::after {
        content: 'Drag jobs here';
        display: block;
        text-align: center;
        color: #ADB5BD;
        padding: 2rem 1rem;
        font-size: 0.95rem;
        border: 2px dashed #DEE2E6;
        border-radius: 8px;
        margin: 0.5rem;
    }

    /* Job cards */
    .card {
        background: white;
        border: 1px solid #E9ECEF;
        border-radius: 8px;
        padding: 0.75rem;
        margin-bottom: 0.6rem;
        cursor: grab;
        transition: transform 0.15s, box-shadow 0.15s;
        touch-action: none;
    }

    .card:active { cursor: grabbing; }

    .card:hover {
        transform: translateY(-2px);
        box-shadow: 0 4px 12px rgba(0,0,0,0.15);
    }

    .card.overdue    { border-left: 4px solid #DC3545; }
    .card.due-soon   { border-left: 4px solid #f6c624; }
    .card.on-track   { border-left: 4px solid #53b1b1; }
    .card.no-date    { border-left: 4px solid #1c5858; }

    .card-ref {
        font-weight: 700;
        color: #11222c;
        font-size: 0.95rem;
        margin-bottom: 0.2rem;
        white-space: nowrap;
        overflow: hidden;
        text-overflow: ellipsis;
    }

    .card-project {
        color: #11222c;
        font-size: 0.85rem;
        margin-bottom: 0.15rem;
        white-space: nowrap;
        overflow: hidden;
        text-overflow: ellipsis;
    }

    .card-contact {
        color: #1c5858;
        font-size: 0.8rem;
        margin-bottom: 0.4rem;
        white-space: nowrap;
        overflow: hidden;
        text-overflow: ellipsis;
    }

    .card-meta {
        display: flex;
        gap: 0.3rem;
        flex-wrap: wrap;
        margin-bottom: 0.3rem;
    }

    .badge {
        display: inline-block;
        padding: 0.15rem 0.4rem;
        border-radius: 4px;
        font-size: 0.65rem;
        font-weight: 700;
        text-transform: uppercase;
        letter-spacing: 0.3px;
    }

    .badge-overdue  { background: #DC3545; color: white; }
    .badge-due-soon { background: #f6c624; color: #11222c; }
    .badge-on-track { background: #53b1b1; color: white; }
    .badge-no-date  { background: #1c5858; color: white; }
    .badge-kp       { background: #11222c; color: white; }

    .card-date {
        font-size: 0.75rem;
        color: #1c5858;
    }

    .card-link {
        display: inline-block;
        margin-top: 0.3rem;
        font-size: 0.7rem;
        color: #f69000;
        text-decoration: none;
        font-weight: 600;
    }

    .card-link:hover {
        text-decoration: underline;
    }

    /* SortableJS drag states */
    .sortable-ghost {
        opacity: 0.4;
        background: #FFF3CD;
        border: 2px dashed #f69000;
    }

    .sortable-chosen {
        box-shadow: 0 8px 24px rgba(0,0,0,0.2);
        transform: rotate(1.5deg) scale(1.03);
    }

    .sortable-drag {
        opacity: 0.9;
    }

    .card.updating {
        opacity: 0.5;
        pointer-events: none;
    }

    .card.updating::after {
        content: 'Updating...';
        display: block;
        text-align: center;
        color: #f69000;
        font-weight: 600;
        font-size: 0.8rem;
        margin-top: 0.3rem;
    }

    /* Toast notifications */
    #toast-container {
        position: fixed;
        bottom: 1.5rem;
        right: 1.5rem;
        z-index: 9999;
        display: flex;
        flex-direction: column;
        gap: 0.5rem;
    }

    .toast {
        padding: 0.75rem 1.25rem;
        border-radius: 8px;
        font-weight: 600;
        font-size: 0.9rem;
        color: white;
        box-shadow: 0 4px 15px rgba(0,0,0,0.2);
        animation: slideIn 0.3s ease-out;
        max-width: 350px;
    }

    .toast.success { background: #28a745; }
    .toast.error   { background: #DC3545; }
    .toast.info    { background: #11222c; }

    @keyframes slideIn {
        from { transform: translateX(100%); opacity: 0; }
        to   { transform: translateX(0); opacity: 1; }
    }

    @keyframes fadeOut {
        from { opacity: 1; }
        to   { opacity: 0; transform: translateY(10px); }
    }

    /* Loading overlay */
    .loading-overlay {
        position: fixed;
        top: 0; left: 0; right: 0; bottom: 0;
        background: rgba(245, 247, 250, 0.85);
        display: flex;
        align-items: center;
        justify-content: center;
        z-index: 9998;
    }

    .loading-overlay.hidden { display: none; }

    .spinner {
        width: 48px;
        height: 48px;
        border: 5px solid #E9ECEF;
        border-top-color: #f69000;
        border-radius: 50%;
        animation: spin 0.8s linear infinite;
    }

    @keyframes spin {
        to { transform: rotate(360deg); }
    }

    /* Error banner */
    .error-banner {
        background: #FFF3CD;
        border: 1px solid #FFEEBA;
        color: #856404;
        padding: 0.75rem 1rem;
        text-align: center;
        font-weight: 600;
        display: none;
    }

    .error-banner.show { display: block; }

    /* Responsive */
    @media (max-width: 1024px) {
        .board { grid-template-columns: repeat(2, 1fr); }
        .header-title { font-size: 1.2rem; }
    }

    @media (max-width: 600px) {
        .board { grid-template-columns: 1fr; }
        .header { flex-direction: column; gap: 0.5rem; text-align: center; }
    }
</style>
</head>
<body>

<div class="header">
    <div class="header-left">
        <img src="/api/logo" alt="HDL" class="header-logo" onerror="this.style.display='none'">
        <span class="header-title">Kickplate Workshop Stages</span>
    </div>
    <div class="header-right">
        <span class="header-time" id="last-updated">Loading...</span>
        <button class="btn-refresh" onclick="loadJobs()">Refresh</button>
    </div>
</div>

<div class="error-banner" id="error-banner"></div>

<div class="board">
    <div class="column">
        <div class="column-header new">
            <span class="column-title">New</span>
            <span class="column-count" id="count-new">0</span>
        </div>
        <div class="card-list" id="list-new" data-stage="Kickplate - New"></div>
    </div>
    <div class="column">
        <div class="column-header processing">
            <span class="column-title">Processing</span>
            <span class="column-count" id="count-processing">0</span>
        </div>
        <div class="card-list" id="list-processing" data-stage="Kickplate - Processing"></div>
    </div>
    <div class="column">
        <div class="column-header complete">
            <span class="column-title">Job Complete</span>
            <span class="column-count" id="count-complete">0</span>
        </div>
        <div class="card-list" id="list-complete" data-stage="Kickplate - Job Complete"></div>
    </div>
    <div class="column">
        <div class="column-header collect">
            <span class="column-title">To Collect</span>
            <span class="column-count" id="count-collect">0</span>
        </div>
        <div class="card-list" id="list-collect" data-stage="Kickplate - To Collect"></div>
    </div>
</div>

<div id="toast-container"></div>
<div class="loading-overlay" id="loading-overlay">
    <div class="spinner"></div>
</div>

<script>
// =========================================================================
// State
// =========================================================================
let isDragging = false;
let refreshTimer = null;

const STAGE_TO_LIST = {
    'Kickplate - New': 'list-new',
    'Kickplate - Processing': 'list-processing',
    'Kickplate - Job Complete': 'list-complete',
    'Kickplate - To Collect': 'list-collect'
};

const STAGE_TO_COUNT = {
    'Kickplate - New': 'count-new',
    'Kickplate - Processing': 'count-processing',
    'Kickplate - Job Complete': 'count-complete',
    'Kickplate - To Collect': 'count-collect'
};

// =========================================================================
// Card HTML
// =========================================================================
function createCardHTML(job) {
    let statusClass = 'no-date';
    let badgeHTML = '<span class="badge badge-no-date">NO ETD</span>';

    if (job.isOverdue) {
        statusClass = 'overdue';
        badgeHTML = '<span class="badge badge-overdue">' + job.daysOverdue + 'd OVERDUE</span>';
    } else if (job.isDueSoon) {
        statusClass = 'due-soon';
        badgeHTML = '<span class="badge badge-due-soon">DUE SOON</span>';
    } else if (job.isOnTrack) {
        statusClass = 'on-track';
        badgeHTML = '<span class="badge badge-on-track">ON TRACK</span>';
    }

    let kpBadge = job.qtyTotal > 0
        ? '<span class="badge badge-kp">' + job.qtyTotal + ' KP</span>'
        : '';

    let dateStr = job.etd ? 'ETD: ' + job.etd : '';

    return '<div class="card ' + statusClass + '" data-id="' + job.id + '" data-stage="' + job.stage + '">'
        + '<div class="card-ref">' + job.reference + '</div>'
        + '<div class="card-project">' + (job.projectName || '—') + '</div>'
        + '<div class="card-contact">' + (job.firstName || '—') + '</div>'
        + '<div class="card-meta">' + badgeHTML + kpBadge + '</div>'
        + (dateStr ? '<div class="card-date">' + dateStr + '</div>' : '')
        + '<a href="' + job.cin7Url + '" target="_blank" class="card-link" onclick="event.stopPropagation()">Open in Cin7</a>'
        + '</div>';
}

// =========================================================================
// Load jobs from API
// =========================================================================
function loadJobs() {
    fetch('/api/jobs')
        .then(function(resp) {
            if (!resp.ok) throw new Error('Failed to load jobs');
            return resp.json();
        })
        .then(function(data) {
            hideError();
            hideLoading();

            // Populate columns
            var stages = Object.keys(STAGE_TO_LIST);
            for (var i = 0; i < stages.length; i++) {
                var stage = stages[i];
                var listEl = document.getElementById(STAGE_TO_LIST[stage]);
                var jobs = data.jobs[stage] || [];

                listEl.innerHTML = '';
                for (var j = 0; j < jobs.length; j++) {
                    listEl.innerHTML += createCardHTML(jobs[j]);
                }

                document.getElementById(STAGE_TO_COUNT[stage]).textContent = jobs.length;
            }

            document.getElementById('last-updated').textContent = 'Updated: ' + data.timestamp;
        })
        .catch(function(err) {
            hideLoading();
            showError('Failed to load jobs. Will retry...');
            console.error(err);
        });
}

// =========================================================================
// Update stage via API
// =========================================================================
function updateStage(orderId, newStage, cardEl, fromList, oldIndex) {
    cardEl.classList.add('updating');

    fetch('/api/jobs/' + orderId + '/stage', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ stage: newStage })
    })
    .then(function(resp) { return resp.json(); })
    .then(function(data) {
        cardEl.classList.remove('updating');

        if (data.success) {
            // Update the card's data-stage attribute
            cardEl.setAttribute('data-stage', newStage);
            showToast('Moved to ' + newStage.replace('Kickplate - ', ''), 'success');
        } else {
            // Revert: move card back
            revertCard(cardEl, fromList, oldIndex);
            showToast('Failed: ' + (data.error || 'Unknown error'), 'error');
        }
        updateCounts();
    })
    .catch(function(err) {
        cardEl.classList.remove('updating');
        revertCard(cardEl, fromList, oldIndex);
        showToast('Network error - card reverted', 'error');
        updateCounts();
    });
}

function revertCard(cardEl, fromList, oldIndex) {
    // Remove from current location
    if (cardEl.parentNode) {
        cardEl.parentNode.removeChild(cardEl);
    }
    // Insert back at original position
    var refNode = fromList.children[oldIndex] || null;
    fromList.insertBefore(cardEl, refNode);
}

// =========================================================================
// Update column counts
// =========================================================================
function updateCounts() {
    var stages = Object.keys(STAGE_TO_LIST);
    for (var i = 0; i < stages.length; i++) {
        var stage = stages[i];
        var listEl = document.getElementById(STAGE_TO_LIST[stage]);
        var countEl = document.getElementById(STAGE_TO_COUNT[stage]);
        countEl.textContent = listEl.querySelectorAll('.card').length;
    }
}

// =========================================================================
// Toast notifications
// =========================================================================
function showToast(message, type) {
    var container = document.getElementById('toast-container');
    var toast = document.createElement('div');
    toast.className = 'toast ' + (type || 'info');
    toast.textContent = message;
    container.appendChild(toast);

    setTimeout(function() {
        toast.style.animation = 'fadeOut 0.3s ease-out forwards';
        setTimeout(function() {
            if (toast.parentNode) toast.parentNode.removeChild(toast);
        }, 300);
    }, 3000);
}

// =========================================================================
// Error banner
// =========================================================================
function showError(msg) {
    var banner = document.getElementById('error-banner');
    banner.textContent = msg;
    banner.classList.add('show');
}

function hideError() {
    document.getElementById('error-banner').classList.remove('show');
}

function hideLoading() {
    document.getElementById('loading-overlay').classList.add('hidden');
}

// =========================================================================
// Initialize SortableJS
// =========================================================================
function initSortable() {
    var lists = document.querySelectorAll('.card-list');
    for (var i = 0; i < lists.length; i++) {
        new Sortable(lists[i], {
            group: 'workshop',
            animation: 150,
            ghostClass: 'sortable-ghost',
            chosenClass: 'sortable-chosen',
            dragClass: 'sortable-drag',
            delay: 100,
            delayOnTouchOnly: true,
            touchStartThreshold: 5,
            filter: '.card-link',
            preventOnFilter: false,
            onStart: function() {
                isDragging = true;
            },
            onEnd: function(evt) {
                isDragging = false;

                // Only call API if column actually changed
                if (evt.from === evt.to) {
                    return;
                }

                var cardEl = evt.item;
                var orderId = parseInt(cardEl.getAttribute('data-id'));
                var newStage = evt.to.getAttribute('data-stage');
                var fromList = evt.from;
                var oldIndex = evt.oldIndex;

                updateStage(orderId, newStage, cardEl, fromList, oldIndex);
            }
        });
    }
}

// =========================================================================
// Auto-refresh
// =========================================================================
function startAutoRefresh() {
    refreshTimer = setInterval(function() {
        if (!isDragging) {
            loadJobs();
        }
    }, """ + str(AUTO_REFRESH_MINUTES) + """ * 60 * 1000);
}

// =========================================================================
// Init
// =========================================================================
document.addEventListener('DOMContentLoaded', function() {
    loadJobs();
    initSortable();
    startAutoRefresh();
});
</script>
</body>
</html>"""


# =============================================================================
# ENTRY POINT
# =============================================================================

if __name__ == '__main__':
    log.info("Starting Kickplate Workshop Stages on http://localhost:5000")
    app.run(host='0.0.0.0', port=5000, debug=False)
