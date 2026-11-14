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
    detail: document.getElementById('funnel-detail'),
    detailSummary: document.getElementById('funnel-detail-summary'),
    detailBody: document.getElementById('funnel-detail-body'),
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
  // Das Detail EINES Spreads: nur Symbol und Tag. Welche Strikes und welcher
  // Verfall gelten, entscheidet die Route mit denselben Stufen wie die Liste -
  // der Browser schickt keine Strike-Wahl, die zur Zeile im Widerspruch stehen
  // koennte.
  var DETAIL_API = '/api/v1/gap-spreads/detail';

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
  var detailGeneration = 0;  // dasselbe fuer das Detail eines Spreads

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

  // Ein Vorzeichen ist eine Aussage, kein Schmuck: "+185,00" und "-145,00"
  // stehen als Text da, damit die Richtung nicht an einer Farbe haengt.
  function signedDe(value, digits) {
    if (!num(value)) return '–';
    return (value > 0 ? '+' : '') + de(value, digits);
  }

  function pctText(value) {
    if (!num(value)) return '–';
    return (value > 0 ? '+' : '') + de(value, 0) + ' %';
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
    clearDetail();
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

    // Die ZEILE ist der Griff zum Detail - kein Knopf, kein Chevron. Sie ist
    // per Tab erreichbar (tabindex), nennt ihr Ziel (aria-controls) und ihren
    // Zustand (aria-expanded, vom Setzen der aktiven Zeile gepflegt).
    row.className = 'row-detail';
    row.setAttribute('tabindex', '0');
    row.setAttribute('aria-expanded', 'false');
    row.setAttribute('aria-controls', 'funnel-detail');
    row.addEventListener('click', function () {
      loadDetail(entry.symbol, row);
    });
    row.addEventListener('keydown', function (ev) {
      // Enter und Leertaste oeffnen wie ein Klick. Space muss gebremst werden,
      // sonst scrollt die Seite, statt das Detail zu laden.
      if (ev.key !== 'Enter' && ev.key !== ' ' && ev.key !== 'Spacebar') return;
      ev.preventDefault();
      loadDetail(entry.symbol, row);
    });

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

  // --- Detail eines Spreads ------------------------------------------------ //

  function setActiveRow(row) {
    var rows = el.tbody.childNodes || [];
    for (var i = 0; i < rows.length; i += 1) {
      var aktiv = rows[i] === row;
      rows[i].setAttribute('aria-expanded', aktiv ? 'true' : 'false');
      if (aktiv) rows[i].classList.add('row-active');
      else rows[i].classList.remove('row-active');
    }
  }

  function clearDetail() {
    detailGeneration += 1;             // eine laufende Anfrage darf nicht mehr zeichnen
    letztesDetail = null;
    if (!el.detail) return;
    el.detail.hidden = true;
    el.detail.open = false;
    if (el.detailSummary) el.detailSummary.textContent = 'Detail zum Spread';
    if (el.detailBody) el.detailBody.textContent = '';
  }

  function openDetail(summary) {
    if (!el.detail) return;
    el.detail.hidden = false;
    el.detail.open = true;
    if (el.detailSummary) el.detailSummary.textContent = summary;
    // In den Blick scrollen, nicht springen: der Block steht zwischen Tabelle
    // und Ausschluessen, also direkt unter der geklickten Zeile.
    if (typeof el.detail.scrollIntoView === 'function') {
      el.detail.scrollIntoView({ block: 'nearest' });
    }
  }

  function messageForDetail(antwort) {
    var body = antwort.body;
    var code = body && body.error;
    if (code === 'unknown_symbol') {
      return 'Zu diesem Titel gibt es an diesem Tag keine Zeile.';
    }
    if (code === 'leg_not_listed') {
      return 'Ein Leg dieses Spreads liegt nicht in der Kette.';
    }
    if (code === 'chain_unavailable') {
      return 'Die Optionskette dieses Titels ist nicht lesbar.';
    }
    if (code === 'no_next_earnings') {
      return 'Für diesen Titel ist kein nächster Berichtstermin bekannt.';
    }
    if (code === 'no_expiry_after_earnings') {
      return 'Kein gelisteter Verfall liegt nach dem nächsten Bericht.';
    }
    if (code === 'no_candidate' || code === 'no_spread_ge_rr' || code === 'no_gap') {
      return body && body.message ? body.message
        : 'Dieser Titel ist an diesem Tag kein Kandidat des Trichters.';
    }
    return messageFor(antwort.status, body);
  }

  function loadDetail(symbol, row) {
    if (!current || !symbol) return;
    var day = current.day;
    var gen = detailGeneration + 1;
    detailGeneration = gen;
    setActiveRow(row);
    openDetail('Detail zu ' + symbol + ' — lädt …');
    el.detailBody.textContent = '';

    fetch(DETAIL_API + '?symbol=' + encodeURIComponent(symbol)
        + '&day=' + encodeURIComponent(day),
      { headers: { Accept: 'application/json' }, cache: 'no-store' })
      .then(function (res) {
        return res.text().then(function (text) {
          var body = null;
          try { body = JSON.parse(text); } catch (err) { body = null; }
          return { ok: res.ok, status: res.status, body: body };
        });
      })
      .then(function (antwort) {
        if (gen !== detailGeneration) return;
        if (!antwort.ok) {
          renderDetailError(symbol, messageForDetail(antwort));
          return;
        }
        if (!antwort.body || typeof antwort.body !== 'object') {
          renderDetailError(symbol, 'Die Antwort war kein JSON.');
          return;
        }
        renderDetail(antwort.body);
      })
      .catch(function (err) {
        if (gen !== detailGeneration) return;
        renderDetailError(symbol, 'Detail für ' + symbol + ' fehlgeschlagen: ' + err.message);
      });
  }

  function renderDetailError(symbol, text) {
    el.detailBody.textContent = '';
    var fehler = document.createElement('p');
    fehler.className = 'err';
    fehler.textContent = text;
    el.detailBody.appendChild(fehler);
    // Der Block bleibt offen und die Zeile aktiv: ein zweiter Klick ist der
    // zweite Versuch, kein neuer Zustand.
    openDetail('Detail zu ' + symbol);
  }

  function subtitle(text) {
    var p = document.createElement('p');
    p.className = 'detail-sub';
    p.textContent = text;
    return p;
  }

  function table(headers, rows) {
    var tabelle = document.createElement('table');
    tabelle.className = 'detail-table';
    var thead = document.createElement('thead');
    var kopf = document.createElement('tr');
    headers.forEach(function (text) {
      var th = document.createElement('th');
      th.textContent = text;
      kopf.appendChild(th);
    });
    thead.appendChild(kopf);
    tabelle.appendChild(thead);
    var tbody = document.createElement('tbody');
    rows.forEach(function (zellen) {
      var tr = document.createElement('tr');
      zellen.forEach(function (value) {
        var td = document.createElement('td');
        td.textContent = String(value);
        tr.appendChild(td);
      });
      tbody.appendChild(tr);
    });
    tabelle.appendChild(tbody);
    return tabelle;
  }

  function legTable(legs) {
    var rows = (Array.isArray(legs) ? legs : []).map(function (leg) {
      return [
        leg.side === 'short' ? 'Short' : 'Long',
        de(leg.strike, 2),
        de(leg.bid, 2),
        de(leg.ask, 2),
        de(leg.mid, 2),
        num(leg.iv) ? de(leg.iv, 2) + ' %' : '–',
        de(leg.open_interest, 0),
        de(leg.delta, 2)
      ];
    });
    return table(['Seite', 'Strike', 'Bid', 'Ask', 'Mid', 'IV', 'OI', 'Delta'], rows);
  }

  function scenarioTable(scenarios) {
    var rows = (Array.isArray(scenarios) ? scenarios : []).map(function (s) {
      return [
        pctText(s.change_pct),
        de(s.underlying, 2),
        signedDe(s.pl_expiry, 2),
        signedDe(s.delta, 2),
        signedDe(s.gamma, 2),
        signedDe(s.vega, 2),
        signedDe(s.theta, 2)
      ];
    });
    return table(['Prozent', 'Basiswert', 'G&V', 'Delta', 'Gamma', 'Vega', 'Theta'], rows);
  }

  var SVG_NS = 'http://www.w3.org/2000/svg';

  function svgNode(name, attrs) {
    var node = document.createElementNS(SVG_NS, name);
    if (attrs) {
      Object.keys(attrs).forEach(function (key) {
        node.setAttribute(key, String(attrs[key]));
      });
    }
    return node;
  }

  // Rueckfallbreite, wenn der Block noch keine messbare Breite hat (etwa weil
  // er versteckt ist). Sonst gilt, was der Kasten wirklich breit ist.
  var CHART_W = 960;

  // Das zuletzt gezeichnete Detail: ein breiteres Fenster zeichnet damit neu.
  var letztesDetail = null;

  // Gezeichnet wird in Pixeln der Breite, die der Block JETZT hat: so bleibt
  // die Schrift 11 px gross statt bei schmalem Fenster auf 6 px zu schrumpfen,
  // und das Bild fuellt den Kasten trotzdem ganz, weil viewBox und Kasten
  // dieselbe Breite haben (width 100 %).
  function chartBreite() {
    var kasten = el.detailBody;
    var breite = kasten && num(kasten.clientWidth) ? kasten.clientWidth : CHART_W;
    return Math.max(420, Math.min(1600, Math.round(breite)));
  }

  // Kurstticks: 50 %, 70 %, 90 %, 110 %, 130 %, 150 %, 170 % des Spot, auf
  // Cent gerundet und auf die gezeichnete Spanne beschnitten. Liegen weniger
  // als fuenf in der Spanne (Serie enger als der Bereich), wird die Achse
  // gleichmaessig geteilt - eine Achse ohne Beschriftung ist keine Achse.
  function kursTicks(spot, xmin, xmax) {
    var anteile = [0.5, 0.7, 0.9, 1.1, 1.3, 1.5, 1.7];
    var ticks = [];
    anteile.forEach(function (anteil) {
      var preis = Math.round(spot * anteil * 100) / 100;
      if (preis < xmin - 1e-9 || preis > xmax + 1e-9) return;
      if (ticks.indexOf(preis) >= 0) return;
      ticks.push(preis);
    });
    if (ticks.length >= 5) return ticks;
    ticks = [];
    for (var i = 0; i <= 5; i += 1) {
      ticks.push(Math.round((xmin + (xmax - xmin) * i / 5) * 100) / 100);
    }
    return ticks;
  }

  // Wie viele Nachkommastellen eine Achsenstufe braucht: eine Stufe von 2,5
  // ist als "3" falsch beschriftet.
  function stellenVon(ticks) {
    if (ticks.length < 2) return 0;
    var abstand = Math.abs(ticks[1] - ticks[0]);
    return Math.abs(abstand - Math.round(abstand)) > 1e-9 ? 1 : 0;
  }

  // G&V-Ticks: runde Stufen (1, 2, 2,5, 5, 10 x Zehnerpotenz), damit die
  // Beschriftung "0" und "100" heisst und nicht "-7" und "+56". Die Null ist
  // immer dabei, weil die Nulllinie die eine wichtige Linie im Bild ist.
  function gvTicks(ymin, ymax) {
    var spanne = ymax - ymin;
    var roh = spanne / 4;
    var stufe = Math.pow(10, Math.floor(Math.log(roh) / Math.LN10));
    var kandidaten = [1, 2, 2.5, 5, 10];
    var schritt = stufe * 10;
    for (var i = 0; i < kandidaten.length; i += 1) {
      if (stufe * kandidaten[i] >= roh) { schritt = stufe * kandidaten[i]; break; }
    }
    function sammle(weite) {
      var aus = [];
      var start = Math.ceil(ymin / weite - 1e-9) * weite;
      for (var wert = start; wert <= ymax + 1e-9 && aus.length < 9; wert += weite) {
        aus.push(Math.round(wert * 1000) / 1000);
      }
      return aus;
    }
    var ticks = sammle(schritt);
    // Zu grob gestuft (nur drei Linien): eine Stufe feiner, aber nicht feiner
    // als noetig - eine Achse mit fuenf Zahlen liest sich, eine mit zwanzig nicht.
    while (ticks.length < 4 && schritt > spanne / 12) {
      schritt = schritt / 2;
      ticks = sammle(schritt);
    }
    return ticks;
  }

  // Zwei Kurven in EINEM Bild: heute (durchgezogen, aus dem Modell) und bei
  // Verfall (gestrichelt, exakt). Nulllinie, Marker am Spot, beschriftete
  // Achsen in Kurs und G&V. Gezeichnet aus der Serie der Antwort, nicht aus
  // einer zweiten Rechnung im Browser.
  function chart(detail) {
    var serie = Array.isArray(detail.series) ? detail.series : [];
    var breite = chartBreite();
    var hoehe = Math.max(280, Math.min(560, Math.round(breite * 0.375)));
    var box = svgNode('svg', {
      viewBox: '0 0 ' + breite + ' ' + hoehe,
      preserveAspectRatio: 'xMidYMid meet',
      width: '100%',
      class: 'detail-chart-svg',
      role: 'img',
      'aria-label': 'G&V von ' + (detail.symbol || '') + ': durchgezogen heute,'
        + ' gestrichelt bei Verfall'
    });
    // Titel und Beschreibung sind der Text, den ein Screenreader statt der
    // Linien liest; aria-label allein nennt nur die Kurven, nicht die Achsen.
    var titel = svgNode('title');
    titel.textContent = 'G&V je Spread über den Kurs — heute und bei Verfall';
    box.appendChild(titel);
    var beschreibung = svgNode('desc');
    beschreibung.textContent = 'Durchgezogene Linie: G&V heute nach dem Modell. '
      + 'Gestrichelte Linie: G&V bei Verfall. Senkrechte Linie: Kurs '
      + de(detail.spot, 2) + '. Waagerechte Achse: Kurs. Senkrechte Achse: '
      + 'G&V in US-Dollar je Spread.';
    box.appendChild(beschreibung);
    if (!serie.length || !num(detail.spot)) return box;

    var padL = 84, padR = 22, padT = 18, padB = 56;
    var preise = serie.map(function (p) { return p.price; });
    var xmin = Math.min.apply(null, preise);
    var xmax = Math.max.apply(null, preise);
    var werte = [0];
    serie.forEach(function (p) {
      if (num(p.pl_expiry)) werte.push(p.pl_expiry);
      if (num(p.pl_today)) werte.push(p.pl_today);
    });
    var ymin = Math.min.apply(null, werte);
    var ymax = Math.max.apply(null, werte);
    if (ymax === ymin) ymax = ymin + 1;
    // Ein Polster von 6 %: sonst kleben Minimum und Maximum der Kurve am
    // Rahmen und die Linie sieht aus wie die Kante des Bildes.
    var polster = (ymax - ymin) * 0.06;
    var ylo = ymin - polster;
    var yhi = ymax + polster;

    var plotX = padL, plotY = padT;
    var plotW = breite - padL - padR;
    var plotH = hoehe - padT - padB;

    function x(price) { return plotX + (price - xmin) / (xmax - xmin) * plotW; }
    function y(pl) { return plotY + (yhi - pl) / (yhi - ylo) * plotH; }

    function pfad(key) {
      var teile = [];
      serie.forEach(function (p) {
        if (!num(p[key])) return;
        teile.push((teile.length ? 'L' : 'M') + x(p.price).toFixed(1) + ' ' + y(p[key]).toFixed(1));
      });
      return teile.join(' ');
    }

    // Rahmen und Nulllinie: ohne sie ist eine Kurve nur eine Linie.
    box.appendChild(svgNode('rect', {
      x: plotX, y: plotY, width: plotW, height: plotH,
      fill: 'none', stroke: 'rgba(250,250,250,.10)'
    }));

    // Werte auf der G&V-Achse: runde Stufen, Null immer dabei. Die Zahl steht
    // links der Achse in ihrem eigenen Rand - so ueberlappt keine Beschriftung
    // den Rahmen.
    var gvWerte = gvTicks(ylo, yhi);
    var gvStellen = stellenVon(gvWerte);
    gvWerte.forEach(function (wert) {
      var yy = y(wert);
      box.appendChild(svgNode('line', {
        x1: plotX, y1: yy, x2: plotX + plotW, y2: yy,
        stroke: 'rgba(250,250,250,.06)', 'stroke-width': 1
      }));
      box.appendChild(svgNode('line', {
        x1: plotX - 5, y1: yy, x2: plotX, y2: yy,
        stroke: 'rgba(250,250,250,.30)', 'stroke-width': 1
      }));
      var yLabel = svgNode('text', {
        x: plotX - 10, y: yy + 3.5, fill: 'rgba(250,250,250,.62)',
        'font-size': 11, 'font-family': 'monospace', 'text-anchor': 'end',
        class: 'chart-y-tick'
      });
      yLabel.textContent = signedDe(wert, gvStellen);
      box.appendChild(yLabel);
    });

    box.appendChild(svgNode('line', {
      x1: plotX, y1: y(0), x2: plotX + plotW, y2: y(0),
      stroke: 'rgba(250,250,250,.36)', 'stroke-width': 1
    }));

    box.appendChild(svgNode('path', {
      d: pfad('pl_today'), fill: 'none', stroke: '#fafafa', 'stroke-width': 1.6
    }));
    box.appendChild(svgNode('path', {
      d: pfad('pl_expiry'), fill: 'none', stroke: '#e05534', 'stroke-width': 1.6,
      'stroke-dasharray': '5 4'
    }));

    if (detail.spot >= xmin && detail.spot <= xmax) {
      box.appendChild(svgNode('line', {
        x1: x(detail.spot), y1: plotY, x2: x(detail.spot), y2: plotY + plotH,
        stroke: 'rgba(250,250,250,.46)', 'stroke-dasharray': '2 4'
      }));
      var naheRand = x(detail.spot) > plotX + plotW - 78;
      var spotText = svgNode('text', {
        // unten am Rand, nicht oben: dort liegen die Plateaus der Kurven
        x: x(detail.spot) + (naheRand ? -5 : 5), y: plotY + plotH - 8,
        fill: 'rgba(250,250,250,.80)', 'font-size': 11, 'font-family': 'monospace',
        'text-anchor': naheRand ? 'end' : 'start', class: 'chart-spot',
        // dunkler Rand um die Schrift: die Linie dahinter kreuzt die Zahl
        // nicht, ohne dass ein Kasten die Kurve verdeckt
        stroke: '#091217', 'stroke-width': 3, 'stroke-linejoin': 'round',
        'paint-order': 'stroke'
      });
      spotText.textContent = 'Spot ' + de(detail.spot, 2);
      box.appendChild(spotText);
    }

    // Preisachse: 50 % bis 170 % des Spot als Ticks, damit auch die Mitte
    // einen Wert traegt und nicht nur der linke Rand. Beschriftet mit dem
    // Kurs, nicht mit dem Prozentwert - die Kurve laeuft ueber den Kurs.
    kursTicks(detail.spot, xmin, xmax).forEach(function (preis) {
      var xx = x(preis);
      box.appendChild(svgNode('line', {
        x1: xx, y1: plotY + plotH, x2: xx, y2: plotY + plotH + 5,
        stroke: 'rgba(250,250,250,.30)', 'stroke-width': 1
      }));
      var xLabel = svgNode('text', {
        x: xx, y: plotY + plotH + 18, fill: 'rgba(250,250,250,.62)',
        'font-size': 11, 'font-family': 'monospace', 'text-anchor': 'middle',
        class: 'chart-x-tick'
      });
      xLabel.textContent = de(preis, 2);
      box.appendChild(xLabel);
    });

    // Achsentitel: die eine Zeile sagt, was die Zahlen bedeuten.
    var kursTitel = svgNode('text', {
      x: plotX + plotW / 2, y: hoehe - 8, fill: 'rgba(250,250,250,.48)',
      'font-size': 11, 'font-family': 'monospace', 'text-anchor': 'middle',
      class: 'chart-axis-title'
    });
    kursTitel.textContent = 'Kurs';
    box.appendChild(kursTitel);

    var gvMitte = plotY + plotH / 2;
    var gvTitel = svgNode('text', {
      x: 14, y: gvMitte, fill: 'rgba(250,250,250,.48)', 'font-size': 11,
      'font-family': 'monospace', 'text-anchor': 'middle',
      transform: 'rotate(-90 14 ' + gvMitte + ')', class: 'chart-axis-title'
    });
    gvTitel.textContent = 'G&V (USD je Spread)';
    box.appendChild(gvTitel);

    return box;
  }

  // Die Legende steht unter dem Bild in Textform: sie nennt beide Linien in
  // Worten, und der Strich daneben ist das zweite Signal - nie allein die
  // Farbe, nie allein die Strichart.
  function chartLegend() {
    var legende = document.createElement('div');
    legende.className = 'detail-legend';
    [['solid', 'heute (Modell)'], ['dashed', 'bei Verfall']].forEach(function (paar) {
      var zeile = document.createElement('span');
      zeile.className = 'detail-legend-item';
      var strich = document.createElement('i');
      strich.className = paar[0];
      zeile.appendChild(strich);
      var wort = document.createElement('span');
      wort.textContent = paar[1];
      zeile.appendChild(wort);
      legende.appendChild(zeile);
    });
    return legende;
  }

  function renderDetail(detail) {
    el.detailBody.textContent = '';

    var kopf = document.createElement('p');
    kopf.className = 'detail-head';
    kopf.textContent = (detail.symbol || '–')
      + (detail.name ? ' · ' + detail.name : '')
      + ' · ' + (detail.strategy_label || STRATEGY[detail.strategy] || '–')
      + ' · Verfall ' + (detail.expiration || '–')
      + ' · Debit ' + de(detail.net_debit, 2)
      + ' · max. Verlust ' + de(detail.max_loss, 2)
      + ' · Break-even ' + de(detail.breakeven, 2)
      + ' · max. Gewinn ' + de(detail.max_profit, 2)
      + ' · R/R 1 zu ' + de(detail.rr, 2);
    el.detailBody.appendChild(kopf);

    el.detailBody.appendChild(subtitle('Beide Legs'));
    el.detailBody.appendChild(legTable(detail.legs));

    el.detailBody.appendChild(subtitle('G&V über den Kurs — heute und bei Verfall'));
    var chartBox = document.createElement('div');
    chartBox.className = 'detail-chart';
    chartBox.appendChild(chart(detail));
    chartBox.appendChild(chartLegend());
    el.detailBody.appendChild(chartBox);
    letztesDetail = detail;

    el.detailBody.appendChild(subtitle('Szenarien'));
    el.detailBody.appendChild(scenarioTable(detail.scenarios));

    var notes = Array.isArray(detail.notes) ? detail.notes.filter(Boolean) : [];
    if (notes.length) {
      var hinweis = document.createElement('p');
      hinweis.className = 'detail-notes';
      hinweis.textContent = notes.join(' ');
      el.detailBody.appendChild(hinweis);
    }

    openDetail('Detail zu ' + (detail.symbol || '–') + ' · '
      + (detail.strategy_label || STRATEGY[detail.strategy] || '–')
      + ' · Verfall ' + (detail.expiration || '–'));
  }

  function render() {
    var body = current.body;
    var ergebnisse = Array.isArray(body.results) ? body.results : [];
    var ausschluesse = Array.isArray(body.excluded) ? body.excluded : [];
    var fehlschlaege = Array.isArray(body.failures) ? body.failures : [];
    var counts = body.counts || {};
    var notes = Array.isArray(body.notes) ? body.notes.filter(Boolean) : [];

    // Ein neuer Lauf baut neue Zeilen: das Detail der alten Zeile ist damit
    // veraltet und geht zu - ein Block, der zu einer Zeile zeigt, die es nicht
    // mehr gibt, waere eine falsche Auskunft.
    clearDetail();
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

  // Ein breiteres Fenster zeichnet das Bild neu: gezeichnet wird in Pixeln der
  // aktuellen Breite, damit die Schrift 11 px bleibt. Ohne das schrumpft die
  // Beschriftung mit, sobald der Kasten schmaler wird.
  var resizeTimer = null;
  if (typeof window !== 'undefined' && window.addEventListener) {
    window.addEventListener('resize', function () {
      if (!letztesDetail) return;
      if (resizeTimer !== null) clearTimeout(resizeTimer);
      resizeTimer = setTimeout(function () {
        resizeTimer = null;
        var kasten = el.detailBody && el.detailBody.querySelector
          ? el.detailBody.querySelector('.detail-chart') : null;
        if (!kasten || !kasten.firstChild) return;
        kasten.replaceChild(chart(letztesDetail), kasten.firstChild);
      }, 150);
    });
  }

  el.run.addEventListener('click', function () { run(el.day.value.trim()); });
  el.day.addEventListener('keydown', function (ev) {
    if (ev.key !== 'Enter') return;
    ev.preventDefault();
    run(el.day.value.trim());
  });
})();
