FROM python:3.12.11-slim-bookworm
WORKDIR /app
COPY pyproject.toml agent.py dispatcher.py providers.py rails.py ./
RUN pip install --no-cache-dir .
RUN addgroup --system agent && adduser --system --ingroup agent --no-create-home agent && chown -R agent:agent /app
USER agent
CMD ["python", "agent.py", "start"]
