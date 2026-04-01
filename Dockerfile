FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY id3_organiser.py .
COPY templates/ templates/
COPY enrichment/ enrichment/

EXPOSE 5000

CMD ["python", "id3_organiser.py", "--no-browser"]
