#!/bin/bash

# Setup script for django-ergo project
# This script should be run after cloning the repository in the container

set -e

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

# Activate virtual environment if not already active
if [[ "$VIRTUAL_ENV" == "" ]]; then
    echo "📦 Activating virtual environment..."
    source ~/.bashrc
    eval "$(pyenv init -)"
    pyenv activate django-ergo_env || pyenv local django-ergo_env
fi

# Install project in editable mode
echo "📦 Installing project in editable mode..."
python3 -m pip install -e .

# Install pre-commit hooks
echo "🔧 Installing pre-commit hooks..."
pre-commit install

# Run migrations
echo "🗄️ Running database migrations..."
python3 manage.py migrate

# Create superuser if it doesn't exist
echo "👤 Creating superuser (if needed)..."
python3 manage.py shell -c "
from django.contrib.auth import get_user_model
User = get_user_model()
if not User.objects.filter(is_superuser=True).exists():
    User.objects.create_superuser('admin', 'admin@example.com', 'admin')
    print('Superuser created: admin/admin')
else:
    print('Superuser already exists')
"

# Run tests to verify setup
echo "🧪 Running tests to verify setup..."
python3 -m pytest -vx

echo "✅ Setup complete! You can now:"
echo "  - Run tests: make pytest"
echo "  - Start server: make serve"
echo "  - Run linting: make ruff_check"
echo "  - Format code: make ruff_format" 