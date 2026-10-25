/**
 * Coach-Rückweg: Google Kalender "Training" -> intervals.icu (-> Garmin -> Fenix)
 *
 * Verschiebst du ein geplantes Training im Kalender "Training", überträgt dieses
 * Skript das neue Datum (inkl. Uhrzeit) innerhalb von ca. 1 Minute an intervals.icu.
 * intervals.icu schickt das Training dann an Garmin Connect, die Fenix holt es beim
 * nächsten Sync.
 *
 * Regeln:
 *  - nur Termine, die vom Coach angelegt wurden (geplante Trainings)
 *  - erledigte Termine (Titel beginnt mit ✅) werden ignoriert
 *  - Löschen im Kalender löscht KEIN Training (der Termin kommt beim nächsten Sync zurück).
 *    Trainings bitte im Claude-Chat streichen.
 *
 * Einrichtung: siehe COACH.md, Abschnitt "Verschieben im Kalender".
 */

const CONFIG = {
  CALENDAR_ID: 'HIER_KALENDER_ID_EINTRAGEN@group.calendar.google.com',
  ATHLETE_ID: 'i573787',
  TIMEZONE: 'Europe/Berlin',
};

const ICU = 'https://intervals.icu/api/v1';

/** Einmalig manuell ausführen: legt den Trigger an und merkt sich den Kalenderstand. */
function setup() {
  if (!PropertiesService.getScriptProperties().getProperty('INTERVALS_API_KEY')) {
    throw new Error('Bitte zuerst die Skripteigenschaft INTERVALS_API_KEY anlegen (Projekteinstellungen).');
  }
  ScriptApp.getProjectTriggers().forEach(function (t) { ScriptApp.deleteTrigger(t); });
  ScriptApp.newTrigger('onCalendarChange')
    .forUserCalendar(CONFIG.CALENDAR_ID)
    .onEventUpdated()
    .create();
  initSyncToken_();
  Logger.log('Fertig: Trigger aktiv für ' + CONFIG.CALENDAR_ID);
}

/** Wird von Google bei jeder Änderung im Kalender aufgerufen. */
function onCalendarChange() {
  const lock = LockService.getScriptLock();
  if (!lock.tryLock(30000)) return;
  try {
    processChanges_();
  } finally {
    lock.releaseLock();
  }
}

function initSyncToken_() {
  let pageToken, res;
  do {
    res = Calendar.Events.list(CONFIG.CALENDAR_ID, { pageToken: pageToken, maxResults: 2500, showDeleted: true });
    pageToken = res.nextPageToken;
  } while (pageToken);
  PropertiesService.getScriptProperties().setProperty('SYNC_TOKEN', res.nextSyncToken);
}

function processChanges_() {
  const props = PropertiesService.getScriptProperties();
  const token = props.getProperty('SYNC_TOKEN');
  if (!token) { initSyncToken_(); return; }
  let pageToken, res, changed = [];
  try {
    do {
      res = Calendar.Events.list(CONFIG.CALENDAR_ID, { syncToken: token, pageToken: pageToken, showDeleted: true });
      changed = changed.concat(res.items || []);
      pageToken = res.nextPageToken;
    } while (pageToken);
  } catch (err) {
    if (String(err).indexOf('410') !== -1 || String(err).indexOf('Sync token') !== -1) {
      initSyncToken_();   // Token abgelaufen: neu aufsetzen
      return;
    }
    throw err;
  }
  props.setProperty('SYNC_TOKEN', res.nextSyncToken);
  changed.forEach(handleEvent_);
}

function handleEvent_(ev) {
  const priv = (ev.extendedProperties && ev.extendedProperties.private) || {};
  if (priv.source !== 'coach' || !priv.icu_key || priv.icu_key.indexOf('event-') !== 0) return;
  if (ev.status === 'cancelled') return;
  if ((ev.summary || '').indexOf('✅') === 0) return;

  const icuId = priv.icu_key.substring('event-'.length);
  const start = ev.start.dateTime
    ? Utilities.formatDate(new Date(ev.start.dateTime), CONFIG.TIMEZONE, "yyyy-MM-dd'T'HH:mm:ss")
    : ev.start.date + 'T00:00:00';

  const path = '/athlete/' + CONFIG.ATHLETE_ID + '/events/' + icuId;
  const icu = icu_('get', path);
  if (!icu) return;
  // Nur das Datum entscheidet; reine Uhrzeit-Änderungen bleiben im Kalender.
  if ((icu.start_date_local || '').substring(0, 10) === start.substring(0, 10)) return;

  icu_('put', path, { start_date_local: start });
  Logger.log('Verschoben: ' + icu.name + ' -> ' + start);
}

function icu_(method, path, body) {
  const key = PropertiesService.getScriptProperties().getProperty('INTERVALS_API_KEY');
  const opts = {
    method: method,
    muteHttpExceptions: true,
    headers: { Authorization: 'Basic ' + Utilities.base64Encode('API_KEY:' + key) },
  };
  if (body) {
    opts.contentType = 'application/json';
    opts.payload = JSON.stringify(body);
  }
  const r = UrlFetchApp.fetch(ICU + path, opts);
  if (r.getResponseCode() === 404) return null;
  if (r.getResponseCode() >= 300) throw new Error('intervals.icu ' + r.getResponseCode() + ': ' + r.getContentText());
  const txt = r.getContentText();
  return txt ? JSON.parse(txt) : null;
}
