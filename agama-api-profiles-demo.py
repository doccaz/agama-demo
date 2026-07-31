import requests
import sys
import time

INSTALLER_IP = "<INSTALLER_IP>"
BASE_URL = f"http://{INSTALLER_IP}/api/v0"

def apply_profile(profile_name, disk_path):
    print(f"[*] Applying Profile: {profile_name} to {disk_path}")
    profile = PROFILES[profile_name]
    
    # 1. Update storage with the actual disk path
    storage_data = profile['storage']
    storage_data['device'] = disk_path
    
    # 2. Send Storage Proposal
    print("[*] Sending Storage Proposal...")
    requests.put(f"{BASE_URL}/storage/proposal", json=storage_data)
    
    # 3. Send Security Config (only if 'atm-full')
    if profile['security']:
        print("[*] Configuring TPM2 Full Disk Encryption...")
        requests.patch(f"{BASE_URL}/security", json=profile['security'])

def main():
    if len(sys.argv) < 2 or sys.argv[1] not in PROFILES:
        print("Usage: python3 demo.py [atm-slim|atm-full]")
        return

    selected = sys.argv[1]
    
    # Discovery Step
    devices = requests.get(f"{BASE_URL}/storage/devices").json()
    target_disk = devices[0]['path']
    
    # Apply logic
    apply_profile(selected, target_disk)
    
    # Start Installation
    print(f"[*] Triggering {selected} installation...")
    requests.post(f"{BASE_URL}/installation/start")

if __name__ == "__main__":
    main()

