/* Gap-Scan - die Kandidaten eines Reaktionstags als Tabelle auf GET /.

   Warum eine eigene Datei: der Block laedt eine Route, benennt vier
   Fehlerzustaende und baut neun Spalten - das ist Verhalten, kein Text, und
   zwischen den beiden bestehenden Skripten in screen.html waere es nicht mehr
   zu lesen. Kein Framework, kein Build-Schritt: eine Datei, die der Browser
   so nimmt.

   Drei Dinge tut dieser Block bewusst NICHT:
     * er startet keinen Scan beim Laden der Seite - der Lauf kostet je Titel
       einen Abruf, also bleibt er ein ausdruecklicher Klick;
     * er holt keine Optionskette und rechnet keinen Spread - die Kette kostet
       Sekunden je Titel und wird erst beim Oeffnen eines Titels geholt;
     * er zeigt die Session nicht an. ``session_uncertain`` steht in der
       Antwort, ist aber kein Kriterium der Anzeige (Entscheidung Markus) und
       wird hier nicht gelesen.
   Die Strategie waehlt ebenfalls nicht der Browser: die Route liefert je
   Kandidat ``strategy``, hier steht nur ihr Klartext.
*/
(function () {
  var block = document.getElementById('gap-scan');
  if (!block) return;

  var el = {
    day: document.getElementById('gscan-day'),
    run: document.getElementById('gscan-run'),
    state: document.getElementById('gscan-state'),
    error: document.getElementById('gscan-error'),
    counts: document.getElementById('gscan-counts'),
    stale: document.getElementById('gscan-stale'),
    empty: document.getElementById('gscan-empty'),
    wrap: document.getElementById('gscan-table-wrap'),
    thead: document.getElementById('gscan-thead'),
    tbody: document.getElementById('gscan-tbody')
  };
  if (!el.day || !el.run || !el.tbody) return;

  // Die eine Route dieses Abschnitts. Die Zahlen des Laufs (Schwelle,
  // Umsatzhuerde, Obergrenze) bleiben beim Server: ein Feld dafuer waere eine
  // zweite Wahrheit neben seinen Standardwerten.
  var API = '/api/v1/gap-scan';

  // Die Antwort traegt die Strategie-ID, die die Route selbst gewaehlt hat.
  // Hier steht nur, wie sie in Klartext heisst - "Bull Call" ist die Spalte,
  // der lange Name der Tooltip.
  var STRATEGY = {
    bull_call_debit: 'Bull Call',
    bear_put_debit: 'Bear Put'
  };
  var STRATEGY_LONG = {
    bull_call_debit: 'Bull Call Spread (Debit)',
    bear_put_debit: 'Bear Put Spread (Debit)'
  };
  var DIRECTION = { up: '↑', down: '↓' };
  var DAY_RE = /^\d{4}-\d{2}-\d{2}$/;
  // Der Kalender kennt den Tag, die Tagestabelle noch nicht: das ist kein
  // ruhiger Tag, sondern ein Zustand der QUELLE - und wird als solcher benannt.
  var UNPUBLISHED = 'Tagesschlusskurse für diesen Tag sind noch nicht '
    + 'veröffentlicht — bitte später oder am nächsten Tag erneut scannen.';

  var generation = 0;      // nur die neueste Anfrage darf zeichnen
  var timer = null;
  var current = null;      // {day, body, at}

  function n(value) { return typeof value === 'number' && isFinite(value) ? value : 0; }

  function pct(value) {
    if (typeof value !== 'number' || !isFinite(value)) return '–';
    return (value > 0 ? '+' : '') + value.toFixed(2) + ' %';
  }

  function money(value) {
    return typeof value === 'number' && isFinite(value) ? value.toFixed(2) : '–';
  }

  // Umsatz in USD, in der Groessenordnung gelesen: siebenstellige Zahlen in
  // einer schmalen Spalte sind keine Information, sondern eine Wand.
  function turnover(value) {
    if (typeof value !== 'number' || !isFinite(value)) return '–';
    if (value >= 1e9) return (value / 1e9).toFixed(2) + ' Mrd';
    if (value >= 1e6) return (value / 1e6).toFixed(1) + ' Mio';
    if (value >= 1e3) return (value / 1e3).toFixed(0) + ' Tsd';
    return value.toFixed(0);
  }

  function two(value) { return (value < 10 ? '0' : '') + value; }

  function clock(date) {
    return two(date.getHours()) + ':' + two(date.getMinutes()) + ':' + two(date.getSeconds());
  }

  function isoDay(date) {
    return date.getFullYear() + '-' + two(date.getMonth() + 1) + '-' + two(date.getDate());
  }

  // "gestern" ist der letzte KALENDERTAG vor heute, nicht "letzter Handelstag":
  // den kennt nur der Kalender, und der wird gefragt.
  function yesterday() {
    var d = new Date();
    d.setDate(d.getDate() - 1);
    return isoDay(d);
  }

  function stopTimer() {
    if (timer !== null) { clearInterval(timer); timer = null; }
  }

  // Kein Balken, keine erfundene Prozentzahl: die Route antwortet einmal am
  // Ende, also zeigt der Text die echten Sekunden seit dem Klick.
  function startTimer(day, startedAt, gen) {
    stopTimer();
    function paint() {
      var sekunden = Math.round((Date.now() - startedAt) / 1000);
      el.state.textContent = 'Scan läuft für ' + day + ' … ' + sekunden + ' s';
    }
    paint();
    timer = setInterval(function () {
      if (gen !== generation) { stopTimer(); return; }
      paint();
    }, 250);
  }

  // Die vier Zustaende aus dem Auftrag, jeder mit eigenem Satz: "kein
  // gueltiges Datum" ist kein leeres Ergebnis, "Kalender nicht verfuegbar"
  // keine ruhige Null.
  function messageFor(status, body) {
    var code = body && body.error;
    if (code === 'invalid_day') {
      return 'Kein gültiges Datum — bitte einen Reaktionstag im Format JJJJ-MM-TT angeben.';
    }
    if (code === 'calendar_unavailable') {
      // Der mitgelieferte Grund bleibt stehen: "nicht verfuegbar" allein sagt
      // nicht, ob die Quelle schwieg oder mit 403 geantwortet hat.
      var text = 'Kalender nicht verfügbar — '
        + (body.message || 'die Kalenderquelle hat nicht geantwortet.');
      if (body.source_code) {
        text += ' (' + body.source_code + (body.http_status ? ' ' + body.http_status : '') + ')';
      }
      return text;
    }
    if (body && body.message) return body.message;
    return 'HTTP ' + status;
  }

  function countsText(day, counts) {
    return 'Tag ' + day
      + ' · geprüft ' + n(counts.gaps_computed)
      + ' · über Schwelle ' + n(counts.over_threshold)
      + ' · über Umsatzhürde ' + n(counts.turnover_passed)
      + ' · angezeigt ' + n(counts.selected);
  }

  // "Kein Kandidat" nennt IMMER die Zahl der Fehlschlaege: ein Tag, an dem
  // dreissig Titel nicht messbar waren, ist kein ruhiger Tag.
  function emptyText(counts) {
    var teile = [];
    if (n(counts.candidate_rows) === 0) {
      teile.push('Der Kalender listet für diesen Tag keine Aktie.');
    } else if (n(counts.gaps_computed) === 0) {
      teile.push('Keine der ' + n(counts.candidate_rows) + ' gelisteten Aktien ließ sich messen.');
    } else {
      teile.push('Kein Titel hat Schwelle und Umsatzhürde bestanden.');
    }
    var fehler = n(counts.failures);
    if (fehler) {
      teile.push(fehler + (fehler === 1 ? ' Fehlschlag' : ' Fehlschläge') + ' beim Abruf der Tageskurse.');
    }
    return 'Kein Kandidat — ' + teile.join(' ');
  }

  function clearTable() {
    current = null;
    el.tbody.innerHTML = '';
    el.thead.innerHTML = '';
    el.wrap.hidden = true;
    el.counts.hidden = true;
    el.empty.hidden = true;
    el.stale.hidden = true;
    el.stale.textContent = '';
  }

  function scan(day) {
    if (!day || !DAY_RE.test(day)) {
      // Das Datumsfeld liefert "leer" statt eines Unsinns - dann wird nicht
      // angefragt, sondern benannt, was fehlt.
      clearTable();
      el.state.textContent = 'Kein Scan gelaufen';
      el.error.textContent = 'Kein gültiges Datum — bitte einen Reaktionstag im Format JJJJ-MM-TT angeben.';
      el.error.hidden = false;
      return;
    }
    var gen = generation + 1;
    generation = gen;
    var startedAt = Date.now();

    el.error.hidden = true;
    el.empty.hidden = true;
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
          clearTable();
          el.state.textContent = 'Scan fehlgeschlagen';
          el.error.textContent = messageFor(antwort.status, antwort.body);
          el.error.hidden = false;
          return;
        }
        if (!antwort.body || typeof antwort.body !== 'object') {
          clearTable();
          el.state.textContent = 'Scan fehlgeschlagen';
          el.error.textContent = 'Die Antwort war kein JSON.';
          el.error.hidden = false;
          return;
        }
        current = { day: day, body: antwort.body, at: new Date() };
        render();
      })
      .catch(function (err) {
        if (gen !== generation) return;
        stopTimer();
        el.run.disabled = false;
        clearTable();
        el.state.textContent = 'Scan fehlgeschlagen';
        el.error.textContent = 'Scan für ' + day + ' fehlgeschlagen: ' + err.message;
        el.error.hidden = false;
      });
  }

  function addTh(row, text, title) {
    var th = document.createElement('th');
    th.textContent = text;
    if (title) th.title = title;
    row.appendChild(th);
  }

  function buildHead() {
    var row = document.createElement('tr');
    addTh(row, '#', 'Rang nach Größe der Bewegung');
    addTh(row, 'Symbol');
    addTh(row, 'Gap %', 'Schluss des Reaktionstags gegen den Schluss davor');
    addTh(row, 'Richtung', '↑ Gap nach oben (Call) · ↓ Gap nach unten (Put)');
    addTh(row, 'Strategie', 'die zur Richtung passende Strategie der Route');
    addTh(row, 'Kurs', 'Schlusskurs des Reaktionstags');
    addTh(row, 'Umsatz', 'Schlusskurs × Volumen des Reaktionstags, in USD');
    addTh(row, 'Termin', 'Earnings-Datum des Titels');
    addTh(row, 'Öffnen', 'Ticker und Strategie ins Formular übernehmen und laden');
    el.thead.innerHTML = '';
    el.thead.appendChild(row);
  }

  function textCell(text, cls, title) {
    var td = document.createElement('td');
    td.textContent = text;
    if (cls) td.className = cls;
    if (title) td.title = title;
    return td;
  }

  // "Oeffnen" setzt Ticker und Strategie in das BESTEHENDE Formular und sendet
  // es ab - kein zweites Formular, keine neue Route. Die Strategie reist als
  // Feld mit, das der Server ohnehin parst.
  function openEntry(entry) {
    var form = document.getElementById('screener-form');
    if (!form || !entry || !entry.symbol) return;
    var symbol = String(entry.symbol).toUpperCase();
    var strategy = entry.strategy
      || (entry.direction === 'down' ? 'bear_put_debit' : 'bull_call_debit');

    var radios = form.querySelectorAll('input[name="strategy"]');
    for (var i = 0; i < radios.length; i += 1) {
      if (radios[i].value === strategy) { radios[i].checked = true; break; }
    }

    var feld = document.getElementById('ticker');
    if (feld) {
      feld.value = symbol;
      feld.dispatchEvent(new Event('input', { bubbles: true }));
    }
    if (typeof form.requestSubmit === 'function') form.requestSubmit();
    else form.submit();
  }

  function buildRow(entry, index) {
    var row = document.createElement('tr');
    row.setAttribute('data-symbol', entry.symbol || '');
    row.setAttribute('data-direction', entry.direction || '');

    row.appendChild(textCell(String(index + 1)));

    var tdSymbol = document.createElement('td');
    tdSymbol.textContent = entry.symbol || '–';
    if (entry.name) {
      // Name als Unterzeile, wie in der Earnings-Uebersicht. Die Session steht
      // hier absichtlich NICHT.
      var name = document.createElement('span');
      name.className = 'escan-sub';
      name.textContent = entry.name;
      tdSymbol.appendChild(name);
    }
    row.appendChild(tdSymbol);

    var richtung = DIRECTION[entry.direction] || '–';
    var strategie = STRATEGY[entry.strategy] || '';
    row.appendChild(textCell(pct(entry.gap_pct), '', 'Schluss gegen den Schluss davor'));
    row.appendChild(textCell(richtung, '', strategie || entry.direction || ''));
    row.appendChild(textCell(strategie || '–', '',
      STRATEGY_LONG[entry.strategy] || entry.strategy || 'keine Strategie zu dieser Richtung'));
    row.appendChild(textCell(money(entry.close), '', 'Schlusskurs des Reaktionstags'));
    row.appendChild(textCell(turnover(entry.turnover_usd), '',
      typeof entry.turnover_usd === 'number'
        ? Math.round(entry.turnover_usd) + ' USD Umsatz' : 'Umsatz unbekannt'));
    row.appendChild(textCell(entry.earnings_date || '–', '', 'Earnings-Datum des Titels'));

    var tdOpen = document.createElement('td');
    var button = document.createElement('button');
    button.type = 'button';
    button.className = 'gscan-btn';
    button.textContent = 'Öffnen';
    button.title = 'Einzelscan für ' + (entry.symbol || '') + ' öffnen'
      + (strategie ? ' — ' + (STRATEGY_LONG[entry.strategy] || strategie) : '');
    button.addEventListener('click', function () { openEntry(entry); });
    tdOpen.appendChild(button);
    row.appendChild(tdOpen);

    return row;
  }

  function render() {
    var body = current.body;
    var entries = Array.isArray(body.candidates) ? body.candidates : [];
    var counts = body.counts || {};

    buildHead();
    el.tbody.innerHTML = '';
    entries.forEach(function (entry, index) {
      el.tbody.appendChild(buildRow(entry, index));
    });

    el.counts.textContent = countsText(current.day, counts);
    el.counts.hidden = false;
    el.error.hidden = true;

    // Der Kalenderstand wird benannt, die Zeilen bleiben stehen: alte Zeilen
    // sind alt, nicht falsch.
    var veraltet = body.day_state === 'stale';
    el.stale.textContent = veraltet
      ? 'Kalenderstand veraltet — die Zeilen stammen aus einem alten Kalenderstand.'
      : '';
    el.stale.hidden = !veraltet;

    if (!entries.length) {
      el.wrap.hidden = true;
      // Ein Tag, dessen Tagesschlusskurse noch nicht veroeffentlicht sind, ist
      // NICHT "nichts hat sich bewegt": die Route sagt es (``result_state``),
      // und hier steht der Grund statt eines erfundenen ruhigen Tages.
      el.empty.textContent = body.result_state === 'history_not_published'
        ? UNPUBLISHED : emptyText(counts);
      el.empty.hidden = false;
    } else {
      el.empty.hidden = true;
      el.wrap.hidden = false;
    }

    el.state.textContent = 'Stand ' + clock(current.at) + ' · ' + entries.length
      + (entries.length === 1 ? ' Zeile' : ' Zeilen');
  }

  // Vorgabe ist gestern - der letzte abgeschlossene Kalendertag. Kein Lauf
  // beim Laden: gescannt wird auf Klick.
  if (!el.day.value) el.day.value = yesterday();

  el.run.addEventListener('click', function () { scan(el.day.value.trim()); });
  el.day.addEventListener('keydown', function (ev) {
    if (ev.key !== 'Enter') return;
    ev.preventDefault();
    scan(el.day.value.trim());
  });
})();
