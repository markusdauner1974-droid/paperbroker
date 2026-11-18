/* Gap-Spread-Trichter - EINE Tabelle auf GET /.

   Warum eine eigene Datei: der Block laedt eine Route, haelt vier
   Fehlerzustaende auseinander und baut fünfzehn Spalten - das ist Verhalten,
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
    expandAll: document.getElementById('funnel-expand-all'),
    stale: document.getElementById('funnel-stale'),
    empty: document.getElementById('funnel-empty'),
    wrap: document.getElementById('funnel-table-wrap'),
    tbody: document.getElementById('funnel-tbody'),
    foot: document.getElementById('funnel-foot'),
    cap: document.getElementById('funnel-cap'),
    // Die Kopfzeile nennt dieselbe Huerde wie die Zaehlerzeile. Sie wird aus
    // ``limits`` gefuellt, nicht im Template festgeschrieben - sonst behauptet
    // die Seite eine Grenze, die der Lauf nicht benutzt hat.
    rr: document.getElementById('funnel-rr'),
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

  // Ein kuratiertes Datum: der EINE Schreibweg neben dem Scan. Der Server
  // prueft Format, Vergangenheit und die 270-Tage-Grenze.
  var HAND_API = '/api/v1/hand-earnings';

  // Die Texte kommen als EIN JSON-Block ins gerenderte HTML (id="i18n-js"):
  // eine Quelle, keine zweite Katalogdatei in JavaScript. Der Block nennt auch
  // die Mehrfach-Regel der Sprache - dieselbe, die der Server benutzt.
  var I18N = (function () {
    var node = document.getElementById('i18n-js');
    if (!node) return { texts: {}, plural: 'one_other' };
    try {
      return JSON.parse(node.textContent || '{}');
    } catch (err) {
      return { texts: {}, plural: 'one_other' };
    }
  }());

  // Die aufgeloeste Sprache kommt aus demselben Block. Jede Abfrage traegt sie
  // mit - sonst antwortet der Server in der Quellsprache und die Seite mischt
  // zwei Sprachen. Fehlt das Feld, bleibt die Abfrage wie sie war.
  var LANG = I18N && I18N.lang ? String(I18N.lang) : '';

  function inLanguage(url) {
    return LANG ? url + '&lang=' + encodeURIComponent(LANG) : url;
  }

  // Dieselben Regeln wie ``texts/registry.py``, benannt nach dem Feld, das der
  // Block mitliefert. Eine 3-6-Formen-Regel steht hier NICHT auf Vorrat.
  var PLURAL_RULES = {
    one_other: function (count) { return count === 1 ? 'one' : 'other'; }
  };

  // Text nach Schluessel, mit {platzhalter}-Einsetzung. Fehlt der Schluessel,
  // steht der Schluessel da: sichtbar falsch statt still leer.
  function T(key, vars) {
    var text = (I18N.texts || {})[key];
    if (text && typeof text === 'object') {
      var rule = PLURAL_RULES[I18N.plural] || PLURAL_RULES.one_other;
      var branch = rule(vars ? vars.n : undefined);
      text = text[branch] || text.other || text.one;
    }
    if (typeof text !== 'string') return key;
    if (!vars) return text;
    return text.replace(/\{(\w+)\}/g, function (match, name) {
      return Object.prototype.hasOwnProperty.call(vars, name) ? String(vars[name]) : match;
    });
  }

  // Die Strategie waehlt die Route. Hier steht nur, wie sie heisst, falls die
  // Antwort keinen Klartext mitliefert.
  var STRATEGY = {
    bull_call_debit: T('js.strategy.bull_call_debit'),
    bear_put_debit: T('js.strategy.bear_put_debit')
  };
  // Grund eines Ausschlusses in Klartext. Der Code selbst bleibt im Tooltip -
  // ein Leser soll den Grund lesen, nicht einen Bezeichner entziffern.
  //
  // Die R/R-Huerde steht NICHT hier: sie gehoert zur Antwort
  // (``limits.rr_min``, siehe ``rrHurdleText``). Eine Grenze, die die Datei
  // selbst kennt, wuerde bei jedem Lauf mit anderer Huerde luegen.
  var REASONS = {
    no_next_earnings: T('js.reason.no_next_earnings'),
    no_expiry_after_earnings: T('js.reason.no_expiry_after_earnings'),
    not_optionable: T('js.reason.not_optionable'),
    chain_unavailable: T('js.reason.chain_unavailable')
    // move_too_far fehlt hier ABSICHTLICH: der Satz der Route nennt die noetige
    // Bewegung UND die Grenze ("Der Kurs müsste sich um 61,3 % steigen;
    // zugelassen sind 20 %.") - ein Etikett waere weniger Auskunft, nicht mehr.
  };

  // Die R/R-Huerde im Klartext, aus der Antwort statt aus dieser Datei: die
  // Route nennt in ``limits``, welche Grenze sie angewandt hat. Fehlt der
  // Block (aeltere Antwort), bleibt die heutige Vorgabe stehen - ein alter
  // Server liefert dann den alten Satz, keine kaputte Seite.
  var RR_HURDLE_FALLBACK = T('js.rr_hurdle.fallback');

  function rrHurdleText(body) {
    var limits = body && body.limits;
    if (limits && num(limits.rr_min)) {
      return '1 : ' + fmt(limits.rr_min, 1).replace(/,0$/, '');
    }
    return RR_HURDLE_FALLBACK;
  }

  var DAY_RE = /^\d{4}-\d{2}-\d{2}$/;
  var STALE = T('js.stale');

  var generation = 0;      // nur die neueste Anfrage darf zeichnen
  var timer = null;
  var current = null;      // {day, body, at}
  var detailGeneration = 0;  // dasselbe fuer das Detail eines Spreads
  var groups = [];         // die aufklappbaren Gruppen der aktuellen Tabelle

  // Hoechstens EINE Griechen-Kurve liegt ueber den beiden G&V-Kurven; null
  // heisst "kein Overlay" - und genau dann sieht das Bild aus wie bisher.
  var overlayMetrik = null;

  // Die vier Metriken, in der Reihenfolge der Tasten. Der Name kommt aus dem
  // Katalog; Taste, rechte Achse und Legende lesen dieselbe Zeichenkette.
  var GREEKS = ['delta', 'gamma', 'vega', 'theta'];

  function greekWord(metric) { return T('js.greeks.' + metric); }

  function n(value) { return typeof value === 'number' && isFinite(value) ? value : 0; }

  function num(value) { return typeof value === 'number' && isFinite(value); }

  // Strikes werden auf die Genauigkeit verglichen, in der die Zeile sie
  // SPEICHERT: zwei Dezimalen. Ein adjustierter Kontrakt mit 15,335 kommt als
  // 15,34 zurueck - ein 1e-6-Fenster fände ihn nie, ein halber Cent ist genau
  // die Breite, die eine solche Zelle danebenliegen kann. Die Route prueft
  // dieselbe Genauigkeit (``_same_strike``), damit Anzeige und Antwort
  // dasselbe Paar meinen.
  var STRIKE_TOLERANCE = 0.005;

  function sameStrike(one, other) {
    return num(one) && num(other) && Math.abs(one - other) <= STRIKE_TOLERANCE;
  }

  // Zahlen in der Schreibweise der Sprache, die der Block nennt: Deutsch mit
  // Dezimalkomma, Englisch mit Punkt. Sonst liest der Leser "1.35" als Tausender
  // und "135.00" als Hundertfünfunddreißig - oder in der englischen Seite ein
  // deutsches Komma mitten im englischen Satz.
  var NUMBERS = (I18N && I18N.numbers) || { decimal: '.', thousands: ',' };

  function fmt(value, digits) {
    if (!num(value)) return '–';
    var text = value.toFixed(digits === undefined ? 2 : digits);
    return NUMBERS.decimal === ',' ? text.replace('.', ',') : text;
  }

  function gapText(value) {
    if (!num(value)) return '–';
    return (value > 0 ? '+' : '') + fmt(value, 1) + ' %';
  }

  function directionWord(direction) {
    if (direction === 'up') return { arrow: '↑', word: T('js.direction.up') };
    if (direction === 'down') return { arrow: '↓', word: T('js.direction.down') };
    return { arrow: '–', word: T('js.direction.unknown') };
  }

  // Risiko : Gewinn, mit deutschem Dezimalkomma.
  function rrText(value) {
    return num(value) ? '1 : ' + fmt(value, 1) : '–';
  }

  // Ein Strike, wie ihn der Kopf der Struktur zeigt: ganze Strikes ohne
  // Nachkomma (13, nicht 13,00), krumme mit beiden Stellen (102,75).
  function strikeText(value) {
    if (!num(value)) return '–';
    if (value % 1 === 0) return String(value);
    return fmt(value, 2).replace(/0$/, '');
  }

  // Derselbe Strike als PARAMETER der Route: ein Punkt, kein Komma - die
  // Anzeige spricht deutsch, die Adresse nicht.
  function strikeParam(value) {
    return num(value) ? String(value) : '';
  }

  function liquidityWord(value) {
    return value === 'full' ? T('js.liquidity.full') : T('js.liquidity.thin');
  }

  // Ein Vorzeichen ist eine Aussage, kein Schmuck: "+185,00" und "-145,00"
  // stehen als Text da, damit die Richtung nicht an einer Farbe haengt.
  function signedDe(value, digits) {
    if (!num(value)) return '–';
    return (value > 0 ? '+' : '') + fmt(value, digits);
  }

  function pctText(value) {
    if (!num(value)) return '–';
    return (value > 0 ? '+' : '') + fmt(value, 0) + ' %';
  }

  // Die drei Zahlen der Erreichbarkeit. Kein Wert wird erfunden: was die Route
  // als null liefert, steht als Strich da - eine 0 waere eine Messung, die es
  // nicht gibt.
  function moveText(value) {
    if (!num(value)) return '—';
    return (value > 0 ? '+' : '') + fmt(value, 1) + ' %';
  }

  function moveArrow(value) {
    if (!num(value)) return '';
    return value > 0 ? '↑ ' : '↓ ';
  }

  // Ein Strich mit Grund: "keine Messung" ist eine Aussage ueber die Historie,
  // nicht ueber den Titel - der Grund gehoert unter die Zahl.
  function historyWord(state) {
    if (state === 'too_short') return T('js.history.too_short');
    if (state === 'source_failed') return T('js.history.source_failed');
    return '';
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
      el.state.textContent = T('js.state.running', { day: day, seconds: sekunden });
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
      return T('js.error.invalid_day');
    }
    if (body && body.message) return body.message;
    return 'HTTP ' + status;
  }

  // Mehrfachform: der Zweig steht im Katalog, die Auswahl kommt aus der Regel,
  // die der Block mitliefert - dieselbe, die die Registry benutzt.
  function plural(count, key) {
    return T(key, { n: count });
  }

  // Die Grenze der noetigen Bewegung, die dieser Lauf wirklich benutzt hat -
  // nicht die Vorgabe der Seite: eine Antwort mit anderer Grenze muss als
  // solche lesbar sein. Sie steht in ``limits``; die aeltere Antwort trug sie
  // nur oben, und ganz ohne Zahl bleibt die Vorgabe der Seite.
  function moveCap(body) {
    var limits = body && body.limits;
    if (limits && num(limits.max_required_move_pct)) {
      return fmt(limits.max_required_move_pct, 0);
    }
    return num(body && body.max_required_move_pct) ? fmt(body.max_required_move_pct, 0) : '30';
  }

  // Die Zaehlerzeile nennt jede Huerde des Trichters in der Reihenfolge, in der
  // sie greift: gemessen, Umsatz, Kandidaten, Termin, Verfall, R/R, noetige
  // Bewegung, angezeigt.
  function countsText(counts, body) {
    return [
      plural(n(counts.gaps_computed), 'js.counts.gaps'),
      T('js.counts.turnover', { n: n(counts.turnover_passed) }),
      plural(n(counts.candidates), 'js.counts.candidates'),
      T('js.counts.next_earnings', { n: n(counts.with_next_earnings) }),
      T('js.counts.expiry', { n: n(counts.with_expiry_after_earnings) }),
      T('js.counts.rr', { n: n(counts.with_spread_ge_rr), rr: rrHurdleText(body) }),
      T('js.counts.move', { n: n(counts.with_reachable_move), cap: moveCap(body) }),
      T('js.counts.shown', { n: n(counts.shown) })
    ].join(', ');
  }

  // Eine Formulierung fuer Fuss UND Methodik-Absatz: welche Fenster gemessen
  // wurden (HANDELSTAGE), welche Restlaufzeit sie abdecken (KALENDERTAGE) und
  // an wie vielen Fenstern. Zwei Einheiten, zwei Namen - "dte-Handelstage"
  // gibt es nicht, dte allein ist die Kalenderdistanz bis Verfall.
  function hitSentence(sessions, calendarDays, windows) {
    return T('js.foot.hit_sentence', {
      sessions: sessions, calendar: calendarDays, windows: windows
    });
  }

  // Der Fuss erklaert, was "Treffer" misst - und nennt Fensterlaenge in
  // Handelstagen, Restlaufzeit in Kalendertagen und Anzahl der Fenster, soweit
  // sie fuer den Lauf eindeutig sind. Zwei verschiedene Laufzeiten an einem Tag
  // sind kein Grund, eine Zahl zu erfinden: dann steht "dte" da.
  function footText(rows, body) {
    var dten = [];
    var fenster = [];
    var laengen = [];
    (Array.isArray(rows) ? rows : []).forEach(function (entry) {
      if (num(entry.dte) && dten.indexOf(entry.dte) < 0) dten.push(entry.dte);
      if (num(entry.window_sessions) && entry.window_sessions > 0
          && laengen.indexOf(entry.window_sessions) < 0) {
        laengen.push(entry.window_sessions);
      }
      if (num(entry.windows_measured) && entry.windows_measured > 0
          && fenster.indexOf(entry.windows_measured) < 0) {
        fenster.push(entry.windows_measured);
      }
    });
    var sessions = laengen.length === 1 ? String(laengen[0]) : 'dte';
    var kalender = dten.length === 1 ? String(dten[0]) : 'dte';
    var anzahl = fenster.length === 1 ? String(fenster[0]) : 'n';
    return hitSentence(sessions, kalender, anzahl)
      + ' ' + T('js.foot.frequency')
      // Die Spalte Chance steht in der Liste, also wird sie hier erklaert und
      // nicht nur im Detail: sie ist eine Lesehilfe, und sie sortiert nichts.
      + ' ' + T('js.foot.chance');
  }

  // "Kein Kandidat" nennt IMMER die Zahl der Ausschluesse: ein Tag, an dem
  // zwanzig Titel ausschieden, ist kein ruhiger Tag ohne Befund.
  function emptyText(excluded, failures) {
    var teile = [plural(excluded.length, 'js.plural.excluded')];
    if (failures.length) {
      teile.push(plural(failures.length, 'js.plural.failure'));
    }
    return T('js.empty.no_candidate', { parts: teile.join(', ') });
  }

  function clearResult() {
    current = null;
    groups = [];
    syncExpandAll();
    el.tbody.innerHTML = '';
    el.wrap.hidden = true;
    el.counts.hidden = true;
    el.counts.textContent = '';
    el.empty.hidden = true;
    el.empty.textContent = '';
    el.notes.hidden = true;
    el.notes.textContent = '';
    if (el.foot) { el.foot.hidden = true; el.foot.textContent = ''; }
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
    el.state.textContent = T('js.state.failed');
    el.error.textContent = text;
    el.error.hidden = false;
  }

  function run(day) {
    if (!day || !DAY_RE.test(day)) {
      // Das Datumsfeld liefert "leer" statt Unsinn - dann wird nicht angefragt,
      // sondern benannt, was fehlt.
      clearResult();
      el.state.textContent = T('js.state.not_started');
      el.error.textContent = T('js.error.invalid_day');
      el.error.hidden = false;
      return;
    }
    var gen = generation + 1;
    generation = gen;
    var startedAt = Date.now();

    el.error.hidden = true;
    el.run.disabled = true;
    startTimer(day, startedAt, gen);

    fetch(inLanguage(API + '?day=' + encodeURIComponent(day)),
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
          fail(T('js.error.not_json'));
          return;
        }
        current = { day: day, body: antwort.body, at: new Date() };
        render();
      })
      .catch(function (err) {
        if (gen !== generation) return;
        stopTimer();
        el.run.disabled = false;
        fail(T('js.error.run_failed', { day: day, message: err.message }));
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

  // Optionen: Open Interest beider Legs und die Spanne, die man zahlt. Eine
  // Unterzeile traegt EINE Spanne - die schlechtere der beiden Legs, so rechnet
  // die Route sie; sie als Paar zu drucken waere eine zweite, erfundene Zahl.
  function optionsCell(entry) {
    var oi = Array.isArray(entry.open_interest) ? entry.open_interest : [];
    var paare = Array.isArray(entry.bid_ask_pct);
    var ba = paare ? entry.bid_ask_pct : [];
    var td = document.createElement('td');
    td.title = paare
      ? T('js.cell.options.title_per_leg')
      : T('js.cell.options.title_one_leg');
    td.textContent = T('js.cell.options.oi', { long: fmt(oi[0], 0), short: fmt(oi[1], 0) });
    var spreads = document.createElement('span');
    spreads.className = 'sub';
    spreads.textContent = paare
      ? T('js.cell.options.bid_ask_pair', { long: fmt(ba[0], 1), short: fmt(ba[1], 1) })
      : T('js.cell.options.bid_ask_max', { spread: fmt(entry.bid_ask_pct, 1) });
    td.appendChild(spreads);
    var liquid = document.createElement('span');
    liquid.className = 'sub';
    liquid.textContent = liquidityWord(entry.liquidity);
    td.appendChild(liquid);
    return td;
  }

  // Die fünfzehn Zellen NACH Nummer und Symbol - fuer die Hauptzeile und fuer
  // ihre Unterzeilen dieselben. Deshalb nimmt die Funktion eine SICHT und keine
  // Zeile: eine Unterzeile ist eine Struktur mit denselben Fragen.
  function bodyCells(entry) {
    var zellen = [];
    var richtung = directionWord(entry.direction);

    zellen.push(stacked(richtung.arrow + ' ' + gapText(entry.gap_pct), richtung.word,
      'dir', T('js.cell.gap.title')));

    zellen.push(cell(entry.strategy_label || STRATEGY[entry.strategy] || '–', '',
      entry.strategy || ''));

    zellen.push(cell(fmt(entry.close, 2), '', T('js.cell.close.title')));

    zellen.push(stacked(fmt(entry.long_strike, 1) + ' / ' + fmt(entry.short_strike, 1),
      T('js.cell.strikes.sub'), '',
      T('js.cell.strikes.title', {
        long: fmt(entry.long_strike, 1), short: fmt(entry.short_strike, 1)
      })));

    zellen.push(cell(fmt(entry.max_loss, 2), '', T('js.cell.max_loss.title')));
    zellen.push(cell(fmt(entry.max_profit, 2), '', T('js.cell.max_profit.title')));
    zellen.push(cell(rrText(entry.rr), '', T('js.cell.rr.title')));

    // Chance: Trefferquote x R/R - eine Lesehilfe, keine zweite Messung. Sie
    // steht direkt neben dem R/R, weil sie aus ihm und der Trefferquote
    // daneben gerechnet ist; sortiert wird trotzdem nach Trefferquote.
    zellen.push(cell(num(entry.chance) ? fmt(entry.chance, 2) : '—', '',
      T('js.cell.chance.title')));

    // Noetige Bewegung: Vorzeichen UND Pfeil, damit die Richtung nicht an einer
    // Zahl haengt, die man als Betrag lesen könnte.
    zellen.push(stacked(moveArrow(entry.required_move_pct) + moveText(entry.required_move_pct),
      num(entry.required_move_pct)
        ? (entry.required_move_pct > 0 ? T('js.move.rise') : T('js.move.fall')) : '',
      'dir', T('js.cell.move.title')));

    // Schwankung: annualisiert oben, der mittlere Tagesausschlag klein darunter.
    zellen.push(stacked(num(entry.volatility_pct) ? fmt(entry.volatility_pct, 1) + ' %' : '—',
      num(entry.daily_move_pct)
        ? T('js.cell.volatility.day', { value: fmt(entry.daily_move_pct, 1) }) : '',
      '', T('js.cell.volatility.title')));

    // Treffer: die gemessene Häufigkeit, darunter die Zahl der Fenster und -
    // klein - ihre Länge in HANDELSTAGEN. dte in der Spalte daneben ist die
    // Kalenderdistanz; die beiden Zahlen sind absichtlich nicht dieselbe.
    var fensterText = num(entry.windows_measured) && entry.windows_measured > 0
      ? T('js.cell.windows', { count: entry.windows_measured }) : historyWord(entry.history_state);
    if (num(entry.windows_measured) && entry.windows_measured > 0
        && num(entry.window_sessions) && entry.window_sessions > 0) {
      fensterText += ' · ' + T('js.cell.windows_sessions', { count: entry.window_sessions });
    }
    zellen.push(stacked(num(entry.hit_rate_pct) ? fmt(entry.hit_rate_pct, 0) + ' %' : '—',
      fensterText, '',
      T('js.cell.hit.title', {
        sessions: num(entry.window_sessions) && entry.window_sessions > 0
          ? entry.window_sessions : 'dte'
      })));

    zellen.push(cell(entry.expiration || '–', '', T('js.cell.expiration.title')));
    zellen.push(cell(num(entry.dte) ? String(entry.dte) : '–', '',
      T('js.cell.dte.title')));
    // Ein kuratiertes Datum steht nie wie ein Quellentermin da: die Zeile traegt
    // das Abzeichen unter dem Datum, das sagt, woher es kommt.
    if (entry.next_earnings_source === 'manual_curated') {
      zellen.push(stacked(entry.next_earnings_date || '–',
        curatedBadge(entry), 'hc-curated', T('js.cell.report.title_next')));
    } else {
      zellen.push(cell(entry.next_earnings_date || '–',
        entry.earnings_date
          ? T('js.cell.report.title_earnings_day', { date: entry.earnings_date })
          : T('js.cell.report.title_next')));
    }
    zellen.push(optionsCell(entry));
    return zellen;
  }

  // Eine Variante als SICHT auf dieselben fünfzehn Spalten: Strikes, Debit,
  // Gewinn, R/R, Chance, noetige Bewegung, Treffer und Optionen sind ihre eigenen;
  // Gap, Kurs, Strategie, Verfall, Laufzeit und Bericht gehoeren zum Titel und
  // bleiben die der Zeile. Die Schwankung kommt aus demselben Bars wie die der
  // Zeile, deshalb steht sie hier unveraendert.
  function variantView(entry, variant) {
    return {
      symbol: entry.symbol,
      name: entry.name,
      gap_pct: entry.gap_pct,
      direction: entry.direction,
      strategy: entry.strategy,
      strategy_label: entry.strategy_label,
      close: entry.close,
      long_strike: variant.long_strike,
      short_strike: variant.short_strike,
      max_loss: variant.max_loss,
      max_profit: variant.max_profit,
      rr: variant.rr,
      chance: variant.chance,
      required_move_pct: variant.required_move_pct,
      volatility_pct: entry.volatility_pct,
      daily_move_pct: entry.daily_move_pct,
      hit_rate_pct: variant.hit_rate_pct,
      windows_measured: variant.windows_measured,
      window_sessions: entry.window_sessions,
      history_state: null,
      expiration: entry.expiration,
      dte: entry.dte,
      next_earnings_date: entry.next_earnings_date,
      earnings_date: entry.earnings_date,
      open_interest: variant.open_interest,
      bid_ask_pct: variant.bid_ask_pct,
      liquidity: variant.liquidity
    };
  }

  // Die Nummer der Hauptzeile - und, wenn es Alternativen gibt, der Zaehler
  // davor. Der Zaehler klappt NUR die Gruppe auf; er oeffnet kein Detail, weil
  // "wie viele" und "welche" zwei verschiedene Fragen sind.
  function rankCell(entry, rank, group) {
    var td = document.createElement('td');
    td.className = 'rank';
    var count = Array.isArray(entry.variants) ? entry.variants.length : 0;
    if (count > 0) {
      var button = document.createElement('button');
      button.type = 'button';
      button.className = 'funnel-toggle';
      button.textContent = '▸ +' + count;
      button.setAttribute('aria-expanded', 'false');
      button.setAttribute('aria-controls', group.ids.join(' '));
      button.title = count === 1
        ? T('js.cell.rank.one')
        : T('js.cell.rank.many', { count: count });
      button.addEventListener('click', function (ev) {
        // Das Ereignis darf nicht zur Zeile durchlaufen: sonst oeffnete der
        // Zaehler beim Aufklappen auch das Detail.
        if (ev && typeof ev.stopPropagation === 'function') ev.stopPropagation();
        setGroup(group, !group.open);
      });
      // Enter und Leertaste oeffnen wie ein Klick. preventDefault haelt die
      // native Ausloesung des Knopfes zurueck - sonst schaltete die Taste
      // zweimal und die Gruppe bliebe zu.
      button.addEventListener('keydown', function (ev) {
        if (ev.key !== 'Enter' && ev.key !== ' ' && ev.key !== 'Spacebar') return;
        ev.preventDefault();
        if (typeof ev.stopPropagation === 'function') ev.stopPropagation();
        setGroup(group, !group.open);
      });
      group.button = button;
      td.appendChild(button);
    }
    var nummer = document.createElement('span');
    nummer.className = 'rank-num';
    nummer.textContent = rank;
    td.appendChild(nummer);
    return td;
  }

  function buildRow(entry, rank, group) {
    var row = document.createElement('tr');
    row.setAttribute('data-symbol', entry.symbol || '');
    row.setAttribute('data-direction', entry.direction || '');

    row.appendChild(rankCell(entry, rank, group));
    row.appendChild(stacked(entry.symbol || '–', entry.name || ''));
    bodyCells(entry).forEach(function (td) { row.appendChild(td); });

    // Die ZEILE ist der Griff zum Detail - der Zaehler in ihrer ersten Zelle
    // ist der Griff zur Gruppe. Sie ist per Tab erreichbar (tabindex), nennt
    // ihr Ziel (aria-controls) und ihren Zustand (aria-expanded, vom Setzen
    // der aktiven Zeile gepflegt).
    row.className = 'row-detail';
    row.setAttribute('tabindex', '0');
    row.setAttribute('aria-expanded', 'false');
    row.setAttribute('aria-controls', 'funnel-detail');
    row.addEventListener('click', function () {
      loadDetail(entry.symbol, row, null);
    });
    row.addEventListener('keydown', function (ev) {
      // Enter und Leertaste oeffnen wie ein Klick. Space muss gebremst werden,
      // sonst scrollt die Seite, statt das Detail zu laden.
      if (ev.key !== 'Enter' && ev.key !== ' ' && ev.key !== 'Spacebar') return;
      ev.preventDefault();
      loadDetail(entry.symbol, row, null);
    });

    return row;
  }

  // Eine Unterzeile: die Alternative SELBST, eingerueckt, mit ihrer Nummer
  // 1.1 und ihrem Charakter im Symbolfeld. Ein Klick oeffnet ihr Detail - mit
  // Verfall, Long und Short DIESER Struktur.
  function buildSubRow(entry, variant, number, id) {
    var row = document.createElement('tr');
    row.className = 'sub-row row-detail';
    row.setAttribute('id', id);
    row.setAttribute('data-symbol', entry.symbol || '');
    row.setAttribute('data-variant', variant.label || '');
    row.setAttribute('tabindex', '0');
    row.setAttribute('aria-expanded', 'false');
    row.setAttribute('aria-controls', 'funnel-detail');
    row.hidden = true;

    var nummer = document.createElement('td');
    nummer.className = 'rank';
    var subNummer = document.createElement('span');
    subNummer.className = 'rank-num';
    subNummer.textContent = number;
    nummer.appendChild(subNummer);
    row.appendChild(nummer);

    row.appendChild(stacked((entry.symbol || '–') + ' · ' + (variant.label || '–'), ''));

    bodyCells(variantView(entry, variant)).forEach(function (td) { row.appendChild(td); });

    // Die Varianten gehoeren zum Verfall der Zeile (so baut die Route sie):
    // genau dieser Verfall und diese zwei Strikes werden angefragt.
    function open() { loadStructure(entry, variant, row); }
    row.addEventListener('click', open);
    row.addEventListener('keydown', function (ev) {
      if (ev.key !== 'Enter' && ev.key !== ' ' && ev.key !== 'Spacebar') return;
      ev.preventDefault();
      open();
    });
    return row;
  }

  function loadStructure(entry, variant, row) {
    loadDetail(entry.symbol, row, {
      expiration: entry.expiration,
      long: variant.long_strike,
      short: variant.short_strike
    });
  }

  // Die Gruppen der Tabelle. Eine Gruppe ist EINE Hauptzeile mit ihren
  // Unterzeilen; aufgeklappt wird sie einzeln (Zaehler) oder alle zusammen
  // (Schalter ueber der Tabelle).
  function setGroup(group, open) {
    group.open = !!open;
    for (var i = 0; i < group.subs.length; i += 1) {
      group.subs[i].hidden = !group.open;
    }
    if (group.button) {
      group.button.textContent = (group.open ? '▾ +' : '▸ +') + group.count;
      group.button.setAttribute('aria-expanded', group.open ? 'true' : 'false');
    }
    syncExpandAll();
  }

  function allGroupsOpen() {
    if (!groups.length) return false;
    for (var i = 0; i < groups.length; i += 1) {
      if (!groups[i].open) return false;
    }
    return true;
  }

  // Der Schalter nennt, was er TUT, nicht was gerade ist: sind alle Gruppen
  // offen, schliesst er sie - und heisst dann "alle zuklappen".
  function syncExpandAll() {
    if (!el.expandAll) return;
    var offen = allGroupsOpen();
    el.expandAll.hidden = !groups.length;
    el.expandAll.textContent = offen ? T('js.expand.close') : T('js.expand.open');
    el.expandAll.setAttribute('aria-expanded', offen ? 'true' : 'false');
  }

  // Das Abzeichen eines kuratierten Termins nennt Quelle und Notizdatum - die
  // Zeile sagt damit selbst, woher ihr Datum kommt. Fehlt eines von beiden,
  // bleibt es beim kurzen Satz statt bei einer Luecke im Text.
  function curatedBadge(entry) {
    var hc = (entry && entry.hand_calendar) || {};
    var quelle = hc.source || T('js.handcalendar.source_manual');
    if (!hc.notiert_am) return T('js.handcalendar.curated_badge_plain');
    return T('js.handcalendar.curated_badge', { source: quelle, notiert_am: hc.notiert_am });
  }

  function reasonText(entry, body) {
    if (entry && entry.reason === 'no_spread_ge_rr') {
      return T('js.reason.no_spread_ge_rr', { rr: rrHurdleText(body) });
    }
    return REASONS[entry.reason] || entry.message || entry.reason || T('js.reason.unknown');
  }

  function fillList(details, summary, list, entries, label, textOf, extraOf) {
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
      // Was die Zeile ZUSAETZLICH traegt (Handkalender), haengt der Aufrufer an
      // - die Liste selbst kennt nur Symbol und Satz.
      if (extraOf) extraOf(entry, li);
      list.appendChild(li);
    });
    if (summary) summary.textContent = T('js.list.summary', { label: label, count: entries.length });
    details.hidden = false;
  }

  function failureText(entry) {
    if (entry.code && entry.message) return entry.code + ' — ' + entry.message;
    return entry.message || entry.code || T('js.reason.unknown');
  }

  // --- Handkalender -------------------------------------------------------- //
  // Neben einem Titel ohne Berichtstermin: das Feed-Signal (Ueberschrift + Link,
  // den ein MENSCH im Browser oeffnet) und daneben das Feld, in das er das Datum
  // aus der Ankuendigung tippt. Hier wird nichts aus einem Artikel gelesen und
  // nichts gerechnet - der Server liefert Titel, Link und die Pruefung.

  function hcNode(tag, className, text) {
    var node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined && text !== null) node.textContent = text;
    return node;
  }

  function feedBox(symbol, hc) {
    var box = hcNode('div', 'detail-handcal');
    box.appendChild(hcNode('span', 'hc-label', T('js.handcalendar.signal_label')));

    var titel = hcNode('a', 'hc-title', hc.title);
    titel.href = hc.link;
    titel.target = '_blank';
    // noreferrer: the news site learns nothing about this page.
    titel.rel = 'noopener noreferrer';
    box.appendChild(titel);

    var oeffnen = hcNode('a', 'hc-open', T('js.handcalendar.open_link'));
    oeffnen.href = hc.link;
    oeffnen.target = '_blank';
    oeffnen.rel = 'noopener noreferrer';
    box.appendChild(oeffnen);

    var feldId = 'hc-date-' + symbol;
    var zeile = hcNode('div', 'hc-entry');
    var label = hcNode('label', 'hc-date-label', T('js.handcalendar.date_label'));
    label.htmlFor = feldId;
    zeile.appendChild(label);
    var feld = document.createElement('input');
    feld.type = 'date';
    feld.id = feldId;
    feld.className = 'hc-date';
    feld.setAttribute('aria-describedby', feldId + '-hint');
    zeile.appendChild(feld);
    var knopf = hcNode('button', 'hc-save', T('js.handcalendar.save'));
    knopf.type = 'button';
    zeile.appendChild(knopf);
    var stand = hcNode('span', 'hc-status', '');
    stand.setAttribute('aria-live', 'polite');
    zeile.appendChild(stand);
    box.appendChild(zeile);

    var hinweis = hcNode('p', 'hc-hint', T('js.handcalendar.hint'));
    hinweis.id = feldId + '-hint';
    box.appendChild(hinweis);

    knopf.addEventListener('click', function () {
      saveHandDate(symbol, feld.value, stand, knopf);
    });
    return box;
  }

  // Ein Datum speichern: der Wert geht unveraendert an den Server, die Pruefung
  // (ISO, nicht vergangen, hoechstens 270 Tage) macht er - eine zweite Meiung
  // im Browser waere eine zweite Wahrheit.
  function saveHandDate(symbol, dateText, statusEl, button) {
    statusEl.className = 'hc-status';
    statusEl.textContent = T('js.handcalendar.saving');
    button.disabled = true;
    fetch(HAND_API, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
      body: JSON.stringify({ symbol: symbol, date: dateText })
    })
      .then(function (res) {
        return res.text().then(function (text) {
          var body = null;
          try { body = JSON.parse(text); } catch (err) { body = null; }
          return body;
        });
      })
      .then(function (answer) {
        if (answer && answer.status === 'saved') {
          statusEl.className = 'hc-status ok';
          statusEl.textContent = T('js.handcalendar.saved');
          return;
        }
        var code = (answer && answer.code) || 'not_saved';
        var satz = T('js.handcalendar.code.' + code);
        statusEl.className = 'hc-status bad';
        statusEl.textContent = satz === 'js.handcalendar.code.' + code
          ? T('js.handcalendar.code.not_saved') : satz;
      })
      .catch(function () {
        statusEl.className = 'hc-status bad';
        statusEl.textContent = T('js.handcalendar.code.not_saved');
      })
      .then(function () {
        button.disabled = false;
      });
  }

  // Was eine Ausschlusszeile zusaetzlich traegt: das Abzeichen "abgelaufen",
  // das Feed-Signal mit Feld ODER nur der Satz, dass der Feed nichts hergibt.
  function handCalendarExtra(entry, li) {
    var hc = entry && entry.hand_calendar;
    if (!hc) return;
    if (hc.status === 'expired') {
      li.appendChild(hcNode('span', 'hc-badge expired',
        T('js.handcalendar.expired_badge')));
    }
    if (hc.title && hc.link) {
      li.appendChild(feedBox(entry.symbol, hc));
      return;
    }
    li.appendChild(hcNode('span', 'hc-nosignal', T('js.handcalendar.no_signal')));
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
    overlayMetrik = null;              // ein neues Detail beginnt ohne Overlay
    if (!el.detail) return;
    el.detail.hidden = true;
    el.detail.open = false;
    if (el.detailSummary) el.detailSummary.textContent = T('js.detail.summary');
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
      return T('js.detail.unknown_symbol');
    }
    if (code === 'leg_not_listed') {
      return T('js.detail.leg_not_listed');
    }
    if (code === 'chain_unavailable') {
      return T('js.detail.chain_unavailable');
    }
    if (code === 'no_next_earnings') {
      return T('js.detail.no_next_earnings');
    }
    if (code === 'no_expiry_after_earnings') {
      return T('js.detail.no_expiry');
    }
    if (code === 'no_candidate' || code === 'no_spread_ge_rr' || code === 'no_gap') {
      return body && body.message ? body.message : T('js.detail.no_candidate');
    }
    return messageFor(antwort.status, body);
  }

  // Der Aufruf nennt den Titel und den Tag - und, wenn eine Unterzeile
  // geklickt wurde, die STRUKTUR (Verfall, Long, Short). Ohne Struktur
  // antwortet die Route mit der Trichter-Auswahl.
  function loadDetail(symbol, row, structure) {
    if (!current || !symbol) return;
    var day = current.day;
    var gen = detailGeneration + 1;
    detailGeneration = gen;
    setActiveRow(row);
    openDetail(T('js.detail.loading', { symbol: symbol }));
    el.detailBody.textContent = '';

    var url = DETAIL_API + '?symbol=' + encodeURIComponent(symbol)
      + '&day=' + encodeURIComponent(day);
    if (structure) {
      url += '&expiration=' + encodeURIComponent(structure.expiration || '')
        + '&long=' + encodeURIComponent(strikeParam(structure.long))
        + '&short=' + encodeURIComponent(strikeParam(structure.short));
    }

    fetch(inLanguage(url),
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
          renderDetailError(symbol, T('js.error.not_json'));
          return;
        }
        renderDetail(antwort.body);
      })
      .catch(function (err) {
        if (gen !== detailGeneration) return;
        renderDetailError(symbol, T('js.detail.failed', { symbol: symbol, message: err.message }));
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
    openDetail(T('js.detail.title', { symbol: symbol }));
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
        leg.side === 'short' ? T('js.legs.side.short') : T('js.legs.side.long'),
        fmt(leg.strike, 2),
        fmt(leg.bid, 2),
        fmt(leg.ask, 2),
        fmt(leg.mid, 2),
        num(leg.iv) ? fmt(leg.iv, 2) + ' %' : '–',
        fmt(leg.open_interest, 0),
        fmt(leg.delta, 2)
      ];
    });
    return table([T('js.legs.col.side'), T('js.legs.col.strike'), T('js.legs.col.bid'),
      T('js.legs.col.ask'), T('js.legs.col.mid'), T('js.legs.col.iv'),
      T('js.legs.col.oi'), T('js.legs.col.delta')], rows);
  }

  function scenarioTable(scenarios) {
    var rows = (Array.isArray(scenarios) ? scenarios : []).map(function (s) {
      return [
        pctText(s.change_pct),
        fmt(s.underlying, 2),
        signedDe(s.pl_expiry, 2),
        signedDe(s.delta, 2),
        signedDe(s.gamma, 2),
        signedDe(s.vega, 2),
        signedDe(s.theta, 2)
      ];
    });
    return table([T('js.scenarios.col.change'), T('js.scenarios.col.underlying'),
      T('js.scenarios.col.pl'), T('js.scenarios.col.delta'),
      T('js.scenarios.col.gamma'), T('js.scenarios.col.vega'),
      T('js.scenarios.col.theta')], rows);
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

  // Dieselbe Frage fuer die Griechen-Achse: dort liegen die Stufen oft unter
  // 1 (Gamma je Vol-Punkt), und "0,0" waere eine falsche Zahl. Eine Stufe von
  // 0,05 braucht zwei Stellen, eine von 0,005 drei.
  function tickStellen(ticks) {
    if (ticks.length < 2) return 2;
    var abstand = Math.abs(ticks[1] - ticks[0]);
    if (abstand >= 1) return 0;
    if (abstand >= 0.1) return 1;
    if (abstand >= 0.01) return 2;
    return 3;
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

  // Der Punkt der Serie, der einem Preis am naechsten liegt. Die Antwort fuehrt
  // jeden Szenariopreis als eigenen Punkt; der Suchlauf ist nur der robuste Weg
  // dorthin (ein Sub-Dollar-Raster rundet vierstellig, die Tabelle zweistellig).
  function naechsterPunkt(liste, preis) {
    var best = null;
    var abstand = Infinity;
    liste.forEach(function (punkt) {
      if (!num(punkt.price)) return;
      var d = Math.abs(punkt.price - preis);
      if (d < abstand) { abstand = d; best = punkt; }
    });
    return best;
  }

  // Zwei Kurven in EINEM Bild: heute (durchgezogen, aus dem Modell) und bei
  // Verfall (gestrichelt, exakt). Nulllinie, Marker am Spot, beschriftete
  // Achsen in Kurs und G&V. Gezeichnet aus der Serie der Antwort, nicht aus
  // einer zweiten Rechnung im Browser.
  function chart(detail, metric) {
    var serie = Array.isArray(detail.series) ? detail.series : [];
    // Ohne Argument gilt der Zustand der Tasten - so zeichnet auch der
    // Fenster-Handler (der nur `detail` kennt) das aktive Overlay mit.
    var metrik = metric === undefined ? overlayMetrik : metric;
    var breite = chartBreite();
    var hoehe = Math.max(280, Math.min(560, Math.round(breite * 0.375)));
    var box = svgNode('svg', {
      viewBox: '0 0 ' + breite + ' ' + hoehe,
      preserveAspectRatio: 'xMidYMid meet',
      width: '100%',
      class: 'detail-chart-svg',
      role: 'img',
      'aria-label': T('js.chart.aria', { symbol: detail.symbol || '' })
        + (metrik ? ' · ' + T('js.chart.aria.greek', { greek: greekWord(metrik) }) : '')
    });
    // Titel und Beschreibung sind der Text, den ein Screenreader statt der
    // Linien liest; aria-label allein nennt nur die Kurven, nicht die Achsen.
    var titel = svgNode('title');
    titel.textContent = T('js.chart.title');
    box.appendChild(titel);
    var beschreibung = svgNode('desc');
    beschreibung.textContent = T('js.chart.desc', { spot: fmt(detail.spot, 2) });
    box.appendChild(beschreibung);
    if (!serie.length || !num(detail.spot)) return box;

    var padL = 84, padT = 18, padB = 56;
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

    // Das Overlay hat seine EIGENE Skala rechts: nur die Werte dieser einen
    // Metrik ueber die ganze Serie, mit demselben Polster. Ohne sie wuerde eine
    // Griechen-Kurve gegen die Dollar-Achse gezeichnet und waere unlesbar.
    var overlay = false;
    var gwerte = [];
    if (metrik) {
      serie.forEach(function (p) { if (num(p[metrik])) gwerte.push(p[metrik]); });
      overlay = gwerte.length > 0;
    }
    var glo = 0;
    var ghi = 1;
    if (overlay) {
      glo = Math.min.apply(null, gwerte);
      ghi = Math.max.apply(null, gwerte);
      if (ghi === glo) ghi = glo + 1;
      var gpolster = (ghi - glo) * 0.06;
      glo -= gpolster;
      ghi += gpolster;
    }
    // Die rechte Achse ist schmaler als die G&V-Achse links (84 px mit Titel):
    // 58 px reichen fuer Striche, Zahlen und den gedrehten Titel.
    var padR = overlay ? 58 : 22;

    var plotX = padL, plotY = padT;
    var plotW = breite - padL - padR;
    var plotH = hoehe - padT - padB;

    function x(price) { return plotX + (price - xmin) / (xmax - xmin) * plotW; }
    function y(pl) { return plotY + (yhi - pl) / (yhi - ylo) * plotH; }
    function yg(value) { return plotY + (ghi - value) / (ghi - glo) * plotH; }

    function pfad(key, skala) {
      var teile = [];
      serie.forEach(function (p) {
        if (!num(p[key])) return;
        teile.push((teile.length ? 'L' : 'M') + x(p.price).toFixed(1) + ' '
          + skala(p[key]).toFixed(1));
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

    // Die rechte Achse gehoert dem Overlay: eigene Stufen (dieselbe Rundung wie
    // links), eigene Beschriftung, und sie verschwindet mit dem Overlay
    // vollstaendig - ohne aktive Metrik gibt es hier nichts zu lesen.
    if (overlay) {
      var gticks = gvTicks(glo, ghi);
      var gstellen = tickStellen(gticks);
      gticks.forEach(function (wert) {
        var gy = yg(wert);
        box.appendChild(svgNode('line', {
          x1: plotX + plotW, y1: gy, x2: plotX + plotW + 5, y2: gy,
          stroke: 'rgba(77,195,255,.45)', 'stroke-width': 1
        }));
        var glabel = svgNode('text', {
          x: plotX + plotW + 9, y: gy + 3.5, fill: 'rgba(77,195,255,.78)',
          'font-size': 10, 'font-family': 'monospace', 'text-anchor': 'start',
          class: 'chart-greek-tick'
        });
        glabel.textContent = signedDe(wert, gstellen);
        box.appendChild(glabel);
      });
      // Die Null ist nur dann eine Linie, wenn sie im Bereich liegt: eine
      // Nulllinie ausserhalb der Skala waere eine Linie am Rand.
      if (glo <= 0 && ghi >= 0) {
        box.appendChild(svgNode('line', {
          x1: plotX, y1: yg(0), x2: plotX + plotW, y2: yg(0),
          stroke: 'rgba(77,195,255,.22)', 'stroke-width': 1
        }));
      }
      var gmitte = plotY + plotH / 2;
      var gtitel = svgNode('text', {
        x: breite - 8, y: gmitte, fill: 'rgba(77,195,255,.62)', 'font-size': 10,
        'font-family': 'monospace', 'text-anchor': 'middle',
        transform: 'rotate(-90 ' + (breite - 8) + ' ' + gmitte + ')',
        class: 'chart-axis-title chart-greek-axis-title'
      });
      gtitel.textContent = T('js.chart.axis.greek.' + metrik);
      box.appendChild(gtitel);
    }

    box.appendChild(svgNode('path', {
      d: pfad('pl_today', y), fill: 'none', stroke: '#fafafa', 'stroke-width': 1.6
    }));
    box.appendChild(svgNode('path', {
      d: pfad('pl_expiry', y), fill: 'none', stroke: '#e05534', 'stroke-width': 1.6,
      'stroke-dasharray': '5 4'
    }));

    // Die Griechen-Kurve liegt DARUEBER, hellblau und anders gestrichelt als
    // die Verfallslinie - Farbe und Strichart, zwei Signale. Nur die heutige
    // Modellkurve: bei Verfall sind Gamma/Vega/Theta null, eine zweite Linie
    // waere leer.
    if (overlay) {
      box.appendChild(svgNode('path', {
        d: pfad(metrik, yg), fill: 'none', stroke: '#4dc3ff', 'stroke-width': 1.6,
        'stroke-dasharray': '6 4', class: 'chart-greek-path'
      }));
      // Sieben Werte an den Szenariopreisen - gelesen aus dem Punkt der Serie,
      // der dort liegt, damit die Zahl mit der Tabelle darunter uebereinstimmt.
      var szenarien = Array.isArray(detail.scenarios) ? detail.scenarios : [];
      szenarien.forEach(function (s) {
        if (!num(s.underlying)) return;
        var punkt = naechsterPunkt(serie, s.underlying);
        if (!punkt || !num(punkt[metrik])) return;
        if (punkt.price < xmin - 1e-9 || punkt.price > xmax + 1e-9) return;
        var mx = x(punkt.price);
        var my = yg(punkt[metrik]);
        var amRandLinks = mx < plotX + 26;
        var amRandRechts = mx > plotX + plotW - 26;
        var platzOben = my > plotY + 26;
        var marke = svgNode('text', {
          x: mx + (amRandLinks ? 3 : (amRandRechts ? -3 : 0)),
          y: my + (platzOben ? -6 : 14),
          fill: '#4dc3ff', 'font-size': 11, 'font-family': 'monospace',
          'text-anchor': amRandLinks ? 'start' : (amRandRechts ? 'end' : 'middle'),
          // dunkler Rand um die Schrift, wie beim Spot-Label: die Kurve laeuft
          // sonst durch die Zahl
          stroke: '#091217', 'stroke-width': 3, 'stroke-linejoin': 'round',
          'paint-order': 'stroke', class: 'chart-greek-value'
        });
        marke.textContent = signedDe(punkt[metrik], 2);
        box.appendChild(marke);
      });
    }

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
      spotText.textContent = T('js.chart.spot', { value: fmt(detail.spot, 2) });
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
      xLabel.textContent = fmt(preis, 2);
      box.appendChild(xLabel);
    });

    // Achsentitel: die eine Zeile sagt, was die Zahlen bedeuten.
    var kursTitel = svgNode('text', {
      x: plotX + plotW / 2, y: hoehe - 8, fill: 'rgba(250,250,250,.48)',
      'font-size': 11, 'font-family': 'monospace', 'text-anchor': 'middle',
      class: 'chart-axis-title'
    });
    kursTitel.textContent = T('js.chart.axis.price');
    box.appendChild(kursTitel);

    var gvMitte = plotY + plotH / 2;
    var gvTitel = svgNode('text', {
      x: 14, y: gvMitte, fill: 'rgba(250,250,250,.48)', 'font-size': 11,
      'font-family': 'monospace', 'text-anchor': 'middle',
      transform: 'rotate(-90 14 ' + gvMitte + ')', class: 'chart-axis-title'
    });
    gvTitel.textContent = T('js.chart.axis.pl');
    box.appendChild(gvTitel);

    return box;
  }

  // Der Satz neben den Legende-Eintraegen: EINE Zeile, nur bei aktivem Overlay.
  // Die Zahlen kommen fertig aus der Antwort (greek_explain) - hier wird nur
  // formatiert, nie gerechnet.
  function explainText(metric, detail) {
    var werte = (detail && detail.greek_explain) || {};
    if (metric === 'delta') {
      return T('js.greeks.explain.delta', { delta: fmt(werte.delta_value, 0),
        cents: fmt(werte.delta_value, 0) });
    }
    if (metric === 'theta') {
      return T('js.greeks.explain.theta', { theta: signedDe(werte.theta_value, 2),
        theta_cents: fmt(werte.theta_cents, 0) });
    }
    return T('js.greeks.explain.' + metric);
  }

  // Die Legende steht unter dem Bild in Textform: sie nennt beide Linien in
  // Worten, und der Strich daneben ist das zweite Signal - nie allein die
  // Farbe, nie allein die Strichart. Der dritte Eintrag erscheint NUR, solange
  // ein Overlay aktiv ist, und nennt die Metrik beim Namen; rechts daneben
  // steht der eine Satz, der sie erklaert.
  function chartLegend(metric, detail) {
    var legende = document.createElement('div');
    legende.className = 'detail-legend';
    // Die Eintraege stecken in EINEM Block: so kann der Satz rechts daneben
    // stehen und trotzdem als Ganzes unter die Eintraege rutschen, wenn der
    // Platz nicht reicht - statt die Eintraege zu quetschen.
    var liste = document.createElement('span');
    liste.className = 'detail-legend-items';
    var eintraege = [['solid', T('js.chart.legend.today')],
      ['dashed', T('js.chart.legend.expiry')]];
    if (metric) {
      eintraege.push(['greek', T('js.chart.legend.greek', { greek: greekWord(metric) })]);
    }
    eintraege.forEach(function (paar) {
      var zeile = document.createElement('span');
      zeile.className = 'detail-legend-item';
      var strich = document.createElement('i');
      strich.className = paar[0];
      zeile.appendChild(strich);
      var wort = document.createElement('span');
      wort.textContent = paar[1];
      zeile.appendChild(wort);
      liste.appendChild(zeile);
    });
    legende.appendChild(liste);
    if (metric) {
      var satz = document.createElement('span');
      satz.className = 'detail-legend-note';
      // aria-live: der Satz wechselt mit der Metrik, ohne dass der Fokus
      // dorthin springt - ein Screenreader liest die Aenderung trotzdem vor.
      satz.setAttribute('aria-live', 'polite');
      satz.textContent = explainText(metric, detail);
      legende.appendChild(satz);
    }
    return legende;
  }

  // Die Zeile, zu der ein Detail gehoert. Die drei Zahlen der Erreichbarkeit
  // misst die LISTE fuer diesen Titel und Tag; das Detail wiederholt sie, statt
  // eine zweite Historie zu holen und zwei Zahlen zur selben Frage zu zeigen.
  function rowFor(symbol) {
    var liste = current && current.body && Array.isArray(current.body.results)
      ? current.body.results : [];
    for (var i = 0; i < liste.length; i += 1) {
      if (liste[i] && liste[i].symbol === symbol) return liste[i];
    }
    return null;
  }

  // Die Zahlen der Erreichbarkeit gehoeren zur STRUKTUR, nicht zum Titel: 1.1
  // hat ihre eigene noetige Bewegung und ihre eigene Trefferquote, gemessen an
  // denselben Bars und derselben Laufzeit wie die Zeile. Eine Struktur, die die
  // Liste nicht kennt (also kein Charakter ist), bekommt keine Zahl geliehen.
  function structureFor(detail) {
    var zeile = rowFor(detail.symbol);
    if (!zeile) return null;
    if (detail.is_row_pick) return zeile;
    var strikes = detail.strikes || {};
    var liste = Array.isArray(zeile.variants) ? zeile.variants : [];
    for (var i = 0; i < liste.length; i += 1) {
      var variante = liste[i];
      if (num(variante.long_strike) && num(variante.short_strike)
          && sameStrike(variante.long_strike, strikes.long)
          && sameStrike(variante.short_strike, strikes.short)) {
        return {
          required_move_pct: variante.required_move_pct,
          volatility_pct: zeile.volatility_pct,
          daily_move_pct: zeile.daily_move_pct,
          hit_rate_pct: variante.hit_rate_pct,
          windows_measured: variante.windows_measured,
          window_sessions: zeile.window_sessions,
          dte: zeile.dte,
          history_state: null
        };
      }
    }
    return { required_move_pct: null, volatility_pct: null, daily_move_pct: null,
      hit_rate_pct: null, windows_measured: null, window_sessions: null,
      dte: zeile.dte, history_state: null };
  }

  // Ein Datum der Route ist ISO ("2027-02-19"), gelesen wird es deutsch
  // ("19.02.2027"): der Leser sucht den Tag, nicht die Sortierform. Ein Feld
  // ohne Datum bleibt ein Strich - kein erfundenes Datum.
  function dayText(iso) {
    var treffer = /^(\d{4})-(\d{2})-(\d{2})$/.exec(String(iso === undefined || iso === null ? '' : iso));
    if (!treffer) return '—';
    return treffer[3] + '.' + treffer[2] + '.' + treffer[1];
  }

  // Eine Zelle des Kennzahlen-Rasters: Beschriftung UEBER dem Wert. Die
  // Beschriftung traegt die Bedeutung, damit die Zahl daneben keine Zuordnung
  // aus dem Zusammenhang braucht.
  function figure(label, value) {
    var zelle = document.createElement('span');
    zelle.className = 'fig';
    var beschriftung = document.createElement('span');
    beschriftung.className = 'fig-label';
    beschriftung.textContent = label;
    var wert = document.createElement('span');
    wert.className = 'fig-value';
    wert.textContent = value;
    zelle.appendChild(beschriftung);
    zelle.appendChild(wert);
    return zelle;
  }

  // Die acht Zahlen des Spreads, in dieser Reihenfolge. Das Raster ist IMMER
  // achtzellig: fehlt eine Zahl, steht dort ein Strich - eine fehlende Zelle
  // wuerde die Spalten untereinander verschieben und die Reihenfolge luegen.
  // Die drei Zahlen der Erreichbarkeit kommen aus der Zeile (structureFor);
  // kennt die Liste die Struktur nicht, bleiben genau diese drei leer.
  function figures(detail, zeile) {
    var raster = document.createElement('div');
    raster.className = 'detail-figures';
    raster.appendChild(figure(T('js.figures.debit'), fmt(detail.net_debit, 2)));
    raster.appendChild(figure(T('js.figures.breakeven'), fmt(detail.breakeven, 2)));
    raster.appendChild(figure(T('js.figures.max_loss'), fmt(detail.max_loss, 2)));
    raster.appendChild(figure(T('js.figures.max_profit'), fmt(detail.max_profit, 2)));
    raster.appendChild(figure(T('js.figures.rr'), rrText(detail.rr)));
    raster.appendChild(figure(T('js.figures.required_move'),
      zeile ? moveText(zeile.required_move_pct) : '—'));
    raster.appendChild(figure(T('js.figures.volatility'),
      zeile && num(zeile.volatility_pct) ? fmt(zeile.volatility_pct, 1) + ' %' : '—'));
    raster.appendChild(figure(T('js.figures.hit_rate'),
      zeile && num(zeile.hit_rate_pct) ? fmt(zeile.hit_rate_pct, 0) + ' %' : '—'));
    return raster;
  }

  // Der Kopf des Details hat DREI Ebenen statt einer Zeile mit dreizehn
  // Angaben: Titel (Symbol, Paar, Charakter als Etikett, Name), Untertitel
  // (Strategie und Verfall) und das Raster der acht Zahlen. In einem Absatz
  // gingen die Zuordnungen in den Trennpunkten verloren - "1 : 2,2 · Break-even
  // 25,90 · Debit 1,90" liest niemand als Tabelle.
  function detailHead(detail, zeile) {
    var strikes = detail.strikes || {};
    var kopf = document.createElement('div');
    kopf.className = 'detail-head';

    var titel = document.createElement('p');
    titel.className = 'detail-title';
    var paar = num(strikes.long) && num(strikes.short)
      ? strikeText(strikes.long) + '/' + strikeText(strikes.short) : null;
    titel.textContent = String(detail.symbol || '–') + (paar ? ' · ' + paar : '');
    // Der Charakter ist ein Etikett NEBEN dem Paar, kein Satzglied: er
    // beschreibt das Paar, nicht den Titel.
    if (detail.structure_label) {
      var etikett = document.createElement('span');
      etikett.className = 'detail-tag';
      etikett.textContent = detail.structure_label;
      titel.appendChild(etikett);
    }
    if (detail.name) {
      var namenszug = document.createElement('span');
      namenszug.className = 'detail-name';
      namenszug.textContent = ' · ' + detail.name;
      titel.appendChild(namenszug);
    }
    kopf.appendChild(titel);

    var unter = document.createElement('p');
    // Zwei Klassen: `detail-sub` benennt die Ebene, `detail-head-sub` haelt sie
    // von den Zwischentiteln des Blocks (Legs, Bild, Szenarien) getrennt - die
    // sind Versalien, der Untertitel des Kopfes ist Fliesstext.
    unter.className = 'detail-sub detail-head-sub';
    unter.textContent = T('js.detail.subtitle', {
      strategy: detail.strategy_label || STRATEGY[detail.strategy] || '–',
      expiration: dayText(detail.expiration)
    });
    kopf.appendChild(unter);

    kopf.appendChild(figures(detail, zeile));
    return kopf;
  }

  // Die Tastenwahl ueber dem Bild: vier echte <button>, genau EINE Metrik
  // gleichzeitig. `aria-pressed` sagt, welche aktiv ist - die orange Kante ist
  // nur die zweite Aussage dazu. Ein zweiter Klick auf die aktive Taste nimmt
  // das Overlay wieder weg; jede andere Taste ERSETZT es.
  function greekButtons(onToggle) {
    var zeile = document.createElement('div');
    zeile.className = 'detail-greeks';
    zeile.setAttribute('role', 'group');
    zeile.setAttribute('aria-label', T('js.greeks.label'));
    var knoepfe = GREEKS.map(function (metric) {
      var knopf = document.createElement('button');
      knopf.type = 'button';
      knopf.className = 'greek-btn';
      knopf.setAttribute('data-greek', metric);
      knopf.setAttribute('aria-pressed', 'false');
      knopf.textContent = greekWord(metric);
      function waehle() {
        overlayMetrik = overlayMetrik === metric ? null : metric;
        sync();
        onToggle();
      }
      // Enter und Leertaste: `preventDefault` unterdrueckt den Klick, den der
      // Browser sonst zusaetzlich ausloest - sonst schaltet die Taste zweimal.
      knopf.addEventListener('click', waehle);
      knopf.addEventListener('keydown', function (ev) {
        if (ev.key !== 'Enter' && ev.key !== ' ' && ev.key !== 'Spacebar') return;
        ev.preventDefault();
        waehle();
      });
      zeile.appendChild(knopf);
      return knopf;
    });
    function sync() {
      knoepfe.forEach(function (knopf) {
        knopf.setAttribute('aria-pressed',
          knopf.getAttribute('data-greek') === overlayMetrik ? 'true' : 'false');
      });
    }
    return { node: zeile, sync: sync };
  }

  function renderDetail(detail) {
    el.detailBody.textContent = '';
    var zeile = structureFor(detail);
    el.detailBody.appendChild(detailHead(detail, zeile));

    el.detailBody.appendChild(subtitle(T('js.detail.legs_heading')));
    el.detailBody.appendChild(legTable(detail.legs));

    el.detailBody.appendChild(subtitle(T('js.detail.chart_heading')));
    // Beim Oeffnen KEIN Overlay: das Bild ist genau das von vorher, die
    // Tasten stehen sichtbar und inaktiv darueber.
    overlayMetrik = null;
    var chartBox = document.createElement('div');
    chartBox.className = 'detail-chart';

    function zeichne() {
      chartBox.textContent = '';
      chartBox.appendChild(chart(detail, overlayMetrik));
      chartBox.appendChild(chartLegend(overlayMetrik, detail));
    }

    var tasten = greekButtons(zeichne);
    el.detailBody.appendChild(tasten.node);
    zeichne();
    el.detailBody.appendChild(chartBox);
    letztesDetail = detail;

    el.detailBody.appendChild(subtitle(T('js.detail.scenarios_heading')));
    el.detailBody.appendChild(scenarioTable(detail.scenarios));

    // Der Methodik-Absatz steht bei den Notizen des Details, nicht im Kopf: er
    // nennt Fensterlaenge IN HANDELSTAGEN, die Restlaufzeit in KALENDERTAGEN
    // und die Anzahl der Fenster - die drei Zahlen, ohne die eine Trefferquote
    // nicht lesbar ist. Dieselbe Formulierung wie im Fuss.
    if (zeile && num(zeile.window_sessions) && zeile.window_sessions > 0
        && num(zeile.windows_measured) && zeile.windows_measured > 0) {
      var methodik = document.createElement('p');
      methodik.className = 'detail-notes';
      // Dieselben zwei Absaetze wie im Fuss, nur mit "weiter" im letzten Satz:
      // hier steht die Chance schon in der Liste daneben.
      methodik.textContent = T('js.detail.method', {
        sentence: hitSentence(zeile.window_sessions,
          num(zeile.dte) ? zeile.dte : 'dte', zeile.windows_measured)
          + ' ' + T('js.foot.frequency')
          + ' ' + T('js.foot.chance_method')
      });
      el.detailBody.appendChild(methodik);
    }

    var notes = Array.isArray(detail.notes) ? detail.notes.filter(Boolean) : [];
    if (notes.length) {
      var hinweis = document.createElement('p');
      hinweis.className = 'detail-notes';
      hinweis.textContent = notes.join(' ');
      el.detailBody.appendChild(hinweis);
    }

    openDetail(T('js.detail.title', { symbol: detail.symbol || '–' }) + ' · '
      + (detail.strategy_label || STRATEGY[detail.strategy] || '–')
      + ' · ' + T('js.detail.expiry_label', { expiration: detail.expiration || '–' }));
  }

  function render() {
    var body = current.body;
    var ergebnisse = Array.isArray(body.results) ? body.results : [];
    var ausschluesse = Array.isArray(body.excluded) ? body.excluded : [];
    var fehlschlaege = Array.isArray(body.failures) ? body.failures : [];
    var counts = body.counts || {};
    if (el.cap) el.cap.textContent = moveCap(body);
    if (el.rr) el.rr.textContent = rrHurdleText(body);
    var notes = Array.isArray(body.notes) ? body.notes.filter(Boolean) : [];

    // Ein neuer Lauf baut neue Zeilen: das Detail der alten Zeile ist damit
    // veraltet und geht zu - ein Block, der zu einer Zeile zeigt, die es nicht
    // mehr gibt, waere eine falsche Auskunft.
    clearDetail();
    el.tbody.innerHTML = '';
    groups = [];
    // Jede Zeile kommt mit ihren Unterzeilen in die Tabelle - zusammen und in
    // dieser Reihenfolge. Die Sortierung der Route ordnet die ZEILEN; die
    // Gruppe haengt an ihrer Zeile, damit sie beim Sortieren nicht zerfaellt.
    ergebnisse.forEach(function (entry, index) {
      var rank = String(index + 1);
      var varianten = Array.isArray(entry.variants) ? entry.variants : [];
      var ids = varianten.map(function (_variante, stelle) {
        return 'funnel-sub-' + rank + '-' + (stelle + 1);
      });
      var group = { rank: rank, count: varianten.length, ids: ids, subs: [],
        open: false, button: null };
      el.tbody.appendChild(buildRow(entry, rank, group));
      varianten.forEach(function (variante, stelle) {
        var sub = buildSubRow(entry, variante, rank + '.' + (stelle + 1), ids[stelle]);
        group.subs.push(sub);
        el.tbody.appendChild(sub);
      });
      if (varianten.length) {
        groups.push(group);
        setGroup(group, false);
      }
    });
    syncExpandAll();

    el.counts.textContent = countsText(counts, body);
    el.counts.hidden = false;

    // Der Fuss gehoert zur Tabelle: ohne Zeilen gibt es nichts zu erklaeren.
    if (el.foot) {
      el.foot.textContent = ergebnisse.length ? footText(ergebnisse, body) : '';
      el.foot.hidden = !ergebnisse.length;
    }
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
      T('js.list.excluded'), function (entry) { return reasonText(entry, body); },
      handCalendarExtra);
    fillList(el.failuresBlock, el.failuresSummary, el.failures, fehlschlaege,
      T('js.list.failures'), failureText);

    el.state.textContent = T('js.state.ready', {
      clock: clock(current.at),
      rows: plural(ergebnisse.length, 'js.plural.rows')
    });
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
  // "alle aufklappen" schaltet ALLE Gruppen zugleich - und laesst jeder Gruppe
  // danach ihren eigenen Zaehler: der Schalter ist eine Abkuerzung, kein Modus.
  if (el.expandAll) {
    el.expandAll.addEventListener('click', function () {
      var offen = !allGroupsOpen();
      groups.forEach(function (group) { setGroup(group, offen); });
    });
  }
  el.day.addEventListener('keydown', function (ev) {
    if (ev.key !== 'Enter') return;
    ev.preventDefault();
    run(el.day.value.trim());
  });
})();
