/**
 * OdooAction.gs — "act from Google Chat" web app for Supreme Detailing (Tier 3).
 *
 * A booking card's buttons (built by booking_card.py) open this web app with an
 * HMAC-signed, time-limited link. It resolves the booking's linked CRM
 * opportunity (calendar.event.opportunity_id) and updates its stage:
 *   • action=paid  -> stage "Booked" (6)               [Mark paid / cash in hand]
 *   • action=menu  -> a page of stage buttons
 *   • action=stage -> the chosen stage
 * Every write is HMAC-verified and logged as a chatter note on the opportunity.
 *
 * DEPLOY (see CHAT_ACTIONS_SETUP.md):
 *   1. New Apps Script project (under admin@supremedetailing.co.nz), paste this.
 *   2. Project Settings -> Script Properties: ODOO_URL, ODOO_DB, ODOO_USER,
 *      ODOO_API_KEY, SHARED_SECRET  (values in CHAT_ACTIONS_SETUP.md).
 *   3. Deploy -> New deployment -> Web app -> Execute as: Me,
 *      Who has access: Anyone -> copy the /exec URL.
 *   4. Put that URL in the cron's SD_ACTION_URL and the same SHARED_SECRET in
 *      SD_ACTION_SECRET (GitHub secrets / .env).
 *
 * The SHARED_SECRET here MUST equal SD_ACTION_SECRET on the poster side, and the
 * signing string MUST match booking_card._sign():  "action|event|stage|exp".
 */

var CFG = (function () {
  var p = PropertiesService.getScriptProperties();
  return {
    ODOO_URL: p.getProperty('ODOO_URL'),
    ODOO_DB: p.getProperty('ODOO_DB'),
    ODOO_USER: p.getProperty('ODOO_USER'),
    ODOO_API_KEY: p.getProperty('ODOO_API_KEY'),
    SHARED_SECRET: p.getProperty('SHARED_SECRET')
  };
})();

var STAGE = { NEW: 1, BOOKED_UNPAID: 5, BOOKED: 6, WON: 4 };
var STAGE_NAMES = { 1: 'New', 5: 'Booked (Unpaid)', 6: 'Booked', 4: 'Won' };
var ALLOWED_STAGES = [1, 5, 6, 4];

// ---------------------------------------------------------------------------
// entry point
// ---------------------------------------------------------------------------
function doGet(e) {
  try {
    var p = e.parameter || {};
    verify_(p);
    if (p.action === 'menu') return htmlMenu_(p.event);

    var uid = login_();
    var oppId = resolveOpp_(uid, p.event);
    if (!oppId) return page_('No opportunity is linked to this booking.', false);

    if (p.action === 'paid') {
      setStage_(uid, oppId, STAGE.BOOKED);
      note_(uid, oppId, 'Marked <b>paid</b> via Google Chat.');
      return page_('Marked paid — opportunity moved to <b>Booked</b>.', true);
    }
    if (p.action === 'stage') {
      var sid = parseInt(p.stage, 10);
      if (ALLOWED_STAGES.indexOf(sid) < 0) return page_('That stage is not allowed.', false);
      setStage_(uid, oppId, sid);
      note_(uid, oppId, 'Stage changed to <b>' + STAGE_NAMES[sid] + '</b> via Google Chat.');
      return page_('Stage updated to <b>' + STAGE_NAMES[sid] + '</b>.', true);
    }
    return page_('Unknown action.', false);
  } catch (err) {
    return page_('Error: ' + (err && err.message ? err.message : err), false);
  }
}

// ---------------------------------------------------------------------------
// security
// ---------------------------------------------------------------------------
function verify_(p) {
  if (!p.action || !p.event || !p.exp || !p.sig) throw new Error('missing params');
  if (Number(p.exp) < Math.floor(Date.now() / 1000)) throw new Error('this link has expired');
  var stage = p.stage || '';
  var expect = hmacHex_(p.action + '|' + p.event + '|' + stage + '|' + p.exp, CFG.SHARED_SECRET);
  if (expect !== p.sig) throw new Error('invalid signature');
}

function hmacHex_(msg, key) {
  var raw = Utilities.computeHmacSha256Signature(msg, key);
  return raw.map(function (b) { return ('0' + (b & 0xff).toString(16)).slice(-2); }).join('');
}

// ---------------------------------------------------------------------------
// Odoo JSON-RPC
// ---------------------------------------------------------------------------
function rpc_(service, method, args) {
  var url = CFG.ODOO_URL.replace(/\/+$/, '') + '/jsonrpc';
  var payload = { jsonrpc: '2.0', method: 'call',
                  params: { service: service, method: method, args: args } };
  var res = UrlFetchApp.fetch(url, {
    method: 'post', contentType: 'application/json',
    payload: JSON.stringify(payload), muteHttpExceptions: true
  });
  var data = JSON.parse(res.getContentText());
  if (data.error) throw new Error(JSON.stringify(data.error.data || data.error));
  return data.result;
}

function login_() {
  return rpc_('common', 'login', [CFG.ODOO_DB, CFG.ODOO_USER, CFG.ODOO_API_KEY]);
}

function execKw_(uid, model, method, args, kwargs) {
  return rpc_('object', 'execute_kw',
              [CFG.ODOO_DB, uid, CFG.ODOO_API_KEY, model, method, args, kwargs || {}]);
}

function resolveOpp_(uid, eventId) {
  var ev = execKw_(uid, 'calendar.event', 'read', [[parseInt(eventId, 10)], ['opportunity_id']]);
  if (ev && ev[0] && ev[0].opportunity_id) return ev[0].opportunity_id[0];
  return null;
}

function setStage_(uid, oppId, stageId) {
  execKw_(uid, 'crm.lead', 'write', [[oppId], { stage_id: stageId }]);
}

function note_(uid, oppId, body) {
  try {
    execKw_(uid, 'crm.lead', 'message_post', [[oppId]],
            { body: body, message_type: 'comment' });
  } catch (e) { /* audit note is best-effort */ }
}

// ---------------------------------------------------------------------------
// HTML
// ---------------------------------------------------------------------------
function htmlMenu_(eventId) {
  var exp = Math.floor(Date.now() / 1000) + 3600;
  var base = ScriptApp.getService().getUrl();
  var btns = ALLOWED_STAGES.map(function (sid) {
    var sig = hmacHex_('stage|' + eventId + '|' + sid + '|' + exp, CFG.SHARED_SECRET);
    var url = base + '?action=stage&event=' + eventId + '&stage=' + sid + '&exp=' + exp + '&sig=' + sig;
    return '<a class="btn" href="' + url + '">' + STAGE_NAMES[sid] + '</a>';
  }).join('');
  return page_('<div class="h">Change stage</div>' + btns, true, true);
}

function page_(msg, ok, isMenu) {
  var colour = ok ? '#E17726' : '#DC3545';
  var icon = ok ? (isMenu ? '📋' : '✅') : '⚠️';
  var html =
    '<!doctype html><html><head><meta name="viewport" content="width=device-width, initial-scale=1">' +
    '<style>' +
    'body{font-family:Questrial,Helvetica,Arial,sans-serif;background:#F5F2F0;margin:0;padding:40px 16px;text-align:center;color:#2E1F14}' +
    '.card{max-width:420px;margin:0 auto;background:#fff;border-radius:16px;padding:32px 24px;box-shadow:0 8px 30px rgba(0,0,0,.08);border-top:6px solid ' + colour + '}' +
    '.i{font-size:44px;line-height:1}' +
    '.h{font-weight:700;font-size:18px;margin:12px 0}' +
    'p{font-size:16px;line-height:1.5}' +
    '.btn{display:block;margin:10px 0;padding:12px 16px;background:' + '#E17726' + ';color:#fff;text-decoration:none;border-radius:10px;font-weight:600}' +
    '.foot{margin-top:20px;font-size:12px;color:#A1A1A1}' +
    '</style></head><body><div class="card">' +
    '<div class="i">' + icon + '</div><p>' + msg + '</p>' +
    '<div class="foot">Supreme Detailing · you can close this tab</div>' +
    '</div></body></html>';
  return HtmlService.createHtmlOutput(html).setXFrameOptionsMode(HtmlService.XFrameOptionsMode.ALLOWALL);
}
