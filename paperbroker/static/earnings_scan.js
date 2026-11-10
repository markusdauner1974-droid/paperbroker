/* Earnings-Uebersicht - der gemeinsame Tages-Scan als Tabelle auf GET /.

   Warum eine eigene Datei: der Block sind rund 500 Zeilen Verhalten (Filter
   uebernehmen, Zustaende benennen, spaete Antworten verwerfen). Inline in
   screen.html waere er zwischen den beiden bestehenden Skripten nicht mehr zu
   lesen, und die Seite traegt ihren Text - dieser Block traegt Verhalten.
   Kein Framework, kein Build-Schritt: eine Datei, die der Browser so nimmt.

   Die Konfiguration steht am Abschnitt selbst (data-api, data-fields,
   data-strategies); der Server fuellt sie, damit Felder und Zustaende nicht in
   zwei Sprachen gepflegt werden muessen.
*/

// Earnings-Uebersicht: der gemeinsame Tages-Scan (GET /api/v1/earnings-scan)
// als Tabelle unter dem Kalender.
//
// Drei Dinge tut dieser Block bewusst NICHT:
//  * er erfindet keinen Zustand - gezeigt wird, was die Route geantwortet hat;
//  * er schaetzt keinen Fortschritt - die Route antwortet einmal am Ende, also
//    laeuft der Balken unbestimmt und die Abdeckung erscheint mit der Antwort;
//  * er laesst keine alte Antwort ein neueres Bild ueberschreiben - jede
//    Anfrage traegt eine laufende Nummer, nur die neueste darf zeichnen.
(function () {
  var block = document.getElementById('earnings-scan');
  if (!block) return;
  var form = document.getElementById('screener-form');
  if (!form) return;

  // Route, Feldnamen und Strategiekoepfe kommen aus EINEM JSON-Block des
  // Servers. Die Feldnamen sind dieselben, die die Route parst - eine zweite
  // Liste hier wuerde bei jedem neuen Filter still veralten (und die Route
  // wuerde die Anfrage dann als unbekannten Parameter abweisen).
  var config = {};
  var configEl = document.getElementById('escan-config');
  try { config = JSON.parse(configEl ? configEl.textContent : '{}') || {}; }
  catch (err) { config = {}; }

  var API = config.api || '/api/v1/earnings-scan';
  var FIELDS = Array.isArray(config.fields) ? config.fields.slice() : [];
  var STRATEGIES = Array.isArray(config.strategies) ? config.strategies.slice() : [];
  if (!STRATEGIES.length || !FIELDS.length) return;

  var el = {
    state: document.getElementById('escan-state'),
    progress: document.getElementById('escan-progress'),
    progressText: document.getElementById('escan-progress-text'),
    error: document.getElementById('escan-error'),
    coverage: document.getElementById('escan-coverage'),
    controls: document.getElementById('escan-controls'),
    onlyHits: document.getElementById('escan-only-hits'),
    stale: document.getElementById('escan-stale'),
    empty: document.getElementById('escan-empty'),
    wrap: document.getElementById('escan-table-wrap'),
    thead: document.getElementById('escan-thead'),
    tbody: document.getElementById('escan-tbody')
  };

  // Die Zustaende der Route in EINER Tabelle, mit eigenem Text und eigener
  // Farbe: "nicht geprueft" sieht nicht aus wie "null Treffer", "Daten zu alt"
  // nicht wie "keine passenden Spreads".
  var STATUS = {
    hits: { text: 'Treffer', cls: 'st-hits' },
    no_match: { text: 'keine passenden Spreads', cls: 'st-none' },
    stale_data: { text: 'Daten zu alt', cls: 'st-stale' },
    incomplete_data: { text: 'Daten unvollständig', cls: 'st-warn' },
    fetch_failed: { text: 'Abruffehler', cls: 'st-failed' },
    not_checked: { text: 'nicht geprüft', cls: 'st-unchecked' },
    window_blocked: { text: 'Fenster verhindert', cls: 'st-blocked' },
    window_unknown: { text: 'Fenster unklar', cls: 'st-warn' }
  };
  var FRESHNESS = {
    fresh: 'Kurse frisch', partial: 'teils veraltet', stale: 'Kurse zu alt',
    unchecked: 'Alter ungeprüft', unknown: 'Kurse unbekannt'
  };
  var COMPLETENESS = {
    complete: 'Kette vollständig', partial: 'Kette unvollständig',
    unknown: 'Kette leer/unbekannt'
  };

  var generation = 0;      // only the newest request may draw
  var timer = null;
  var current = null;      // {day, params, body, at} - what the table shows

  function n(value) { return typeof value === 'number' && isFinite(value) ? value : 0; }

  function pct(value) {
    return typeof value === 'number' && isFinite(value) ? value.toFixed(2) + ' %' : '–';
  }

  function money(value) {
    return typeof value === 'number' && isFinite(value) ? value.toFixed(2) : '–';
  }

  function clock(date) {
    function two(v) { return (v < 10 ? '0' : '') + v; }
    return two(date.getHours()) + ':' + two(date.getMinutes()) + ':' + two(date.getSeconds());
  }

  function specOf(id) {
    for (var i = 0; i < STRATEGIES.length; i += 1) {
      if (STRATEGIES[i].id === id) return STRATEGIES[i];
    }
    return null;
  }

  function activeSpecs() {
    var ids = (current && current.body && current.body.strategies) || [];
    var out = ids.map(specOf).filter(Boolean);
    return out.length ? out : STRATEGIES.slice();
  }

  // Die Filter des Formulars, so wie die Einzelansicht sie liest: leere Felder
  // sind "Standard" (der Server behaelt seinen Wert), nicht "null".
  function collect() {
    var out = new URLSearchParams();
    FIELDS.forEach(function (name) {
      var nodes = form.querySelectorAll('[name="' + name + '"]');
      Array.prototype.forEach.call(nodes, function (node) {
        if (node.disabled) return;
        var typ = (node.type || '').toLowerCase();
        if ((typ === 'radio' || typ === 'checkbox') && !node.checked) return;
        if (node.value === '') return;
        out.append(name, node.value);
      });
    });
    return out;
  }

  function setStale(on, text) {
    if (!on) {
      block.classList.remove('is-stale');
      el.stale.hidden = true;
      el.stale.textContent = '';
      return;
    }
    block.classList.add('is-stale');
    el.stale.textContent = text || ('Filter geändert — die Ergebnisse von '
      + (current ? clock(current.at) : '') + ' sind nicht mehr aktuell.');
    el.stale.hidden = false;
  }

  function stopTimer() {
    if (timer !== null) { clearInterval(timer); timer = null; }
  }

  function startProgress(day, startedAt, gen) {
    stopTimer();
    el.progress.hidden = false;
    function paint() {
      var sekunden = Math.round((Date.now() - startedAt) / 1000);
      el.progressText.textContent = 'Scan läuft für ' + day + ' … ' + sekunden
        + ' s · Abdeckung erscheint am Ende';
    }
    paint();
    timer = setInterval(function () {
      if (gen !== generation) { stopTimer(); return; }
      paint();
    }, 250);
  }

  function errorText(body, status) {
    if (body && body.error && body.error.message) {
      return body.error.message + (body.error.code ? ' (' + body.error.code + ')' : '');
    }
    return 'HTTP ' + status;
  }

  function scan(day) {
    if (!day) return;
    var gen = generation + 1;
    generation = gen;
    var params = collect();
    var url = API + '?day=' + encodeURIComponent(day)
      + (params.toString() ? '&' + params.toString() : '');
    var startedAt = Date.now();

    el.error.hidden = true;
    el.empty.hidden = true;
    el.state.textContent = 'Scan läuft für ' + day;
    // Die sichtbaren Zeilen stammen aus einem älteren Lauf: sie werden
    // ersetzt, sind aber bis zur Antwort ausdruecklich als alt markiert.
    if (current) {
      setStale(true, 'Neuer Scan läuft — die sichtbaren Ergebnisse von '
        + clock(current.at) + ' werden gerade ersetzt.');
    }
    startProgress(day, startedAt, gen);

    fetch(url, { headers: { Accept: 'application/json' }, cache: 'no-store' })
      .then(function (res) {
        return res.text().then(function (text) {
          var body = null;
          try { body = JSON.parse(text); } catch (err) { body = null; }
          if (!res.ok) throw new Error(errorText(body, res.status));
          if (!body || typeof body !== 'object') {
            throw new Error('Die Antwort war kein JSON.');
          }
          return body;
        });
      })
      .then(function (body) {
        if (gen !== generation) return;   // a newer scan owns the panel
        stopTimer();
        el.progress.hidden = true;
        current = { day: day, params: params, body: body, at: new Date() };
        render();
      })
      .catch(function (err) {
        if (gen !== generation) return;
        stopTimer();
        el.progress.hidden = true;
        el.state.textContent = 'Scan fehlgeschlagen';
        el.error.textContent = 'Scan für ' + day + ' fehlgeschlagen: ' + err.message;
        el.error.hidden = false;
        if (current) {
          setStale(true, 'Der Scan ist fehlgeschlagen — die sichtbaren Ergebnisse von '
            + clock(current.at) + ' sind älter.');
        }
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
    addTh(row, 'Aktie');
    addTh(row, 'Status');
    activeSpecs().forEach(function (spec) {
      addTh(row, spec.short, spec.label + ' — Trefferzahl dieser Strategie');
    });
    addTh(row, 'Bester Verfall',
      'Verfall des besten Treffers (höchste Rendite, wie die Spaltensortierung der Einzelsicht)');
    addTh(row, 'Max. Verlust',
      'max. Verlust des besten Treffers, je Einheit und vor Kosten');
    addTh(row, 'Rendite',
      'max. Gewinn / max. Verlust des besten Treffers, je Einheit und vor Kosten');
    addTh(row, 'Daten', 'Kursalter und Vollständigkeit der gelesenen Kette');
    el.thead.innerHTML = '';
    el.thead.appendChild(row);
  }

  function bestOf(entry) {
    var best = null;
    activeSpecs().forEach(function (spec) {
      var s = (entry.strategies || {})[spec.id];
      if (!s || !s.best) return;
      if (best === null) { best = { pair: s.best, spec: spec }; return; }
      var alt = typeof best.pair.return_pct === 'number' ? best.pair.return_pct : -Infinity;
      var neu = typeof s.best.return_pct === 'number' ? s.best.return_pct : -Infinity;
      if (neu > alt) best = { pair: s.best, spec: spec };
    });
    return best;
  }

  function singleUrl(symbol, strategyId, expiration) {
    var params = new URLSearchParams(current && current.params ? current.params.toString() : '');
    params.set('ticker', symbol);
    params.set('strategy', strategyId);
    if (expiration) params.set('expiration', expiration);
    return '/?' + params.toString() + '#screener-form';
  }

  function buildRow(entry) {
    var row = document.createElement('tr');
    var hits = 0;
    activeSpecs().forEach(function (spec) {
      var s = (entry.strategies || {})[spec.id];
      if (s && n(s.hits) > 0) hits += n(s.hits);
    });
    row.setAttribute('data-hits', String(hits));
    row.setAttribute('data-status', entry.status || '');

    var td = document.createElement('td');
    td.textContent = entry.symbol || '–';
    if (entry.name) {
      var name = document.createElement('span');
      name.className = 'escan-sub';
      name.textContent = entry.name;
      td.appendChild(name);
    }
    row.appendChild(td);

    var zustand = STATUS[entry.status] || { text: entry.status || 'unbekannt', cls: 'st-none' };
    var tdStatus = document.createElement('td');
    var span = document.createElement('span');
    span.className = zustand.cls;
    span.textContent = zustand.text;
    tdStatus.appendChild(span);
    if (entry.exclusion) {
      tdStatus.title = (entry.exclusion.code || '') + ': ' + (entry.exclusion.message || '');
    }
    row.appendChild(tdStatus);

    activeSpecs().forEach(function (spec) {
      var zelle = document.createElement('td');
      var s = (entry.strategies || {})[spec.id];
      if (!s) {
        zelle.textContent = '–';
        zelle.title = 'Strategie wurde nicht gescannt';
      } else if (n(s.hits) > 0) {
        var link = document.createElement('a');
        link.className = 'hit';
        link.href = singleUrl(entry.symbol, spec.id, s.best && s.best.expiration);
        link.textContent = String(n(s.hits));
        link.title = 'Einzelscan: ' + entry.symbol + ' · ' + spec.label
          + (s.best && s.best.expiration ? ' · Verfall ' + s.best.expiration : '');
        zelle.appendChild(link);
      } else if (s.status === 'no_match') {
        zelle.textContent = '0';
        zelle.title = 'geprüft — kein Spread hat die Filter bestanden';
      } else if (s.status === 'stale_data') {
        zelle.textContent = '–';
        zelle.className = 'st-stale';
        zelle.title = 'Daten zu alt — nicht als 0 Treffer gewertet';
      } else if (s.status === 'incomplete_data') {
        zelle.textContent = '0';
        zelle.className = 'st-warn';
        zelle.title = 'Kette nur teilweise lesbar — 0 Treffer in den lesbaren Teilen';
      } else {
        var info = STATUS[s.status] || STATUS.not_checked;
        zelle.textContent = '–';
        zelle.className = info.cls;
        zelle.title = info.text + ' — keine Aussage über Treffer';
      }
      row.appendChild(zelle);
    });

    var best = bestOf(entry);
    var tdExp = document.createElement('td');
    if (best) {
      tdExp.textContent = best.pair.expiration || '–';
      var kurz = document.createElement('span');
      kurz.className = 'escan-sub';
      kurz.textContent = best.spec.short;
      tdExp.appendChild(kurz);
    } else {
      tdExp.textContent = '–';
    }
    row.appendChild(tdExp);

    var tdLoss = document.createElement('td');
    tdLoss.textContent = best ? money(best.pair.max_loss) : '–';
    row.appendChild(tdLoss);

    var tdReturn = document.createElement('td');
    tdReturn.textContent = best ? pct(best.pair.return_pct) : '–';
    row.appendChild(tdReturn);

    var tdData = document.createElement('td');
    var daten = entry.data;
    if (!daten) {
      tdData.textContent = 'keine Daten';
      tdData.className = 'st-unchecked';
      tdData.title = 'Für diese Aktie wurde keine Kette gelesen';
    } else {
      var teile = [];
      if (daten.freshness) teile.push(FRESHNESS[daten.freshness] || daten.freshness);
      if (daten.completeness) teile.push(COMPLETENESS[daten.completeness] || daten.completeness);
      if (typeof daten.chain_contracts === 'number') {
        teile.push(daten.chain_contracts + ' Kontrakte');
      }
      tdData.textContent = teile.join(' · ');
      if (typeof daten.chain_quote_age_minutes === 'number') {
        tdData.title = 'neuester Kurs ' + daten.chain_quote_age_minutes + ' min alt';
      }
    }
    row.appendChild(tdData);
    return row;
  }

  function coverageText(day, cov, counts) {
    var geprueft = n(counts.hits) + n(counts.no_match) + n(counts.stale_data)
      + n(counts.incomplete_data);
    var fehler = n(counts.fetch_failed);
    var ungeprueft = n(counts.not_checked);
    var fenster = n(counts.window_blocked) + n(counts.window_unknown);
    var teile = [
      'Tag ' + day,
      'gelistet ' + n(cov.day_rows),
      'mit Optionen ' + n(cov.optionable_rows),
      'ausgewählt ' + n(cov.selected),
      'geprüft ' + geprueft,
      'Abruffehler ' + fehler
    ];
    if (ungeprueft) teile.push('nicht geprüft ' + ungeprueft);
    if (fenster) teile.push('Fenster verhindert ' + fenster);
    if (n(cov.without_options)) teile.push('ohne Optionen ' + n(cov.without_options));
    if (typeof cov.elapsed_seconds === 'number') {
      teile.push('Dauer ' + cov.elapsed_seconds.toFixed(1) + ' s');
    }
    var text = teile.join(' · ');
    var gruende = (cov.truncation_reasons || []).map(function (grund) {
      return grund.message || grund.code || '';
    }).filter(Boolean);
    if (cov.truncated && gruende.length) {
      text += ' · unvollständig: ' + gruende.join('; ');
    } else if (cov.complete === false && !ungeprueft) {
      text += ' · unvollständig';
    }
    return text;
  }

  function emptyText(body) {
    var cov = body.coverage || {};
    if (n(cov.day_rows) === 0) {
      return 'Der Tag listet nichts in der Quelle — es gibt nichts zu scannen.';
    }
    if (n(cov.selected) === 0) {
      return 'Der Tag listet ' + n(cov.day_rows) + ' Aktien, aber keine hat gelistete '
        + 'Optionen — gescannt wurde nichts.';
    }
    return 'Der Scan hat keine Aktie geprüft — die Abdeckung oben nennt den Grund.';
  }

  function applyFilter() {
    if (!current) return;
    var nurTreffer = !!(el.onlyHits && el.onlyHits.checked);
    var zeilen = el.tbody.querySelectorAll('tr');
    var sichtbar = 0;
    Array.prototype.forEach.call(zeilen, function (row) {
      var verstecken = nurTreffer && Number(row.getAttribute('data-hits')) === 0;
      row.hidden = verstecken;
      if (!verstecken) sichtbar += 1;
    });
    if (!zeilen.length) {
      el.wrap.hidden = true;
      el.empty.textContent = emptyText(current.body);
      el.empty.hidden = false;
      return;
    }
    if (!sichtbar) {
      var counts = current.body.counts || {};
      var ohneDaten = n(counts.fetch_failed) + n(counts.not_checked);
      el.empty.textContent = 'Keine der ' + zeilen.length + ' gescannten Aktien hat einen Treffer'
        + (ohneDaten ? ' (davon ' + ohneDaten + ' ohne brauchbare Daten — siehe Status)' : '') + '.';
      el.empty.hidden = false;
      el.wrap.hidden = true;
      return;
    }
    el.empty.hidden = true;
    el.wrap.hidden = false;
  }

  function render() {
    var body = current.body;
    var entries = Array.isArray(body.entries) ? body.entries : [];
    buildHead();
    el.tbody.innerHTML = '';
    entries.forEach(function (entry) { el.tbody.appendChild(buildRow(entry)); });
    el.coverage.textContent = coverageText(current.day, body.coverage || {}, body.counts || {});
    el.coverage.hidden = false;
    el.controls.hidden = false;
    el.error.hidden = true;
    setStale(false);
    var namen = activeSpecs().map(function (spec) { return spec.short; }).join(', ');
    el.state.textContent = 'Stand ' + clock(current.at) + ' · ' + entries.length
      + ' Zeilen · ' + namen;
    applyFilter();
  }

  Array.prototype.forEach.call(document.querySelectorAll('[data-scan-day]'), function (button) {
    button.addEventListener('click', function () {
      scan(button.getAttribute('data-scan-day'));
    });
  });

  // Jede Aenderung an den Filtern macht die sichtbaren Zahlen alt - sie bleiben
  // stehen (samt ihren Links), sind aber als nicht mehr aktuell markiert.
  ['input', 'change'].forEach(function (name) {
    form.addEventListener(name, function () { if (current) setStale(true); });
  });

  if (el.onlyHits) el.onlyHits.addEventListener('change', applyFilter);
})();
