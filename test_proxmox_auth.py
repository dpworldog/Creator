#!/usr/bin/env python3
"""
Test script to verify Proxmox authentication
"""
import asyncio
import aiohttp
import ssl
import json

# Your Proxmox settings
PROXMOX_HOST = "213.136.76.161"
PROXMOX_USER = "root@pam"
PROXMOX_PASSWORD = "darshv12"
PROXMOX_PORT = 8006

async def test_proxmox_auth():
    """Test Proxmox authentication"""
    
    base_url = f"https://{PROXMOX_HOST}:{PROXMOX_PORT}/api2/json"
    
    # Create SSL context that ignores certificate verification
    ssl_context = ssl.create_default_context()
    ssl_context.check_hostname = False
    ssl_context.verify_mode = ssl.CERT_NONE
    
    print("🧪 Testing Proxmox Authentication...")
    print(f"📡 Connecting to: {base_url}")
    
    try:
        connector = aiohttp.TCPConnector(ssl=ssl_context)
        timeout = aiohttp.ClientTimeout(total=30)
        
        auth_data = {
            'username': PROXMOX_USER,
            'password': PROXMOX_PASSWORD
        }
        
        async with aiohttp.ClientSession(connector=connector, timeout=timeout) as session:
            async with session.post(
                f"{base_url}/access/ticket",
                data=auth_data
            ) as response:
                
                response_text = await response.text()
                print(f"\n📄 Response Status: {response.status}")
                print(f"📄 Response Body: {response_text}")
                
                if response.status == 200:
                    try:
                        data = await response.json()
                        
                        ticket = data['data']['ticket']
                        csrf_token = data['data']['CSRFPreventionToken']
                        
                        print(f"\n✅ Authentication SUCCESS!")
                        print(f"🎫 Ticket: {ticket[:20]}...")
                        print(f"🔒 CSRF Token: {csrf_token[:20]}...")
                        
                        # Test getting VM ID with proper auth
                        print(f"\n🧪 Testing VM ID retrieval...")
                        
                        headers = {
                            'Cookie': f'PVEAuthCookie={ticket}',
                            'CSRFPreventionToken': csrf_token
                        }
                        
                        async with session.get(
                            f"{base_url}/cluster/nextid",
                            headers=headers
                        ) as vm_response:
                            
                            vm_response_text = await vm_response.text()
                            print(f"📄 VM ID Response Status: {vm_response.status}")
                            print(f"📄 VM ID Response: {vm_response_text}")
                            
                            if vm_response.status == 200:
                                vm_data = await vm_response.json()
                                next_id = vm_data['data']
                                print(f"✅ Next VM ID: {next_id}")
                            else:
                                print(f"❌ VM ID request failed: {vm_response.status}")
                        
                        return True
                        
                    except json.JSONDecodeError:
                        print(f"❌ Invalid JSON response: {response_text}")
                        return False
                else:
                    print(f"❌ Authentication failed: {response.status}")
                    return False
                    
    except Exception as e:
        print(f"💥 Exception: {e}")
        return False

if __name__ == "__main__":
    asyncio.run(test_proxmox_auth())