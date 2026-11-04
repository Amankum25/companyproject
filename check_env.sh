#!/usr/bin/env bash
echo "=== Python Version ==="
python3 --version

echo ""
echo "=== pip status ==="
python3 -m pip --version 2>/dev/null || echo "pip not available"

echo ""
echo "=== NautilusTrader latest versions on PyPI ==="
curl -s https://pypi.org/pypi/nautilus-trader/json -o /tmp/nt.json
python3 << 'EOF'
import json
with open("/tmp/nt.json") as f:
    d = json.load(f)
versions = sorted(d["releases"].keys())
print("Last 15 releases:")
for v in versions[-15:]:
    print(f"  {v}")
print(f"\nLatest info:")
info = d["info"]
print(f"  Latest: {info['version']}")
EOF

echo ""
echo "=== System packages ==="
dpkg -l | grep -E "build-essential|clang|rustc|libssl" 2>/dev/null || echo "checking apt..."

echo ""
echo "=== Rust ==="
rustc --version 2>/dev/null || echo "rust not installed"
cargo --version 2>/dev/null || echo "cargo not installed"

echo ""
echo "=== Disk space ==="
df -h /

echo ""
echo "=== Network connectivity ==="
curl -s --max-time 5 https://api.bybit.com/v5/market/time 2>/dev/null | python3 -c "import sys,json; d=json.load(sys.stdin); print('Bybit API reachable, time:', d)" 2>/dev/null || echo "Cannot reach Bybit API (expected from WSL)"

