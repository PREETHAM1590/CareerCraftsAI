FROM python:3.12-slim
WORKDIR /app
COPY relay-requirements.lock .
RUN pip install --no-cache-dir --require-hashes -r relay-requirements.lock
COPY relay.py .
CMD ["uvicorn", "relay:app", "--host", "0.0.0.0", "--port", "4302", "--no-access-log"]
