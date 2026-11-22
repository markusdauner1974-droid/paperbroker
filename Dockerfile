# paperbroker options screener - QNAP deployment (Phase 5)
# python:3.13-slim + pip (judge decision: no uv second tooling path, no
# multi-stage complexity for a LAN service). arrow, requests, flask, and since the
# stock analysis runs in this process: yfinance and beautifulsoup4.
FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY paperbroker ./paperbroker
COPY setup.py ./
# The stock analysis lives in analytics/ and runs in this same process
# (paperbroker/routes/quality.py resolves it next to the package). Without this
# line the image builds, the healthcheck stays green, and /qualitaet/ answers 500
# with FileNotFoundError.
COPY analytics ./analytics

# non-root (CWE-250): app user owns /app/.cache so the named volume
# mounted there inherits usable ownership
RUN useradd --system --create-home --uid 10001 screener \
    && mkdir -p /app/.cache \
    && chown -R screener:screener /app
USER screener

# app binds on all interfaces INSIDE the container namespace - exposure is
# controlled solely by the published port mapping (127.0.0.1:8090:8089 in
# the stack file), never by a wide-open 0.0.0.0 host binding
EXPOSE 8089

# liveness only: /healthz never calls CBOE/xang (judge decision) - a
# coupled check would mark the container unhealthy whenever an external
# service is down, which breaks restart semantics
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8089/healthz', timeout=3)" || exit 1

# A real WSGI server instead of `flask run`, which calls itself a development server on every
# start. One worker with threads: the process keeps the scanner connection, the listing cache and
# the ISIN store in memory, so several workers would multiply the requests to Yahoo and Wikidata
# instead of sharing them. The threads cover the waiting: a search or a statement load blocks only
# its own request.
#
# Eight threads against a gate of six (analytics/app.py, FACT_WORKERS): the gate is the real bound
# on outbound lookups, and two threads stay free for requests that are not waiting on Yahoo. With
# four threads a single stalled lookup could have taken the whole server - `flask run` was threaded
# too, by default (flask/app.py:655) and without a ceiling, so nothing here is narrower than before.
CMD ["gunicorn", "--worker-class", "gthread", "--workers", "1", "--threads", "8", \
     "--timeout", "120", "--graceful-timeout", "30", "--access-logfile", "-", \
     "--bind", "0.0.0.0:8089", "paperbroker.server:create_app()"]