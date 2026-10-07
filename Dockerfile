# paperbroker options screener - QNAP deployment (Phase 5)
# python:3.13-slim + pip (3 deps: arrow, requests, flask - judge decision:
# no uv second tooling path, no multi-stage complexity for a LAN service)
FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY paperbroker ./paperbroker
COPY setup.py ./

# app binds on all interfaces INSIDE the container namespace - exposure is
# controlled solely by the published port mapping (127.0.0.1:8090:8089 in
# the stack file), never by a wide-open 0.0.0.0 host binding
EXPOSE 8089

# liveness only: /healthz never calls CBOE/xang (judge decision) - a
# coupled check would mark the container unhealthy whenever an external
# service is down, which breaks restart semantics
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8089/healthz', timeout=3)" || exit 1

CMD ["python", "-m", "flask", "--app", "paperbroker.server:create_app()", "run", "--host", "0.0.0.0", "--port", "8089"]