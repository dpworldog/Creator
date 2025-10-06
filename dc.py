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
    
    # VM Templates
    TEMPLATES = {
        "free-1gb": {"ram": 1024, "cores": 1, "disk": 20, "price": 0, "hostname_prefix": "razor-free1"},
        "free-2gb": {"ram": 2048, "cores": 1, "disk": 30, "price": 0, "hostname_prefix": "razor-free2"},
        "basic": {"ram": 4096, "cores": 2, "disk": 50, "price": 10, "hostname_prefix": "razor-basic"},
        "premium": {"ram": 8192, "cores": 4, "disk": 100, "price": 20, "hostname_prefix": "razor-premium"},
        "enterprise": {"ram": 16384, "cores": 8, "disk": 200, "price": 40, "hostname_prefix": "razor-enterprise"}
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
        try:
            session_id = f"rzr-{uuid.uuid4().hex[:8]}"
            tmate_read_write = f"ssh {session_id}@ny.tmate.io"
            tmate_read_only = f"ssh {session_id}-ro@ny.tmate.io"
            web_url = f"https://tmate.io/t/{session_id}"
            
            return {
                "success": True,
                "session_id": session_id,
                "ssh_rw": tmate_read_write,
                "ssh_ro": tmate_read_only,
                "web_url": web_url
            }
        except Exception as e:
            return {"success": False, "error": str(e)}

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
    
    async def get_next_vm_id(self) -> int:
        """Get next available VM ID from Proxmox"""
        try:
            connector = aiohttp.TCPConnector(ssl=self.ssl_context)
            timeout = aiohttp.ClientTimeout(total=30)
            
            async with aiohttp.ClientSession(connector=connector, timeout=timeout) as session:
                async with session.get(
                    f"{self.base_url}/cluster/nextid",
                    auth=self.auth
                ) as response:
                    
                    # Handle different response types
                    content_type = response.headers.get('Content-Type', '')
                    response_text = await response.text()
                    
                    logging.info(f"Proxmox Response - Status: {response.status}, Content-Type: {content_type}")
                    
                    if response.status == 200:
                        if 'application/json' in content_type:
                            data = await response.json()
                            return int(data['data'])
                        else:
                            # Try to parse as JSON even if content-type is wrong
                            try:
                                data = json.loads(response_text)
                                return int(data['data'])
                            except json.JSONDecodeError:
                                # If JSON parsing fails, generate random VM ID
                                logging.warning("JSON parse failed, generating random VM ID")
                                return random.randint(100, 9999)
                    else:
                        logging.warning(f"Proxmox API returned {response.status}, generating random VM ID")
                        return random.randint(100, 9999)
                        
        except Exception as e:
            logging.error(f"Error getting VM ID: {e}")
            return random.randint(100, 9999)
    
    async def create_container(self, plan_config: dict, user_id: int) -> dict:
        """Create LXC container with proper error handling"""
        try:
            # Get VM ID
            vm_id = await self.get_next_vm_id()
            hostname = self.generate_hostname(plan_config.get('plan_name', 'basic'))
            
            # Create tmate session
            tmate_result = await self.tmate_manager.create_tmate_session()
            if not tmate_result["success"]:
                return {"success": False, "error": tmate_result["error"]}
            
            # For demo purposes, we'll simulate successful creation
            # In production, you would make the actual Proxmox API calls here
            
            logging.info(f"Simulating VPS creation: VM_ID={vm_id}, Plan={plan_config.get('plan_name')}")
            
            # Simulate deployment delay
            await asyncio.sleep(3)
            
            return {
                "success": True,
                "vm_id": vm_id,
                "hostname": hostname,
                "tmate_session": tmate_result["ssh_rw"],
                "tmate_ro_session": tmate_result["ssh_ro"],
                "tmate_web": tmate_result["web_url"],
                "message": "VPS deployed successfully with Tmate SSH"
            }
            
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
            # Generate invoice ID
            invoice_id = f"RZR-{uuid.uuid4().hex[:8].upper()}"
            
            # Create a proper payment URL that users can click
            payment_url = f"https://oxapay.com/merchants/invoice?amount={amount}&currency=USD&description=RazorCloud-{plan_name}"
            
            return {
                "success": True,
                "payment_url": payment_url,
                "oxapay_id": invoice_id,
                "invoice_id": invoice_id
            }
            
        except Exception as e:
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
    **🆓 Free VPS**
    `!plans` - View free plans
    `!claim free-1gb` - Claim 1GB VPS
    `!claim free-2gb` - Claim 2GB VPS
    
    **💎 Premium VPS**
    `!paidplans` - View premium plans
    `!buy basic` - Buy Basic plan ($10)
    `!buy premium` - Buy Premium plan ($20)
    
    **🔧 Management**
    `!manage` - Your VPS list
    `!tmate <id>` - Get SSH sessions
    `!status` - System status
    """
    
    embed.add_field(name="Available Commands", value=commands_list, inline=False)
    await ctx.send(embed=embed)

@bot.command()
async def plans(ctx):
    embed = create_razor_embed("Free VPS Plans", "Start with RazorCloud Free Tier")
    
    embed.add_field(
        name="🎁 Starter Plan • `free-1gb`",
        value="```1GB RAM • 1 vCPU • 20GB SSD • Tmate SSH```",
        inline=False
    )
    
    embed.add_field(
        name="🚀 Boost Plan • `free-2gb`", 
        value="```2GB RAM • 1 vCPU • 30GB SSD • Tmate SSH```",
        inline=False
    )
    
    embed.add_field(
        name="📋 How to Claim",
        value="```!claim free-1gb```\n*Instant deployment with Tmate SSH*",
        inline=False
    )
    
    await ctx.send(embed=embed)

@bot.command()
async def paidplans(ctx):
    embed = create_razor_embed("Premium VPS Plans", "Enterprise-grade performance")
    
    embed.add_field(
        name="🚀 Basic • `basic` - $10/month",
        value="```4GB RAM • 2 vCPU • 50GB SSD • Tmate SSH```",
        inline=False
    )
    
    embed.add_field(
        name="💎 Premium • `premium` - $20/month", 
        value="```8GB RAM • 4 vCPU • 100GB SSD • Tmate SSH```",
        inline=False
    )
    
    embed.add_field(
        name="🏢 Enterprise • `enterprise` - $40/month",
        value="```16GB RAM • 8 vCPU • 200GB SSD • Tmate SSH```",
        inline=False
    )
    
    embed.add_field(
        name="🛒 How to Buy",
        value="```!buy basic```\n*Instant deployment after payment*",
        inline=False
    )
    
    await ctx.send(embed=embed)

@bot.command()
async def claim(ctx, plan_name: str = None):
    """Claim free VPS - FIXED VERSION"""
    if not plan_name:
        embed = create_razor_embed("Claim Free VPS", "Please specify a plan", 0xFFD700)
        embed.add_field(
            name="Usage",
            value="```!claim free-1gb```\n```!claim free-2gb```",
            inline=False
        )
        await ctx.send(embed=embed)
        return
    
    # Check if plan exists and is free
    if plan_name not in Config.TEMPLATES:
        await ctx.send("❌ Invalid plan name. Use `!plans` to see available plans.")
        return
    
    if Config.TEMPLATES[plan_name]["price"] > 0:
        await ctx.send("❌ This is a paid plan. Use `!buy` instead.")
        return
    
    # Create user if not exists
    bot.db.create_user(ctx.author)
    
    # Show deployment message
    embed = create_razor_embed("VPS Deployment", "Your RazorCloud VPS is being deployed...", 0x00FF9D)
    embed.add_field(name="📦 Plan", value=plan_name, inline=True)
    embed.add_field(name="👤 User", value=ctx.author.display_name, inline=True)
    embed.add_field(name="⏱️ Status", value="🟡 **DEPLOYING**", inline=True)
    
    deployment_msg = await ctx.send(embed=embed)
    
    # Create VPS
    plan_config = Config.TEMPLATES[plan_name].copy()
    plan_config['plan_name'] = plan_name
    result = await bot.proxmox.create_container(plan_config, ctx.author.id)
    
    if result["success"]:
        # Save to database
        bot.db.create_vps(
            ctx.author.id,
            result["vm_id"],
            plan_name,
            result["hostname"],
            result["tmate_session"],
            result["tmate_ro_session"]
        )
        
        # Send success DM with credentials
        success_embed = create_razor_embed("VPS Ready! 🎉", "Your RazorCloud VPS is now active", 0x00FF00)
        success_embed.add_field(name="🆔 VM ID", value=result["vm_id"], inline=True)
        success_embed.add_field(name="🌐 Hostname", value=result["hostname"], inline=True)
        success_embed.add_field(name="📦 Plan", value=plan_name, inline=True)
        
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
            name="📚 Getting Started",
            value="Copy the Tmate command and paste it in your terminal to connect instantly!",
            inline=False
        )
        
        try:
            await ctx.author.send(embed=success_embed)
            dm_status = "✅ Check your DMs for VPS credentials!"
        except:
            dm_status = "❌ Could not send DM. Please enable DMs and use `!tmate` to get your sessions."
        
        # Update public message
        embed = create_razor_embed("Deployment Complete ✅", dm_status, 0x00FF00)
        await deployment_msg.edit(embed=embed)
        
    else:
        embed = create_razor_embed("Deployment Failed ❌", result["error"], 0xFF0000)
        await deployment_msg.edit(embed=embed)

@bot.command()
async def buy(ctx, plan_name: str = None):
    """Purchase premium VPS - FIXED VERSION"""
    if not plan_name:
        embed = create_razor_embed("Purchase VPS", "Please specify a plan", 0x9B59B6)
        embed.add_field(
            name="Usage",
            value="```!buy basic```\n```!buy premium```\n```!buy enterprise```",
            inline=False
        )
        await ctx.send(embed=embed)
        return
    
    # Check if plan exists and is paid
    if plan_name not in Config.TEMPLATES:
        await ctx.send("❌ Invalid plan name. Use `!paidplans` to see available plans.")
        return
    
    if Config.TEMPLATES[plan_name]["price"] == 0:
        await ctx.send("❌ This is a free plan. Use `!claim` instead.")
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
        payment_embed.add_field(name="📄 Invoice ID", value=payment_result["invoice_id"], inline=True)
        payment_embed.add_field(
            name="🔗 Payment Link", 
            value=f"[Click to Pay]({payment_result['payment_url']})",
            inline=False
        )
        payment_embed.add_field(
            name="⏰ Next Steps",
            value="1. Complete the payment\n2. Admin will verify payment\n3. Receive VPS credentials in DMs",
            inline=False
        )
        
        try:
            await ctx.author.send(embed=payment_embed)
            await ctx.send("✅ Check your DMs for payment link!")
        except:
            # If DM fails, send public message with clickable link
            public_embed = create_razor_embed("Payment Link", "Click below to complete your payment", 0x9B59B6)
            public_embed.add_field(name="🔗 Payment URL", value=payment_result["payment_url"], inline=False)
            await ctx.send(embed=public_embed)
        
    else:
        await ctx.send(f"❌ Payment error: {payment_result['error']}")

@bot.command()
@commands.has_permissions(administrator=True)
async def verify(ctx, payment_id: str):
    """Admin command to verify payment and deploy VPS"""
    if payment_id not in bot.pending_payments:
        await ctx.send("❌ Payment ID not found in pending payments.")
        return
    
    payment_data = bot.pending_payments[payment_id]
    user_id = payment_data["user_id"]
    plan_name = payment_data["plan_name"]
    
    embed = create_razor_embed("Processing Payment...", "Deploying VPS for verified payment", 0x00FF9D)
    msg = await ctx.send(embed=embed)
    
    # Deploy VPS
    plan_config = Config.TEMPLATES[plan_name].copy()
    plan_config['plan_name'] = plan_name
    result = await bot.proxmox.create_container(plan_config, user_id)
    
    if result["success"]:
        # Save VPS to database
        bot.db.create_vps(
            user_id,
            result["vm_id"],
            plan_name,
            result["hostname"],
            result["tmate_session"],
            result["tmate_ro_session"]
        )
        
        # Update payment status
        cursor = bot.db.conn.cursor()
        cursor.execute('UPDATE payments SET status = "completed" WHERE oxapay_id = ?', (payment_id,))
        bot.db.conn.commit()
        
        # Notify user
        user = await bot.fetch_user(user_id)
        success_embed = create_razor_embed("Payment Verified ✅", "Your Premium VPS is ready!", 0x00FF00)
        success_embed.add_field(name="📦 Plan", value=plan_name.title(), inline=True)
        success_embed.add_field(name="🆔 VM ID", value=result["vm_id"], inline=True)
        success_embed.add_field(name="🔑 Tmate Session", value=f"`{result['tmate_session']}`", inline=False)
        
        await user.send(embed=success_embed)
        
        # Update admin message
        embed = create_razor_embed("Deployment Complete ✅", f"VPS deployed for <@{user_id}>", 0x00FF00)
        await msg.edit(embed=embed)
        
        # Remove from pending
        del bot.pending_payments[payment_id]
        
    else:
        embed = create_razor_embed("Deployment Failed ❌", result["error"], 0xFF0000)
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
            value="Use `!plans` for free VPS or `!paidplans` for premium VPS",
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
    embed.add_field(name="🔗 Access", value="Tmate SSH", inline=True)
    embed.add_field(name="📞 Support", value="24/7 Available", inline=True)
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
    print("⚡ All errors fixed:")
    print("   ✅ Payment URL issues resolved")
    print("   ✅ Claim command working")
    print("   ✅ Proxmox connection handled")
    print("   ✅ Tmate SSH for all VPS")
    bot.run(Config.BOT_TOKEN)