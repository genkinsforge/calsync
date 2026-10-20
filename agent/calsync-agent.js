// calsync agent for the work Mac. JavaScript for Automation + EventKit; nothing to install.
//
//   osascript -l JavaScript calsync-agent.js --help
//
// The shell wrapper (calsync-agent.sh) does the HTTP with curl; this script only
// talks to EventKit and reads/writes local JSON files.

ObjC.import('Foundation');
ObjC.import('AppKit');    // NSWorkspace (Outlook detection)
ObjC.import('EventKit');
ObjC.import('stdlib');   // $.exit

var VERSION = '0.1.0';
var USAGE = [
  'calsync-agent.js ' + VERSION,
  '',
  'Usage: osascript -l JavaScript calsync-agent.js [--config FILE] MODE',
  '',
  'Modes:',
  '  --help                     this text',
  '  --self-test                run the built-in tests of the matching logic (no calendar access)',
  '  --list-calendars           show every calendar EventKit can see (title, source, type, id)',
  '  --calendar-names           print the calsync calendar names from the config, one per line',
  '  --export OUT.json          write this Mac\'s real events for each mapped calendar (POST body)',
  '  --apply BLOCKS.json --calendar NAME [--dry-run]',
  '                             create/update/delete calsync Busy blocks to match the hub',
  '',
  'Config (default ~/.config/calsync-agent/config.json):',
  '  {"hub_url": "...", "token": "...", "days_back": 1, "days_ahead": 60,',
  '   "calendars": {"phdata": {"title": "Calendar", "source": "jared@phdata.io"},',
  '                 "kiewit": {"id": "<calendarIdentifier from --list-calendars>"}}}',
].join('\n');

// EventKit enum values (stable public ABI).
var EK_ENTITY_EVENT = 0;
var EK_SPAN_THIS_EVENT = 0;
var EK_AUTH = {0: 'notDetermined', 1: 'restricted', 2: 'denied', 3: 'fullAccess', 4: 'writeOnly'};
var EK_SOURCE = {0: 'local', 1: 'exchange', 2: 'caldav', 3: 'mobileme', 4: 'subscribed', 5: 'birthdays'};
var EK_AVAIL = {'-1': 'notSupported', 0: 'busy', 1: 'free', 2: 'tentative', 3: 'unavailable'};
var EK_STATUS = {0: 'none', 1: 'confirmed', 2: 'tentative', 3: 'canceled'};
var EK_PART = {0: 'unknown', 1: 'pending', 2: 'accepted', 3: 'declined', 4: 'tentative', 5: 'delegated',
               6: 'completed', 7: 'inProcess'};
var MIRROR_RE = /^([0-9a-f]{12})\.([a-z0-9][a-z0-9_-]{0,31})\.([0-9a-f]{24})$/;
var MARK_LINE = 'calsync-mirror: ';
var HASH_LINE = 'calsync-hash: ';

// ------------------------------------------------------------------ small helpers

function log(msg) { console.log(msg); }   // console.log goes to stderr in osascript

function str(x) {
  if (x === undefined || x === null) return '';
  var v = ObjC.unwrap(x);
  return (v === undefined || v === null) ? '' : String(v);
}

function num(x) { var v = ObjC.unwrap(x); return typeof v === 'number' ? v : Number(v); }

function bool(x) {
  var v = ObjC.unwrap(x);   // the bridge returns BOOL as boolean but NSInteger as string
  if (typeof v === 'string') return v !== '' && v !== '0' && v !== 'false';
  return !!v;
}

function home() { return ObjC.unwrap($.NSHomeDirectory()); }

function readFile(path) {
  var s = $.NSString.stringWithContentsOfFileEncodingError(path, $.NSUTF8StringEncoding, null);
  if (s.isNil()) throw new Error('cannot read ' + path);
  return ObjC.unwrap(s);
}

function writeFile(path, text) {
  var ok = $(text).writeToFileAtomicallyEncodingError(path, true, $.NSUTF8StringEncoding, null);
  if (!ok) throw new Error('cannot write ' + path);
}

function nsDate(ms) { return $.NSDate.dateWithTimeIntervalSince1970(ms / 1000); }

function msOf(d) { return d === undefined || d === null || (d.isNil && d.isNil()) ? NaN : num(d.timeIntervalSince1970) * 1000; }

function isoZ(ms) { return new Date(ms).toISOString().replace(/\.\d{3}Z$/, 'Z'); }

function localDateStr(ms) {
  var d = new Date(ms);
  function p(n) { return (n < 10 ? '0' : '') + n; }
  return d.getFullYear() + '-' + p(d.getMonth() + 1) + '-' + p(d.getDate());
}

function localMidnightMs(dateStr) {
  var parts = dateStr.split('-');
  return new Date(+parts[0], +parts[1] - 1, +parts[2]).getTime();
}

function parseArgs(argv) {
  var a = {config: home() + '/.config/calsync-agent/config.json', dryRun: false};
  for (var i = 0; i < argv.length; i++) {
    var k = argv[i];
    if (k === '--config') a.config = argv[++i];
    else if (k === '--export') { a.mode = 'export'; a.out = argv[++i]; }
    else if (k === '--apply') { a.mode = 'apply'; a.blocks = argv[++i]; }
    else if (k === '--calendar') a.calendar = argv[++i];
    else if (k === '--dry-run') a.dryRun = true;
    else if (k === '--list-calendars') a.mode = 'list';
    else if (k === '--calendar-names') a.mode = 'names';
    else if (k === '--self-test') a.mode = 'selftest';
    else if (k === '--help' || k === '-h') a.mode = 'help';
    else throw new Error('unknown argument: ' + k);
  }
  return a;
}

function loadConfig(path) {
  var cfg = JSON.parse(readFile(path));
  if (!cfg.calendars || typeof cfg.calendars !== 'object') throw new Error('config: "calendars" missing');
  cfg.days_back = cfg.days_back === undefined ? 1 : cfg.days_back;
  cfg.days_ahead = cfg.days_ahead === undefined ? 60 : cfg.days_ahead;
  return cfg;
}

// ------------------------------------------------------------------ pure logic (unit-tested by --self-test)

function parseMirror(s) {
  var m = MIRROR_RE.exec(s || '');
  return m ? {install: m[1], source: m[2], id: s} : null;
}

// Find calsync's marker on an event: URL calsync://<id>, else a notes line.
function markerOf(url, notes) {
  if (url && url.indexOf('calsync://') === 0) {
    var id = url.substring('calsync://'.length).replace(/\/$/, '');
    if (parseMirror(id)) return id;
  }
  var lines = (notes || '').split(/\r?\n/);
  for (var i = 0; i < lines.length; i++) {
    if (lines[i].indexOf(MARK_LINE) === 0) {
      var v = lines[i].substring(MARK_LINE.length).trim();
      if (parseMirror(v)) return v;
    }
  }
  return null;
}

function hashOf(notes) {
  var lines = (notes || '').split(/\r?\n/);
  for (var i = 0; i < lines.length; i++) {
    if (lines[i].indexOf(HASH_LINE) === 0) return lines[i].substring(HASH_LINE.length).trim();
  }
  return '';
}

function blockNotes(block) {
  return MARK_LINE + block.mirror_id + '\n' + HASH_LINE + block.hash + '\nManaged by calsync. Changes here are overwritten.';
}

// Desired time span of a block in epoch ms (all-day: local midnight to last-day 23:59:59).
function blockSpan(block) {
  if (block.all_day) {
    return {start: localMidnightMs(block.start), end: localMidnightMs(block.end) - 1000};
  }
  return {start: Date.parse(block.start), end: Date.parse(block.end)};
}

// existing: [{ref, marker, hash, start, end, allDay}] for every event in the calendar window.
// Returns {create: [block], update: [{block, ref}], remove: [ref], keep: n}. Unmarked events and
// events marked by another installation are never in update/remove.
// Blocks whose source calendar is in `frozen` (the hub could not read it) are never removed.
function planApply(blocks, existing, installId, windowStartMs, frozen) {
  frozen = frozen || [];
  var ours = {}, dupes = [];
  existing.forEach(function (e) {
    var p = parseMirror(e.marker);
    if (!p || p.install !== installId) return;
    if (ours[e.marker]) dupes.push(e.ref); else ours[e.marker] = e;
  });
  var want = {}, plan = {create: [], update: [], remove: dupes.slice(), keep: 0};
  blocks.forEach(function (b) {
    if (!parseMirror(b.mirror_id) || parseMirror(b.mirror_id).install !== installId) return;
    want[b.mirror_id] = true;
    var e = ours[b.mirror_id];
    var span = blockSpan(b);
    if (!e) plan.create.push(b);
    else if (e.hash !== b.hash || e.allDay !== !!b.all_day || Math.abs(e.start - span.start) > 1000 ||
             Math.abs(e.end - span.end) > 1000) plan.update.push({block: b, ref: e.ref});
    else plan.keep++;
  });
  Object.keys(ours).forEach(function (mid) {
    var e = ours[mid];
    if (!want[mid] && e.end > windowStartMs && frozen.indexOf(parseMirror(mid).source) < 0) plan.remove.push(e.ref);
  });
  return plan;
}

// Plain JS record of an EventKit-shaped event (already unwrapped), as the hub expects it.
function toPushRecord(r) {
  return {
    id: r.externalId + '@' + isoZ(r.occurrence || r.start),
    title: r.title, all_day: r.allDay,
    start: r.allDay ? localDateStr(r.start) : isoZ(r.start),
    end: r.allDay ? localDateStr(r.end + 1000) : isoZ(r.end),
    location: r.location, notes: r.notes, url: r.url,
    availability: EK_AVAIL[r.availability] || 'busy',
    status: EK_STATUS[r.status] || 'none',
    self_status: r.selfStatus === null ? null : (EK_PART[r.selfStatus] || 'unknown'),
    marker: markerOf(r.url, r.notes),
  };
}

// ------------------------------------------------------------------ EventKit access

function eventStore() {
  var store = $.EKEventStore.alloc.init;
  var status = num($.EKEventStore.authorizationStatusForEntityType(EK_ENTITY_EVENT));
  if (status === 3) return store;
  if (status !== 0) {
    throw new Error('Calendar access is "' + (EK_AUTH[status] || status) + '". Allow it in System Settings > ' +
      'Privacy & Security > Calendars (Full Access) for Terminal and/or osascript, then run again.');
  }
  var done = false, granted = false;
  var cb = function (ok, err) { granted = !!ok; done = true; };
  if (store.respondsToSelector('requestFullAccessToEventsWithCompletion:')) {
    store.requestFullAccessToEventsWithCompletion(cb);        // macOS 14+
  } else {
    store.requestAccessToEntityTypeCompletion(EK_ENTITY_EVENT, cb);
  }
  var deadline = Date.now() + 120000;
  while (!done && Date.now() < deadline) {
    $.NSRunLoop.currentRunLoop.runUntilDate($.NSDate.dateWithTimeIntervalSinceNow(0.25));
  }
  if (!granted) throw new Error('Calendar access was not granted (answer the macOS prompt, or allow it in System Settings).');
  return $.EKEventStore.alloc.init;   // a fresh store sees calendars after the grant
}

function allCalendars(store) {
  var arr = store.calendarsForEntityType(EK_ENTITY_EVENT), out = [];
  for (var i = 0; i < num(arr.count); i++) {
    var c = arr.objectAtIndex(i), src = c.source;
    out.push({
      obj: c, title: str(c.title), id: str(c.calendarIdentifier),
      source: str(src.title), sourceType: EK_SOURCE[num(src.sourceType)] || String(num(src.sourceType)),
      writable: bool(c.allowsContentModifications),
    });
  }
  return out;
}

function outlookInstalled() {
  var url = $.NSWorkspace.sharedWorkspace.URLForApplicationWithBundleIdentifier('com.microsoft.Outlook');
  return !url.isNil();
}

var OUTLOOK_HINT = 'EventKit only sees accounts added to macOS Internet Accounts / Calendar.app. If this ' +
  'Exchange account exists only in Outlook, add it in System Settings > Internet Accounts (if your employer ' +
  'allows it). Reading legacy Outlook through AppleScript is not implemented (new Outlook has no AppleScript).';

function resolveCalendar(cals, spec, name) {
  var hits = cals.filter(function (c) {
    if (spec.id) return c.id === spec.id;
    return c.title === spec.title && (!spec.source || c.source === spec.source);
  });
  if (hits.length === 1) return hits[0];
  if (hits.length === 0) {
    throw new Error('calendar "' + name + '" (' + JSON.stringify(spec) + ') not found in EventKit. ' +
      (outlookInstalled() ? OUTLOOK_HINT : 'Run --list-calendars to see what is visible.'));
  }
  throw new Error('calendar "' + name + '" matches ' + hits.length + ' calendars; use "id" from --list-calendars.');
}

function eventsIn(store, cal, startMs, endMs) {
  var pred = store.predicateForEventsWithStartDateEndDateCalendars(nsDate(startMs), nsDate(endMs),
    $.NSArray.arrayWithObject(cal.obj));
  var arr = store.eventsMatchingPredicate(pred), out = [];
  for (var i = 0; i < num(arr.count); i++) {
    var ev = arr.objectAtIndex(i);
    var url = ev.URL;
    var selfStatus = null;
    var atts = ev.attendees;
    if (atts && !atts.isNil()) {
      for (var j = 0; j < num(atts.count); j++) {
        var p = atts.objectAtIndex(j);
        if (bool(p.isCurrentUser)) selfStatus = num(p.participantStatus);
      }
    }
    var occ = msOf(ev.occurrenceDate);
    out.push({
      obj: ev,
      externalId: str(ev.calendarItemExternalIdentifier) || str(ev.eventIdentifier),
      title: str(ev.title), start: msOf(ev.startDate), end: msOf(ev.endDate), allDay: bool(ev.isAllDay),
      occurrence: isNaN(occ) ? null : occ,
      location: str(ev.location), notes: str(ev.notes),
      url: (url && !url.isNil()) ? str(url.absoluteString) : '',
      availability: num(ev.availability), status: num(ev.status), selfStatus: selfStatus,
    });
  }
  return out;
}

// ------------------------------------------------------------------ modes

function listCalendars() {
  var cals = allCalendars(eventStore());
  var lines = ['title | source | type | writable | id'];
  cals.forEach(function (c) {
    lines.push([c.title, c.source, c.sourceType, c.writable ? 'yes' : 'no', c.id].join(' | '));
  });
  if (!cals.some(function (c) { return c.sourceType === 'exchange'; })) {
    lines.push('');
    lines.push('No Exchange calendars are visible to EventKit.');
    if (outlookInstalled()) lines.push(OUTLOOK_HINT);
  }
  return lines.join('\n');
}

function exportEvents(cfg, outPath) {
  var store = eventStore(), cals = allCalendars(store);
  var now = Date.now(), start = now - cfg.days_back * 86400000, end = now + cfg.days_ahead * 86400000;
  var body = {agent_version: VERSION, generated_at: isoZ(now), calendars: {}};
  Object.keys(cfg.calendars).forEach(function (name) {
    try {
      var cal = resolveCalendar(cals, cfg.calendars[name], name);
      var evs = eventsIn(store, cal, start, end).map(toPushRecord);
      body.calendars[name] = {events: evs};
      log(name + ': ' + evs.length + ' events from "' + cal.title + '" (' + cal.source + ')');
    } catch (e) {
      body.calendars[name] = {error: String(e.message || e)};
      log(name + ': ERROR ' + (e.message || e));
    }
  });
  writeFile(outPath, JSON.stringify(body));
  return '';
}

function saveBlock(store, ev, block) {
  var span = blockSpan(block);
  ev.setTitle('Busy');
  ev.setAllDay(!!block.all_day);
  ev.setStartDate(nsDate(span.start));
  ev.setEndDate(nsDate(span.end));
  ev.setLocation('');
  ev.setNotes(blockNotes(block));
  ev.setURL($.NSURL.URLWithString('calsync://' + block.mirror_id));
  ev.setAvailability(0);           // busy
  ev.setAlarms($.NSArray.array);   // no reminders
  var err = $();
  if (!store.saveEventSpanCommitError(ev, EK_SPAN_THIS_EVENT, true, err)) {
    throw new Error('save failed: ' + errText(err));
  }
}

function errText(err) {
  try { return str(err.localizedDescription); } catch (e) { return 'unknown error'; }
}

function applyBlocks(cfg, blocksPath, name, dryRun) {
  var resp = JSON.parse(readFile(blocksPath));
  if (!resp.install_id || !resp.window || !Array.isArray(resp.blocks)) throw new Error('bad blocks file');
  if (resp.calendar !== name) throw new Error('blocks are for ' + resp.calendar + ', not ' + name);
  var store = eventStore();
  var cal = resolveCalendar(allCalendars(store), cfg.calendars[name], name);
  if (!cal.writable) throw new Error('calendar "' + name + '" is read-only in EventKit');
  var winStart = Date.parse(resp.window.start), winEnd = Date.parse(resp.window.end);
  var existing = eventsIn(store, cal, winStart - 86400000, winEnd + 86400000).map(function (r, i) {
    return {ref: r, marker: markerOf(r.url, r.notes), hash: hashOf(r.notes), start: r.start, end: r.end,
            allDay: r.allDay};
  });
  var plan = planApply(resp.blocks, existing, resp.install_id, winStart, resp.frozen_sources || []);
  var tag = (dryRun ? 'DRY-RUN ' : '') + '[' + name + '] ';
  plan.create.forEach(function (b) { log(tag + 'create Busy ' + b.start + ' -> ' + b.end); });
  plan.update.forEach(function (u) { log(tag + 'update Busy ' + u.block.start + ' -> ' + u.block.end); });
  plan.remove.forEach(function (r) { log(tag + 'delete Busy ' + isoZ(r.start) + ' -> ' + isoZ(r.end)); });
  var errors = 0;
  if (!dryRun) {
    plan.create.forEach(function (b) {
      try {
        var ev = $.EKEvent.eventWithEventStore(store);
        ev.setCalendar(cal.obj);
        saveBlock(store, ev, b);
      } catch (e) { errors++; log(tag + 'create failed: ' + e.message); }
    });
    plan.update.forEach(function (u) {
      try {
        if (markerOf(u.ref.url, u.ref.notes) !== u.block.mirror_id) throw new Error('marker changed; skipped');
        saveBlock(store, u.ref.obj, u.block);
      } catch (e) { errors++; log(tag + 'update failed: ' + e.message); }
    });
    plan.remove.forEach(function (r) {
      try {
        var p = parseMirror(markerOf(r.url, r.notes));
        if (!p || p.install !== resp.install_id) throw new Error('not ours; refused');
        var err = $();
        if (!store.removeEventSpanCommitError(r.obj, EK_SPAN_THIS_EVENT, true, err)) throw new Error(errText(err));
      } catch (e) { errors++; log(tag + 'delete failed: ' + e.message); }
    });
  }
  log(tag + 'create=' + plan.create.length + ' update=' + plan.update.length + ' delete=' + plan.remove.length +
      ' unchanged=' + plan.keep + ' errors=' + errors);
  if (errors) throw new Error(errors + ' write(s) failed on ' + name);
  return '';
}

// ------------------------------------------------------------------ self-test

function selfTest() {
  var fails = [], n = 0;
  function eq(a, b, msg) { n++; if (JSON.stringify(a) !== JSON.stringify(b)) fails.push(msg + ': ' + JSON.stringify(a) + ' != ' + JSON.stringify(b)); }
  var INST = 'aaaaaaaaaaaa', OTHER = 'bbbbbbbbbbbb';
  function mid(inst, c) { return inst + '.personal.' + new Array(25).join(c); }
  var b1 = {mirror_id: mid(INST, '1'), title: 'Busy', start: '2026-10-21T15:00:00Z', end: '2026-10-21T16:00:00Z', all_day: false, hash: 'h1'};
  var b2 = {mirror_id: mid(INST, '2'), title: 'Busy', start: '2026-10-22T15:00:00Z', end: '2026-10-22T16:00:00Z', all_day: false, hash: 'h2'};
  var t = function (s) { return Date.parse(s); };
  var existing = [
    {ref: 'unmarked', marker: null, hash: '', start: t('2026-10-21T15:00:00Z'), end: t('2026-10-21T16:00:00Z'), allDay: false},
    {ref: 'b1', marker: b1.mirror_id, hash: 'h1', start: t(b1.start), end: t(b1.end), allDay: false},
    {ref: 'b2old', marker: b2.mirror_id, hash: 'old', start: t(b2.start), end: t(b2.end), allDay: false},
    {ref: 'gone', marker: mid(INST, '3'), hash: 'h3', start: t('2026-10-23T15:00:00Z'), end: t('2026-10-23T16:00:00Z'), allDay: false},
    {ref: 'aged', marker: mid(INST, '4'), hash: 'h4', start: t('2026-10-01T15:00:00Z'), end: t('2026-10-01T16:00:00Z'), allDay: false},
    {ref: 'foreign', marker: mid(OTHER, '5'), hash: 'h5', start: t('2026-10-23T15:00:00Z'), end: t('2026-10-23T16:00:00Z'), allDay: false},
  ];
  var plan = planApply([b1, b2], existing, INST, t('2026-10-19T12:00:00Z'));
  eq(plan.create.length, 0, 'nothing to create');
  eq(plan.update.map(function (u) { return u.ref; }), ['b2old'], 'hash change updates');
  eq(plan.remove, ['gone'], 'only our in-window orphan is removed');
  eq(plan.keep, 1, 'unchanged kept');
  eq(planApply([b1, b2], existing, INST, t('2026-10-19T12:00:00Z'), ['personal']).remove, [],
     'blocks of a source the hub could not read are kept');
  var plan2 = planApply([b1], [], INST, 0);
  eq(plan2.create.length, 1, 'missing block is created');
  var moved = existing.slice(1, 2).map(function (e) { return {ref: 'b1', marker: e.marker, hash: 'h1', start: e.start + 3600000, end: e.end + 3600000, allDay: false}; });
  eq(planApply([b1], moved, INST, 0).update.length, 1, 'hand-moved block is put back');
  eq(planApply([b1], [{ref: 'x', marker: b1.mirror_id}, {ref: 'y', marker: b1.mirror_id}].map(function (e) {
    return {ref: e.ref, marker: e.marker, hash: 'h1', start: t(b1.start), end: t(b1.end), allDay: false}; }), INST, 0).remove,
    ['y'], 'duplicate mirrors are removed');
  eq(markerOf('calsync://' + b1.mirror_id, ''), b1.mirror_id, 'marker from URL');
  eq(markerOf('', 'x\n' + MARK_LINE + b2.mirror_id + '\n'), b2.mirror_id, 'marker from notes');
  eq(markerOf('https://teams.microsoft.com/x', 'calsync-mirror: nonsense'), null, 'no marker');
  eq(hashOf(blockNotes(b2)), 'h2', 'hash round trip');
  var rec = toPushRecord({externalId: 'EXT', title: 'Standup', start: t('2026-10-21T15:00:00Z'), end: t('2026-10-21T15:30:00Z'),
    allDay: false, occurrence: t('2026-10-21T15:00:00Z'), location: '', notes: '', url: '', availability: 2, status: 1, selfStatus: 3});
  eq([rec.id, rec.availability, rec.status, rec.self_status, rec.marker], ['EXT@2026-10-21T15:00:00Z', 'tentative', 'confirmed', 'declined', null], 'push record');
  var day = toPushRecord({externalId: 'D', title: 'Off', start: localMidnightMs('2026-10-21'), end: localMidnightMs('2026-10-22') - 1000,
    allDay: true, occurrence: null, location: '', notes: '', url: '', availability: 1, status: 0, selfStatus: null});
  eq([day.start, day.end, day.availability], ['2026-10-21', '2026-10-22', 'free'], 'all-day record');
  eq(blockSpan({all_day: true, start: '2026-10-21', end: '2026-10-22'}).end, localMidnightMs('2026-10-22') - 1000, 'all-day span');
  if (fails.length) throw new Error('self-test failed:\n' + fails.join('\n'));
  return 'self-test OK (' + n + ' checks)';
}

// ------------------------------------------------------------------ entry point

function run(argv) {
  var args;
  try { args = parseArgs(argv); } catch (e) { log(e.message); log(USAGE); $.exit(2); }
  try {
    switch (args.mode) {
      case 'selftest': return selfTest();
      case 'list': return listCalendars();
      case 'names': return Object.keys(loadConfig(args.config).calendars).join('\n');
      case 'export':
        if (!args.out) throw new Error('--export needs an output file');
        return exportEvents(loadConfig(args.config), args.out);
      case 'apply':
        if (!args.blocks || !args.calendar) throw new Error('--apply needs BLOCKS.json and --calendar NAME');
        var cfg = loadConfig(args.config);
        if (!cfg.calendars[args.calendar]) throw new Error('calendar ' + args.calendar + ' not in config');
        return applyBlocks(cfg, args.blocks, args.calendar, args.dryRun);
      default: return USAGE;
    }
  } catch (e) {
    log('calsync-agent: ' + (e.message || e));
    $.exit(1);
  }
}
