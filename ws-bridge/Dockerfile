FROM public.ecr.aws/docker/library/python:3.13-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy the entire ws_bridge package
COPY core/ ws_bridge/core/
COPY adapters/ ws_bridge/adapters/
COPY manager.py ws_bridge/manager.py
COPY health.py ws_bridge/health.py
COPY main.py ws_bridge/main.py

# Create __init__.py for the ws_bridge package
RUN touch ws_bridge/__init__.py

EXPOSE 8080

CMD ["python", "-m", "ws_bridge.main"]
