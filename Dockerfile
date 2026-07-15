FROM mcr.microsoft.com/playwright/python:v1.42.0-jammy

WORKDIR /app

# tini como init (PID 1) recolhe processos zumbi do Chromium.
# Sem isso, os subprocessos orfaos do navegador acumulam e esgotam a
# tabela de PIDs do container -> [Errno 11] Resource temporarily unavailable.
RUN apt-get update && apt-get install -y tini && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install -r requirements.txt
RUN playwright install chromium

COPY . .

EXPOSE 8000

ENTRYPOINT ["tini", "--"]
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
