/* Gap-Spread-Trichter - EINE Tabelle auf GET /.

   Warum eine eigene Datei: der Block laedt eine Route, haelt vier
   Fehlerzustaende auseinander und baut vierzehn Spalten - das ist Verhalten,
   kein Text. Kein Framework, kein Build-Schritt, kein CDN: eine Datei, die der
   Browser so nimmt.

   Drei Dinge tut dieser Block bewusst NICHT:
     * er startet keinen Lauf beim Laden der Seite - der Trichter holt je Titel
       eine Optionskette, also bleibt er ein ausdruecklicher Klick;
     * er waehlt keine Strategie und keinen Verfall - beides kommt aus der
       Antwort der Route (``strategy``, ``strategy_label``, ``expiration``);
     * er faerbt die Richtung. Der Nutzer ist rot-gruen-schwach: die Richtung
       steht als Pfeil UND als Wort, nie als Farbpaar.

   Jede Zelle entsteht mit createElement und textContent - die Antwort ist
   fremder Text und wird nie als Markup eingesetzt.
*/
(function () {
  var block = document.getElementById('gap-funnel');
  if (!block) return;

  var el = {
    day: document.getElementById('funnel-day'),
    run: document.getElementById('funnel-run'),
    state: document.getElementById('funnel-state'),
    error: document.getElementById('funnel-error'),
    counts: document.getElementById('funnel-counts'),
    notes: document.getElementById('funnel-notes'),
    stale: document.getElementById('funnel-stale'),
    empty: document.getElementById('funnel-empty'),
    wrap: document.getElementById('funnel-table-wrap'),
    tbody: document.getElementById('funnel-tbody'),
    excludedBlock: document.getElementById('funnel-excluded-block'),
    excludedSummary: document.getElementById('funnel-excluded-summary'),
    excluded: document.getElementById('funnel-excluded'),
    failuresBlock: document.getElementById('funnel-failures-block'),
    failuresSummary: document.getElementById('funnel-failures-summary'),
    failures: document.getElementById('funnel-failures')
  };
  if (!el.day || !el.run || !el.tbody) return;

  // Die eine Route dieses Abschnitts. Schwelle, Umsatzhuerde, R/R-Grenze und
  // Mindest-OI bleiben beim Server: ein Feld dafuer waere eine zweite Wahrheit
  // neben seinen Standardwerten.
  var API = '/api/v1/gap-spreads';

  // Die Strategie waehlt die Route. Hier steht nur, wie sie heisst, falls die
  // Antwort keinen Klartext mitliefert.
  var STRATEGY = {
    bull_call_debit: 'Bull Call',
    bear_put_debit: 'Bear Put'
  };
  // Grund eines Ausschlusses in Klartext. Der Code selbst bleibt im Tooltip -
  // ein Leser soll den Grund lesen, nicht einen Bezeichner entziffern.
  var REASONS = {
    no_next_earnings: 'kein nächster Bericht bekannt',
    no_expiry_after_earnings: 'kein Verfall nach dem nächsten Bericht',
    no_spread_ge_rr: 'kein Spread mit R/R mindestens 1 zu 2',
    not_optionable: 'keine gelisteten Optionen',
    chain_unavailable: 'Optionskette nicht lesbar'
  };
  var DAY_RE = /^\d{4}-\d{2}-\d{2}$/;
  var STALE = 'Kalenderstand veraltet — die Zeilen stammen aus einem alten '
    + 'Kalenderstand und können sich noch ändern.';

  var generation = 0;      // nur die neueste Anfrage darf zeichnen
  var timer = null;
  var current = null;      // {day, body, at}

  function n(value) { return typeof value === 'number' && isFinite(value) ? value : 0; }

  function num(value) { return typeof value === 'number' && isFinite(value); }

  // Deutsche Zahlen: Dezimalkomma, kein Punkt. Sonst liest der Leser "1.35" als
  // Tausender und "135.00" als Hundertfünfunddreißig.
  function de(value, digits) {
    if (!num(value)) return '–';
    var text = value.toFixed(digits === undefined ? 2 : digits);
    return text.replace('.', ',');
  }

  function gapText(value) {
    if (!num(value)) return '–';
    return (value > 0 ? '+' : '') + de(value, 1) + ' %';
  }

  function directionWord(direction) {
    if (direction === 'up') return { arrow: '↑', word: 'aufwärts' };
    if (direction === 'down') return { arrow: '↓', word: 'abwärts' };
    return { arrow: '–', word: 'unbekannt' };
  }

  // "1 zu 2,3" - das Verhaeltnis als Satz, weil "2,3 : 1" auf einem
  // Bildschirm mit schmaler Spalte als zwei Zahlen gelesen wird.
  function rrText(value) {
    return num(value) ? '1 zu ' + de(value, 1) : '–';
  }

  function liquidityWord(value) {
    return value === 'full' ? 'voll liquide' : 'dünn';
  }

  function two(value) { return (value < 10 ? '0' : '') + value; }

  function clock(date) {
    return two(date.getHours()) + ':' + two(date.getMinutes()) + ':' + two(date.getSeconds());
  }

  function isoDay(date) {
    return date.getFullYear() + '-' + two(date.getMonth() + 1) + '-' + two(date.getDate());
  }

  // "gestern" ist der letzte KALENDERTAG vor heute, nicht "letzter Handelstag":
  // den kennt nur der Kalender, und den fragt die Route.
  function yesterday() {
    var d = new Date();
    d.setDate(d.getDate() - 1);
    return isoDay(d);
  }

  function stopTimer() {
    if (timer !== null) { clearInterval(timer); timer = null; }
  }

  // Kein Balken, keine erfundene Prozentzahl: die Route antwortet einmal am
  // Ende, also zeigen wir die echten Sekunden seit dem Klick.
  function startTimer(day, startedAt, gen) {
    stopTimer();
    function paint() {
      var sekunden = Math.round((Date.now() - startedAt) / 1000);
      el.state.textContent = 'Lauf für ' + day + ' … ' + sekunden + ' s';
    }
    paint();
    timer = setInterval(function () {
      if (gen !== generation) { stopTimer(); return; }
      paint();
    }, 250);
  }

  function messageFor(status, body) {
    var code = body && body.error;
    if (code === 'invalid_day') {
      return 'Kein gültiges Datum — bitte einen Reaktionstag im Format JJJJ-MM-TT angeben.';
    }
    if (body && body.message) return body.message;
    return 'HTTP ' + status;
  }

  function plural(count, one, many) {
    return count + ' ' + (count === 1 ? one : many);
  }

  // Die Zaehlerzeile nennt jede Huerde des Trichters in der Reihenfolge, in der
  // sie greift: gemessen, Umsatz, Kandidaten, Termin, Verfall, R/R, angezeigt.
  function countsText(counts) {
    return [
      plural(n(counts.gaps_computed), 'Gap gemessen', 'Gaps gemessen'),
      n(counts.turnover_passed) + ' über Umsatzhürde',
      plural(n(counts.candidates), 'Kandidat', 'Kandidaten'),
      n(counts.with_next_earnings) + ' mit nächstem Termin',
      n(counts.with_expiry_after_earnings) + ' mit passendem Verfall',
      n(counts.with_spread_ge_rr) + ' mit R/R mindestens 1 zu 2',
      n(counts.shown) + ' angezeigt'
    ].join(', ');
  }

  // "Kein Kandidat" nennt IMMER die Zahl der Ausschluesse: ein Tag, an dem
  // zwanzig Titel ausschieden, ist kein ruhiger Tag ohne Befund.
  function emptyText(excluded, failures) {
    var teile = [plural(excluded.length, 'Ausschluss', 'Ausschlüsse')];
    if (failures.length) {
      teile.push(plural(failures.length, 'Fehlschlag', 'Fehlschläge'));
    }
    return 'Kein Kandidat — ' + teile.join(', ') + '.';
  }

  function clearResult() {
    current = null;
    el.tbody.innerHTML = '';
    el.wrap.hidden = true;
    el.counts.hidden = true;
    el.counts.textContent = '';
    el.empty.hidden = true;
    el.empty.textContent = '';
    el.notes.hidden = true;
    el.notes.textContent = '';
    el.stale.hidden = true;
    el.stale.textContent = '';
    hideList(el.excludedBlock, el.excluded);
    hideList(el.failuresBlock, el.failures);
  }

  function hideList(details, list) {
    if (details) details.hidden = true;
    if (details) details.open = false;
    if (list) list.innerHTML = '';
  }

  function fail(text) {
    clearResult();
    el.state.textContent = 'Lauf fehlgeschlagen';
    el.error.textContent = text;
    el.error.hidden = false;
  }

  function run(day) {
    if (!day || !DAY_RE.test(day)) {
      // Das Datumsfeld liefert "leer" statt Unsinn - dann wird nicht angefragt,
      // sondern benannt, was fehlt.
      clearResult();
      el.state.textContent = 'Kein Lauf gestartet';
      el.error.textContent = 'Kein gültiges Datum — bitte einen Reaktionstag im '
        + 'Format JJJJ-MM-TT angeben.';
      el.error.hidden = false;
      return;
    }
    var gen = generation + 1;
    generation = gen;
    var startedAt = Date.now();

    el.error.hidden = true;
    el.run.disabled = true;
    startTimer(day, startedAt, gen);

    fetch(API + '?day=' + encodeURIComponent(day),
      { headers: { Accept: 'application/json' }, cache: 'no-store' })
      .then(function (res) {
        return res.text().then(function (text) {
          var body = null;
          try { body = JSON.parse(text); } catch (err) { body = null; }
          return { ok: res.ok, status: res.status, body: body };
        });
      })
      .then(function (antwort) {
        if (gen !== generation) return;   // eine neuere Anfrage besitzt das Panel
        stopTimer();
        el.run.disabled = false;
        if (!antwort.ok) {
          fail(messageFor(antwort.status, antwort.body));
          return;
        }
        if (!antwort.body || typeof antwort.body !== 'object') {
          fail('Die Antwort war kein JSON.');
          return;
        }
        current = { day: day, body: antwort.body, at: new Date() };
        render();
      })
      .catch(function (err) {
        if (gen !== generation) return;
        stopTimer();
        el.run.disabled = false;
        fail('Lauf für ' + day + ' fehlgeschlagen: ' + err.message);
      });
  }

  function cell(text, cls, title) {
    var td = document.createElement('td');
    td.textContent = text;
    if (cls) td.className = cls;
    if (title) td.title = title;
    return td;
  }

  // Eine Zelle mit Unterzeile: der Hauptwert steht gross, die Einordnung
  // darunter (Name, Richtungswort, Legs). Kein Markup, nur Text.
  function stacked(main, sub, cls, title) {
    var td = document.createElement('td');
    if (cls) td.className = cls;
    if (title) td.title = title;
    td.textContent = main;
    if (sub) {
      var klein = document.createElement('span');
      klein.className = 'sub';
      klein.textContent = sub;
      td.appendChild(klein);
    }
    return td;
  }

  function optionsCell(entry) {
    var oi = Array.isArray(entry.open_interest) ? entry.open_interest : [];
    var ba = Array.isArray(entry.bid_ask_pct) ? entry.bid_ask_pct : [];
    var td = document.createElement('td');
    td.title = 'Open Interest beider Legs (Long / Short) · Bid-Ask-Aufschlag in '
      + 'Prozent des Mittelkurses, je Leg';
    td.textContent = 'OI ' + de(oi[0], 0) + ' / ' + de(oi[1], 0);
    var spreads = document.createElement('span');
    spreads.className = 'sub';
    spreads.textContent = 'Bid-Ask ' + de(ba[0], 1) + ' % / ' + de(ba[1], 1) + ' %';
    td.appendChild(spreads);
    var liquid = document.createElement('span');
    liquid.className = 'sub';
    liquid.textContent = liquidityWord(entry.liquidity);
    td.appendChild(liquid);
    return td;
  }

  function buildRow(entry, index) {
    var row = document.createElement('tr');
    row.setAttribute('data-symbol', entry.symbol || '');
    row.setAttribute('data-direction', entry.direction || '');

    row.appendChild(cell(String(index + 1), '', 'Rang nach Größe des Gaps'));

    row.appendChild(stacked(entry.symbol || '–', entry.name || ''));

    var richtung = directionWord(entry.direction);
    row.appendChild(stacked(richtung.arrow + ' ' + gapText(entry.gap_pct), richtung.word,
      'dir', 'Gap: Schluss des Reaktionstags gegen den Schluss davor'));

    var strategie = entry.strategy_label || STRATEGY[entry.strategy] || '–';
    row.appendChild(cell(strategie, '', entry.strategy || ''));

    row.appendChild(cell(de(entry.close, 2), '', 'Schlusskurs des Reaktionstags'));

    row.appendChild(stacked(de(entry.long_strike, 1) + ' / ' + de(entry.short_strike, 1),
      'Long / Short', '',
      'Long ' + de(entry.long_strike, 1) + ' · Short ' + de(entry.short_strike, 1)));

    row.appendChild(cell(de(entry.net_debit, 2), '', 'Debit je Einheit'));
    row.appendChild(cell(de(entry.max_loss, 2), '', 'maximaler Verlust je Einheit'));
    row.appendChild(cell(de(entry.max_profit, 2), '', 'maximaler Gewinn je Einheit'));
    row.appendChild(cell(rrText(entry.rr), '', 'Gewinn zu Verlust bei Verfall'));
    row.appendChild(cell(entry.expiration || '–', '',
      'Verfall nach dem nächsten Bericht'));
    row.appendChild(cell(num(entry.dte) ? String(entry.dte) : '–', '',
      'Kalendertage bis Verfall'));
    row.appendChild(cell(entry.next_earnings_date || '–', '',
      entry.earnings_date ? 'Bericht des Reaktionstags: ' + entry.earnings_date
        : 'nächster bekannter Bericht'));
    row.appendChild(optionsCell(entry));

    return row;
  }

  function reasonText(entry) {
    return REASONS[entry.reason] || entry.message || entry.reason || 'Grund unbekannt';
  }

  function fillList(details, summary, list, entries, label, textOf) {
    if (!details || !list) return;
    list.innerHTML = '';
    if (!Array.isArray(entries) || !entries.length) {
      details.hidden = true;
      details.open = false;
      return;
    }
    entries.forEach(function (entry) {
      var li = document.createElement('li');
      var symbol = document.createElement('b');
      symbol.textContent = entry.symbol || '–';
      li.appendChild(symbol);
      var span = document.createElement('span');
      span.textContent = textOf(entry);
      li.appendChild(span);
      li.title = [entry.reason || entry.code || '', entry.message || '']
        .filter(Boolean).join(' · ');
      list.appendChild(li);
    });
    if (summary) summary.textContent = label + ' (' + entries.length + ')';
    details.hidden = false;
  }

  function failureText(entry) {
    if (entry.code && entry.message) return entry.code + ' — ' + entry.message;
    return entry.message || entry.code || 'Grund unbekannt';
  }

  function render() {
    var body = current.body;
    var ergebnisse = Array.isArray(body.results) ? body.results : [];
    var ausschluesse = Array.isArray(body.excluded) ? body.excluded : [];
    var fehlschlaege = Array.isArray(body.failures) ? body.failures : [];
    var counts = body.counts || {};
    var notes = Array.isArray(body.notes) ? body.notes.filter(Boolean) : [];

    el.tbody.innerHTML = '';
    ergebnisse.forEach(function (entry, index) {
      el.tbody.appendChild(buildRow(entry, index));
    });

    el.counts.textContent = countsText(counts);
    el.counts.hidden = false;
    el.error.hidden = true;

    // Notizen sind eine ruhige Zeile, keine Warnung: sie beschreiben den Lauf,
    // nicht einen Fehler.
    el.notes.textContent = notes.join(' ');
    el.notes.hidden = !notes.length;

    // Ein alter Kalenderstand wird benannt, die Zeilen bleiben stehen: alte
    // Zeilen sind alt, nicht falsch.
    var veraltet = body.day_state === 'stale';
    el.stale.textContent = veraltet ? STALE : '';
    el.stale.hidden = !veraltet;

    if (!ergebnisse.length) {
      el.wrap.hidden = true;
      el.empty.textContent = emptyText(ausschluesse, fehlschlaege);
      el.empty.hidden = false;
    } else {
      el.empty.hidden = true;
      el.wrap.hidden = false;
    }

    fillList(el.excludedBlock, el.excludedSummary, el.excluded, ausschluesse,
      'Ausschlüsse', reasonText);
    fillList(el.failuresBlock, el.failuresSummary, el.failures, fehlschlaege,
      'Fehlschläge', failureText);

    el.state.textContent = 'Stand ' + clock(current.at) + ' · '
      + plural(ergebnisse.length, 'Zeile', 'Zeilen');
  }

  // Vorgabe ist gestern - der letzte abgeschlossene Kalendertag. Kein Lauf beim
  // Laden der Seite: gefiltert wird auf Klick.
  if (!el.day.value) el.day.value = yesterday();

  el.run.addEventListener('click', function () { run(el.day.value.trim()); });
  el.day.addEventListener('keydown', function (ev) {
    if (ev.key !== 'Enter') return;
    ev.preventDefault();
    run(el.day.value.trim());
  });
})();
