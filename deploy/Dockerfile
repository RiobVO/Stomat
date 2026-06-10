# Сборка из корня репо: docker build -f deploy/Dockerfile .
FROM python:3.12-slim

WORKDIR /app

# editable-установка обязательна: код резолвит spike_nlu относительно
# исходного дерева (telegram/app.py: parents[3]/spike_nlu) — обычная
# установка в site-packages ломает путь к фикстурам и промпту.
COPY pyproject.toml ./
COPY src ./src
RUN pip install --no-cache-dir -e .[llm]

COPY alembic.ini ./
COPY migrations ./migrations
COPY spike_nlu/prompts ./spike_nlu/prompts
COPY spike_nlu/data/messages.jsonl ./spike_nlu/data/messages.jsonl
COPY deploy/entrypoint.sh /entrypoint.sh

RUN useradd --create-home navbat && chmod +x /entrypoint.sh
USER navbat

ENTRYPOINT ["/entrypoint.sh"]
