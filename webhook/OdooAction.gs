// OdooAction.gs — signed "act from Chat" web app for Supreme Detailing booking cards.
// Tapping a card button opens this (doGet), which HMAC-verifies the signed link and writes to
// Odoo via JSON-RPC. Config lives in Script Properties (never in code).
//   Script Properties: ODOO_URL, ODOO_DB, ODOO_USER, ODOO_API_KEY, SHARED_SECRET
//   SHARED_SECRET must equal the GitHub Actions secret SD_ACTION_SECRET (used by booking_card.py).
// Signing (matches booking_card._sign): HMAC-SHA256(hex) over "action|event|stage|exp".
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

// Payment journals. NB: this Odoo has ONLY a Bank journal (id 13, BNK1) — no Cash journal —
// so Cash currently also posts to Bank (the method is captured in the CRM note). To give cash
// its own ledger, create a Cash journal in Odoo and set PAY_JOURNAL.cash to its id.
var PAY_JOURNAL = { bank: 13, cash: 13 };
var PAY_LABEL = { bank: 'Bank transfer', cash: 'Cash' };

function doGet(e) {
  try {
    var p = e.parameter || {};
    verify_(p);
    if (p.action === 'menu') return htmlMenu_(p.event);   // Change-stage menu
    if (p.action === 'paid') return htmlPayMenu_(p.event); // Mark-paid -> choose method
    var uid = login_();
    var oppId = resolveOpp_(uid, p.event);

    if (p.action === 'payreg') {   // register the payment (method carried in the stage slot)
      var method = (p.stage === 'cash') ? 'cash' : 'bank';
      var invId = oppId ? resolveInvoice_(uid, oppId) : null;
      if (invId) registerPayment_(uid, invId, PAY_JOURNAL[method]);
      if (oppId) {
        setStage_(uid, oppId, STAGE.BOOKED);
        note_(uid, oppId, 'Marked paid (' + PAY_LABEL[method] + ') via Google Chat' +
              (invId ? ' — invoice settled.' : ' (no open invoice found).'));
      }
      var msg = invId
        ? 'Payment registered (' + PAY_LABEL[method] + ') — invoice marked <b>Paid</b> and booking moved to <b>Booked</b>.'
        : (oppId ? 'No open invoice found — booking moved to <b>Booked</b>.'
                 : 'No opportunity/invoice is linked to this booking.');
      return page_(msg, true);
    }

    if (!oppId) return page_('No opportunity is linked to this booking.', false);
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

function rpc_(service, method, args) {
  var url = CFG.ODOO_URL.replace(/\/+$/, '') + '/jsonrpc';
  var payload = { jsonrpc: '2.0', method: 'call', params: { service: service, method: method, args: args } };
  var res = UrlFetchApp.fetch(url, {
    method: 'post', contentType: 'application/json',
    payload: JSON.stringify(payload), muteHttpExceptions: true
  });
  var data = JSON.parse(res.getContentText());
  if (data.error) throw new Error(JSON.stringify(data.error.data || data.error));
  return data.result;
}

function login_() { return rpc_('common', 'login', [CFG.ODOO_DB, CFG.ODOO_USER, CFG.ODOO_API_KEY]); }

function execKw_(uid, model, method, args, kwargs) {
  return rpc_('object', 'execute_kw', [CFG.ODOO_DB, uid, CFG.ODOO_API_KEY, model, method, args, kwargs || {}]);
}

function resolveOpp_(uid, eventId) {
  var ev = execKw_(uid, 'calendar.event', 'read', [[parseInt(eventId, 10)], ['opportunity_id']]);
  if (ev && ev[0] && ev[0].opportunity_id) return ev[0].opportunity_id[0];
  return null;
}

// opp -> its sale orders -> the first POSTED, still-owing customer invoice (skip already-paid,
// so tapping Mark-paid twice never double-pays).
function resolveInvoice_(uid, oppId) {
  var opp = execKw_(uid, 'crm.lead', 'read', [[oppId], ['order_ids']]);
  if (!opp || !opp[0] || !opp[0].order_ids || !opp[0].order_ids.length) return null;
  var orders = execKw_(uid, 'sale.order', 'read', [opp[0].order_ids, ['invoice_ids']]);
  var invIds = [];
  orders.forEach(function (o) { if (o.invoice_ids && o.invoice_ids.length) invIds = invIds.concat(o.invoice_ids); });
  if (!invIds.length) return null;
  var moves = execKw_(uid, 'account.move', 'read', [invIds, ['move_type', 'state', 'payment_state', 'amount_residual']]);
  for (var i = 0; i < moves.length; i++) {
    var m = moves[i];
    if (m.move_type === 'out_invoice' && m.state === 'posted' && m.amount_residual > 0 &&
        m.payment_state !== 'paid' && m.payment_state !== 'in_payment') {
      return m.id;
    }
  }
  return null;
}

// Register a full Manual payment for the invoice via the standard account.payment.register
// wizard (amount defaults to the residual, auto-reconciled -> invoice flips to Paid/In Payment).
function registerPayment_(uid, invoiceId, journalId) {
  var ctx = { active_ids: [invoiceId], active_model: 'account.move' };
  var wizId = execKw_(uid, 'account.payment.register', 'create', [{ journal_id: journalId }], { context: ctx });
  if (Array.isArray(wizId)) wizId = wizId[0];
  execKw_(uid, 'account.payment.register', 'action_create_payments', [[wizId]], { context: ctx });
}

function setStage_(uid, oppId, stageId) { execKw_(uid, 'crm.lead', 'write', [[oppId], { stage_id: stageId }]); }

function note_(uid, oppId, body) {
  try { execKw_(uid, 'crm.lead', 'message_post', [[oppId]], { body: body, message_type: 'comment' }); } catch (e) {}
}

// Mark-paid -> choose how they paid (both post to the Bank journal until a Cash journal exists).
function htmlPayMenu_(eventId) {
  var exp = Math.floor(Date.now() / 1000) + 3600;
  var base = ScriptApp.getService().getUrl();
  var btns = [['bank', '🏦 Bank transfer'], ['cash', '💵 Cash']].map(function (m) {
    var sig = hmacHex_('payreg|' + eventId + '|' + m[0] + '|' + exp, CFG.SHARED_SECRET);
    var url = base + '?action=payreg&event=' + eventId + '&stage=' + m[0] + '&exp=' + exp + '&sig=' + sig;
    return '<a class="btn" href="' + url + '">' + m[1] + '</a>';
  }).join('');
  return page_('<div class="h">Mark paid — how did they pay?</div>' + btns, true, true);
}

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
    '.i{font-size:44px;line-height:1}.h{font-weight:700;font-size:18px;margin:12px 0}p{font-size:16px;line-height:1.5}' +
    '.btn{display:block;margin:10px 0;padding:12px 16px;background:#E17726;color:#fff;text-decoration:none;border-radius:10px;font-weight:600}' +
    '.foot{margin-top:20px;font-size:12px;color:#A1A1A1}' +
    '</style></head><body><div class="card">' +
    '<div class="i">' + icon + '</div><p>' + msg + '</p>' +
    '<div class="foot">Supreme Detailing · you can close this tab</div>' +
    '</div></body></html>';
  return HtmlService.createHtmlOutput(html).setXFrameOptionsMode(HtmlService.XFrameOptionsMode.ALLOWALL);
}
