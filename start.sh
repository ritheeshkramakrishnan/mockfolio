#!/bin/bash
# PaperTrade — Quick Start
set -e

echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  PaperTrade — Starting up"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

# Create .env if it doesn't exist
if [ ! -f .env ]; then
  cp .env.example .env
  echo "✓ Created .env (edit it to add your ANTHROPIC_API_KEY)"
fi

# Install dependencies
echo "Installing Python packages…"
pip3 install -r requirements.txt -q

echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  Open: http://localhost:5000"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""

# Load .env and start Flask
export $(grep -v '^#' .env | xargs) 2>/dev/null || true
python3 app.py
