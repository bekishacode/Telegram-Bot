import os
import logging
import asyncio
import re
from dotenv import load_dotenv
from telegram import Bot, Update, ReplyKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from flask import Flask, request, jsonify
import requests
import json
import time

load_dotenv()

# Configuration
BOT_TOKEN = os.getenv('BOT_TOKEN')
SF_INSTANCE_URL = os.getenv('SF_INSTANCE_URL')
SF_CLIENT_ID = os.getenv('SF_CLIENT_ID')
SF_CLIENT_SECRET = os.getenv('SF_CLIENT_SECRET')

# Salesforce Configuration
SF_OBJECT_NAME = "Contact"
SF_CHAT_ID_FIELD = "Telegram_Chat_ID__c"

# Telegram Group IDs
TELEGRAM_GROUPS = {
    "main_promotions": os.getenv('MAIN_GROUP_ID', ""),
    "announcements": os.getenv('ANNOUNCEMENTS_GROUP_ID', "")
}

app = Flask(__name__)

# Configure logging for production
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

class TelegramMiddleware:
    def __init__(self):
        # Initialize bot components
        self.bot = None
        self.application = None
        self.access_token = None
        self.token_expiry = 0
        self.initialize_bot()
        logger.info("🚀 Telegram-Salesforce Middleware Started")
    
    def initialize_bot(self):
        """Initialize bot components with error handling"""
        try:
            # Create bot and application
            self.bot = Bot(token=BOT_TOKEN)
            self.application = Application.builder().token(BOT_TOKEN).build()
            
            # Setup handlers
            self.setup_handlers()
            
            # Initialize the application (synchronously)
            asyncio.run(self.application.initialize())
            
            logger.info("✅ Bot initialized successfully")
        except Exception as e:
            logger.error(f"❌ Failed to initialize bot: {e}")
    
    def setup_handlers(self):
        """Setup Telegram bot handlers"""
        if not self.application:
            return
            
        self.application.add_handler(CommandHandler("start", self.start_command))
        self.application.add_handler(CommandHandler("register", self.register_command))
        self.application.add_handler(CommandHandler("help", self.help_command))
        self.application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.handle_message))
        self.application.add_handler(MessageHandler(filters.CONTACT, self.handle_contact_share))
    
    async def start_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /start - Find existing Contact and store Chat ID"""
        user = update.effective_user
        chat_id = update.effective_chat.id
        
        logger.info(f"👤 User started bot: {user.first_name} (ID: {chat_id})")
        
        # Check if this Chat ID is already registered
        existing_contact = await self.find_contact_by_chat_id(chat_id)
        
        if existing_contact:
            contact_name = existing_contact.get('Name', 'Valued Customer')
            await update.message.reply_text(
                f"👋 Welcome back, {contact_name}!\n\n"
                "You're already registered for promotions."
            )
            return
        
        # New user - ask for phone number to find their Contact
        keyboard = [
            ["📱 Share Phone Number", "📧 Enter Email Address"],
            ["❌ I don't have an account"]
        ]
        
        reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True, one_time_keyboard=True)
        
        await update.message.reply_text(
            f"👋 Welcome to Bank of Abyssinia, {user.first_name}!\n\n"
            "To connect with your existing account and receive personalized promotions, "
            "please share your phone number or email address.",
            reply_markup=reply_markup
        )
    
    async def register_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /register - Restart registration process"""
        keyboard = [
            ["📱 Share Phone Number", "📧 Enter Email Address"]
        ]
        
        reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True, one_time_keyboard=True)
        
        await update.message.reply_text(
            "Please share your phone number or email to connect with your account:",
            reply_markup=reply_markup
        )
    
    async def help_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /help command"""
        help_text = """
🤖 **Bank of Abyssinia Telegram Bot**

**Available Commands:**
/start - Start bot and register for promotions
/help - Show this help message  
/register - Connect your Salesforce account

**About:**
This bot sends you personalized promotions and updates from Bank of Abyssinia through Salesforce integration.
"""
        await update.message.reply_text(help_text, parse_mode='Markdown')
    
    async def handle_contact_share(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle phone number sharing via contact button"""
        contact = update.message.contact
        chat_id = update.effective_chat.id
        user = update.effective_user
        
        if contact:
            phone_number = contact.phone_number
            logger.info(f"📱 Received contact: {phone_number} from {chat_id}")
            
            # Extract last 9 digits for matching
            last_9_digits = self.extract_last_9_digits(phone_number)
            
            # Search for Contact by phone number (matching last 9 digits)
            contact_record = await self.find_contact_by_phone(last_9_digits)
            
            if contact_record:
                # Found Contact - update with Chat ID
                success = await self.update_contact_chat_id(contact_record['Id'], chat_id, user)
                
                if success:
                    contact_name = contact_record.get('Name', 'Valued Customer')
                    await update.message.reply_text(
                        f"✅ Successfully connected, {contact_name}!\n\n"
                        "You will now receive personalized promotions and updates.",
                        reply_markup=None
                    )
                else:
                    await update.message.reply_text(
                        "❌ Connection failed. Please try again.",
                        reply_markup=None
                    )
            else:
                # No Contact found - create new Contact
                success = await self.create_new_contact(
                    first_name=user.first_name,
                    last_name=user.last_name or "Telegram User",
                    phone_number=phone_number,
                    chat_id=chat_id,
                    user=user
                )
                
                if success:
                    await update.message.reply_text(
                        "✅ Welcome! We've created a new account for you.\n\n"
                        "You will now receive promotions and updates from Bank of Abyssinia.",
                        reply_markup=None
                    )
                else:
                    await update.message.reply_text(
                        "❌ Failed to create account. Please try again or contact support.",
                        reply_markup=None
                    )
    
    async def handle_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle text messages for phone/email input"""
        message_text = update.message.text
        chat_id = update.effective_chat.id
        user = update.effective_user
        
        if message_text == "❌ I don't have an account":
            await update.message.reply_text(
                "Please contact our customer support to create an account first.",
                reply_markup=None
            )
            return
        
        if message_text in ["📱 Share Phone Number", "📧 Enter Email Address"]:
            if message_text == "📱 Share Phone Number":
                await update.message.reply_text(
                    "Please enter your phone number:\n\nExamples: 0912121212, 0712121212, 912121212, +251912121212",
                    reply_markup=None
                )
            else:
                await update.message.reply_text(
                    "Please enter your email address:",
                    reply_markup=None
                )
            return
        
        # Check if message is a phone number
        if self.is_phone_number(message_text):
            logger.info(f"📞 Received phone: {message_text} from {chat_id}")
            
            # Extract last 9 digits for matching
            last_9_digits = self.extract_last_9_digits(message_text)
            
            contact_record = await self.find_contact_by_phone(last_9_digits)
            
            if contact_record:
                success = await self.update_contact_chat_id(contact_record['Id'], chat_id, user)
                if success:
                    contact_name = contact_record.get('Name', 'Valued Customer')
                    await update.message.reply_text(
                        f"✅ Successfully connected, {contact_name}! You'll receive promotions soon.",
                        reply_markup=None
                    )
                else:
                    await update.message.reply_text("❌ Connection failed. Please try again.")
            else:
                # No Contact found - create new one
                success = await self.create_new_contact(
                    first_name=user.first_name,
                    last_name=user.last_name or "Telegram User",
                    phone_number=message_text,
                    chat_id=chat_id,
                    user=user
                )
                
                if success:
                    await update.message.reply_text(
                        "✅ Welcome! We've created a new account for you.\n\n"
                        "You will now receive promotions and updates from Bank of Abyssinia.",
                        reply_markup=None
                    )
                else:
                    await update.message.reply_text(
                        "❌ Failed to create account. Please try again or contact support.",
                        reply_markup=None
                    )
        
        # Check if message is an email
        elif self.is_email(message_text):
            logger.info(f"📧 Received email: {message_text} from {chat_id}")
            
            contact_record = await self.find_contact_by_email(message_text)
            
            if contact_record:
                success = await self.update_contact_chat_id(contact_record['Id'], chat_id, user)
                if success:
                    contact_name = contact_record.get('Name', 'Valued Customer')
                    await update.message.reply_text(
                        f"✅ Successfully connected, {contact_name}! You'll receive promotions soon.",
                        reply_markup=None
                    )
                else:
                    await update.message.reply_text("❌ Connection failed. Please try again.")
            else:
                await update.message.reply_text(
                    "❌ No account found with this email.\n\n"
                    "Please share your phone number to create a new account:\n\nExamples: 0912121212, 0712121212, 912121212",
                    reply_markup=None
                )
    
    async def create_new_contact(self, first_name, last_name, phone_number, chat_id, user):
        """Create a new Contact record when no existing match is found"""
        try:
            access_token = await self.get_salesforce_token()
            if not access_token:
                return False
            
            contact_data = {
                "FirstName": first_name,
                "LastName": last_name,
                "MobilePhone": phone_number,
                "Phone": phone_number,
                SF_CHAT_ID_FIELD: str(chat_id)
            }
            
            if user.username:
                contact_data["Telegram_Username__c"] = user.username
            
            logger.info(f"🆕 Creating new Contact: {first_name} {last_name}, Phone: {phone_number}")
            
            success = await self.salesforce_create(SF_OBJECT_NAME, contact_data, access_token)
            
            if success:
                logger.info(f"✅ Created new Contact for {first_name} {last_name} with phone {phone_number}")
            else:
                logger.error(f"❌ Failed to create new Contact for {first_name} {last_name}")
            
            return success
            
        except Exception as e:
            logger.error(f"❌ Error creating new contact: {e}")
            return False
    
    async def find_contact_by_chat_id(self, chat_id):
        """Find Contact by existing Chat ID"""
        try:
            access_token = await self.get_salesforce_token()
            if not access_token:
                return None
            
            query = f"SELECT Id, Name, FirstName, LastName, Phone, MobilePhone, Email FROM Contact WHERE {SF_CHAT_ID_FIELD} = '{chat_id}' LIMIT 1"
            return await self.salesforce_query(query, access_token)
            
        except Exception as e:
            logger.error(f"Error finding contact by chat ID: {e}")
            return None
    
    async def find_contact_by_phone(self, last_9_digits):
        """Find Contact by phone number - match by last 9 digits"""
        try:
            access_token = await self.get_salesforce_token()
            if not access_token:
                return None
            
            query = f"""
            SELECT Id, Name, FirstName, LastName, Phone, MobilePhone, Email 
            FROM Contact 
            WHERE Phone LIKE '%{last_9_digits}'
               OR MobilePhone LIKE '%{last_9_digits}'
            LIMIT 1
            """
            
            return await self.salesforce_query(query, access_token)
            
        except Exception as e:
            logger.error(f"Error finding contact by phone: {e}")
            return None
    
    async def find_contact_by_email(self, email):
        """Find Contact by email address"""
        try:
            access_token = await self.get_salesforce_token()
            if not access_token:
                return None
            
            clean_email = email.strip().lower()
            query = f"SELECT Id, Name, FirstName, LastName, Phone, Email FROM Contact WHERE Email = '{clean_email}' LIMIT 1"
            
            return await self.salesforce_query(query, access_token)
            
        except Exception as e:
            logger.error(f"Error finding contact by email: {e}")
            return None
    
    async def update_contact_chat_id(self, contact_id, chat_id, user):
        """Update Contact with Telegram Chat ID"""
        try:
            access_token = await self.get_salesforce_token()
            if not access_token:
                return False
            
            endpoint = f"{SF_INSTANCE_URL}/services/data/v65.0/sobjects/Contact/{contact_id}"
            
            update_data = {
                SF_CHAT_ID_FIELD: str(chat_id)
            }
            
            if user.username:
                update_data["Telegram_Username__c"] = user.username
            if user.first_name:
                update_data["Telegram_First_Name__c"] = user.first_name
            if user.last_name:
                update_data["Telegram_Last_Name__c"] = user.last_name
            
            headers = {
                'Authorization': f'Bearer {access_token}',
                'Content-Type': 'application/json'
            }
            
            logger.info(f"🔄 Updating Contact {contact_id} with Chat ID: {chat_id}")
            
            response = requests.patch(endpoint, headers=headers, json=update_data)
            
            if response.status_code == 204:
                logger.info(f"✅ Successfully updated Contact {contact_id} with Chat ID: {chat_id}")
                return True
            else:
                logger.error(f"❌ Failed to update Contact {contact_id}: {response.status_code} - {response.text}")
                return False
                
        except Exception as e:
            logger.error(f"❌ Error updating contact chat ID: {e}")
            return False
    
    async def get_salesforce_token(self):
        """Get Salesforce access token using client_credentials"""
        try:
            if self.access_token and time.time() < (self.token_expiry - 300):
                return self.access_token
            
            token_url = f"{SF_INSTANCE_URL}/services/oauth2/token"
            payload = {
                'grant_type': 'client_credentials',
                'client_id': SF_CLIENT_ID,
                'client_secret': SF_CLIENT_SECRET
            }
            
            headers = {'Content-Type': 'application/x-www-form-urlencoded'}
            
            response = requests.post(token_url, data=payload, headers=headers)
            
            if response.status_code == 200:
                token_data = response.json()
                self.access_token = token_data['access_token']
                self.token_expiry = time.time() + token_data.get('expires_in', 3600)
                logger.info("✅ Salesforce access token acquired")
                return self.access_token
            else:
                logger.error(f"❌ Token request failed: {response.status_code} - {response.text}")
                return None
                
        except Exception as e:
            logger.error(f"❌ Token exception: {e}")
            return None
    
    async def salesforce_query(self, query, access_token):
        """Execute SOQL query"""
        try:
            headers = {
                'Authorization': f'Bearer {access_token}',
                'Content-Type': 'application/json'
            }
            
            url = f"{SF_INSTANCE_URL}/services/data/v58.0/query"
            params = {'q': query}
            
            response = requests.get(url, headers=headers, params=params)
            
            if response.status_code == 200:
                data = response.json()
                return data['records'][0] if data['totalSize'] > 0 else None
            else:
                logger.error(f"Salesforce query error: {response.status_code} - {response.text}")
                return None
                
        except Exception as e:
            logger.error(f"Salesforce query exception: {e}")
            return None
    
    async def salesforce_create(self, object_name, create_data, access_token):
        """Create Salesforce record"""
        try:
            headers = {
                'Authorization': f'Bearer {access_token}',
                'Content-Type': 'application/json'
            }
            
            url = f"{SF_INSTANCE_URL}/services/data/v58.0/sobjects/{object_name}/"
            
            response = requests.post(url, headers=headers, json=create_data)
            
            if response.status_code == 201:
                logger.info(f"✅ Successfully created {object_name} record")
                return True
            else:
                logger.error(f"❌ Failed to create {object_name}: {response.status_code} - {response.text}")
                return False
                
        except Exception as e:
            logger.error(f"Salesforce create exception: {e}")
            return False
    
    async def send_to_user(self, chat_id, message, attachment_url=None):
        """Send message to specific Telegram user"""
        try:
            if not self.bot:
                logger.error("❌ Bot not initialized")
                return False
                
            if attachment_url:
                await self.bot.send_photo(
                    chat_id=chat_id,
                    photo=attachment_url,
                    caption=message,
                    parse_mode='HTML'
                )
            else:
                await self.bot.send_message(
                    chat_id=chat_id,
                    text=message,
                    parse_mode='HTML'
                )
            
            logger.info(f"✅ Message sent to user {chat_id}")
            return True
            
        except Exception as e:
            logger.error(f"❌ Failed to send to user {chat_id}: {e}")
            return False
    
    async def send_to_group(self, group_id, message, attachment_url=None):
        """Send message to Telegram group"""
        try:
            if not self.bot:
                logger.error("❌ Bot not initialized")
                return False
                
            if attachment_url:
                await self.bot.send_photo(
                    chat_id=group_id,
                    photo=attachment_url,
                    caption=message,
                    parse_mode='HTML'
                )
            else:
                await self.bot.send_message(
                    chat_id=group_id,
                    text=message,
                    parse_mode='HTML'
                )
            
            logger.info(f"✅ Message sent to group {group_id}")
            return True
            
        except Exception as e:
            logger.error(f"❌ Failed to send to group {group_id}: {e}")
            return False

    def is_phone_number(self, text):
        phone_pattern = r'^(\+?251|0)?[97]\d{8}$'
        return re.match(phone_pattern, text.strip()) is not None
    
    def is_email(self, text):
        email_pattern = r'^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$'
        return re.match(email_pattern, text.strip()) is not None
    
    def extract_last_9_digits(self, phone):
        digits = re.sub(r'[^\d]', '', phone)
        return digits[-9:] if len(digits) >= 9 else digits

# Initialize middleware
middleware = TelegramMiddleware()

# Flask Routes

@app.route('/api/send-to-user', methods=['POST'])
def api_send_to_user():
    try:
        data = request.get_json()
        
        if not data or 'chat_id' not in data or 'message' not in data:
            return jsonify({'error': 'Missing chat_id or message'}), 400
        
        chat_id = data['chat_id']
        message = data['message']
        attachment_url = data.get('attachment_url')
        
        # Use asyncio.run for simplicity
        success = asyncio.run(middleware.send_to_user(chat_id, message, attachment_url))
        
        if success:
            return jsonify({
                'status': 'success', 
                'message': 'Message sent to Telegram user',
                'chat_id': chat_id
            })
        else:
            return jsonify({'error': 'Failed to send message to Telegram user'}), 500
            
    except Exception as e:
        logger.error(f"❌ Send to user error: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/send-to-group', methods=['POST'])
def api_send_to_group():
    try:
        data = request.get_json()
        
        if not data or 'message' not in data:
            return jsonify({'error': 'Missing message'}), 400
        
        group_id = data.get('group_id', TELEGRAM_GROUPS.get('main_promotions'))
        message = data['message']
        attachment_url = data.get('attachment_url')
        
        if not group_id:
            return jsonify({'error': 'No group ID configured'}), 400
        
        success = asyncio.run(middleware.send_to_group(group_id, message, attachment_url))
        
        if success:
            return jsonify({
                'status': 'success', 
                'message': 'Message sent to Telegram group',
                'group_id': group_id
            })
        else:
            return jsonify({'error': 'Failed to send message to Telegram group'}), 500
            
    except Exception as e:
        logger.error(f"❌ Send to group error: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/send-to-all-contacts', methods=['POST'])
def api_send_to_all_contacts():
    try:
        data = request.get_json()
        
        if not data or 'message' not in data:
            return jsonify({'error': 'Missing message'}), 400
        
        message = data['message']
        attachment_url = data.get('attachment_url')
        
        access_token = asyncio.run(middleware.get_salesforce_token())
        if not access_token:
            return jsonify({'error': 'Failed to get Salesforce access token'}), 500
        
        query = f"SELECT Id, Name, {SF_CHAT_ID_FIELD} FROM Contact WHERE {SF_CHAT_ID_FIELD} != null"
        
        headers = {
            'Authorization': f'Bearer {access_token}',
            'Content-Type': 'application/json'
        }
        
        url = f"{SF_INSTANCE_URL}/services/data/v58.0/query"
        params = {'q': query}
        
        response = requests.get(url, headers=headers, params=params)
        
        if response.status_code != 200:
            return jsonify({'error': f'Salesforce query failed: {response.text}'}), 500
        
        data = response.json()
        contacts = data['records']
        
        if not contacts:
            return jsonify({'status': 'success', 'message': 'No registered contacts found', 'sent_count': 0})
        
        results = []
        for contact in contacts:
            chat_id = contact[SF_CHAT_ID_FIELD]
            if chat_id:
                success = asyncio.run(middleware.send_to_user(chat_id, message, attachment_url))
                results.append({
                    'contact_id': contact['Id'],
                    'contact_name': contact.get('Name', 'Unknown'),
                    'chat_id': chat_id,
                    'success': success
                })
        
        successful_sends = sum(1 for r in results if r['success'])
        
        return jsonify({
            'status': 'success',
            'message': f'Message sent to {successful_sends}/{len(contacts)} registered contacts',
            'total_contacts': len(contacts),
            'successful_sends': successful_sends,
            'failed_sends': len(contacts) - successful_sends,
            'results': results
        })
            
    except Exception as e:
        logger.error(f"❌ Send to all contacts error: {e}")
        return jsonify({'error': str(e)}), 500

# Webhook Route
@app.route('/webhook', methods=['POST'])
def webhook():
    """Handle incoming Telegram updates via webhook"""
    try:
        if request.is_json:
            update_data = request.get_json()
            logger.info(f"📥 Received webhook update: {update_data}")
            
            # Process the update
            if middleware.application:
                update = Update.de_json(update_data, middleware.application.bot)
                asyncio.run(middleware.application.process_update(update))
                logger.info("✅ Webhook update processed successfully")
            else:
                logger.error("❌ Application not initialized")
            
            return jsonify({'status': 'ok'})
        else:
            logger.error("❌ Webhook received non-JSON data")
            return jsonify({'error': 'Invalid data'}), 400
    except Exception as e:
        logger.error(f"❌ Webhook error: {e}")
        return jsonify({'error': str(e)}), 500

def setup_webhook():
    """Set up Telegram webhook"""
    try:
        if not middleware.bot:
            logger.error("❌ Bot not initialized for webhook setup")
            return False
        
        # Get your Render app URL
        webhook_url = "https://telegram-bot-fotq.onrender.com/webhook"
        
        logger.info(f"🔗 Setting webhook to: {webhook_url}")
        
        # Set webhook using asyncio.run
        success = asyncio.run(middleware.bot.set_webhook(webhook_url))
        
        if success:
            logger.info("✅ Webhook set successfully")
            return True
        else:
            logger.error("❌ Failed to set webhook")
            return False
            
    except Exception as e:
        logger.error(f"❌ Webhook setup error: {e}")
        return False

@app.route('/set-webhook', methods=['GET'])
def set_webhook_route():
    """Manual webhook setup endpoint"""
    success = setup_webhook()
    return jsonify({'success': success, 'message': 'Webhook setup attempted'})

@app.route('/delete-webhook', methods=['GET'])
def delete_webhook():
    """Delete webhook endpoint"""
    try:
        if middleware.bot:
            success = asyncio.run(middleware.bot.delete_webhook())
            return jsonify({'success': success, 'message': 'Webhook deleted'})
        else:
            return jsonify({'success': False, 'message': 'Bot not initialized'})
    except Exception as e:
        logger.error(f"❌ Delete webhook error: {e}")
        return jsonify({'success': False, 'message': str(e)})

# Add the debug endpoint
@app.route('/debug', methods=['GET'])
def debug_info():
    """Debug endpoint to check bot status"""
    bot_status = "Initialized" if middleware.bot else "Not Initialized"
    app_status = "Initialized" if middleware.application else "Not Initialized"
    sf_token_status = "Available" if middleware.access_token else "Not Available"
    
    return jsonify({
        'bot_status': bot_status,
        'application_status': app_status,
        'salesforce_token_status': sf_token_status,
        'environment_variables_set': {
            'BOT_TOKEN': bool(BOT_TOKEN),
            'SF_INSTANCE_URL': bool(SF_INSTANCE_URL),
            'SF_CLIENT_ID': bool(SF_CLIENT_ID),
            'SF_CLIENT_SECRET': bool(SF_CLIENT_SECRET)
        }
    })

@app.route('/bot-status', methods=['GET'])
def bot_status():
    """Check bot status"""
    try:
        if not middleware.bot:
            return jsonify({'error': 'Bot not initialized'}), 500
            
        # Get bot info
        bot_info = asyncio.run(middleware.bot.get_me())
        
        # Get webhook info
        webhook_info = asyncio.run(middleware.bot.get_webhook_info())
        
        status_info = {
            'bot_initialized': bool(middleware.bot),
            'application_initialized': bool(middleware.application),
            'bot_username': bot_info.username,
            'bot_first_name': bot_info.first_name,
            'webhook_info': {
                'url': webhook_info.url,
                'has_custom_certificate': webhook_info.has_custom_certificate,
                'pending_update_count': webhook_info.pending_update_count,
                'last_error_date': webhook_info.last_error_date,
                'last_error_message': webhook_info.last_error_message,
                'max_connections': webhook_info.max_connections,
                'allowed_updates': webhook_info.allowed_updates
            }
        }
        
        return jsonify(status_info)
        
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/health', methods=['GET'])
def health_check():
    return jsonify({
        'status': 'healthy', 
        'service': 'telegram-salesforce-middleware',
        'available_groups': TELEGRAM_GROUPS,
        'salesforce_object': SF_OBJECT_NAME,
        'chat_id_field': SF_CHAT_ID_FIELD
    })

@app.route('/')
def home():
    bot_status = "✅ Running" if middleware.bot else "❌ Not Running"
    return jsonify({
        'message': 'Telegram-Salesforce Bot is running!',
        'bot_status': bot_status,
        'endpoints': {
            'debug': 'GET /debug',
            'health': 'GET /health',
            'bot_status': 'GET /bot-status',
            'set_webhook': 'GET /set-webhook',
            'delete_webhook': 'GET /delete-webhook',
            'send_to_all_contacts': 'POST /api/send-to-all-contacts',
            'send_to_group': 'POST /api/send-to-group',
            'send_to_user': 'POST /api/send-to-user',
            'webhook': 'POST /webhook'
        }
    })

# Setup webhook when app starts
@app.before_first_request
def setup_webhook_on_start():
    """Setup webhook when the application starts"""
    logger.info("🔄 Setting up webhook on application start...")
    setup_webhook()

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)