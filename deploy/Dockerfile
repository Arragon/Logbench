# Optional deployment example, not required by the application.
# Pin the base-image digest in your own production image policy.
FROM python:3.12-slim
WORKDIR /app
COPY . /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
EXPOSE 50004
ENTRYPOINT ["python", "-B", "run.py"]
CMD ["--host", "0.0.0.0"]
