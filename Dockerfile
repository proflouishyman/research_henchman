FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /srv/app

COPY requirements.txt ./requirements.txt
RUN pip install -r ./requirements.txt

COPY . .

EXPOSE 8876

# Run directly from repository root module path.
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8876"]
