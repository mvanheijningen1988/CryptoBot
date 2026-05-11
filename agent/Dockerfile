FROM python:3.11.12-slim-bookworm

WORKDIR /app

COPY agent/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt

COPY . /app

ENV PYTHONPATH=/app
EXPOSE 8100

CMD ["uvicorn", "agent.app.main:app", "--host", "0.0.0.0", "--port", "8100"]
