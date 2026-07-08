/**
 * Supreme Detailing — Odoo booking webhook → GitHub relay
 * -------------------------------------------------------
 * Odoo can POST a webhook when a customer checks out, but its webhook action can't carry an
 * auth header, and GitHub's repository_dispatch trigger REQUIRES one. This tiny Web App sits
 * in the middle: Odoo POSTs here (unauthenticated) and this script calls GitHub with the token,
 * firing the sync workflow within seconds so the booking reserves its slot before anyone else
 * can take it. Same reshaper pattern as the Chat-alerts relay.
 *
 * DEPLOY (one-time):
 *   1. script.google.com → New project → paste this file.
 *   2. Project Settings (gear) → Script Properties → Add:
 *        GH_TOKEN   = <your GitHub PAT>   (fine-grained: repo supreme-detailing-cron,
 *                                          Repository permissions → Contents: Read and write;
 *                                          or a classic PAT with the "repo" scope)
 *        RELAY_KEY  = <any long random string>   (shared secret; must match Odoo's webhook URL)
 *   3. Deploy → New deployment → type "Web app":
 *        Execute as: Me
 *        Who has access: Anyone            (so Odoo can POST without a Google login)
 *      → copy the /exec Web app URL.
 *   4. Give Claude the URL; the Odoo webhook is set to  <URL>?key=<RELAY_KEY>
 */

var REPO = 'bigsexy-odoo/supreme-detailing-cron';
var EVENT_TYPE = 'booking-created';

function doPost(e) {
  var props = PropertiesService.getScriptProperties();
  // Shared-secret gate: the URL Odoo posts to carries ?key=<RELAY_KEY>. Reject anything else
  // so a random POST to this public URL can't spin up sync runs.
  var expected = props.getProperty('RELAY_KEY');
  var got = (e && e.parameter && e.parameter.key) || '';
  if (!expected || got !== expected) {
    return _json({ ok: false, error: 'unauthorised' }, 401);
  }
  var token = props.getProperty('GH_TOKEN');
  var res = UrlFetchApp.fetch('https://api.github.com/repos/' + REPO + '/dispatches', {
    method: 'post',
    contentType: 'application/json',
    headers: {
      'Authorization': 'token ' + token,
      'Accept': 'application/vnd.github+json',
      'X-GitHub-Api-Version': '2022-11-28',
    },
    payload: JSON.stringify({ event_type: EVENT_TYPE }),
    muteHttpExceptions: true,
  });
  var code = res.getResponseCode();
  // GitHub returns 204 No Content on a successful dispatch.
  return _json({ ok: code === 204, github_status: code, body: res.getContentText() }, 200);
}

function doGet() {
  return _json({ ok: true, msg: 'SD booking relay live — POST with ?key=<RELAY_KEY> to trigger a sync.' }, 200);
}

function _json(obj, code) {
  return ContentService
    .createTextOutput(JSON.stringify(obj))
    .setMimeType(ContentService.MimeType.JSON);
}
