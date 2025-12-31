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
    
    def get_active_support_conversation(self, channel_user_id):
        """Get active Support Conversation for a channel user"""
        try:
            access_token = self.sf_auth.get_access_token()
            if not access_token:
                return None
            
            headers = {
                'Authorization': f'Bearer {access_token}',
                'Content-Type': 'application/json'
            }
            
            query = f"""
            SELECT Id, Name, Status__c, Channel_User_Name__c,
                   Created_Date__c, Last_Activity_Date__c,
                   Last_Message_Date__c
            FROM Support_Conversation__c 
            WHERE Channel_User_Name__c = '{channel_user_id}'
            AND Status__c = 'Active'
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
            logger.error(f"❌ Error getting active conversation: {e}")
            return None
    
    def get_active_sessions(self, conversation_id):
        """Get active chat sessions for a conversation"""
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
            WHERE Support_Conversation__c = '{conversation_id}'
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
    
    def create_channel_user_with_conversation(self, telegram_id, phone=None, contact_id=None, first_name=None, last_name=None):
        """Create Channel_User__c AND Support_Conversation__c together"""
        try:
            access_token = self.sf_auth.get_access_token()
            if not access_token:
                return None
            
            headers = {
                'Authorization': f'Bearer {access_token}',
                'Content-Type': 'application/json'
            }
            
            # 1. CREATE CHANNEL USER
            channel_user_url = f"{SF_INSTANCE_URL}/services/data/v58.0/sobjects/Channel_User__c/"
            
            # Generate a name for the channel user
            if first_name and last_name:
                name = f'Telegram: {first_name} {last_name}'
            elif phone:
                name = f'Telegram: {phone}'
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
            
            logger.info(f"📝 Creating Channel User for {telegram_id}")
            response = requests.post(channel_user_url, headers=headers, json=channel_user_data, timeout=30)
            
            if response.status_code != 201:
                logger.error(f"❌ Failed to create Channel_User__c: {response.status_code} - {response.text}")
                return None
            
            channel_user_result = response.json()
            channel_user_id = channel_user_result['id']
            logger.info(f"✅ Created Channel_User__c: {channel_user_id}")
            
            # 2. CREATE SUPPORT CONVERSATION (Active state)
            conversation_url = f"{SF_INSTANCE_URL}/services/data/v58.0/sobjects/Support_Conversation__c/"
            
            conversation_data = {
                'Channel_User_Name__c': channel_user_id,
                'Status__c': 'Active',
                'Created_Date__c': time.strftime('%Y-%m-%dT%H:%M:%S.000Z', time.gmtime()),
                'Last_Activity_Date__c': time.strftime('%Y-%m-%dT%H:%M:%S.000Z', time.gmtime()),
                'Last_Message_Date__c': time.strftime('%Y-%m-%dT%H:%M:%S.000Z', time.gmtime())
            }
            
            logger.info(f"📝 Creating Support Conversation for Channel User {channel_user_id}")
            response = requests.post(conversation_url, headers=headers, json=conversation_data, timeout=30)
            
            if response.status_code != 201:
                logger.error(f"⚠️ Failed to create Support_Conversation__c: {response.status_code} - {response.text}")
                # Channel user was created, but conversation failed - return channel user ID only
                return {'channelUserId': channel_user_id, 'conversationId': None}
            
            conversation_result = response.json()
            conversation_id = conversation_result['id']
            logger.info(f"✅ Created Support_Conversation__c: {conversation_id}")
            
            # 3. UPDATE CONTACT WITH TELEGRAM ID (if contact exists)
            if contact_id:
                self.update_contact_telegram_id(contact_id, telegram_id)
            
            return {
                'channelUserId': channel_user_id,
                'conversationId': conversation_id
            }
                
        except Exception as e:
            logger.error(f"❌ Error creating channel user with conversation: {e}")
            return None
    
    def link_channel_user_to_contact(self, channel_user_id, contact_id):
        """Link existing Channel_User__c to Contact"""
        try:
            access_token = self.sf_auth.get_access_token()
            if not access_token:
                return False
            
            headers = {
                'Authorization': f'Bearer {access_token}',
                'Content-Type': 'application/json'
            }
            
            url = f"{SF_INSTANCE_URL}/services/data/v58.0/sobjects/Channel_User__c/{channel_user_id}"
            data = {
                'Contact__c': contact_id
            }
            
            response = requests.patch(url, headers=headers, json=data, timeout=30)
            
            if response.status_code == 204:
                logger.info(f"✅ Linked Channel_User__c {channel_user_id} to Contact {contact_id}")
                return True
            else:
                logger.error(f"❌ Failed to link Channel_User__c: {response.status_code} - {response.text}")
                return False
                
        except Exception as e:
            logger.error(f"❌ Error linking channel user: {e}")
            return False
    
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
    
    def create_new_session(self, conversation_id, chat_id, first_name=None, last_name=None):
        """Trigger creation of new chat session via webhook"""
        try:
            payload = {
                'channelType': 'Telegram',
                'chatId': str(chat_id),
                'message': 'Customer started support session via menu option 1',
                'messageId': f"TG_SESSION_{int(time.time())}",
                'firstName': first_name,
                'lastName': last_name,
                'isSessionStart': True,
                'conversationId': conversation_id
            }
            
            success = self.forward_to_salesforce(payload)
            
            if success:
                logger.info(f"✅ Triggered session creation for conversation {conversation_id}")
                return True
            else:
                logger.error(f"❌ Failed to trigger session creation")
                return False
                
        except Exception as e:
            logger.error(f"❌ Error creating new session: {e}")
            return False
    
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

    def get_queue_position(self, conversation_id):
        """Get queue position for a conversation"""
        try:
            access_token = self.sf_auth.get_access_token()
            if not access_token:
                return None
            
            headers = {
                'Authorization': f'Bearer {access_token}',
                'Content-Type': 'application/json'
            }
            
            # Get all waiting sessions in queue ordered by creation time
            query = f"""
            SELECT Id, CreatedDate 
            FROM Chat_Session__c 
            WHERE Owner.Name = 'New Telegram Messages'
            AND Status__c = 'Waiting'
            AND Support_Conversation__c = '{conversation_id}'
            ORDER BY CreatedDate ASC
            """
            encoded_query = requests.utils.quote(query)
            url = f"{SF_INSTANCE_URL}/services/data/v58.0/query?q={encoded_query}"
            
            response = requests.get(url, headers=headers, timeout=30)
            
            if response.status_code == 200:
                data = response.json()
                records = data.get('records', [])
                
                # Find position of our session
                for i, record in enumerate(records):
                    if record.get('Id'):  # We would need session ID here
                        return i + 1  # Position in queue (1-based)
            
            return None
            
        except Exception as e:
            logger.error(f"❌ Error getting queue position: {e}")
            return None

# Initialize bot manager
bot_manager = TelegramBotManager()

# Utility functions
def is_phone_number(text):
    if not text:
        return False
    phone_pattern = r'^(\+?251|0)?[97]\d{8}$'
    return re.match(phone_pattern, text.strip()) is not None

def show_main_menu(chat_id, user_name=None, has_active_session=False):
    """Show main menu with options"""
    if has_active_session:
        menu_text = f"""
🔔 *You have an active support session*

*Choose an option:*

1️⃣ *Continue Support Session* - Go back to your active support conversation
2️⃣ *Track your Case* - Check status of existing cases
3️⃣ *New Support Request* - Start a new support session
4️⃣ *Main Menu* - See all options
        """
    else:
        welcome_text = "👋 *Welcome to Bank of Abyssinia Support!*"
        if user_name:
            welcome_text = f"👋 *Welcome back, {user_name}!*"
        
        menu_text = f"""
{welcome_text}

*Choose an option:*

1️⃣ *Contact Customer Support* - Connect with our support team
2️⃣ *Track your Case* - Check status of existing cases

*Simply type the number (1 or 2) to select an option.*
        """
    
    return bot_manager.send_message(chat_id, menu_text, parse_mode='Markdown')

def handle_contact_support(chat_id, channel_user_id, conversation_id, first_name=None, last_name=None):
    """Handle Contact Customer Support option"""
    try:
        # Show typing indicator
        bot_manager.send_typing_action(chat_id)
        
        if not conversation_id:
            error_text = """
❌ *Sorry, we couldn't find your conversation.*

Please try again or contact support through other channels.
            """
            return bot_manager.send_message(chat_id, error_text, parse_mode='Markdown')
        
        # Check for existing active sessions
        active_sessions = bot_manager.get_active_sessions(conversation_id)
        
        if active_sessions:
            # Active session exists
            session = active_sessions[0]
            session_status = session.get('Status__c', 'Unknown')
            
            if session_status == 'Active':
                response_text = """
✅ *You have an active support session!*

You're currently connected with an agent. Please continue your conversation.
                """
            else:
                # Session is waiting in queue
                response_text = """
⏳ *Your support request is already in the queue.*

Please wait for an agent to join. You can describe your issue now.
                """
        else:
            # No active session - create new one
            success = bot_manager.create_new_session(conversation_id, chat_id, first_name, last_name)
            
            if not success:
                error_text = """
❌ *Sorry, we couldn't create a support session.*

Please try again in a few moments.
                """
                return bot_manager.send_message(chat_id, error_text, parse_mode='Markdown')
            
            # Session was created successfully
            response_text = """
✅ *Support session created!*

You are now in the queue. An agent will be with you shortly.

Please describe your issue or question when you're ready.
            """
        
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

def handle_continue_session(chat_id, conversation_id):
    """Handle Continue Support Session option"""
    active_sessions = bot_manager.get_active_sessions(conversation_id)
    
    if not active_sessions:
        response_text = """
ℹ️ *You don't have an active support session.*

Please start a new support session using option 1.
        """
    else:
        session = active_sessions[0]
        session_status = session.get('Status__c', 'Unknown')
        
        if session_status == 'Active':
            response_text = """
✅ *Returning to your support session...*

You're connected with an agent. Please continue your conversation.
            """
        else:
            response_text = """
⏳ *Returning to your support request...*

You're still in the queue. An agent will join shortly.
            """
    
    return bot_manager.send_message(chat_id, response_text, parse_mode='Markdown')

def process_incoming_message(chat_id, message_text, user_data):
    """Process incoming Telegram message"""
    try:
        # Show typing indicator
        bot_manager.send_typing_action(chat_id)
        
        chat_id_str = str(chat_id)
        message_lower = message_text.strip().lower()
        
        logger.info(f"📥 Processing message from {chat_id}: {message_text}")
        
        # STEP 1: Check if Channel_User__c exists
        channel_user = bot_manager.check_existing_channel_user(chat_id_str)
        
        if channel_user:
            # ✅ Channel User EXISTS
            logger.info(f"✅ Existing Channel User found: {channel_user['Id']}")
            
            # Get conversation for this user
            conversation = bot_manager.get_active_support_conversation(channel_user['Id'])
            conversation_id = conversation['Id'] if conversation else None
            
            # Get user info for personalized greeting
            user_name = None
            if channel_user.get('Contact__r'):
                user_name = channel_user['Contact__r'].get('FirstName')
            elif user_data:
                user_name = user_data.get('first_name')
            
            # Check for active sessions
            active_sessions = []
            if conversation_id:
                active_sessions = bot_manager.get_active_sessions(conversation_id)
            
            has_active_session = len(active_sessions) > 0
            
            # Handle menu selections
            if message_lower in ['1', 'contact', 'support', 'contact support', 'customer support']:
                return handle_contact_support(
                    chat_id, 
                    channel_user['Id'],
                    conversation_id,
                    channel_user['Contact__r'].get('FirstName') if channel_user.get('Contact__r') else user_data.get('first_name'),
                    channel_user['Contact__r'].get('LastName') if channel_user.get('Contact__r') else user_data.get('last_name')
                )
            
            elif message_lower in ['2', 'track', 'track case', 'case', 'my case']:
                return handle_track_case(chat_id)
            
            elif message_lower in ['3', 'new support', 'new session'] and has_active_session:
                # User wants to start new session while one exists
                response_text = """
🔄 *Starting new support session...*

Your previous session will be ended and a new one will be created.
                """
                bot_manager.send_message(chat_id, response_text, parse_mode='Markdown')
                return handle_contact_support(
                    chat_id, 
                    channel_user['Id'],
                    conversation_id,
                    channel_user['Contact__r'].get('FirstName') if channel_user.get('Contact__r') else user_data.get('first_name'),
                    channel_user['Contact__r'].get('LastName') if channel_user.get('Contact__r') else user_data.get('last_name')
                )
            
            elif message_lower == '4' and has_active_session:
                # Show main menu
                return show_main_menu(chat_id, user_name, has_active_session)
            
            elif message_lower == '/start':
                # Show main menu for existing users
                return show_main_menu(chat_id, user_name, has_active_session)
            
            elif message_lower in ['hi', 'hello', 'hey', 'help', 'menu']:
                # Greeting - show appropriate menu
                return show_main_menu(chat_id, user_name, has_active_session)
            
            elif has_active_session and message_lower == '1':
                # Continue active session
                return handle_continue_session(chat_id, conversation_id)
            
            else:
                # Regular message - forward to Salesforce
                logger.info(f"📤 Forwarding message to Salesforce from user {chat_id}")
                
                # Determine if this should go to active session
                session_id = None
                if active_sessions:
                    session_id = active_sessions[0]['Id']
                
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
                
                # Add conversation and session info if available
                if conversation_id:
                    payload['conversationId'] = conversation_id
                
                if session_id:
                    payload['sessionId'] = session_id
                    payload['hasActiveSession'] = True
                
                # Add contact info if available
                if channel_user.get('Contact__r'):
                    payload['contactId'] = channel_user['Contact__r'].get('Id')
                
                # Forward to Salesforce
                success = bot_manager.forward_to_salesforce(payload)
                
                if success:
                    # Only send confirmation for non-menu, non-session messages
                    if (len(message_text) > 3 and 
                        message_lower not in ['ok', 'thanks', 'thank you', '1', '2', '3', '4'] and
                        not has_active_session):
                        
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
        
        else:
            # ❌ Channel User DOES NOT EXIST - Start registration
            logger.info(f"❌ No Channel User found for {chat_id}")
            
            if message_lower == '/start':
                welcome_text = """
👋 *Welcome to Bank of Abyssinia Support!*

To get started with our support services, we need to register you in our system.

Please share your *phone number* to begin:

Example: *0912121212*
                """
                bot_manager.send_message(chat_id, welcome_text, parse_mode='Markdown')
                return
            
            elif is_phone_number(message_text):
                # User provided phone number - create Channel User AND Conversation
                clean_phone = bot_manager.clean_phone_number(message_text)
                
                logger.info(f"📱 Creating Channel User and Conversation for {chat_id} with phone: {clean_phone}")
                
                # Check if Contact exists with this phone
                contact = bot_manager.find_contact_by_phone(clean_phone)
                
                contact_id = contact['Id'] if contact else None
                
                # Create Channel User AND Support Conversation together
                result = bot_manager.create_channel_user_with_conversation(
                    telegram_id=chat_id_str,
                    phone=clean_phone,
                    contact_id=contact_id,
                    first_name=user_data.get('first_name'),
                    last_name=user_data.get('last_name')
                )
                
                if not result:
                    error_text = "❌ *Sorry, there was an error creating your account. Please try again.*"
                    return bot_manager.send_message(chat_id, error_text, parse_mode='Markdown')
                
                # Show welcome message
                if contact:
                    contact_name = contact.get('FirstName', 'Customer')
                    welcome_text = f"""
✅ *Welcome back, {contact_name}!*

You're now registered in our support system.
                    """
                else:
                    welcome_text = """
✅ *Registration Successful!*

You're now connected to our support system.
                    """
                
                bot_manager.send_message(chat_id, welcome_text, parse_mode='Markdown')
                
                # Show main menu
                return show_main_menu(chat_id, contact.get('FirstName') if contact else None)
            
            else:
                # User didn't provide phone number
                if chat_id_str in user_registration_state:
                    # Already in registration flow
                    error_text = "📱 *Please enter a valid phone number:*\n\nExample: *0912121212*"
                else:
                    # First message wasn't /start or phone
                    error_text = """
👋 *Welcome to Bank of Abyssinia Support!*

To begin, please share your *phone number*:

Example: *0912121212*

Or type */start* to see the welcome message.
                    """
                
                return bot_manager.send_message(chat_id, error_text, parse_mode='Markdown')
                    
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

# Test endpoints
@app.route('/test-registration/<phone>', methods=['GET'])
def test_registration(phone):
    """Test registration endpoint"""
    try:
        # Simulate a user registration
        chat_id = "123456789"  # Test chat ID
        clean_phone = bot_manager.clean_phone_number(phone)
        
        # Create Channel User and Conversation
        result = bot_manager.create_channel_user_with_conversation(
            telegram_id=chat_id,
            phone=clean_phone,
            first_name="Test",
            last_name="User"
        )
        
        if result:
            return jsonify({
                'status': 'success',
                'message': 'Test registration complete',
                'result': result
            })
        else:
            return jsonify({
                'status': 'error',
                'message': 'Registration failed'
            }), 500
            
    except Exception as e:
        logger.error(f"❌ Test registration error: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/test-conversation/<telegram_id>', methods=['GET'])
def test_conversation(telegram_id):
    """Test conversation endpoint"""
    try:
        # Find channel user
        channel_user = bot_manager.check_existing_channel_user(telegram_id)
        
        if not channel_user:
            return jsonify({'error': 'Channel user not found'}), 404
        
        # Get conversation
        conversation = bot_manager.get_active_support_conversation(channel_user['Id'])
        
        if conversation:
            return jsonify({
                'status': 'success',
                'message': 'Conversation found',
                'channel_user': {
                    'id': channel_user['Id'],
                    'name': channel_user.get('Name')
                },
                'conversation': {
                    'id': conversation['Id'],
                    'status': conversation.get('Status__c')
                }
            })
        else:
            return jsonify({
                'status': 'error',
                'message': 'No active conversation found'
            }), 404
            
    except Exception as e:
        logger.error(f"❌ Test conversation error: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/test', methods=['GET'])
def test():
    return jsonify({
        'status': 'online',
        'service': 'Telegram Bot Integration',
        'version': '4.0',
        'architecture': 'Channel User → Support Conversation → Chat Sessions',
        'workflow': '1. Create Channel User & Conversation → 2. Menu Options → 3. Session Creation',
        'endpoints': {
            'webhook': 'POST /webhook',
            'send_to_user': 'POST /api/send-to-user',
            'set_webhook': 'GET /set-webhook',
            'test_registration': 'GET /test-registration/<phone>',
            'test_conversation': 'GET /test-conversation/<telegram_id>',
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
            'version': '4.0',
            'architecture': 'Channel User → Support Conversation → Chat Sessions',
            'telegram_bot': '✅ Set' if BOT_TOKEN else '❌ Missing',
            'salesforce_connection': '✅ Connected' if access_token else '❌ Failed',
            'workflow': 'Persistent Conversation Model',
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
        'architecture': 'Channel User → Support Conversation → Chat Sessions',
        'version': '4.0',
        'status': 'Running'
    })

if __name__ == '__main__':
    logger.info("=" * 50)
    logger.info("🚀 Starting Telegram Bot v4.0 (Persistent Conversation Model)")
    logger.info("=" * 50)
    
    if missing_vars:
        logger.error(f"❌ Missing environment variables: {', '.join(missing_vars)}")
    else:
        logger.info("✅ All environment variables are set")
    
    logger.info("📱 Channel Type: Telegram")
    logger.info("👤 Architecture: Channel User → Support Conversation → Chat Sessions")
    logger.info("🔄 Conversation: Persistent (Active state)")
    logger.info("💬 Sessions: Temporary (per support request)")
    logger.info(f"🌐 Starting server on port {PORT}")
    app.run(host='0.0.0.0', port=PORT, debug=False)