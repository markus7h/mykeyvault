FROM python:3.12-slim

# nodejs bleibt im Image — bw CLI braucht node zur Laufzeit
RUN apt-get update && apt-get install -y nodejs npm && \
    npm install -g @bitwarden/cli && \
    rm -rf /var/lib/apt/lists/* /root/.npm

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY main.py .

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
