FROM python:3.11.12-slim-bookworm

WORKDIR /app

COPY manager/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt

COPY . /app

ENV PYTHONPATH=/app
EXPOSE 8000

CMD ["uvicorn", "manager.app.main:app", "--host", "0.0.0.0", "--port", "8000"]
