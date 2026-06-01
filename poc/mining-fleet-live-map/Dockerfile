FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY kafka_state.py live_map_server.py ./
COPY static/ static/

ENV LIVE_MAP_PORT=8080
EXPOSE 8080

CMD ["python", "live_map_server.py"]
