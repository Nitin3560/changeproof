FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml README.md ./
COPY changeproof ./changeproof
COPY examples ./examples
RUN pip install --no-cache-dir '.[benchmark]'
COPY configs ./configs
CMD ["uvicorn", "examples.payment_service.vulnerable.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
