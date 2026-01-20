#!/bin/bash
# Local testing script for Flipkart Discount Monitor
# This script helps you test the setup before deploying to GitHub

echo "==========================================="
echo "Flipkart Discount Monitor - Local Test"
echo "==========================================="
echo ""

# Check if Python is installed
if ! command -v python3 &> /dev/null; then
    echo "❌ Error: Python 3 is not installed"
    exit 1
fi

echo "✅ Python 3 found: $(python3 --version)"

# Check if pip is installed
if ! command -v pip3 &> /dev/null; then
    echo "❌ Error: pip3 is not installed"
    exit 1
fi

echo "✅ pip3 found"
echo ""

# Install dependencies
echo "Installing dependencies..."
pip3 install -r requirements.txt

echo ""
echo "==========================================="
echo "Testing Configuration"
echo "==========================================="
echo ""

# Check environment variables
if [ -z "$SENDER_EMAIL" ]; then
    echo "⚠️  Warning: SENDER_EMAIL not set"
    echo "   Set it with: export SENDER_EMAIL='your-email@gmail.com'"
else
    echo "✅ SENDER_EMAIL is set: $SENDER_EMAIL"
fi

if [ -z "$RECEIVER_EMAIL" ]; then
    echo "⚠️  Warning: RECEIVER_EMAIL not set"
    echo "   Set it with: export RECEIVER_EMAIL='your-email@gmail.com'"
else
    echo "✅ RECEIVER_EMAIL is set: $RECEIVER_EMAIL"
fi

if [ -z "$EMAIL_PASSWORD" ]; then
    echo "⚠️  Warning: EMAIL_PASSWORD not set"
    echo "   Set it with: export EMAIL_PASSWORD='your-app-password'"
else
    echo "✅ EMAIL_PASSWORD is set: ********"
fi

echo ""
echo "==========================================="
echo "Running Discount Check..."
echo "==========================================="
echo ""

# Run the script
python3 check_discount.py

echo ""
echo "==========================================="
echo "Test Complete!"
echo "==========================================="
echo ""
echo "Next steps:"
echo "1. Review the output above for any errors"
echo "2. If successful, commit and push to GitHub"
echo "3. Configure GitHub Secrets in your repository"
echo "4. Enable GitHub Actions"
echo ""

