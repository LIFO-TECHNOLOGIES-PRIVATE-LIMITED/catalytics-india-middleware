"""
Check if the dashboard is accessible from the network
"""
import socket
import subprocess

print("=" * 70)
print("CO MIDDLEWARE - NETWORK ACCESS CHECK")
print("=" * 70)
print()

# Get local IP addresses
print("Your IP addresses:")
print("-" * 70)
try:
    hostname = socket.gethostname()
    print(f"Hostname: {hostname}")
    
    # Get all IP addresses
    ip_addresses = socket.gethostbyname_ex(hostname)[2]
    for ip in ip_addresses:
        if not ip.startswith('127.'):
            print(f"  → {ip}:8787")
            print(f"     Access from other computers: http://{ip}:8787")
except Exception as e:
    print(f"Error getting IP: {e}")

print()
print("=" * 70)
print("FIREWALL CHECK")
print("=" * 70)
print()

# Check if firewall rule exists
try:
    result = subprocess.run(
        ['netsh', 'advfirewall', 'firewall', 'show', 'rule', 'name=CO Middleware Dashboard'],
        capture_output=True,
        text=True
    )
    
    if 'No rules match' in result.stdout or result.returncode != 0:
        print("❌ Firewall rule NOT found")
        print()
        print("To allow network access:")
        print("  1. Right-click on 'allow_firewall.bat'")
        print("  2. Select 'Run as administrator'")
        print()
    else:
        print("✅ Firewall rule exists")
        print()
        print("Port 8787 is allowed through Windows Firewall")
        print()
except Exception as e:
    print(f"⚠️  Could not check firewall: {e}")
    print()

print("=" * 70)
print("CONFIGURATION CHECK")
print("=" * 70)
print()

# Check .env file
import os
env_path = os.path.join(os.path.dirname(__file__), '.env')
if os.path.exists(env_path):
    print("Checking .env file...")
    with open(env_path, 'r') as f:
        for line in f:
            if 'WEB_UI_HOST' in line:
                print(f"  {line.strip()}")
            if 'WEB_UI_PORT' in line:
                print(f"  {line.strip()}")
    print()
    
    # Check if 0.0.0.0 is set
    with open(env_path, 'r') as f:
        content = f.read()
        if 'WEB_UI_HOST=0.0.0.0' in content:
            print("✅ WEB_UI_HOST is set to 0.0.0.0 (network accessible)")
        elif 'WEB_UI_HOST=localhost' in content:
            print("❌ WEB_UI_HOST is set to localhost (only local access)")
            print("   Change to: WEB_UI_HOST=0.0.0.0")
        else:
            print("⚠️  WEB_UI_HOST not found in .env")
            print("   Add: WEB_UI_HOST=0.0.0.0")
else:
    print("❌ .env file not found")

print()
print("=" * 70)
print()

input("Press Enter to exit...")
