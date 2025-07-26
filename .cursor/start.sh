#!/bin/bash

# Setup script for django-ergo project
# This script should be run after cloning the repository in the container

set -e

# Create log file with timestamp
LOG_FILE="/workspace/.cursor/startup.log"
mkdir -p "$(dirname "$LOG_FILE")"

# Function to log with timestamp
log_with_timestamp() {
    while IFS= read -r line; do
        echo "[$(date '+%Y-%m-%d %H:%M:%S')] $line" | tee -a "$LOG_FILE"
    done
}

# Redirect all output to both terminal and log file
exec > >(log_with_timestamp)
exec 2>&1

echo "🚀 Django-ergo startup script started"
echo "📝 Logging to: $LOG_FILE"

echo "🚀 Starting postgres..."
sudo service postgresql start
echo "🚀 Waiting for postgres to be ready..."
until sudo -u postgres pg_isready -h localhost; do
  echo "Waiting for PostgreSQL to be ready..."
  sleep 1
done

echo "🚀 Setting up django-ergo project..."

# Ensure we're in the project directory
if [ ! -f "manage.py" ]; then
    echo "❌ Error: manage.py not found. Please run this script from the project root."
    exit 1
fi

# Set up environment for uv and virtual environment
echo "📦 Setting up environment..."
export PATH="/home/ubuntu/.local/bin:$PATH"
export VENV_ROOT="/home/ubuntu/.venv"
export PATH="$VENV_ROOT/bin:$PATH"
export PYTHONPATH="/workspace/src:$PYTHONPATH"

# Clean up any problematic coverage files
echo "🧹 Cleaning up coverage files..."
rm -f /workspace/.coverage* /workspace/htmlcov/* 2>/dev/null || true

# Install test dependencies directly without editable install
echo "📦 Installing test dependencies..."
uv pip install pytest pytest-django pytest-cov coverage django psycopg2-binary

# Install pre-commit hooks
echo "🔧 Installing pre-commit hooks..."
pre-commit install || echo "⚠️ Pre-commit install failed, continuing..."

# Run migrations
echo "🗄️ Running database migrations..."
python manage.py migrate --settings=tests.example_app.settings

# Create superuser if it doesn't exist
echo "👤 Creating superuser (if needed)..."
python manage.py shell --settings=tests.example_app.settings -c "
from django.contrib.auth import get_user_model
User = get_user_model()
if not User.objects.filter(is_superuser=True).exists():
    User.objects.create_superuser('admin', 'admin@example.com', 'admin')
    print('Superuser created: admin/admin')
else:
    print('Superuser already exists')
"

echo "✅ Setup complete! You can now:"
echo "  - Run tests: pytest -v --ds=tests.example_app.settings"
echo "  - Run tests with coverage: pytest -v --ds=tests.example_app.settings --cov=src/django_ergo --cov-report=html"
echo "  - Start server: python manage.py runserver --settings=tests.example_app.settings"
echo "  - Run linting: ruff check ."
echo "  - Format code: ruff format ."
