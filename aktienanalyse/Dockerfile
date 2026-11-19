# Der Baustein "Aktienanalyse" als eigener Container.
#
# Er laeuft NICHT an der Oberflaeche: erreichbar ist er nur im internen Netz des
# Stapels unter http://aktienanalyse:8766, von dort leitet das Dashboard
# /qualitaet/* weiter. Deshalb --host 0.0.0.0 (im Container ist die Schleife
# sonst nur von innen erreichbar) und der Pfadanfang /qualitaet, unter dem der
# Baustein seine eigenen Abfragen baut.
#
# Kein Geheimnis im Abbild: das NAS-Passwort kommt zur Laufzeit aus
# /run/secrets/nas_password oder AKTIENANALYSE_NAS_PASSWORD.

FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Erst die Abhaengigkeiten: eine Aenderung an der Oberflaeche soll nicht jedes
# Mal yfinance und pandas neu installieren.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8766

# Die Gesundheit ist die Seite selbst - nicht der Port. Ein Prozess, der laeuft,
# aber keine Seite liefert, ist nicht gesund.

HEALTHCHECK --interval=30s --timeout=6s --start-period=25s --retries=3 \
    CMD python -c "import sys,urllib.request; \
sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8766/qualitaet/', timeout=5).status == 200 else 1)"

# Die NAS-Adresse und das Passwort kommen aus der Umgebung (AKTIENANALYSE_NAS_URL,
# AKTIENANALYSE_NAS_PASSWORD bzw. der Mount). app.py liest sie selbst - deshalb
# steht hier kein Geheimnis und keine Adresse fest.
CMD ["python", "app.py", "--port", "8766", "--host", "0.0.0.0", "--base-path", "/qualitaet"]
