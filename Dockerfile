FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    INVOICE_DATA_DIR=/app/var \
    INVOICE_SYNTHETIC=1 \
    INVOICE_PROVIDER=simulated \
    INVOICE_PROFILE_DIR=/app/config/invoice_profiles \
    OMP_NUM_THREADS=1 \
    OPENBLAS_NUM_THREADS=1 \
    MKL_NUM_THREADS=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends texlive-latex-base texlive-latex-recommended texlive-latex-extra texlive-fonts-recommended texlive-fonts-extra texlive-lang-greek tex-gyre poppler-utils \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 invoice \
    && useradd --uid 10001 --gid invoice --create-home invoice

WORKDIR /app
COPY pyproject.toml /app/
COPY requirements.lock.txt /app/
COPY src /app/src
COPY config /app/config
RUN pip install -r requirements.lock.txt && pip install --no-deps . && mkdir -p /app/var && chown -R invoice:invoice /app/var

USER invoice
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3)" || exit 1
CMD ["python", "-m", "invoice_optimizer", "serve", "--host", "0.0.0.0", "--port", "8000"]
