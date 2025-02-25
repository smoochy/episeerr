FROM python:3.9-slim

WORKDIR /app

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application
COPY episeerr.py .
COPY .env.example .env.example

# Create logs directory
RUN mkdir -p logs

# Set proper permissions
RUN chmod +x episeerr.py

# Expose port
EXPOSE 5000

# Run application
CMD ["python", "episeerr.py"]
