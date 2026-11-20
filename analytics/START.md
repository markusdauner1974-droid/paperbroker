# Aktienanalyse öffnen

Die Oberfläche orientiert sich funktional an der TraderFox-Visualisierungsseite für einzelne Aktien. Die Aktienanalyse startet mit der Tickersuche. Das SAP-Archiv enthält eine dokumentierte Momentaufnahme vom 18. November 2026.

## Vorhandene Installation aktualisieren

Entpacke diese Version vollständig in den bisherigen Ordner und ersetze die vorhandenen Programmdateien. Für die NAS-Anbindung und den Abruf von Abschlüssen werden auch `nas_scanner.py`, `sec_statements.py` und `filing_statements.py` benötigt. Beende den bisherigen Server im Terminal mit Strg+C und starte die neue Version auf Port 8766.

```bash
python3 "$HOME/Downloads/analytics/app.py" --port 8766 --nas http://192.168.178.10:8089
```

Bei aktiver NAS-Anmeldung fragt das lokale Terminal nach dem vorhandenen Stock-Scanner-Passwort. Die Eingabe bleibt verborgen. Die Anmeldung erfolgt direkt beim angegebenen NAS. Das Passwort wird nicht gespeichert und die Sitzung bleibt nur im Prozessspeicher. Verwende die HTTP-Adresse im eigenen vertrauenswürdigen Heimnetz. Danach [Port 8766](http://127.0.0.1:8766/) öffnen und einen Ticker wie `AAPL` oder `SAP.DE` laden. Unter **NAS-Scanner** stehen die verfügbaren Werte mit Snapshot-Datum. Ohne `--nas` startet die reguläre Aktienanalyse.

## Auf Solus neu starten

Das ZIP-Archiv entpacken. Im entpackten Ordner ein Terminal öffnen. Einmalig eine virtuelle Umgebung und die Abhängigkeit einrichten.

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
```

Danach das Analyseprogramm starten.

```bash
.venv/bin/python app.py
```

Im Browser [http://127.0.0.1:8765](http://127.0.0.1:8765) öffnen. Der Server läuft nur lokal. Zum Beenden im Terminal Strg+C drücken.

## Aktien auswählen

- Über **Suchen** einen Firmennamen, Ticker oder eine ISIN auflösen und einen Treffer samt Börse auswählen.
- Über **Ticker laden** einen bekannten Börsenticker direkt öffnen, etwa `AAPL`, `SAP.DE` oder `NESN.SW`.
- **SAP-Archiv vom 18.11.2026** öffnet die historischen Referenzwerte. Das funktioniert offline durch direktes Öffnen von `index.html`.
- Vergleichsticker durch Kommas trennen. Für die benchmarkbereinigte Saisonalität etwa `^GDAXI` oder `^GSPC` eingeben.

## Daten und Unterschiede

Mit `--nas` verwendet die Anwendung zuerst den gespeicherten Bestand von xang. Die Quellen für letzten Tageskurs, Kurshistorie, Kennzahlen und Abschlüsse stehen über der Übersicht. Angaben ohne Datengrundlage erscheinen als Gedankenstrich. Renditen verwenden, sofern für den jeweiligen Vergleich durchgängig verfügbar, bereinigte Schlusskurse. Sie werden in der jeweiligen Notierungswährung berechnet.

Die Originalscores, das Vergleichsuniversum und der Morningstar-Datenbestand stehen für die SAP-Momentaufnahme als Referenz zur Verfügung, soweit Werte auf der Seite erfasst wurden. Die Anwendung hat keinen Morningstar-Datenfeed. Für frei ausgewählte Aktien zeigen die Checks einen eigenen, ausdrücklich gekennzeichneten Score. Die Schwellenwerte stehen bei den Einzelkriterien und bei fehlenden Kennzahlen bleibt der vollständige Nenner erhalten und die mögliche Punktespanne wird angezeigt. Eine Übereinstimmung mit den laufenden TraderFox-Scores, Marktperzentilen und Kursen ist damit offen. Piotroski wird bei vollständigen Eingabedaten aus den angezeigten Kriterien berechnet. TraderFox-Faktorscores, automatische Branchen-Peers, Marktlisten saisonaler Signale und die vollständigen Original-Scorekarten für alle Aktien sind nicht eingebunden.

Fair Value verwendet historische Multiplikatoren am Geschäftsjahresende. Für die Dividendenvariante werden die zwölf Monate vor jedem Stichtag betrachtet. Saisonalität mittelt vollständige Kalenderjahre ab 2010. Die stärksten und schwächsten Fenster sind jeweils 30 Kalendertage lang. Der Gewinnfluss verwendet die jüngste verfügbare jährliche Gewinn- und Verlustrechnung. Diese Methoden sind innerhalb der Anwendung beschriftet.

## Checks und Rangliste

Die vier Checks enthalten je 15 Kriterien. Eine fünfjährige Wachstumsrate setzt sechs Jahreswerte voraus. TTM-Wachstum benötigt acht aufeinanderfolgende Quartale. Fehlende Werte werden als Gedankenstrich gezeigt. Die Dividendenwertung setzt dokumentierte Zahlungen voraus und ist bei weniger als 2 % tatsächlich gezahlter Zwölfmonatsrendite ausgesetzt. Die xang-Anbieterrendite steht als eigene Kennzahl in der Übersicht.

Die farbigen Balken zeigen zunächst die Zielskala. Für einen relativen Vergleich unter dem Check **Vergleichsgruppe auswerten** anklicken. Die vorausgefüllte US- oder Deutschland-Liste ist eine kleine Auswahl und kann durch eigene Ticker ersetzt werden. Aktuelle Aktie, Ausfälle und nicht wertbare Aktien gehen nicht in den Nenner der Quote ein. Alle einbezogenen Titel werden anhand derselben mindestens acht Kriterien bewertet. Die grüne Leiste zeigt den Anteil mit mehr Punkten und öffnet per Klick die Liste. Beim Qualitätscheck werden für diese Gruppe die veröffentlichten TraderFox-Rangschwellen auf die verfügbaren Kennzahlen angewandt. Für KGV und KUV werden nur positive Werte gerankt und niedrigere Werte erhalten einen höheren Rang. Die übrigen drei Vergleiche verwenden die dokumentierten Schwellen der App. [Veröffentlichte Kriterien und Rangschwellen](https://rankings.traderfox.com/login/) Für die gemeinsamen Einzelkriterien wechseln die Balken auf Gruppenränge. Bei Gleichstand zählen die Vergleichstitel für den Einzelrang zur Hälfte.

Die Robustheit verwendet gemeinsame Handelstage mit der ausgewählten Benchmark. Für US-Ticker in USD nutzt der NAS-Modus SPY als S&P-500-ETF. Xetra-Titel verwenden standardmäßig den DAX. Renditen werden in der jeweiligen Notierungswährung verglichen. Eine geeignete Benchmark in derselben Währung lässt sich im Robustheits-Check auswählen.

## NAS-Scanner und historische Abschlüsse

| Anwendungsteil | Daten im NAS-Modus |
| --- | --- |
| Tickersuche | xang-Aktienuniversum. Yahoo Finance wird angefragt, wenn die Suche im NAS-Bestand keinen Treffer liefert. |
| Tageskurs und Kursverlauf | xang-Bestand mit bis zu rund fünf Jahren täglichen Schlusskursen. Die App prüft auffällige Sprünge mit einer Yahoo-Referenz in gleicher Währung und zum gleichen Stichtag. Bei einer bestätigten Abweichung nutzt die Auswertung splitbereinigte Referenzkurse. Ohne passende Referenz beginnt die Auswertung nach dem letzten ungeklärten Sprung. Bei einzelnen direkt geladenen Tickern ohne NAS-Kursreihe bringt Yahoo Finance die vollständige Ansicht. |
| Aktuelle Kennzahlen | xang-Cache mit unter anderem Börsenwert, KGV, KUV, EPS, Umsatz und Dividendenrendite, soweit vorhanden. Der ursprüngliche Anbieter kann etwa yfinance sein. |
| Vergleiche und Rangliste | xang-Kursreihen und verfügbare Kennzahlen per Sammelabruf. **NAS-Branche auswerten** übernimmt die Gruppenticker. **Abschlüsse der Vergleichstitel ergänzen** lädt die Berichte für die ausgewählte Gruppe. Die Rangliste berücksichtigt Titel mit gleicher Kursquelle und gleichem NAS-Stichtag. Bei aktivierter Abschlussergänzung müssen auch die Abschlussquellen übereinstimmen. |
| Scanner-Snapshot | Relative Stärke, Strategie-Scores, gleitende Durchschnitte, 52-Wochen-Spanne und Earnings-Termine mit Snapshot-Datum. |
| Geschäftsabschlüsse | Für den geöffneten NAS-Titel automatisch nachgeladen. Vorrang haben in USD gemeldete US-GAAP-Daten von [SEC EDGAR](https://www.sec.gov/search-filings/edgar-application-programming-interfaces). Bei fehlendem SEC-Datensatz und für lokale Börsenticker wie SAP.DE nutzt die App Yahoo Finance. Abschlüsse werden getrennt vom Kurs zwischengespeichert. |
| Längere Kurshistorie und Dividenden | Auf Klick **Längere Kurshistorie und Dividenden ergänzen**. Der Tageskurs bleibt aus xang. Die Yahoo-Reihe wird nur bis zum letzten NAS-Handelstag übernommen, wenn Kursdatum und bekannte Notierungswährung passen. |

Die Ansicht zeigt bis zu sechs Jahresperioden und acht Quartale. Datenlücken hängen von den verfügbaren Berichten und deren XBRL-Tags ab. Aus Jahres- und Quartalsberichten abgeleitete Q4-Beträge sind auf additive Größen wie Umsatz beschränkt. Fehlt verwässertes Q4-EPS bei der SEC in Company Facts, sucht die App in veröffentlichten 8-K-Ergebnisanhängen nach der passenden Quartalsspalte. Bei einem eindeutigen Treffer zeigt die Ansicht „Finanzen“ den Link zur Originalmeldung. Wenn SEC-Vergleichszahlen in mehreren Perioden einen Wechsel der Splitbasis belegen, rechnet der Importer ältere EPS-Werte und die zugehörigen Aktienzahlen auf dieselbe Basis um. SEC-Berichte werden nur verwendet, wenn ihr Einreichungsdatum spätestens am Kursstichtag liegt. Bei Yahoo wird das Periodenende geprüft. Über `AKTIENANALYSE_SEC_USER_AGENT` lässt sich der an die SEC übermittelte User-Agent mit einer Kontaktadresse konfigurieren.

Die Stock-Scanner-Abfragen greifen auf gespeicherte Daten zu. Bei abgelaufener NAS-Sitzung die Aktienanalyse neu starten und wieder anmelden. Ohne `--nas` verwendet die App Yahoo Finance.

### Europäische Originalberichte bei Bedarf

Falls du FinancialFilings verwenden möchtest, setze `AKTIENANALYSE_FILINGS_KEY` im Terminal des Servers. Gib den Schlüssel verdeckt ein und starte danach den Server im selben Terminal.

```bash
read -rsp 'FinancialFilings-API-Schlüssel: ' AKTIENANALYSE_FILINGS_KEY
export AKTIENANALYSE_FILINGS_KEY
```

Die App sucht beim Öffnen eines europäischen Tickers per ISIN oder eindeutigem Börsenticker nach dem Unternehmen und fragt dessen veröffentlichte Berichte ab. Sie liest die vom Emittenten ausgezeichneten IFRS-Zahlen aus den passenden Inline-XBRL-Originalen. Bei kompatiblen kumulierten Berichten kann sie Quartalswerte durch Differenzbildung ableiten. Unter **Finanzen** werden die Berichte verlinkt. Dokumente und gelesene Werte bleiben begrenzt im Arbeitsspeicher. Die App fragt die Berichtssuche ab. Die aufbereitete Kennzahlen-API des Anbieters wird nicht verwendet.

Die beiden API-Suchabfragen zählen zum Kontingent des Anbieters. Der Import greift bei zugeordnetem Unternehmen und passenden XBRL-Dokumenten. Reine PDFs und EPS ohne gesicherte Aktiengattung sind über diesen Import nicht auswertbar. Ohne Schlüssel oder passende Originaldaten bleibt Yahoo Finance als Ersatzquelle sichtbar. Der Einhell-spezifische PDF-Import wurde durch diesen allgemeinen Abruf ersetzt.

## NAS-Anbindung automatisch prüfen

Im Programmordner ausführen. Der Test arbeitet mit simulierten API-Antworten und braucht keine Internetverbindung.

```bash
python3 check_nas.py
```

## Apple automatisch prüfen

Bei laufender Aktienanalyse auf Port 8766 im Programmordner ausführen.

```bash
python3 check_parity.py
```

Der Test benötigt Python, Beautiful Soup (wird mit yfinance installiert) und Node.js. Er nimmt die Berechnungsfunktionen von der laufenden Seite, lädt Apple in USD mit der zugehörigen Benchmark und liest die vier sichtbaren Scores der öffentlichen TraderFox-Apple-Seite. Er vergleicht außerdem Umsatz, Betriebsergebnis, Gewinn und verwässertes EPS für das Geschäftsjahr 2026 mit Apples Geschäftsbericht. Im NAS-Modus kann `--mit-yahoo` die optionale Ergänzung für diesen Test abrufen. Ein Rückgabecode von 1 zeigt eine Abweichung oder fehlende Werte an. Code 2 meldet einen blockierten Abruf oder eine ungeeignete Referenzseite. Eine gespeicherte Referenzseite kann mit `--referenz-html DATEI` geprüft werden. Der Test vergleicht einzelne Ausgaben. Vollständige Modellgleichheit erfordert zusätzlich dieselben Datenstände, Regeln und Vergleichsuniversen.
