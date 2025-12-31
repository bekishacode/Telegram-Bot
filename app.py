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

# In-memory storage for user registration state
user_registration_state = {}

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
                   Contact__r.FirstName, Contact__r.LastName,
                   Created_Date__c, Last_Activity_Date__c
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
            
            clean_phone = self.clean_phone_number(phone_number)
            
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
            ORDER BY Created_Date__c DESC
            LIMIT 1
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
    
    def create_channel_user(self, telegram_id, contact_id=None, first_name=None, last_name=None, phone=None):
        """Create Channel_User__c record"""
        try:
            access_token = self.sf_auth.get_access_token()
            if not access_token:
                return None
            
            headers = {
                'Authorization': f'Bearer {access_token}',
                'Content-Type': 'application/json'
            }
            
            # Create Channel_User__c
            channel_user_url = f"{SF_INSTANCE_URL}/services/data/v58.0/sobjects/Channel_User__c/"
            
            # Generate a name for the channel user
            if contact_id:
                name = f'Telegram User: {telegram_id}'
            elif first_name and last_name:
                name = f'Telegram: {first_name} {last_name}'
            else:
                name = f'Telegram: {telegram_id}'
            
            channel_user_data = {
                'Channel_Type__c': 'Telegram',
                'Channel_ID__c': f'telegram_{telegram_id}',
                'Telegram_Chat_ID__c': str(telegram_id),
                'Name': name,
                'Created_Date__c': time.strftime('%Y-%m-%dT%H:%M:%S.000Z', time.gmtime()),
                'Last_Activity_Date__c': time.strftime('%Y-%m-%dT%H:%M:%S.000Z', time.gmtime())
            }
            
            # Add contact relationship if available
            if contact_id:
                channel_user_data['Contact__c'] = contact_id
            
            response = requests.post(channel_user_url, headers=headers, json=channel_user_data, timeout=30)
            
            if response.status_code == 201:
                result = response.json()
                logger.info(f"✅ Created Channel_User__c: {result['id']}")
                return result['id']
            else:
                logger.error(f"❌ Failed to create Channel_User__c: {response.status_code} - {response.text}")
                return None
                
        except Exception as e:
            logger.error(f"❌ Error creating channel user: {e}")
            return None
    
    def update_contact_telegram_id(self, contact_id, telegram_id):
        """Update contact with Telegram Chat ID"""
        try:
            access_token = self.sf_auth.get_access_token()
            if not access_token:
                return False
            
            headers = {
                'Authorization': f'Bearer {access_token}',
                'Content-Type': 'application/json'
            }
            
            url = f"{SF_INSTANCE_URL}/services/data/v58.0/sobjects/Contact/{contact_id}"
            data = {
                'Telegram_Chat_ID__c': str(telegram_id)
            }
            
            response = requests.patch(url, headers=headers, json=data, timeout=30)
            
            if response.status_code == 204:
                logger.info(f"✅ Updated contact {contact_id} with Telegram ID {telegram_id}")
                return True
            else:
                logger.error(f"❌ Failed to update contact: {response.status_code} - {response.text}")
                return False
                
        except Exception as e:
            logger.error(f"❌ Error updating contact: {e}")
            return False
    
    def create_new_contact(self, first_name, last_name, phone, telegram_id):
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
    
    def clean_phone_number(self, phone):
        """Clean phone number for Salesforce"""
        if not phone:
            return ""
        cleaned = re.sub(r'[^\d]', '', phone)
        if cleaned.startswith('251'):
            cleaned = cleaned[3:]
        if not cleaned.startswith('0'):
            cleaned = '0' + cleaned
        return cleaned

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

def show_main_menu(chat_id, user_name=None):
    """Show main menu with options"""
    welcome_text = "👋 *Welcome to Bank of Abyssinia Support!*"
    if user_name:
        welcome_text = f"👋 *Welcome back, {user_name}!*"
    
    menu_text = f"""
{welcome_text}

Please choose an option:

1️⃣ *Contact Customer Support* - Connect with our support team
2️⃣ *Track your Case* - Check the status of your existing cases

*Simply type the number (1 or 2) to select an option.*
"""
    
    return bot_manager.send_message(chat_id, menu_text, parse_mode='Markdown')

def handle_contact_support(chat_id, channel_user_id, first_name=None, last_name=None):
    """Handle Contact Customer Support option"""
    try:
        # Check for active sessions
        active_sessions = bot_manager.get_active_sessions_for_user(channel_user_id)
        
        if active_sessions:
            # Active session exists - inform user
            session = active_sessions[0]
            session_status = session.get('Status__c', 'Unknown')
            
            if session_status == 'Active':
                response_text = """
✅ *You have an active support session!*

Please describe your issue or question, and our agent will assist you.
                """
            else:
                response_text = """
⏳ *Your support request is in the queue.*

An agent will be with you shortly. Please wait for their response.
                """
        else:
            # No active session - create one via Salesforce webhook
            response_text = """
📞 *Connecting you to customer support...*

Please describe your issue or question, and our team will assist you shortly.
            """
            
            # Send a message to Salesforce to trigger session creation
            # The webhook will handle creating the session and assigning to queue
            payload = {
                'channelType': 'Telegram',
                'chatId': str(chat_id),
                'message': 'Customer selected: Contact Customer Support',
                'messageId': f"TG_MENU_{int(time.time())}",
                'firstName': first_name,
                'lastName': last_name,
                'isMenuSelection': True
            }
            
            bot_manager.forward_to_salesforce(payload)
        
        return bot_manager.send_message(chat_id, response_text, parse_mode='Markdown')
        
    except Exception as e:
        logger.error(f"❌ Error handling contact support: {e}")
        error_text = "❌ *Sorry, there was an error connecting to support. Please try again.*"
        return bot_manager.send_message(chat_id, error_text, parse_mode='Markdown')

def handle_track_case(chat_id):
    """Handle Track your Case option"""
    response_text = """
🔍 *Case Tracking*

This feature is coming soon! We're working on allowing you to track your support cases directly here.

For now, please contact customer support for case updates.
    """
    return bot_manager.send_message(chat_id, response_text, parse_mode='Markdown')

def handle_registration_flow(chat_id, message_text, user_data):
    """Handle user registration flow"""
    chat_id_str = str(chat_id)
    
    if chat_id_str not in user_registration_state:
        # Start registration - ask for phone number
        user_registration_state[chat_id_str] = {
            'step': 'phone',
            'user_data': user_data
        }
        
        response_text = """
📱 *Registration*

To get started, please share your phone number:

Example: *0912121212*

We'll check if you already have an account with us.
        """
        return bot_manager.send_message(chat_id, response_text, parse_mode='Markdown')
    
    state = user_registration_state[chat_id_str]
    
    if state['step'] == 'phone':
        if is_phone_number(message_text):
            clean_phone = bot_manager.clean_phone_number(message_text)
            
            # Look for existing contact by phone
            contact = bot_manager.find_contact_by_phone(clean_phone)
            
            if contact:
                # Found existing contact
                state['step'] = 'found_contact'
                state['phone'] = clean_phone
                state['contact_id'] = contact['Id']
                state['first_name'] = contact.get('FirstName')
                state['last_name'] = contact.get('LastName')
                user_registration_state[chat_id_str] = state
                
                # Create channel user for existing contact
                channel_user_id = bot_manager.create_channel_user(
                    telegram_id=chat_id,
                    contact_id=contact['Id']
                )
                
                if channel_user_id:
                    # Update contact with Telegram ID
                    bot_manager.update_contact_telegram_id(contact['Id'], chat_id)
                    
                    # Registration complete
                    user_registration_state.pop(chat_id_str, None)
                    
                    # Show welcome message with name
                    contact_name = contact.get('FirstName', 'Customer')
                    welcome_text = f"""
✅ *Welcome back, {contact_name}!*

You're now connected to our support system.
                    """
                    bot_manager.send_message(chat_id, welcome_text, parse_mode='Markdown')
                    
                    # Show main menu
                    return show_main_menu(chat_id, contact_name)
                else:
                    error_text = "❌ *Sorry, there was an error creating your account. Please try again.*"
                    return bot_manager.send_message(chat_id, error_text, parse_mode='Markdown')
            else:
                # No contact found - ask for name
                state['step'] = 'name'
                state['phone'] = clean_phone
                user_registration_state[chat_id_str] = state
                
                response_text = """
📝 *New Account*

We couldn't find an account with this phone number.

Please provide your *First Name* and *Last Name* (separated by space):

Example: *John Smith*
                """
                return bot_manager.send_message(chat_id, response_text, parse_mode='Markdown')
        else:
            response_text = "📱 *Please enter a valid phone number:*\n\nExample: *0912121212*"
            return bot_manager.send_message(chat_id, response_text, parse_mode='Markdown')
    
    elif state['step'] == 'name':
        name_parts = message_text.strip().split()
        if len(name_parts) >= 2:
            first_name = name_parts[0]
            last_name = ' '.join(name_parts[1:])
            
            # Create new contact
            contact_id = bot_manager.create_new_contact(
                first_name=first_name,
                last_name=last_name,
                phone=state['phone'],
                telegram_id=chat_id
            )
            
            if contact_id:
                # Create channel user
                channel_user_id = bot_manager.create_channel_user(
                    telegram_id=chat_id,
                    contact_id=contact_id,
                    first_name=first_name,
                    last_name=last_name
                )
                
                if channel_user_id:
                    # Registration complete
                    user_registration_state.pop(chat_id_str, None)
                    
                    welcome_text = f"""
✅ *Registration Successful!*

Welcome, {first_name}! You're now connected to our support system.
                    """
                    bot_manager.send_message(chat_id, welcome_text, parse_mode='Markdown')
                    
                    # Show main menu
                    return show_main_menu(chat_id, first_name)
                else:
                    error_text = "❌ *Sorry, there was an error creating your account. Please try again.*"
                    return bot_manager.send_message(chat_id, error_text, parse_mode='Markdown')
            else:
                error_text = "❌ *Sorry, there was an error creating your account. Please try again.*"
                return bot_manager.send_message(chat_id, error_text, parse_mode='Markdown')
        else:
            response_text = """
📝 *Please provide both your First Name and Last Name:*

Example: *John Smith*
            """
            return bot_manager.send_message(chat_id, response_text, parse_mode='Markdown')

def process_incoming_message(chat_id, message_text, user_data):
    """Process incoming Telegram message"""
    try:
        # Show typing indicator
        bot_manager.send_typing_action(chat_id)
        
        chat_id_str = str(chat_id)
        message_lower = message_text.strip().lower()
        
        # Check if user is in registration flow
        if chat_id_str in user_registration_state:
            handle_registration_flow(chat_id, message_text, user_data)
            return
        
        # Check if user exists in Channel_User__c
        channel_user = bot_manager.check_existing_channel_user(chat_id_str)
        
        if not channel_user:
            # New user - start registration
            if message_lower == '/start':
                welcome_text = """
👋 *Welcome to Bank of Abyssinia Support!*

To get started with our support services, we need to register you in our system.

Please share your *phone number* to begin:

Example: *0912121212*
                """
                bot_manager.send_message(chat_id, welcome_text, parse_mode='Markdown')
                return
            else:
                # Start registration
                handle_registration_flow(chat_id, message_text, user_data)
                return
        
        # Existing user - check menu selection
        if message_lower in ['1', 'contact', 'support', 'contact support', 'customer support']:
            # Get user info
            first_name = None
            last_name = None
            
            if channel_user.get('Contact__r'):
                first_name = channel_user['Contact__r'].get('FirstName')
                last_name = channel_user['Contact__r'].get('LastName')
            elif user_data:
                first_name = user_data.get('first_name')
                last_name = user_data.get('last_name')
            
            handle_contact_support(chat_id, channel_user['Id'], first_name, last_name)
            
        elif message_lower in ['2', 'track', 'track case', 'case', 'my case']:
            handle_track_case(chat_id)
            
        elif message_lower == '/start':
            # Show main menu for existing users
            user_name = None
            if channel_user.get('Contact__r'):
                user_name = channel_user['Contact__r'].get('FirstName')
            elif user_data:
                user_name = user_data.get('first_name')
            
            show_main_menu(chat_id, user_name)
            
        else:
            # Regular message - forward to Salesforce
            logger.info(f"👤 Existing user {chat_id}: {message_text}")
            
            # Create payload for Salesforce webhook
            payload = {
                'channelType': 'Telegram',
                'chatId': chat_id_str,
                'message': message_text,
                'messageId': f"TG_{int(time.time())}",
                'firstName': user_data.get('first_name', ''),
                'lastName': user_data.get('last_name', ''),
                'username': user_data.get('username', ''),
                'languageCode': user_data.get('language_code', 'en')
            }
            
            # Add contact info if available
            if channel_user.get('Contact__r'):
                payload['contactId'] = channel_user['Contact__r'].get('Id')
            
            # Forward to Salesforce
            success = bot_manager.forward_to_salesforce(payload)
            
            if success:
                # Check if this is likely a support request (not a menu selection)
                if message_lower not in ['hi', 'hello', 'hey', 'help'] and len(message_text) > 3:
                    # Send confirmation
                    bot_manager.send_message(
                        chat_id,
                        "✅ *Message received.*\n\nOur support team will respond shortly.",
                        parse_mode='Markdown'
                    )
            else:
                bot_manager.send_message(
                    chat_id,
                    "❌ *Sorry, there was an error processing your message. Please try again.*",
                    parse_mode='Markdown'
                )
                    
    except Exception as e:
        logger.error(f"❌ Error processing message: {e}")
        bot_manager.send_message(
            chat_id,
            "❌ *Sorry, an error occurred. Please try again.*",
            parse_mode='Markdown'
        )

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
        'workflow': 'Registration → Main Menu → Support Queue',
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
            'workflow': 'Registration → Menu → Support',
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
        'workflow': '1. User Registration → 2. Main Menu → 3. Support Queue',
        'status': 'Running'
    })

if __name__ == '__main__':
    logger.info("=" * 50)
    logger.info("🚀 Starting Telegram Bot v3.0 (Enhanced Workflow)")
    logger.info("=" * 50)
    
    if missing_vars:
        logger.error(f"❌ Missing environment variables: {', '.join(missing_vars)}")
    else:
        logger.info("✅ All environment variables are set")
    
    logger.info("📱 Channel Type: Telegram")
    logger.info("👤 Workflow: Registration → Menu → Support Queue")
    logger.info("🏗️  System: New Messaging Architecture")
    logger.info(f"🌐 Starting server on port {PORT}")
    app.run(host='0.0.0.0', port=PORT, debug=False)