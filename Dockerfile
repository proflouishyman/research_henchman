FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /srv

COPY requirements.txt /srv/app/requirements.txt
RUN pip install -r /srv/app/requirements.txt

COPY . /srv/app

EXPOSE 8876

# Keep package import path stable (`app.main:app`).
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8876"]

