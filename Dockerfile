# LTM GCC dashboard as a container (used for the Hugging Face demo; works on any Docker host).
# Serves the dashboard on port 7860. Configure with environment variables / platform secrets, never a baked-in .env:
#   DASHBOARD_PASSWORD (required when served publicly), DEMO_MODE, DEMO_EMAIL_DOMAINS, DEMO_DAILY_ANALYSES,
#   OPENAI_API_KEY, SERPER_API_KEY (for "Analyse for LTM"), GMAIL_USER, GMAIL_APP_PASSWORD (for emailing reports).
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

WORKDIR /app

# Python packages, then Chromium (renders the PDF report) with its system libraries and fonts.
COPY requirements.txt .
RUN pip install -r requirements.txt \
 && python -m playwright install --with-deps chromium \
 && rm -rf /var/lib/apt/lists/*

# Hugging Face runs containers as user 1000.
RUN useradd -m -u 1000 user && mkdir -p /app/data && chown -R user /app
COPY --chown=user . /app
USER user

EXPOSE 7860
CMD ["python", "main.py", "--dashboard", "--host", "0.0.0.0", "--port", "7860"]
