"""
Script to check and fix .env file formatting issues
"""
import os

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
env_path = os.path.join(SCRIPT_DIR, ".env")

print("=" * 70)
print("ENV FILE DIAGNOSTIC")
print("=" * 70)
print(f"Checking: {env_path}\n")

if not os.path.exists(env_path):
    print(f"❌ ERROR: .env file not found!")
    print(f"\nCreate a .env file at: {env_path}")
    input("\nPress Enter to exit...")
    exit(1)

print("✓ .env file exists\n")

# Read and show raw content
print("=" * 70)
print("RAW FILE CONTENT (first 1000 chars)")
print("=" * 70)
with open(env_path, 'rb') as f:
    raw_bytes = f.read(1000)
    print(repr(raw_bytes))
print()

# Read and parse
print("=" * 70)
print("PARSED LINES")
print("=" * 70)

config = {}
with open(env_path, 'r', encoding='utf-8') as f:
    line_num = 0
    for line in f:
        line_num += 1
        original = line
        line = line.strip()
        
        if not line or line.startswith('#'):
            continue
        
        if '=' not in line:
            print(f"Line {line_num}: SKIPPED (no =) -> {line[:60]}")
            continue
        
        key, value = line.split('=', 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        
        if key:
            config[key] = value
            if key.startswith('CATALYTICS_'):
                if key == 'CATALYTICS_API_KEY':
                    print(f"Line {line_num}: {key} = {value[:20]}... ({len(value)} chars)")
                else:
                    print(f"Line {line_num}: {key} = {value}")

print()
print("=" * 70)
print("FINAL CONFIGURATION")
print("=" * 70)
print(f"CATALYTICS_API_BASE_URL = {config.get('CATALYTICS_API_BASE_URL', 'NOT SET')}")
print(f"CATALYTICS_ENTITY_ID = {config.get('CATALYTICS_ENTITY_ID', 'NOT SET')}")
api_key = config.get('CATALYTICS_API_KEY', '')
print(f"CATALYTICS_API_KEY = {'SET (' + str(len(api_key)) + ' chars)' if api_key else 'NOT SET'}")
print()

# Check if correct
api_base_url = config.get('CATALYTICS_API_BASE_URL', '')
if '/import' in api_base_url and '192.168.1.42' in api_base_url:
    print("✅ Configuration looks CORRECT!")
    print(f"\nWill call: {api_base_url}/tally-customer-payload/")
else:
    print("❌ Configuration has issues:")
    if not api_base_url:
        print("  - API_BASE_URL is empty")
    elif '/import' not in api_base_url:
        print("  - API_BASE_URL missing /import")
    elif '192.168.1.42' not in api_base_url:
        print("  - API_BASE_URL should use 192.168.1.42 not localhost")
    
    print(f"\nShould be: http://192.168.1.42:8000/import")
    print(f"Currently: {api_base_url}")

print("=" * 70)

input("\nPress Enter to exit...")
