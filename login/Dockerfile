FROM python:3.13-slim

WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY aiphidias_login ./aiphidias_login

RUN useradd --system --uid 10002 --home /srv login
USER login

EXPOSE 3000
CMD ["uvicorn", "aiphidias_login.main:app", "--host", "0.0.0.0", "--port", "3000"]
