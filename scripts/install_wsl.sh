#!/usr/bin/env bash
# =============================================================================
# NautilusTrader 2.0 Environment Setup using uv in WSL2
# =============================================================================
set -e

VENV_PATH="$HOME/.venvs/btc-straddle"
PROJECT_PATH="/mnt/c/Users/amank/Downloads/bitbybit"

echo "======================================================="
echo "  Setting up Python 3.13 + NautilusTrader 2.0"
echo "======================================================="

# Ensure uv is installed
if ! command -v uv &> /dev/null; then
    echo "Installing uv..."
    curl -LsSf https://astral.sh/uv/install.sh | env UV_INSTALL_DIR=/usr/local/bin sh 2>/dev/null || curl -LsSf https://astral.sh/uv/install.sh | sh
    export PATH="$HOME/.local/bin:$PATH"
fi

echo "uv version: $(uv --version)"

# Install Python 3.13 via uv
echo "Ensuring Python 3.13 is available..."
uv python install 3.13

# Create Virtual Environment
echo "Creating virtual environment at $VENV_PATH..."
rm -rf "$VENV_PATH"
uv venv "$VENV_PATH" --python 3.13

# Install NautilusTrader 2.0.0rc8 and dependencies
echo "Installing NautilusTrader 2.0.0rc8, pytest, and python-dotenv..."
uv pip install --python "$VENV_PATH" --pre 'nautilus_trader==2.0.0rc8' pytest python-dotenv fastapi 'uvicorn[standard]' httpx

# Verify installation
echo "Verifying installation..."
"$VENV_PATH/bin/python" "$PROJECT_PATH/verify_install.py"

echo ""
echo "======================================================="
echo "  SETUP COMPLETE"
echo "======================================================="
echo "Activate environment:"
echo "  source $VENV_PATH/bin/activate"
