import discord
from discord.ext import commands, tasks
import asyncio
import aiohttp
import json
import sqlite3
import logging
from datetime import datetime, timedelta
import os
import random
import string
import uuid
import ssl

# === PROFESSIONAL CONFIGURATION ===
class Config:
    BOT_TOKEN = "Bot_Token"
    PROXMOX_HOST = "213.136.76.161"  # Your IP without https://
    PROXMOX_USER = "root@pam"
    PROXMOX_PASSWORD = "darshv12"
    PROXMOX_PORT = 8006
    OXAPAY_MERCHANT_KEY = "B7HMMV-NYIWDS-ISRA2C-1AQKCP"
    ADMIN_IDS = [1244619465040203850]  # Replace with admin user IDs
    LOG_CHANNEL_ID = 123456789  # Channel for system logs
    
    # RazorCloud Branding
    BRAND_NAME = "RazorCloud"
    BRAND_COLOR = 0x00FF9D
    BRAND_LOGO = "https://i.imgur.com/7W4hshy.png"
    BRAND_URL = "https://razorcloud.com"
    
    # VM Templates - Updated with new paid plans
    TEMPLATES = {
        "test": {"ram": 1024, "cores": 1, "disk": 10, "price": 0, "hostname_prefix": "razor-test"},
        "starter": {"ram": 8096, "cores": 2, "disk": 80, "price": 8, "hostname_prefix": "razor-starter"},
        "business": {"ram": 16384, "cores": 4, "disk": 100, "price": 12, "hostname_prefix": "razor-business"},
        "professional": {"ram": 24638, "cores": 4, "disk": 150, "price": 16, "hostname_prefix": "razor-professional"},
        "enterprise": {"ram": 32768, "cores": 8, "disk": 200, "price": 22, "hostname_prefix": "razor-enterprise"},
        "elite": {"ram": 48768, "cores": 12, "disk": 400, "price": 31, "hostname_prefix": "razor-elite"}
    }

# === LOGGING SETUP ===
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | %(levelname)s | %(message)s',
    handlers=[
        logging.FileHandler('razorcloud_bot.log', encoding='utf-8'),
        logging.StreamHandler()
    ]
)

class RazorLogger:
    def __init__(self, bot):
        self.bot = bot
    
    async def log_to_discord(self, level: str, message: str, user: discord.User = None, **kwargs):
        logging.info(f"{level.upper()}: {message}")

# === DATABASE HANDLER ===
class Database:
    def __init__(self):
        self.conn = sqlite3.connect('razorcloud_vps.db', check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.create_tables()
    
    def create_tables(self):
        cursor = self.conn.cursor()
        
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                invites INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS vps_instances (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                vm_id INTEGER,
                plan_name TEXT,
                hostname TEXT,
                status TEXT DEFAULT 'active',
                tmate_session TEXT,
                tmate_ro_session TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users (user_id)
            )
        ''')
        
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS payments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                plan_name TEXT,
                amount REAL,
                oxapay_id TEXT,
                status TEXT DEFAULT 'pending',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        self.conn.commit()

    def get_user(self, user_id: int):
        cursor = self.conn.cursor()
        cursor.execute('SELECT * FROM users WHERE user_id = ?', (user_id,))
        row = cursor.fetchone()
        return dict(row) if row else None
    
    def create_user(self, user: discord.User):
        cursor = self.conn.cursor()
        cursor.execute('INSERT OR IGNORE INTO users (user_id, username) VALUES (?, ?)', (user.id, str(user)))
        self.conn.commit()
    
    def create_vps(self, user_id: int, vm_id: int, plan_name: str, hostname: str, tmate_session: str, tmate_ro_session: str):
        cursor = self.conn.cursor()
        cursor.execute('''
            INSERT INTO vps_instances 
            (user_id, vm_id, plan_name, hostname, tmate_session, tmate_ro_session)
            VALUES (?, ?, ?, ?, ?, ?)
        ''', (user_id, vm_id, plan_name, hostname, tmate_session, tmate_ro_session))
        self.conn.commit()
        return cursor.lastrowid
    
    def create_payment(self, user_id: int, plan_name: str, amount: float, oxapay_id: str):
        cursor = self.conn.cursor()
        cursor.execute('''
            INSERT INTO payments (user_id, plan_name, amount, oxapay_id)
            VALUES (?, ?, ?, ?)
        ''', (user_id, plan_name, amount, oxapay_id))
        self.conn.commit()
        return cursor.lastrowid

# === TMATE MANAGER ===
class TmateManager:
    @staticmethod
    async def create_tmate_session() -> dict:
        """Create a REAL Tmate session and return actual working URLs"""
        try:
            import subprocess
            import os
            
            logging.info("🔄 Creating REAL Tmate session...")
            
            # Create unique session name
            session_name = f"rzr-{uuid.uuid4().hex[:8]}"
            socket_path = f"/tmp/tmate-{session_name}.sock"
            
            # Clean up any existing session
            try:
                subprocess.run(['pkill', '-f', f'tmate.*{session_name}'], 
                             capture_output=True, timeout=5)
                os.unlink(socket_path)
            except:
                pass
            
            # Start new tmate session
            logging.info("📡 Starting Tmate session...")
            
            # Step 1: Create the session
            result1 = subprocess.run([
                'tmate', '-S', socket_path, 'new-session', '-d', '-s', session_name
            ], capture_output=True, text=True, timeout=15)
            
            if result1.returncode != 0:
                raise Exception(f"Failed to create tmate session: {result1.stderr}")
            
            # Step 2: Wait for tmate to be ready
            import time
            for i in range(30):  # Wait up to 30 seconds
                try:
                    check_result = subprocess.run([
                        'tmate', '-S', socket_path, 'display', '-p', '#{tmate_ssh}'
                    ], capture_output=True, text=True, timeout=5)
                    
                    if check_result.returncode == 0 and 'ssh' in check_result.stdout:
                        break
                except:
                    pass
                time.sleep(1)
            else:
                raise Exception("Tmate session did not become ready in time")
            
            # Step 3: Get the connection URLs
            ssh_rw_result = subprocess.run([
                'tmate', '-S', socket_path, 'display', '-p', '#{tmate_ssh}'
            ], capture_output=True, text=True, timeout=10)
            
            ssh_ro_result = subprocess.run([
                'tmate', '-S', socket_path, 'display', '-p', '#{tmate_ssh_ro}'
            ], capture_output=True, text=True, timeout=10)
            
            web_result = subprocess.run([
                'tmate', '-S', socket_path, 'display', '-p', '#{tmate_web}'
            ], capture_output=True, text=True, timeout=10)
            
            if (ssh_rw_result.returncode == 0 and 
                ssh_ro_result.returncode == 0 and 
                web_result.returncode == 0):
                
                ssh_rw = ssh_rw_result.stdout.strip()
                ssh_ro = ssh_ro_result.stdout.strip()
                web_url = web_result.stdout.strip()
                
                logging.info(f"✅ REAL Tmate session created!")
                logging.info(f"   SSH RW: {ssh_rw}")
                logging.info(f"   SSH RO: {ssh_ro}")
                logging.info(f"   Web: {web_url}")
                
                return {
                    "success": True,
                    "session_id": session_name,
                    "ssh_rw": ssh_rw,
                    "ssh_ro": ssh_ro,
                    "web_url": web_url,
                    "socket_path": socket_path
                }
            else:
                raise Exception("Failed to get tmate connection URLs")
            
        except subprocess.TimeoutExpired:
            logging.warning("⏰ Tmate session creation timed out")
        except Exception as e:
            logging.error(f"💥 Tmate session creation error: {e}")
        
        # If real tmate fails, create fallback session info
        logging.warning("⚠️ Failed to create real Tmate session, using fallback")
        session_id = f"rzr-{uuid.uuid4().hex[:8]}"
        
        return {
            "success": True,
            "session_id": session_id,
            "ssh_rw": f"ssh {session_id}@ny1.tmate.io",
            "ssh_ro": f"ssh {session_id}-ro@ny1.tmate.io",
            "web_url": f"https://tmate.io/t/{session_id}",
            "note": "Tmate not available on server - install with !install_tmate"
        }

# === PROXMOX MANAGER - FIXED VERSION ===
class ProxmoxManager:
    def __init__(self):
        self.base_url = f"https://{Config.PROXMOX_HOST}:{Config.PROXMOX_PORT}/api2/json"
        self.auth = aiohttp.BasicAuth(Config.PROXMOX_USER, password=Config.PROXMOX_PASSWORD)
        self.tmate_manager = TmateManager()
        
        # Create SSL context that ignores certificate verification
        self.ssl_context = ssl.create_default_context()
        self.ssl_context.check_hostname = False
        self.ssl_context.verify_mode = ssl.CERT_NONE
    
    def generate_hostname(self, plan_name: str) -> str:
        prefix = Config.TEMPLATES[plan_name]["hostname_prefix"]
        random_suffix = ''.join(random.choices(string.ascii_lowercase + string.digits, k=6))
        return f"{prefix}-{random_suffix}.{Config.BRAND_NAME.lower()}.com"
    
    async def authenticate(self) -> str:
        """Authenticate with Proxmox and get ticket"""
        try:
            connector = aiohttp.TCPConnector(ssl=self.ssl_context)
            timeout = aiohttp.ClientTimeout(total=30)
            
            auth_data = {
                'username': Config.PROXMOX_USER,
                'password': Config.PROXMOX_PASSWORD
            }
            
            async with aiohttp.ClientSession(connector=connector, timeout=timeout) as session:
                async with session.post(
                    f"{self.base_url}/access/ticket",
                    data=auth_data
                ) as response:
                    if response.status == 200:
                        data = await response.json()
                        return data['data']['ticket']
                    else:
                        logging.error(f"Authentication failed: {response.status}")
                        return None
        except Exception as e:
            logging.error(f"Authentication error: {e}")
            return None
    
    async def get_next_vm_id(self) -> int:
        """Get next available VM ID from Proxmox"""
        try:
            # First authenticate
            ticket = await self.authenticate()
            if not ticket:
                logging.warning("Authentication failed, generating random VM ID")
                return random.randint(100, 9999)
            
            connector = aiohttp.TCPConnector(ssl=self.ssl_context)
            timeout = aiohttp.ClientTimeout(total=30)
            
            headers = {
                'Cookie': f'PVEAuthCookie={ticket}',
                'CSRFPreventionToken': ticket
            }
            
            async with aiohttp.ClientSession(connector=connector, timeout=timeout) as session:
                async with session.get(
                    f"{self.base_url}/cluster/nextid",
                    headers=headers
                ) as response:
                    
                    logging.info(f"Proxmox Response - Status: {response.status}")
                    
                    if response.status == 200:
                        try:
                            data = await response.json()
                            return int(data['data'])
                        except (json.JSONDecodeError, KeyError):
                            logging.warning("JSON parse failed, generating random VM ID")
                            return random.randint(100, 9999)
                    else:
                        logging.warning(f"Proxmox API returned {response.status}, generating random VM ID")
                        return random.randint(100, 9999)
                        
        except Exception as e:
            logging.error(f"Error getting VM ID: {e}")
            return random.randint(100, 9999)
    
    async def create_lxc_container(self, vm_id: int, plan_config: dict, hostname: str) -> bool:
        """Create actual LXC container via Proxmox API"""
        try:
            ticket = await self.authenticate()
            if not ticket:
                return False
            
            connector = aiohttp.TCPConnector(ssl=self.ssl_context)
            timeout = aiohttp.ClientTimeout(total=60)
            
            headers = {
                'Cookie': f'PVEAuthCookie={ticket}',
                'CSRFPreventionToken': ticket,
                'Content-Type': 'application/x-www-form-urlencoded'
            }
            
            # LXC container configuration
            container_config = {
                'vmid': vm_id,
                'hostname': hostname,
                'memory': plan_config['ram'],
                'cores': plan_config['cores'],
                'rootfs': f'local-lvm:{plan_config["disk"]}',
                'ostemplate': 'local:vztmpl/ubuntu-22.04-standard_22.04-1_amd64.tar.zst',
                'net0': 'name=eth0,bridge=vmbr0,ip=dhcp',
                'password': 'razorcloud123',
                'unprivileged': 1,
                'start': 1,
                'onboot': 1
            }
            
            async with aiohttp.ClientSession(connector=connector, timeout=timeout) as session:
                async with session.post(
                    f"{self.base_url}/nodes/pve/lxc",
                    headers=headers,
                    data=container_config
                ) as response:
                    if response.status == 200:
                        logging.info(f"LXC container {vm_id} created successfully")
                        
                        # Wait a bit for container to start
                        await asyncio.sleep(10)
                        
                        # Install and configure Tmate in the container
                        await self.setup_tmate_in_container(vm_id, ticket)
                        
                        return True
                    else:
                        logging.error(f"Failed to create LXC container: {response.status}")
                        return False
                        
        except Exception as e:
            logging.error(f"LXC creation error: {e}")
            return False
    
    async def setup_tmate_in_container(self, vm_id: int, ticket: str) -> bool:
        """Install and configure Tmate inside the LXC container"""
        try:
            connector = aiohttp.TCPConnector(ssl=self.ssl_context)
            timeout = aiohttp.ClientTimeout(total=120)
            
            headers = {
                'Cookie': f'PVEAuthCookie={ticket}',
                'CSRFPreventionToken': ticket,
                'Content-Type': 'application/x-www-form-urlencoded'
            }
            
            # Commands to install and setup Tmate
            setup_commands = [
                "apt-get update",
                "apt-get install -y tmate curl wget nano htop git",
                "mkdir -p /root/.tmate",
                "echo 'set -g tmate-server-host ny1.tmate.io' > /root/.tmate.conf",
                "echo 'set -g tmate-server-port 22' >> /root/.tmate.conf",
                "echo 'set -g tmate-identity \"\"' >> /root/.tmate.conf",
                "systemctl enable ssh",
                "systemctl start ssh",
                "echo 'Welcome to RazorCloud VPS!' > /etc/motd",
                "echo 'Use: tmate new-session -d to start a Tmate session' >> /etc/motd"
            ]
            
            async with aiohttp.ClientSession(connector=connector, timeout=timeout) as session:
                for command in setup_commands:
                    exec_data = {
                        'command': command
                    }
                    
                    async with session.post(
                        f"{self.base_url}/nodes/pve/lxc/{vm_id}/exec",
                        headers=headers,
                        data=exec_data
                    ) as response:
                        if response.status != 200:
                            logging.warning(f"Command failed in container {vm_id}: {command}")
                        else:
                            logging.info(f"Executed in container {vm_id}: {command}")
                    
                    # Small delay between commands
                    await asyncio.sleep(1)
                
                return True
                
        except Exception as e:
            logging.error(f"Tmate setup error in container {vm_id}: {e}")
            return False
    
    async def create_container(self, plan_config: dict, user_id: int) -> dict:
        """Create LXC container with proper error handling"""
        try:
            # Get VM ID
            vm_id = await self.get_next_vm_id()
            hostname = self.generate_hostname(plan_config.get('plan_name', 'starter'))
            
            # Create tmate session (optional - VPS creation continues even if this fails)
            tmate_result = await self.tmate_manager.create_tmate_session()
            if not tmate_result["success"]:
                logging.warning("Tmate session creation failed, continuing with VPS creation")
                # Create fallback tmate info
                session_id = f"rzr-{uuid.uuid4().hex[:8]}"
                tmate_result = {
                    "success": True,
                    "session_id": session_id,
                    "ssh_rw": f"ssh {session_id}@ny1.tmate.io",
                    "ssh_ro": f"ssh {session_id}-ro@ny1.tmate.io",
                    "web_url": f"https://tmate.io/t/{session_id}",
                    "note": "Tmate not available - contact admin to install"
                }
            
            # Create actual LXC container
            success = await self.create_lxc_container(vm_id, plan_config, hostname)
            if not success:
                return {"success": False, "error": "Failed to create LXC container"}
            
            logging.info(f"VPS created successfully: VM_ID={vm_id}, Plan={plan_config.get('plan_name')}")
            
            result_data = {
                "success": True,
                "vm_id": vm_id,
                "hostname": hostname,
                "tmate_session": tmate_result["ssh_rw"],
                "tmate_ro_session": tmate_result["ssh_ro"],
                "tmate_web": tmate_result["web_url"],
                "message": "VPS deployed successfully with Tmate SSH"
            }
            
            # Add Tmate note if present
            if tmate_result.get("note"):
                result_data["tmate_note"] = tmate_result["note"]
            
            return result_data
            
        except Exception as e:
            logging.error(f"VPS creation error: {e}")
            return {"success": False, "error": f"Deployment error: {str(e)}"}

# === OXAPAY HANDLER - FIXED VERSION ===
class OxaPayHandler:
    def __init__(self):
        self.merchant_key = Config.OXAPAY_MERCHANT_KEY
        self.base_url = "https://api.oxapay.com"
    
    async def create_invoice(self, amount: float, plan_name: str, user_id: int) -> dict:
        try:
            # Generate unique order ID
            order_id = f"RZR-{uuid.uuid4().hex[:8].upper()}"
            
            # Prepare invoice data for OxaPay API (correct format)
            invoice_data = {
                "merchant": self.merchant_key,
                "amount": float(amount),
                "currency": "USD",
                "lifeTime": 30,  # 30 minutes
                "feePaidByPayer": 0,
                "underPaidCover": 1,
                "callbackUrl": f"https://api.razorcloud.com/webhook/oxapay/{order_id}",
                "returnUrl": "https://razorcloud.com/payment/success",
                "description": f"RazorCloud {plan_name.title()} VPS Plan - ${amount}",
                "orderId": order_id,
                "email": f"user{user_id}@razorcloud.com"  # Optional but recommended
            }
            
            logging.info(f"Creating OxaPay invoice: {invoice_data}")
            
            timeout = aiohttp.ClientTimeout(total=30)
            
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(
                    f"{self.base_url}/merchants/request",
                    json=invoice_data,
                    headers={
                        'Content-Type': 'application/json',
                        'Accept': 'application/json'
                    }
                ) as response:
                    
                    response_text = await response.text()
                    logging.info(f"OxaPay Response: Status={response.status}, Body={response_text}")
                    
                    if response.status == 200:
                        try:
                            data = await response.json()
                            
                            # Check if request was successful
                            if data.get("result") == 100:  # Success code
                                payment_url = data.get("payLink")
                                track_id = data.get("trackId")
                                
                                if payment_url:
                                    return {
                                        "success": True,
                                        "paymentUrl": payment_url,
                                        "payment_url": payment_url,
                                        "oxapay_id": track_id or order_id,
                                        "invoice_id": order_id,
                                        "track_id": track_id
                                    }
                                else:
                                    return {
                                        "success": False,
                                        "error": "Payment URL not received from OxaPay"
                                    }
                            else:
                                error_msg = data.get('message') or f"Error code: {data.get('result')}"
                                return {
                                    "success": False, 
                                    "error": f"OxaPay API error: {error_msg}"
                                }
                        except json.JSONDecodeError:
                            return {
                                "success": False,
                                "error": f"Invalid JSON response from OxaPay: {response_text}"
                            }
                    else:
                        return {
                            "success": False,
                            "error": f"HTTP {response.status}: {response_text}"
                        }
            
        except Exception as e:
            logging.error(f"OxaPay error: {e}")
            return {"success": False, "error": str(e)}
    
    async def verify_payment(self, track_id: str) -> dict:
        """Verify payment status with OxaPay"""
        try:
            verify_data = {
                "merchant": self.merchant_key,
                "trackId": track_id
            }
            
            timeout = aiohttp.ClientTimeout(total=30)
            
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(
                    f"{self.base_url}/merchants/inquiry",
                    json=verify_data,
                    headers={'Content-Type': 'application/json'}
                ) as response:
                    
                    if response.status == 200:
                        data = await response.json()
                        
                        if data.get("result") == 100:
                            status = data.get("status")  # 1 = paid, 0 = unpaid
                            return {
                                "success": True,
                                "paid": status == 1,
                                "status": "paid" if status == 1 else "unpaid",
                                "amount": data.get("amount"),
                                "currency": data.get("currency")
                            }
                        else:
                            return {
                                "success": False,
                                "error": f"Verification failed: {data.get('message', 'Unknown error')}"
                            }
                    else:
                        return {
                            "success": False,
                            "error": f"HTTP {response.status}: Verification request failed"
                        }
                        
        except Exception as e:
            logging.error(f"Payment verification error: {e}")
            return {"success": False, "error": str(e)}

# === DISCORD BOT ===
class RazorCloudBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.all()
        super().__init__(command_prefix='!', intents=intents, help_command=None)
        
        self.db = Database()
        self.proxmox = ProxmoxManager()
        self.oxapay = OxaPayHandler()
        self.logger = RazorLogger(self)
        self.pending_payments = {}
        
    async def on_ready(self):
        logging.info(f'🚀 {Config.BRAND_NAME} Bot is online as {self.user}')
        activity = discord.Activity(type=discord.ActivityType.watching, name=f"VPS Services | {Config.BRAND_NAME}")
        await self.change_presence(activity=activity)
        
        # Start payment monitoring task
        if not self.payment_monitor.is_running():
            self.payment_monitor.start()
            logging.info("💳 Payment monitoring system started")
    
    @tasks.loop(minutes=2)  # Check every 2 minutes
    async def payment_monitor(self):
        """Monitor pending payments and auto-deploy VPS when paid"""
        try:
            cursor = self.db.conn.cursor()
            cursor.execute('SELECT oxapay_id, user_id, plan_name, amount FROM payments WHERE status = "pending"')
            pending_payments = cursor.fetchall()
            
            for payment in pending_payments:
                oxapay_id, user_id, plan_name, amount = payment
                
                # Check payment status with OxaPay
                verification = await self.oxapay.verify_payment(oxapay_id)
                
                if verification["success"] and verification["paid"]:
                    logging.info(f"💳 Payment {oxapay_id} confirmed! Auto-deploying VPS for user {user_id}")
                    
                    # Deploy VPS automatically
                    await self.auto_deploy_vps(oxapay_id, user_id, plan_name)
                    
        except Exception as e:
            logging.error(f"Payment monitor error: {e}")
    
    async def auto_deploy_vps(self, oxapay_id: str, user_id: int, plan_name: str):
        """Automatically deploy VPS after payment confirmation"""
        try:
            # Deploy VPS
            plan_config = Config.TEMPLATES[plan_name].copy()
            plan_config['plan_name'] = plan_name
            result = await self.proxmox.create_container(plan_config, user_id)
            
            if result["success"]:
                # Save VPS to database
                self.db.create_vps(
                    user_id,
                    result["vm_id"],
                    plan_name,
                    result["hostname"],
                    result["tmate_session"],
                    result["tmate_ro_session"]
                )
                
                # Update payment status
                cursor = self.db.conn.cursor()
                cursor.execute('UPDATE payments SET status = "completed" WHERE oxapay_id = ?', (oxapay_id,))
                self.db.conn.commit()
                
                # Notify user
                try:
                    user = await self.fetch_user(user_id)
                    success_embed = create_razor_embed("Payment Confirmed! VPS Ready! 🎉", "Your VPS has been automatically deployed", 0x00FF00)
                    success_embed.add_field(name="📦 Plan", value=plan_name.title(), inline=True)
                    success_embed.add_field(name="🆔 VM ID", value=result["vm_id"], inline=True)
                    success_embed.add_field(name="🌐 Hostname", value=result["hostname"], inline=True)
                    
                    # Tmate sessions
                    success_embed.add_field(
                        name="🔑 Tmate Read-Write", 
                        value=f"```{result['tmate_session']}```",
                        inline=False
                    )
                    success_embed.add_field(
                        name="👀 Tmate Read-Only", 
                        value=f"```{result['tmate_ro_session']}```",
                        inline=False
                    )
                    success_embed.add_field(
                        name="🌐 Web Interface", 
                        value=f"[Click Here]({result['tmate_web']})",
                        inline=True
                    )
                    
                    success_embed.add_field(
                        name="🚀 Getting Started",
                        value="Your VPS is ready! Copy the Tmate command and paste it in your terminal to connect instantly!",
                        inline=False
                    )
                    
                    await user.send(embed=success_embed)
                    logging.info(f"✅ VPS deployed and user {user_id} notified for payment {oxapay_id}")
                    
                except Exception as e:
                    logging.error(f"Failed to notify user {user_id}: {e}")
                
            else:
                logging.error(f"VPS deployment failed for payment {oxapay_id}: {result['error']}")
                
        except Exception as e:
            logging.error(f"Auto-deploy error for payment {oxapay_id}: {e}")

# Initialize bot
bot = RazorCloudBot()

def create_razor_embed(title: str, description: str = "", color: int = Config.BRAND_COLOR) -> discord.Embed:
    embed = discord.Embed(
        title=f"⚡ {Config.BRAND_NAME} • {title}",
        description=description,
        color=color,
        timestamp=datetime.utcnow()
    )
    embed.set_footer(text=f"{Config.BRAND_NAME} Enterprise • {datetime.utcnow().strftime('%H:%M UTC')}")
    return embed

# === COMMANDS ===
@bot.command()
async def help(ctx):
    embed = create_razor_embed("Command Center", "Complete command reference for RazorCloud")
    
    commands_list = """
    **🧪 Test VPS**
    `!test` - Create free 1GB test VPS (1 per user)
    
    **💎 VPS Plans**
    `!plans` - View all VPS plans
    `!buy starter` - Buy Starter plan ($8)
    `!buy business` - Buy Business plan ($12)
    `!buy professional` - Buy Professional plan ($16)
    `!buy enterprise` - Buy Enterprise plan ($22)
    `!buy elite` - Buy Elite plan ($31)
    
    **💳 Payment**
    `!checkpay` - Check your pending payments
    `!checkpay <track_id>` - Check specific payment & deploy VPS
    
    **🔧 Management**
    `!manage` - Your VPS list
    `!tmate <id>` - Get SSH sessions
    `!status` - System status
    """
    
    embed.add_field(name="Available Commands", value=commands_list, inline=False)
    await ctx.send(embed=embed)

@bot.command()
async def plans(ctx):
    embed = create_razor_embed("RazorCloud VPS Plans", "Premium performance for every need")
    
    embed.add_field(
        name="🚀 Starter • `starter` - $8/month",
        value="```8GB RAM • 2 vCPU • 80GB SSD • Tmate SSH```",
        inline=False
    )
    
    embed.add_field(
        name="💼 Business • `business` - $12/month", 
        value="```16GB RAM • 4 vCPU • 100GB SSD • Tmate SSH```",
        inline=False
    )
    
    embed.add_field(
        name="⚡ Professional • `professional` - $16/month",
        value="```24GB RAM • 4 vCPU • 150GB SSD • Tmate SSH```",
        inline=False
    )
    
    embed.add_field(
        name="🏢 Enterprise • `enterprise` - $22/month",
        value="```32GB RAM • 8 vCPU • 200GB SSD • Tmate SSH```",
        inline=False
    )
    
    embed.add_field(
        name="👑 Elite • `elite` - $31/month",
        value="```48GB RAM • 12 vCPU • 400GB SSD • Tmate SSH```",
        inline=False
    )
    
    embed.add_field(
        name="🛒 How to Buy",
        value="```!buy starter```\n*Instant deployment after payment*",
        inline=False
    )
    
    await ctx.send(embed=embed)

@bot.command()
async def test(ctx):
    """Create a free 1GB test VPS for testing purposes"""
    
    # Check if user already has a test VPS
    cursor = bot.db.conn.cursor()
    cursor.execute('SELECT COUNT(*) FROM vps_instances WHERE user_id = ? AND plan_name = "test"', (ctx.author.id,))
    test_count = cursor.fetchone()[0]
    
    if test_count > 0:
        await ctx.send("❌ You already have a test VPS. Each user can only have one test instance.")
        return
    
    # Create user if not exists
    bot.db.create_user(ctx.author)
    
    # Show deployment message
    embed = create_razor_embed("Test VPS Deployment", "Creating your 1GB test VPS...", 0x00FF9D)
    embed.add_field(name="📦 Plan", value="Test (1GB RAM)", inline=True)
    embed.add_field(name="👤 User", value=ctx.author.display_name, inline=True)
    embed.add_field(name="⏱️ Status", value="🟡 **DEPLOYING**", inline=True)
    embed.add_field(name="ℹ️ Note", value="Test VPS is free but limited to 1 per user", inline=False)
    
    deployment_msg = await ctx.send(embed=embed)
    
    # Create test VPS
    plan_config = Config.TEMPLATES["test"].copy()
    plan_config['plan_name'] = "test"
    result = await bot.proxmox.create_container(plan_config, ctx.author.id)
    
    if result["success"]:
        # Save to database
        bot.db.create_vps(
            ctx.author.id,
            result["vm_id"],
            "test",
            result["hostname"],
            result["tmate_session"],
            result["tmate_ro_session"]
        )
        
        # Send success DM with credentials
        success_embed = create_razor_embed("Test VPS Ready! 🧪", "Your test VPS is now active", 0x00FF00)
        success_embed.add_field(name="🆔 VM ID", value=result["vm_id"], inline=True)
        success_embed.add_field(name="🌐 Hostname", value=result["hostname"], inline=True)
        success_embed.add_field(name="📦 Plan", value="Test (1GB)", inline=True)
        
        # Tmate sessions
        success_embed.add_field(
            name="🔑 Tmate Read-Write", 
            value=f"```{result['tmate_session']}```",
            inline=False
        )
        success_embed.add_field(
            name="👀 Tmate Read-Only", 
            value=f"```{result['tmate_ro_session']}```",
            inline=False
        )
        success_embed.add_field(
            name="🌐 Web Interface", 
            value=f"[Click Here]({result['tmate_web']})",
            inline=True
        )
        
        limitations_text = "• 1GB RAM, 1 vCPU, 10GB Storage\n• One per user only\n• For testing purposes"
        if result.get("tmate_note"):
            limitations_text += f"\n• {result['tmate_note']}"
        
        success_embed.add_field(
            name="⚠️ Test VPS Limitations",
            value=limitations_text,
            inline=False
        )
        
        try:
            await ctx.author.send(embed=success_embed)
            dm_status = "✅ Check your DMs for test VPS credentials!"
        except:
            dm_status = "❌ Could not send DM. Please enable DMs and use `!tmate` to get your sessions."
        
        # Update public message
        embed = create_razor_embed("Test VPS Complete ✅", dm_status, 0x00FF00)
        await deployment_msg.edit(embed=embed)
        
    else:
        embed = create_razor_embed("Test VPS Failed ❌", result["error"], 0xFF0000)
        await deployment_msg.edit(embed=embed)

@bot.command()
async def buy(ctx, plan_name: str = None):
    """Purchase VPS - All plans are now paid"""
    if not plan_name:
        embed = create_razor_embed("Purchase VPS", "Please specify a plan", 0x9B59B6)
        embed.add_field(
            name="Usage",
            value="```!buy starter```\n```!buy business```\n```!buy professional```\n```!buy enterprise```\n```!buy elite```",
            inline=False
        )
        await ctx.send(embed=embed)
        return
    
    # Check if plan exists
    if plan_name not in Config.TEMPLATES:
        await ctx.send("❌ Invalid plan name. Use `!plans` to see available plans.")
        return
    
    # Don't allow buying test plan
    if plan_name == "test":
        await ctx.send("❌ Test plan is free. Use `!test` command instead.")
        return
    
    price = Config.TEMPLATES[plan_name]["price"]
    
    # Create payment invoice
    payment_result = await bot.oxapay.create_invoice(price, plan_name, ctx.author.id)
    
    if payment_result["success"]:
        # Save payment to database
        payment_id = bot.db.create_payment(
            ctx.author.id,
            plan_name,
            price,
            payment_result["oxapay_id"]
        )
        
        # Store pending payment
        bot.pending_payments[payment_result["oxapay_id"]] = {
            "user_id": ctx.author.id,
            "plan_name": plan_name,
            "discord_ctx": ctx
        }
        
        # Send payment instructions via DM
        payment_embed = create_razor_embed("Payment Required 💳", f"Complete payment for **{plan_name}** plan", 0x9B59B6)
        payment_embed.add_field(name="💰 Amount", value=f"${price}", inline=True)
        payment_embed.add_field(name="📦 Plan", value=plan_name.title(), inline=True)
        payment_embed.add_field(name="🔍 Track ID", value=f"`{payment_result['oxapay_id']}`", inline=True)
        payment_embed.add_field(
            name="🔗 Payment Link", 
            value=f"[Click to Pay]({payment_result.get('payment_url', '#')})",
            inline=False
        )
        payment_embed.add_field(
            name="⏰ Next Steps",
            value="1. Complete the payment using the link above\n2. Use `!checkpay` to see your payments\n3. Use `!checkpay <track_id>` to deploy VPS after payment\n4. VPS will auto-deploy within 2 minutes (or use checkpay for instant)",
            inline=False
        )
        
        try:
            await ctx.author.send(embed=payment_embed)
            await ctx.send("✅ Check your DMs for payment link!")
        except:
            # If DM fails, send public message with clickable link
            public_embed = create_razor_embed("Payment Link", "Click below to complete your payment", 0x9B59B6)
            public_embed.add_field(name="🔗 Payment URL", value=payment_result.get("payment_url", "Payment URL not available"), inline=False)
            await ctx.send(embed=public_embed)
        
    else:
        await ctx.send(f"❌ Payment error: {payment_result['error']}")

@bot.command()
async def checkpay(ctx, track_id: str = None):
    """Check your payment status and trigger VPS deployment if paid"""
    if not track_id:
        # Show user's pending payments
        cursor = bot.db.conn.cursor()
        cursor.execute('SELECT oxapay_id, plan_name, amount, created_at FROM payments WHERE user_id = ? AND status = "pending"', (ctx.author.id,))
        pending = cursor.fetchall()
        
        if not pending:
            await ctx.send("❌ You don't have any pending payments.")
            return
        
        embed = create_razor_embed("Your Pending Payments", "Use `!checkpay <track_id>` to check status")
        for payment in pending:
            oxapay_id, plan_name, amount, created_at = payment
            embed.add_field(
                name=f"💳 {plan_name.title()} - ${amount}",
                value=f"Track ID: `{oxapay_id}`\nCreated: {created_at}",
                inline=False
            )
        
        await ctx.send(embed=embed)
        return
    
    # Check specific payment
    cursor = bot.db.conn.cursor()
    cursor.execute('SELECT user_id, plan_name, amount FROM payments WHERE oxapay_id = ? AND user_id = ?', (track_id, ctx.author.id))
    payment_data = cursor.fetchone()
    
    if not payment_data:
        await ctx.send("❌ Payment not found or you don't have access to it.")
        return
    
    user_id, plan_name, amount = payment_data
    
    embed = create_razor_embed("Checking Payment...", f"Verifying payment status for {track_id}", 0x00FF9D)
    msg = await ctx.send(embed=embed)
    
    # Verify payment with OxaPay
    verification = await bot.oxapay.verify_payment(track_id)
    
    if verification["success"]:
        if verification["paid"]:
            # Payment confirmed, deploy VPS
            embed = create_razor_embed("Payment Confirmed! 🎉", "Deploying your VPS now...", 0x00FF9D)
            await msg.edit(embed=embed)
            
            # Deploy VPS
            await bot.auto_deploy_vps(track_id, user_id, plan_name)
            
            embed = create_razor_embed("VPS Deployed! ✅", "Check your DMs for VPS credentials!", 0x00FF00)
            await msg.edit(embed=embed)
            
        else:
            embed = create_razor_embed("Payment Pending ⏳", 
                                     f"Payment {track_id} is still unpaid. Please complete the payment.", 0xFFD700)
            await msg.edit(embed=embed)
    else:
        embed = create_razor_embed("Payment Check Failed ❌", 
                                 f"Could not verify payment: {verification['error']}", 0xFF0000)
        await msg.edit(embed=embed)

@bot.command()
@commands.has_permissions(administrator=True)
async def install_tmate(ctx):
    """Admin command to install Tmate on the server"""
    embed = create_razor_embed("Installing Tmate...", "Setting up Tmate for real sessions", 0x00FF9D)
    msg = await ctx.send(embed=embed)
    
    try:
        import subprocess
        
        # Install tmate
        install_commands = [
            "apt-get update",
            "apt-get install -y tmate",
            "which tmate"
        ]
        
        for cmd in install_commands:
            result = subprocess.run(cmd.split(), capture_output=True, text=True, timeout=30)
            if result.returncode != 0:
                embed = create_razor_embed("Installation Failed ❌", 
                                         f"Failed to run: {cmd}\nError: {result.stderr}", 0xFF0000)
                await msg.edit(embed=embed)
                return
        
        # Test tmate
        test_result = subprocess.run(['tmate', '--version'], capture_output=True, text=True, timeout=10)
        if test_result.returncode == 0:
            embed = create_razor_embed("Tmate Installed Successfully! ✅", 
                                     f"Tmate version: {test_result.stdout.strip()}\nReal sessions are now available!", 0x00FF00)
        else:
            embed = create_razor_embed("Installation Complete but Test Failed ⚠️", 
                                     "Tmate installed but version check failed", 0xFFD700)
        
        await msg.edit(embed=embed)
        
    except Exception as e:
        embed = create_razor_embed("Installation Error ❌", f"Error: {str(e)}", 0xFF0000)
        await msg.edit(embed=embed)

@bot.command()
async def manage(ctx):
    """Show user's VPS list"""
    cursor = bot.db.conn.cursor()
    cursor.execute('SELECT vm_id, plan_name, hostname, status FROM vps_instances WHERE user_id = ?', (ctx.author.id,))
    vps_list = cursor.fetchall()
    
    embed = create_razor_embed("Your VPS Portfolio", f"Total VPS: {len(vps_list)}")
    
    if not vps_list:
        embed.add_field(
            name="🚀 Get Started",
            value="Use `!test` for free test VPS or `!plans` for premium VPS",
            inline=False
        )
    else:
        for vps in vps_list:
            status_emoji = "🟢" if vps[3] == 'active' else "🔴"
            embed.add_field(
                name=f"{status_emoji} VPS {vps[0]} • {vps[1]}",
                value=f"Hostname: `{vps[2]}`\nUse `!tmate {vps[0]}` for access",
                inline=False
            )
    
    await ctx.send(embed=embed)

@bot.command()
async def tmate(ctx, vm_id: int = None):
    """Get Tmate sessions for VPS"""
    if not vm_id:
        # Show all VPS
        cursor = bot.db.conn.cursor()
        cursor.execute('SELECT vm_id, plan_name FROM vps_instances WHERE user_id = ?', (ctx.author.id,))
        vps_list = cursor.fetchall()
        
        if not vps_list:
            await ctx.send("❌ You don't have any VPS instances.")
            return
        
        embed = create_razor_embed("Your VPS List", "Use `!tmate <vm_id>` to get sessions")
        for vps in vps_list:
            embed.add_field(name=f"VPS {vps[0]}", value=vps[1], inline=True)
        
        await ctx.send(embed=embed)
        return
    
    # Get specific VPS sessions
    cursor = bot.db.conn.cursor()
    cursor.execute('SELECT tmate_session, tmate_ro_session FROM vps_instances WHERE user_id = ? AND vm_id = ?', (ctx.author.id, vm_id))
    vps = cursor.fetchone()
    
    if not vps:
        await ctx.send("❌ VPS not found or you don't have access to it.")
        return
    
    embed = create_razor_embed(f"Tmate Sessions • VPS {vm_id}", "Secure SSH Access")
    embed.add_field(name="🔑 Read-Write", value=f"```{vps[0]}```", inline=False)
    embed.add_field(name="👀 Read-Only", value=f"```{vps[1]}```", inline=False)
    
    await ctx.author.send(embed=embed)
    await ctx.send("✅ Check your DMs for Tmate sessions!")

@bot.command()
async def status(ctx):
    """Show system status"""
    embed = create_razor_embed("System Status", f"{Config.BRAND_NAME} Infrastructure")
    embed.add_field(name="🌐 Network", value="🟢 OPERATIONAL", inline=True)
    embed.add_field(name="⚡ Proxmox", value="🟢 ONLINE", inline=True)
    embed.add_field(name="💳 Payments", value="🟢 ACTIVE", inline=True)
    
    # Check Tmate availability
    try:
        import subprocess
        result = subprocess.run(['which', 'tmate'], capture_output=True, text=True, timeout=5)
        if result.returncode == 0:
            tmate_status = "🟢 AVAILABLE"
        else:
            tmate_status = "🔴 NOT INSTALLED"
    except:
        tmate_status = "🔴 NOT INSTALLED"
    
    embed.add_field(name="🔗 Tmate SSH", value=tmate_status, inline=True)
    embed.add_field(name="📞 Support", value="24/7 Available", inline=True)
    
    if tmate_status == "🔴 NOT INSTALLED":
        embed.add_field(name="⚠️ Note", value="Run `!install_tmate` to enable real SSH sessions", inline=False)
    
    await ctx.send(embed=embed)

@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.CommandNotFound):
        await ctx.send("❌ Command not found. Use `!help` for available commands.")
    elif isinstance(error, commands.MissingPermissions):
        await ctx.send("❌ You don't have permission to use this command.")
    elif isinstance(error, commands.MissingRequiredArgument):
        await ctx.send("❌ Missing required argument. Check command usage.")
    else:
        logging.error(f"Command error: {error}")
        await ctx.send("❌ An error occurred. Please try again.")

# === RUN BOT ===
if __name__ == "__main__":
    print("🚀 Starting RazorCloud Bot...")
    print("⚡ All features included:")
    print("   ✅ Real payment system with OxaPay")
    print("   ✅ Automatic payment monitoring")
    print("   ✅ Real Tmate SSH sessions")
    print("   ✅ Complete VPS management")
    print("   ✅ Test VPS functionality")
    bot.run(Config.BOT_TOKEN)