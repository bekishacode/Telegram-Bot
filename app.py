import os
import logging
import requests
import time
import re
import json
from flask import Flask, request, jsonify

# Configuration with validation
BOT_TOKEN = os.getenv('BOT_TOKEN')
SALESFORCE_WEBHOOK_URL = os.getenv('SALESFORCE_WEBHOOK_URL')
SF_INSTANCE_URL = os.getenv('SF_INSTANCE_URL')
SF_CLIENT_ID = os.getenv('SF_CLIENT_ID')
SF_CLIENT_SECRET = os.getenv('SF_CLIENT_SECRET')
PORT = int(os.getenv('PORT', 10000))

# Validate required environment variables
missing_vars = []
if not BOT_TOKEN:
    missing_vars.append('BOT_TOKEN')
if not SALESFORCE_WEBHOOK_URL:
    missing_vars.append('SALESFORCE_WEBHOOK_URL')
if not SF_INSTANCE_URL:
    missing_vars.append('SF_INSTANCE_URL')
if not SF_CLIENT_ID:
    missing_vars.append('SF_CLIENT_ID')
if not SF_CLIENT_SECRET:
    missing_vars.append('SF_CLIENT_SECRET')

app = Flask(__name__)

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

class SalesforceAuth:
    """Handles Salesforce OAuth 2.0 authentication"""
    def __init__(self):
        self.instance_url = SF_INSTANCE_URL
        self.client_id = SF_CLIENT_ID
        self.client_secret = SF_CLIENT_SECRET
        self.access_token = None
        self.token_expiry = 0
    
    def get_access_token(self):
        """Get Salesforce access token using client_credentials flow"""
        try:
            if self.access_token and time.time() < (self.token_expiry - 300):
                logger.info("✅ Using cached Salesforce access token")
                return self.access_token
            
            token_url = f"{self.instance_url}/services/oauth2/token"
            payload = {
                'grant_type': 'client_credentials',
                'client_id': self.client_id,
                'client_secret': self.client_secret
            }
            
            headers = {'Content-Type': 'application/x-www-form-urlencoded'}
            
            logger.info("🔑 Requesting Salesforce access token...")
            response = requests.post(token_url, data=payload, headers=headers, timeout=30)
            
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

class TelegramBotManager:
    def __init__(self):
        self.bot_token = BOT_TOKEN
        if self.bot_token:
            self.base_url = f"https://api.telegram.org/bot{self.bot_token}"
        else:
            self.base_url = None
            
        self.sf_webhook = SALESFORCE_WEBHOOK_URL
        self.sf_auth = SalesforceAuth()
        
    def send_message(self, chat_id, text, reply_markup=None, parse_mode='HTML'):
        """Send message to Telegram using direct API"""
        try:
            if not self.base_url:
                logger.error("❌ BOT_TOKEN not configured")
                return False
                
            url = f"{self.base_url}/sendMessage"
            data = {
                'chat_id': chat_id,
                'text': text,
                'parse_mode': parse_mode
            }
            
            if reply_markup:
                data['reply_markup'] = json.dumps(reply_markup)
            
            response = requests.post(url, data=data, timeout=30)
            result = response.json()
            
            if result.get('ok'):
                logger.info(f"✅ Message sent to {chat_id}")
                return True
            else:
                logger.error(f"❌ Failed to send to {chat_id}: {result.get('description')}")
                return False
                
        except Exception as e:
            logger.error(f"❌ Failed to send to {chat_id}: {e}")
            return False
    
    def forward_to_salesforce(self, payload):
        """Forward message to Salesforce with authentication"""
        try:
            if not self.sf_webhook:
                logger.error("❌ SALESFORCE_WEBHOOK_URL not configured")
                return False
            
            # Get Salesforce access token
            access_token = self.sf_auth.get_access_token()
            if not access_token:
                logger.error("❌ Failed to get Salesforce access token")
                return False
            
            headers = {
                'Authorization': f'Bearer {access_token}',
                'Content-Type': 'application/json'
            }
            
            logger.info(f"📤 Forwarding to Salesforce: {self.sf_webhook}")
            logger.info(f"📤 Payload: {json.dumps(payload, indent=2)}")
            
            response = requests.post(
                self.sf_webhook, 
                json=payload, 
                headers=headers, 
                timeout=30
            )
            
            logger.info(f"📤 Salesforce response: {response.status_code} - {response.text}")
            
            if response.status_code == 200:
                logger.info(f"✅ Forwarded to Salesforce: {payload.get('chatId')}")
                return True
            else:
                logger.error(f"❌ Salesforce error {response.status_code}: {response.text}")
                return False
                
        except Exception as e:
            logger.error(f"❌ Error forwarding to Salesforce: {e}")
            return False
    
    def check_existing_channel_user(self, telegram_id):
        """Check if Channel_User__c exists by Telegram Chat ID"""
        try:
            access_token = self.sf_auth.get_access_token()
            if not access_token:
                return None
            
            headers = {
                'Authorization': f'Bearer {access_token}',
                'Content-Type': 'application/json'
            }
            
            query = f"""
            SELECT Id, Name, Channel_Type__c, Channel_ID__c,
                   Telegram_Chat_ID__c, Contact__c, Contact__r.Name,
                   Customer_Profile__c, Created_Date__c, Last_Activity_Date__c
            FROM Channel_User__c 
            WHERE Channel_Type__c = 'Telegram' 
            AND Telegram_Chat_ID__c = '{telegram_id}'
            LIMIT 1
            """
            encoded_query = requests.utils.quote(query)
            url = f"{SF_INSTANCE_URL}/services/data/v58.0/query?q={encoded_query}"
            
            response = requests.get(url, headers=headers, timeout=30)
            
            if response.status_code == 200:
                data = response.json()
                if data['totalSize'] > 0:
                    return data['records'][0]
            return None
            
        except Exception as e:
            logger.error(f"❌ Error checking channel user: {e}")
            return None
    
    def find_contact_by_telegram_id(self, telegram_id):
        """Find contact by Telegram ID in Salesforce"""
        try:
            access_token = self.sf_auth.get_access_token()
            if not access_token:
                return None
            
            headers = {
                'Authorization': f'Bearer {access_token}',
                'Content-Type': 'application/json'
            }
            
            query = f"""
            SELECT Id, FirstName, LastName, Salutation, Phone, MobilePhone, Email 
            FROM Contact 
            WHERE Telegram_Chat_ID__c = '{telegram_id}'
            LIMIT 1
            """
            encoded_query = requests.utils.quote(query)
            url = f"{SF_INSTANCE_URL}/services/data/v58.0/query?q={encoded_query}"
            
            response = requests.get(url, headers=headers, timeout=30)
            
            if response.status_code == 200:
                data = response.json()
                if data['totalSize'] > 0:
                    return data['records'][0]
            return None
            
        except Exception as e:
            logger.error(f"❌ Error finding contact by Telegram ID: {e}")
            return None
    
    def find_contact_by_phone(self, phone_number):
        """Find contact by phone number in Salesforce"""
        try:
            access_token = self.sf_auth.get_access_token()
            if not access_token:
                return None
            
            headers = {
                'Authorization': f'Bearer {access_token}',
                'Content-Type': 'application/json'
            }
            
            clean_phone = re.sub(r'[^\d]', '', phone_number)
            
            query = f"""
            SELECT Id, FirstName, LastName, Salutation, Phone, MobilePhone, Email 
            FROM Contact 
            WHERE Phone LIKE '%{clean_phone}' 
               OR MobilePhone LIKE '%{clean_phone}'
            LIMIT 1
            """
            encoded_query = requests.utils.quote(query)
            url = f"{SF_INSTANCE_URL}/services/data/v58.0/query?q={encoded_query}"
            
            response = requests.get(url, headers=headers, timeout=30)
            
            if response.status_code == 200:
                data = response.json()
                if data['totalSize'] > 0:
                    return data['records'][0]
            return None
            
        except Exception as e:
            logger.error(f"❌ Error finding contact by phone: {e}")
            return None
    
    def find_contact_by_email(self, email):
        """Find contact by email in Salesforce"""
        try:
            access_token = self.sf_auth.get_access_token()
            if not access_token:
                return None
            
            headers = {
                'Authorization': f'Bearer {access_token}',
                'Content-Type': 'application/json'
            }
            
            query = f"""
            SELECT Id, FirstName, LastName, Salutation, Phone, Email 
            FROM Contact 
            WHERE Email = '{email}'
            LIMIT 1
            """
            encoded_query = requests.utils.quote(query)
            url = f"{SF_INSTANCE_URL}/services/data/v58.0/query?q={encoded_query}"
            
            response = requests.get(url, headers=headers, timeout=30)
            
            if response.status_code == 200:
                data = response.json()
                if data['totalSize'] > 0:
                    return data['records'][0]
            return None
            
        except Exception as e:
            logger.error(f"❌ Error finding contact by email: {e}")
            return None
    
    def get_active_sessions_for_user(self, channel_user_id):
        """Get active chat sessions for a channel user"""
        try:
            access_token = self.sf_auth.get_access_token()
            if not access_token:
                return []
            
            headers = {
                'Authorization': f'Bearer {access_token}',
                'Content-Type': 'application/json'
            }
            
            query = f"""
            SELECT Id, Name, Status__c, OwnerId, Owner.Name, 
                   Assigned_Agent__c, Assigned_Agent__r.Name,
                   Created_Date__c, Last_Message_Time__c
            FROM Chat_Session__c 
            WHERE Channel_User__c = '{channel_user_id}'
            AND Status__c IN ('Active', 'Waiting')
            ORDER BY Last_Message_Time__c DESC
            LIMIT 5
            """
            encoded_query = requests.utils.quote(query)
            url = f"{SF_INSTANCE_URL}/services/data/v58.0/query?q={encoded_query}"
            
            response = requests.get(url, headers=headers, timeout=30)
            
            if response.status_code == 200:
                data = response.json()
                return data.get('records', [])
            return []
            
        except Exception as e:
            logger.error(f"❌ Error getting active sessions: {e}")
            return []
    
    def create_new_contact(self, first_name, last_name, phone, gender, telegram_id):
        """Create new contact in Salesforce"""
        try:
            access_token = self.sf_auth.get_access_token()
            if not access_token:
                return None
            
            headers = {
                'Authorization': f'Bearer {access_token}',
                'Content-Type': 'application/json'
            }
            
            url = f"{SF_INSTANCE_URL}/services/data/v58.0/sobjects/Contact/"
            data = {
                'FirstName': first_name,
                'LastName': last_name,
                'Salutation': 'Mr.' if gender.lower() == 'male' else 'Ms.',
                'MobilePhone': phone,
                'Phone': phone,
                'Telegram_Chat_ID__c': str(telegram_id)
            }
            
            response = requests.post(url, headers=headers, json=data, timeout=30)
            
            if response.status_code == 201:
                result = response.json()
                logger.info(f"✅ Created new contact: {result['id']}")
                return result['id']
            else:
                logger.error(f"❌ Failed to create contact: {response.status_code} - {response.text}")
                return None
                
        except Exception as e:
            logger.error(f"❌ Error creating contact: {e}")
            return None
    
    def send_typing_action(self, chat_id):
        """Send typing action to Telegram"""
        try:
            if not self.base_url:
                return False
                
            url = f"{self.base_url}/sendChatAction"
            data = {
                'chat_id': chat_id,
                'action': 'typing'
            }
            
            response = requests.post(url, data=data, timeout=5)
            return response.json().get('ok', False)
                
        except Exception as e:
            logger.error(f"❌ Error sending typing action: {e}")
            return False

# Initialize bot manager
bot_manager = TelegramBotManager()

# Utility functions
def is_phone_number(text):
    if not text:
        return False
    phone_pattern = r'^(\+?251|0)?[97]\d{8}$'
    return re.match(phone_pattern, text.strip()) is not None

def is_email(text):
    if not text:
        return False
    email_pattern = r'^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$'
    return re.match(email_pattern, text.strip()) is not None

def clean_phone_number(phone):
    """Clean phone number for Salesforce"""
    if not phone:
        return ""
    cleaned = re.sub(r'[^\d]', '', phone)
    if cleaned.startswith('251'):
        cleaned = cleaned[3:]
    if not cleaned.startswith('0'):
        cleaned = '0' + cleaned
    return cleaned

def process_incoming_message(chat_id, message_text, user_data):
    """Process incoming Telegram message"""
    try:
        # Show typing indicator
        bot_manager.send_typing_action(chat_id)
        
        # Check if user exists in Channel_User__c
        channel_user = bot_manager.check_existing_channel_user(str(chat_id))
        
        if channel_user:
            # User exists, forward message to Salesforce webhook
            logger.info(f"👤 Existing user {chat_id}: {message_text}")
            
            # Create payload for Salesforce webhook
            payload = {
                'channelType': 'Telegram',
                'chatId': str(chat_id),
                'message': message_text,
                'messageId': f"TG_{int(time.time())}",
                'firstName': user_data.get('first_name', ''),
                'lastName': user_data.get('last_name', ''),
                'username': user_data.get('username', ''),
                'languageCode': user_data.get('language_code', 'en')
            }
            
            # Forward to Salesforce
            success = bot_manager.forward_to_salesforce(payload)
            
            if success:
                # Send confirmation to user
                bot_manager.send_message(
                    chat_id,
                    "✅ Message received. An agent will respond shortly.",
                    parse_mode='Markdown'
                )
            else:
                bot_manager.send_message(
                    chat_id,
                    "❌ Sorry, there was an error processing your message. Please try again.",
                    parse_mode='Markdown'
                )
        else:
            # New user - ask for registration
            logger.info(f"👤 New user {chat_id}: {message_text}")
            
            # Check if it's a phone number
            if is_phone_number(message_text):
                clean_phone = clean_phone_number(message_text)
                
                # Look for existing contact by phone
                contact = bot_manager.find_contact_by_phone(clean_phone)
                
                if contact:
                    # Create Channel_User__c for existing contact
                    create_channel_user_for_contact(contact['Id'], chat_id, user_data)
                else:
                    # Ask for name and gender for new registration
                    bot_manager.send_message(
                        chat_id,
                        "📝 *New Registration Required*\n\n"
                        "Please provide your *First Name* and *Last Name* (separated by space):\n"
                        "Example: *John Smith*",
                        parse_mode='Markdown'
                    )
            elif is_email(message_text):
                # Look for existing contact by email
                contact = bot_manager.find_contact_by_email(message_text)
                
                if contact:
                    create_channel_user_for_contact(contact['Id'], chat_id, user_data)
                else:
                    bot_manager.send_message(
                        chat_id,
                        "❌ *No account found*\n\n"
                        "No account found with this email. Please share your phone number instead.",
                        parse_mode='Markdown'
                    )
            elif message_text.lower() == '/start':
                # Welcome message for new users
                bot_manager.send_message(
                    chat_id,
                    "👋 *Welcome to Bank of Abyssinia Support!*\n\n"
                    "To get started, please share:\n"
                    "• Your *phone number* (0912121212)\n"
                    "• Or your *email address*\n\n"
                    "This will help us identify you in our system.",
                    parse_mode='Markdown'
                )
            else:
                # Check if this might be a name (two words)
                name_parts = message_text.strip().split()
                if len(name_parts) >= 2:
                    # This could be a name during registration
                    # Check if we have phone in user_data
                    if 'registration_phone' in user_data:
                        # Complete registration
                        complete_registration(
                            chat_id, 
                            name_parts[0], 
                            ' '.join(name_parts[1:]), 
                            user_data['registration_phone'],
                            user_data
                        )
                    else:
                        bot_manager.send_message(
                            chat_id,
                            "Please share your phone number first.",
                            parse_mode='Markdown'
                        )
                else:
                    bot_manager.send_message(
                        chat_id,
                        "👋 *Welcome!*\n\n"
                        "Please share your *phone number* to get started:\n"
                        "Example: *0912121212*",
                        parse_mode='Markdown'
                    )
                    
    except Exception as e:
        logger.error(f"❌ Error processing message: {e}")
        bot_manager.send_message(
            chat_id,
            "❌ Sorry, an error occurred. Please try again.",
            parse_mode='Markdown'
        )

def create_channel_user_for_contact(contact_id, telegram_id, user_data):
    """Create Channel_User__c record for existing contact"""
    try:
        # First, update contact with Telegram ID
        access_token = bot_manager.sf_auth.get_access_token()
        if not access_token:
            return False
        
        headers = {
            'Authorization': f'Bearer {access_token}',
            'Content-Type': 'application/json'
        }
        
        # Update contact
        update_url = f"{SF_INSTANCE_URL}/services/data/v58.0/sobjects/Contact/{contact_id}"
        update_data = {'Telegram_Chat_ID__c': str(telegram_id)}
        
        response = requests.patch(update_url, headers=headers, json=update_data, timeout=30)
        
        if response.status_code == 204:
            logger.info(f"✅ Updated contact {contact_id} with Telegram ID")
            
            # Create Channel_User__c
            channel_user_url = f"{SF_INSTANCE_URL}/services/data/v58.0/sobjects/Channel_User__c/"
            channel_user_data = {
                'Channel_Type__c': 'Telegram',
                'Channel_ID__c': f'telegram_{telegram_id}',
                'Telegram_Chat_ID__c': str(telegram_id),
                'Contact__c': contact_id,
                'Name': f'Telegram: {telegram_id}',
                'Created_Date__c': time.strftime('%Y-%m-%dT%H:%M:%S.000Z', time.gmtime()),
                'Last_Activity_Date__c': time.strftime('%Y-%m-%dT%H:%M:%S.000Z', time.gmtime())
            }
            
            response = requests.post(channel_user_url, headers=headers, json=channel_user_data, timeout=30)
            
            if response.status_code == 201:
                result = response.json()
                logger.info(f"✅ Created Channel_User__c: {result['id']}")
                
                # Send welcome message
                bot_manager.send_message(
                    telegram_id,
                    "✅ *Registration Successful!*\n\n"
                    "You are now connected to our support system.\n"
                    "How can we help you today?",
                    parse_mode='Markdown'
                )
                return True
                
        return False
        
    except Exception as e:
        logger.error(f"❌ Error creating channel user: {e}")
        return False

def complete_registration(chat_id, first_name, last_name, phone, user_data):
    """Complete registration for new user"""
    try:
        # Create new contact
        contact_id = bot_manager.create_new_contact(
            first_name=first_name,
            last_name=last_name,
            phone=phone,
            gender='male',  # Default
            telegram_id=chat_id
        )
        
        if contact_id:
            # Create Channel_User__c
            success = create_channel_user_for_contact(contact_id, chat_id, user_data)
            if success:
                return True
        
        bot_manager.send_message(
            chat_id,
            "❌ *Registration Failed*\n\n"
            "Sorry, we couldn't complete your registration. Please try again.",
            parse_mode='Markdown'
        )
        return False
        
    except Exception as e:
        logger.error(f"❌ Error completing registration: {e}")
        return False

# Flask routes
@app.route('/api/send-to-user', methods=['POST'])
def send_to_user():
    """Endpoint for Salesforce to send messages to Telegram"""
    try:
        data = request.get_json()
        
        if not data or 'chat_id' not in data or 'message' not in data:
            return jsonify({'error': 'Missing chat_id or message'}), 400
        
        chat_id = data['chat_id']
        message = data['message']
        
        # Optional: parse mode
        parse_mode = data.get('parse_mode', 'HTML')
        
        success = bot_manager.send_message(chat_id, message, parse_mode=parse_mode)
        
        if success:
            return jsonify({
                'status': 'success', 
                'message': 'Message sent to Telegram',
                'chat_id': chat_id
            })
        else:
            return jsonify({'error': 'Failed to send message to Telegram'}), 500
            
    except Exception as e:
        logger.error(f"❌ Send error: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/webhook', methods=['POST'])
def telegram_webhook():
    """Receive Telegram webhook"""
    try:
        if request.is_json:
            update_data = request.get_json()
            
            if 'message' in update_data:
                message = update_data['message']
                chat_id = message['chat']['id']
                message_text = message.get('text', '')
                user_data = message.get('from', {})
                
                logger.info(f"📥 Telegram message from {chat_id}: {message_text}")
                
                # Handle /start command
                if message_text == '/start':
                    bot_manager.send_message(
                        chat_id,
                        "👋 *Welcome to Bank of Abyssinia Support!*\n\n"
                        "To get started, please share:\n"
                        "• Your *phone number* (0912121212)\n"
                        "• Or your *email address*\n\n"
                        "This will help us identify you in our system.",
                        parse_mode='Markdown'
                    )
                    return jsonify({'status': 'ok'})
                
                # Process the message
                process_incoming_message(chat_id, message_text, user_data)
            
            return jsonify({'status': 'ok'})
        else:
            logger.error("❌ Non-JSON webhook received")
            return jsonify({'error': 'Invalid data format'}), 400
            
    except Exception as e:
        logger.error(f"❌ Webhook error: {e}")
        return jsonify({'error': str(e)}), 500

# Set webhook endpoint
@app.route('/set-webhook', methods=['GET'])
def set_webhook():
    """Set Telegram webhook programmatically"""
    try:
        if not BOT_TOKEN:
            return jsonify({'error': 'BOT_TOKEN not configured'}), 500
            
        webhook_url = f"https://{request.host}/webhook"
        set_url = f"https://api.telegram.org/bot{BOT_TOKEN}/setWebhook?url={webhook_url}"
        
        logger.info(f"🔗 Setting webhook to: {webhook_url}")
        
        response = requests.get(set_url)
        result = response.json()
        
        if result.get('ok'):
            return jsonify({
                'status': 'success',
                'message': f'Webhook set to: {webhook_url}',
                'result': result
            })
        else:
            return jsonify({
                'status': 'error',
                'message': result.get('description'),
                'result': result
            }), 500
            
    except Exception as e:
        logger.error(f"❌ Set webhook error: {e}")
        return jsonify({'error': str(e)}), 500

# Test endpoint
@app.route('/test', methods=['GET'])
def test():
    return jsonify({
        'status': 'online',
        'service': 'Telegram Bot Integration',
        'version': '2.0',
        'new_system': True,
        'endpoints': {
            'webhook': 'POST /webhook',
            'send_to_user': 'POST /api/send-to-user',
            'set_webhook': 'GET /set-webhook',
            'health': 'GET /health'
        }
    })

# Health check
@app.route('/health', methods=['GET'])
def health_check():
    try:
        access_token = bot_manager.sf_auth.get_access_token()
        return jsonify({
            'status': 'healthy' if BOT_TOKEN and access_token else 'unhealthy',
            'service': 'telegram-salesforce-bot',
            'telegram_bot': '✅ Set' if BOT_TOKEN else '❌ Missing',
            'salesforce_connection': '✅ Connected' if access_token else '❌ Failed',
            'new_system': True,
            'timestamp': time.time()
        })
    except:
        return jsonify({
            'status': 'unhealthy',
            'message': 'Health check failed'
        }), 500

@app.route('/')
def home():
    return jsonify({
        'message': 'Telegram Bot for Salesforce Integration',
        'system': 'New Messaging System (v2.0)',
        'models': ['Channel_User__c', 'Support_Conversation__c', 'Chat_Session__c', 'Chat_Message__c'],
        'status': 'Running'
    })

if __name__ == '__main__':
    logger.info("=" * 50)
    logger.info("🚀 Starting Telegram Bot v2.0 (New System)")
    logger.info("=" * 50)
    
    if missing_vars:
        logger.error(f"❌ Missing environment variables: {', '.join(missing_vars)}")
    else:
        logger.info("✅ All environment variables are set")
    
    logger.info("📱 Channel Type: Telegram")
    logger.info("🏗️  System: New Messaging Architecture")
    logger.info(f"🌐 Starting server on port {PORT}")
    app.run(host='0.0.0.0', port=PORT, debug=False)