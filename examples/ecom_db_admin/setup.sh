#!/bin/bash

echo "Setting up EcomDBAdmin example..."

# Check if virtual environment exists
if [ ! -d "venv" ]; then
    echo "Creating virtual environment..."
    python3 -m venv venv
fi

# Activate virtual environment
source venv/bin/activate

# Install dependencies
echo "Installing dependencies..."
pip install -r requirements.txt

# Check for .env file
if [ ! -f ".env" ]; then
    echo "Creating .env file from example..."
    cp .env.example .env
    echo "Please edit .env and add your OpenAI API key"
fi

# Run migrations
echo "Running migrations..."
python manage.py migrate

# Generate sample data
echo "Generating sample data..."
python manage.py generate_sample_data

echo "Setup complete!"
echo ""
echo "To start the server:"
echo "  source venv/bin/activate"
echo "  python manage.py runserver"
echo ""
echo "Admin login: admin/admin123"