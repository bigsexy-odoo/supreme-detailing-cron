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
    SHARED_SECRET: p.getProperty('SHARED_SECRET'),
    SWAP_KEY: p.getProperty('SWAP_KEY'),      // static key for the /schedule page swap link
    GH_TOKEN: p.getProperty('GH_TOKEN'),      // PAT (Actions: read/write on the cron repo) for dispatch
    GH_REPO: 'bigsexy-odoo/supreme-detailing-cron'
  };
})();

var STAGE = { NEW: 1, BOOKED_UNPAID: 5, BOOKED: 6, WON: 4 };
var STAGE_NAMES = { 1: 'New', 5: 'Booked (Unpaid)', 6: 'Booked', 4: 'Won' };
var ALLOWED_STAGES = [1, 5, 6, 4];

// Payment journals. NB: this Odoo has ONLY a Bank journal (id 13, BNK1) — no Cash journal —
// so Cash currently also posts to Bank (the method is captured in the CRM note). To give cash
// its own ledger, create a Cash journal in Odoo and set PAY_JOURNAL.cash to its id.
var PAY_JOURNAL = { bank: 13, cash: 15 };   // Bank (BNK1) + Cash (CSH1) — report separately
var PAY_LABEL = { bank: 'Bank transfer', cash: 'Cash' };

function doGet(e) {
  try {
    var p = e.parameter || {};
    // Customer self-service reschedule (public /reschedule page) — gated by the booking's OWN
    // attendee access_token (per-booking secret, in the customer's email), NOT the staff key.
    if (p.action === 'cbooking') return customerBooking_(p);       // JSONP: booking + availability
    if (p.action === 'creschedule') return customerReschedule_(p); // confirm the customer's new time
    // Swap + reschedule accept a static key (the staff-only /schedule page can't HMAC-sign); else verify the sig.
    if (!((p.action === 'swap' || p.action === 'reschedule') && CFG.SWAP_KEY && p.key === CFG.SWAP_KEY)) verify_(p);
    if (p.action === 'menu') return htmlMenu_(p.event);   // Change-stage menu
    if (p.action === 'paid') return htmlPayMenu_(p.event); // Mark-paid -> choose method
    if (p.action === 'reschedule') {   // /schedule gantt drag -> move the booking's time (+ optional lane)
      var rstart = (p.start || '').replace('T', ' ');
      if (!/^\d{4}-\d{2}-\d{2} \d{2}:\d{2}$/.test(rstart)) return page_('Bad reschedule time.', false);
      var rdet = (p.detailer === 'Alex' || p.detailer === 'Kade') ? p.detailer : '';
      dispatchReschedule_(p.event, rstart, rdet);
      return page_('Rescheduling to <b>' + rstart + '</b>' + (rdet ? ' with <b>' + rdet + '</b>' : '') +
                   ' — the calendar updates in a few seconds.', true);
    }
    var uid = login_();
    if (p.action === 'swap') {   // flip Alex<->Kade -> dispatch the proven reassign_detailer.py
      var evr = execKw_(uid, 'calendar.event', 'read', [[parseInt(p.event, 10)], ['appointment_resource_ids']]);
      var cur = (evr && evr[0] && evr[0].appointment_resource_ids && evr[0].appointment_resource_ids[0]) || 0;
      if (cur !== 1 && cur !== 2) return page_('This booking has no Alex/Kade lane to swap.', false);
      var toName = (cur === 1) ? 'Kade' : 'Alex';   // 1=Alex, 2=Kade -> the other
      dispatchReassign_(p.event, toName);
      return page_('Swapping this booking to <b>' + toName + '</b> — the calendar updates in a few seconds.', true);
    }
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

// Fire the reassign workflow (runs reassign_detailer.py — all 5 places, idempotent).
function dispatchReassign_(eventId, detailer) {
  var res = UrlFetchApp.fetch('https://api.github.com/repos/' + CFG.GH_REPO + '/dispatches', {
    method: 'post', contentType: 'application/json',
    headers: { Authorization: 'Bearer ' + CFG.GH_TOKEN, Accept: 'application/vnd.github+json',
               'X-GitHub-Api-Version': '2022-11-28', 'User-Agent': 'sd-swap' },
    payload: JSON.stringify({ event_type: 'reassign-booking',
      client_payload: { event: String(eventId), detailer: detailer } }),
    muteHttpExceptions: true
  });
  if (res.getResponseCode() >= 300) throw new Error('dispatch failed: ' + res.getResponseCode() + ' ' + res.getContentText());
}

// Fire the reschedule workflow (runs reschedule_booking.py — event time move + hold + SDBK1 source).
function dispatchReschedule_(eventId, start, detailer) {
  var res = UrlFetchApp.fetch('https://api.github.com/repos/' + CFG.GH_REPO + '/dispatches', {
    method: 'post', contentType: 'application/json',
    headers: { Authorization: 'Bearer ' + CFG.GH_TOKEN, Accept: 'application/vnd.github+json',
               'X-GitHub-Api-Version': '2022-11-28', 'User-Agent': 'sd-reschedule' },
    payload: JSON.stringify({ event_type: 'reschedule-booking',
      client_payload: { event: String(eventId), start: String(start), detailer: detailer || '' } }),
    muteHttpExceptions: true
  });
  if (res.getResponseCode() >= 300) throw new Error('dispatch failed: ' + res.getResponseCode() + ' ' + res.getContentText());
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

// ===== Reschedule -> Chat (webhook reshaper) =========================================
// The base.automation on calendar.event (start written on a booking) POSTs here. We read the
// event, work out the detailer, and post a "Rescheduled" card to that detailer's Chat space.
// Covers ALL reschedule paths (customer portal, staff UI, and the /schedule drag) with one card.
var RES_NAME = { 1: 'Alex (North Shore)', 2: 'Kade (Central Auckland)' };

function doPost(e) {
  try {
    var p = (e && e.parameter) || {};
    var uid = login_();
    var cfg = getConfig_(uid, ['sd.reschedule_key', 'sd.gchat_north', 'sd.gchat_central']);
    if (!cfg['sd.reschedule_key'] || p.key !== cfg['sd.reschedule_key']) return _jsonOut({ ok: false, error: 'unauthorised' });
    var body = {};
    try { body = JSON.parse((e && e.postData && e.postData.contents) || '{}'); } catch (_) {}
    var eid = parseInt(body._id || (body[0] && body[0]._id) || p.event, 10);
    if (!eid) return _jsonOut({ ok: false, error: 'no event id' });
    postRescheduleCard_(uid, eid, cfg);
    return _jsonOut({ ok: true, event: eid });
  } catch (err) {
    return _jsonOut({ ok: false, error: String((err && err.message) || err) });
  }
}

function getConfig_(uid, keys) {
  var rows = execKw_(uid, 'ir.config_parameter', 'search_read', [[['key', 'in', keys]]], { fields: ['key', 'value'] });
  var out = {}; (rows || []).forEach(function (r) { out[r.key] = r.value; }); return out;
}

function postRescheduleCard_(uid, eid, cfg) {
  var ev = execKw_(uid, 'calendar.event', 'read',
    [[eid], ['appointment_resource_ids', 'appointment_booker_id', 'appointment_type_id', 'start', 'location']]);
  if (!ev || !ev[0]) return;
  ev = ev[0];
  var rid = (ev.appointment_resource_ids && ev.appointment_resource_ids[0]) || 0;
  var webhook = (rid === 1) ? cfg['sd.gchat_north'] : (rid === 2) ? cfg['sd.gchat_central'] : '';
  if (!webhook) return;
  var cust = (ev.appointment_booker_id && ev.appointment_booker_id[1]) || 'Customer';
  var svc = (ev.appointment_type_id && ev.appointment_type_id[1]) || 'Booking';
  var when = fmtNZ_(ev.start);
  var wasUtc = lastWas_(uid, eid);
  var was = wasUtc ? fmtNZ_(wasUtc) : '';
  var widgets = [{ decoratedText: { topLabel: 'Customer', text: cust } },
                 { decoratedText: { topLabel: 'Service', text: svc } }];
  if (was) widgets.push({ decoratedText: { topLabel: 'Was', text: was } });
  widgets.push({ decoratedText: { topLabel: 'Now', text: when } });
  if (ev.location) widgets.push({ decoratedText: { topLabel: 'Where', text: ev.location } });
  widgets.push({ decoratedText: { topLabel: 'Detailer', text: RES_NAME[rid] || '' } });
  var payload = {
    text: '🔁 *Rescheduled* — ' + cust + ' · ' + (was ? was + ' → ' : '') + when + ' · ' + svc,
    cardsV2: [{ cardId: 'resched-' + eid, card: { header: { title: '🔁 Booking rescheduled', subtitle: (was ? was + '  →  ' + when : when) }, sections: [{ widgets: widgets }] } }]
  };
  UrlFetchApp.fetch(webhook, { method: 'post', contentType: 'application/json', payload: JSON.stringify(payload), muteHttpExceptions: true });
}

// the previous start (the "was" time) from the chatter tracking log (field 13908 = start)
function lastWas_(uid, eid) {
  try {
    var tv = execKw_(uid, 'mail.tracking.value', 'search_read',
      [[['mail_message_id.res_id', '=', eid], ['mail_message_id.model', '=', 'calendar.event'], ['field_id', '=', 13908]]],
      { fields: ['old_value_datetime'], order: 'id desc', limit: 1 });
    return (tv && tv[0] && tv[0].old_value_datetime) || '';
  } catch (e) { return ''; }
}

// UTC 'YYYY-MM-DD HH:MM:SS' -> 'Sun 26 Jul, 9:00 am' (NZ)
function fmtNZ_(utc) {
  var m = /(\d{4})-(\d{2})-(\d{2}) (\d{2}):(\d{2})/.exec(utc || '');
  if (!m) return utc || '';
  var d = new Date(Date.UTC(+m[1], +m[2] - 1, +m[3], +m[4], +m[5]));
  return Utilities.formatDate(d, 'Pacific/Auckland', 'EEE d MMM, h:mm a');
}

function _jsonOut(obj) {
  return ContentService.createTextOutput(JSON.stringify(obj)).setMimeType(ContentService.MimeType.JSON);
}

// ===== Customer self-service reschedule (public /reschedule page) =====================
// Gated by the booking's attendee access_token (the per-booking secret in the customer email).
// cbooking -> JSONP {booking summary + that detailer's working hours + their future bookings}
// creschedule -> validate token, then fire the SAME dispatchReschedule_ pipeline as the staff drag.
function _jsonp(cb, obj) {
  return ContentService.createTextOutput((cb || 'callback') + '(' + JSON.stringify(obj) + ')')
    .setMimeType(ContentService.MimeType.JAVASCRIPT);
}

// the attendee record whose access_token matches, on this event (false if none = bad link)
function validAtt_(uid, eid, token) {
  if (!eid || !token) return false;
  var a = execKw_(uid, 'calendar.attendee', 'search',
    [[['event_id', '=', eid], ['access_token', '=', token]]]);
  return (a && a.length) ? a[0] : false;
}

// UTC 'YYYY-MM-DD HH:MM:SS' -> {day:'YYYY-MM-DD', min:<minutes past midnight>} in NZ local
function toNZ_(utc) {
  var m = /(\d{4})-(\d{2})-(\d{2}) (\d{2}):(\d{2})/.exec(utc || '');
  if (!m) return null;
  var d = new Date(Date.UTC(+m[1], +m[2] - 1, +m[3], +m[4], +m[5]));
  var day = Utilities.formatDate(d, 'Pacific/Auckland', 'yyyy-MM-dd');
  var hm = Utilities.formatDate(d, 'Pacific/Auckland', 'HH:mm').split(':');
  return { day: day, min: (+hm[0]) * 60 + (+hm[1]) };
}

// a resource's working window per weekday (0=Mon..6=Sun) from its resource calendar
function resHours_(uid, resId) {
  var byd = {};
  var r = execKw_(uid, 'appointment.resource', 'read', [[resId], ['resource_calendar_id']]);
  if (r && r[0] && r[0].resource_calendar_id) {
    var att = execKw_(uid, 'resource.calendar.attendance', 'search_read',
      [[['calendar_id', '=', r[0].resource_calendar_id[0]]]], { fields: ['dayofweek', 'hour_from', 'hour_to'] });
    (att || []).forEach(function (a) {
      var d = String(parseInt(a.dayofweek, 10));
      var f = Math.round(a.hour_from * 60), t = Math.round(a.hour_to * 60);
      byd[d] = byd[d] ? [Math.min(byd[d][0], f), Math.max(byd[d][1], t)] : [f, t];
    });
  }
  return byd;
}

function customerBooking_(p) {
  var cb = p.callback || 'callback';
  try {
    var uid = login_();
    var eid = parseInt(p.e, 10), token = p.t;
    if (!validAtt_(uid, eid, token)) return _jsonp(cb, { ok: false, error: 'invalid link' });
    var ev = execKw_(uid, 'calendar.event', 'read', [[eid],
      ['appointment_resource_ids', 'appointment_booker_id', 'appointment_type_id',
       'start', 'stop', 'duration', 'location', 'appointment_status']]);
    if (!ev || !ev[0]) return _jsonp(cb, { ok: false, error: 'not found' });
    ev = ev[0];
    if (ev.appointment_status === 'cancelled') return _jsonp(cb, { ok: false, error: 'cancelled' });
    var resId = (ev.appointment_resource_ids && ev.appointment_resource_ids[0]) || 0;
    var durMin = Math.round((ev.duration || 0) * 60);
    var cur = toNZ_(ev.start);
    // that resource's future booked jobs (for greying overlaps); include this event so the client can exclude it
    var todayUtc = Utilities.formatDate(new Date(), 'UTC', 'yyyy-MM-dd') + ' 00:00:00';
    var others = execKw_(uid, 'calendar.event', 'search_read',
      [[['appointment_resource_ids', 'in', [resId]], ['appointment_status', '=', 'booked'], ['stop', '>', todayUtc]]],
      { fields: ['id', 'start', 'duration'] });
    var taken = (others || []).map(function (o) {
      var n = toNZ_(o.start); return { event: o.id, day: n ? n.day : '', startMin: n ? n.min : 0, dur: Math.round((o.duration || 0) * 60) };
    });
    var at = execKw_(uid, 'appointment.type', 'read',
      [[(ev.appointment_type_id && ev.appointment_type_id[0]) || 0], ['min_schedule_hours', 'min_cancellation_hours']]);
    var lead = (at && at[0]) ? (at[0].min_schedule_hours || 0) : 0;
    return _jsonp(cb, {
      ok: true, event: eid,
      cust: (ev.appointment_booker_id && ev.appointment_booker_id[1]) || '',
      service: (ev.appointment_type_id && ev.appointment_type_id[1]) || 'Booking',
      suburb: ev.location || '', resId: String(resId), resName: RES_NAME[resId] || '',
      durMin: durMin, curDay: cur ? cur.day : '', curStartMin: cur ? cur.min : 0,
      hours: resHours_(uid, resId), taken: taken, leadHours: lead
    });
  } catch (err) {
    return _jsonp(cb, { ok: false, error: String((err && err.message) || err) });
  }
}

function customerReschedule_(p) {
  try {
    var uid = login_();
    var eid = parseInt(p.e, 10), token = p.t;
    if (!validAtt_(uid, eid, token)) return page_('This reschedule link is invalid or has expired.', false);
    var start = (p.start || '').replace('T', ' ');
    if (!/^\d{4}-\d{2}-\d{2} \d{2}:\d{2}$/.test(start)) return page_('That time looks wrong — please try again.', false);
    dispatchReschedule_(String(eid), start, '');   // no detailer change for customers
    return page_('Your booking is being moved to <b>' + start + '</b>. ' +
                 'We&#39;ll email you the updated confirmation shortly.', true);
  } catch (err) {
    return page_('Sorry, something went wrong: ' + ((err && err.message) || err), false);
  }
}
