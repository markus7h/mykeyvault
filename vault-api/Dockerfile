FROM python:3.12-slim

# nodejs bleibt im Image — bw CLI braucht node zur Laufzeit
RUN apt-get update && apt-get install -y nodejs npm && \
    # Version gepinnt: 2026.7.0 crasht beim Sync an SSH-Key-Items
    # (WASM: 'invalid type: JsValue(Object({...privateKey...})), expected a string'),
    # bw serve stirbt dadurch und vault-api liefert keine Secrets mehr.
    # Ungepinnt zieht jeder Rebuild die neueste CLI -- genau so ist es passiert.
    npm install -g @bitwarden/cli@2026.6.0 && \
    rm -rf /var/lib/apt/lists/* /root/.npm

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY main.py run.py ./

CMD ["python", "run.py"]
