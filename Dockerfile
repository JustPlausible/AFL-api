ARG PLAYWRIGHT_VERSION=1.61.0
FROM mcr.microsoft.com/playwright/python:v${PLAYWRIGHT_VERSION}-noble

ARG PLAYWRIGHT_VERSION
ENV PLAYWRIGHT_VERSION=${PLAYWRIGHT_VERSION}

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && python -c "import importlib.metadata, os; installed = importlib.metadata.version('playwright'); expected = os.environ['PLAYWRIGHT_VERSION']; assert installed == expected, f'playwright package {installed} != Docker image {expected}'"

COPY . .

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
