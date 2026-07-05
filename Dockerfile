FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# App source and UI. settings/, inputs/, outputs/ are volume-mounted at runtime.
COPY src ./src
COPY ui ./ui

# Default entrypoint = CLI. The web service overrides it via docker-compose.
ENTRYPOINT ["python", "-m", "src.driftcheck"]
CMD ["run"]
