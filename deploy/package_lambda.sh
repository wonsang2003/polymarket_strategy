#!/usr/bin/env bash
set -euo pipefail

# Package the Polymarket whale monitor for AWS Lambda deployment.
# Usage: bash deploy/package_lambda.sh
# Output: deploy/lambda.zip

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
BUILD_DIR="$PROJECT_ROOT/deploy/build"
OUTPUT="$PROJECT_ROOT/deploy/lambda.zip"

echo "Cleaning previous build..."
rm -rf "$BUILD_DIR" "$OUTPUT"
mkdir -p "$BUILD_DIR"

echo "Installing package into build directory..."
pip install --target "$BUILD_DIR" --quiet "$PROJECT_ROOT"

echo "Copying Lambda handler..."
cp "$PROJECT_ROOT/lambda_handler.py" "$BUILD_DIR/"

echo "Creating zip..."
cd "$BUILD_DIR"
zip -r "$OUTPUT" . -x '*.pyc' '__pycache__/*' '*.dist-info/*' > /dev/null

SIZE=$(du -h "$OUTPUT" | cut -f1)
echo "Done: $OUTPUT ($SIZE)"
echo ""
echo "Next steps:"
echo "  1. Create Lambda function (Python 3.12, 256MB, 5min timeout)"
echo "  2. Upload deploy/lambda.zip"
echo "  3. Set handler to: lambda_handler.handler"
echo "  4. Set env vars: TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID"
echo "  5. Optional: S3_STATE_BUCKET for state persistence across invocations"
echo "  6. Create EventBridge rule: rate(1 hour)"
