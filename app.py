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

# In-memory storage for user session state
user_session_state = {}  # Changed from registration_state to session_state

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
                'message': 'Customer started support session',
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
        """Get queue position for a conversation - FIXED VERSION"""
        try:
            access_token = self.sf_auth.get_access_token()
            if not access_token:
                return None
            
            headers = {
                'Authorization': f'Bearer {access_token}',
                'Content-Type': 'application/json'
            }
            
            # First, get the latest waiting session for this conversation
            session_query = f"""
            SELECT Id, CreatedDate 
            FROM Chat_Session__c 
            WHERE Support_Conversation__c = '{conversation_id}'
            AND Status__c = 'Waiting'
            ORDER BY CreatedDate DESC
            LIMIT 1
            """
            encoded_session_query = requests.utils.quote(session_query)
            session_url = f"{SF_INSTANCE_URL}/services/data/v58.0/query?q={encoded_session_query}"
            
            session_response = requests.get(session_url, headers=headers, timeout=30)
            
            if session_response.status_code != 200:
                return None
            
            session_data = session_response.json()
            if session_data['totalSize'] == 0:
                return None
            
            latest_session_id = session_data['records'][0]['Id']
            latest_session_created = session_data['records'][0]['CreatedDate']
            
            # Now get all waiting sessions and find our position
            all_sessions_query = f"""
            SELECT Id, CreatedDate 
            FROM Chat_Session__c 
            WHERE Owner.Name = 'New Telegram Messages'
            AND Status__c = 'Waiting'
            ORDER BY CreatedDate ASC
            """
            encoded_all_query = requests.utils.quote(all_sessions_query)
            all_url = f"{SF_INSTANCE_URL}/services/data/v58.0/query?q={encoded_all_query}"
            
            all_response = requests.get(all_url, headers=headers, timeout=30)
            
            if all_response.status_code == 200:
                all_data = all_response.json()
                records = all_data.get('records', [])
                
                # Find position of our session
                for i, record in enumerate(records):
                    if record.get('Id') == latest_session_id:
                        return i + 1  # Position in queue (1-based)
            
            return None
            
        except Exception as e:
            logger.error(f"❌ Error getting queue position: {e}")
            return None
    
    def get_session_details(self, session_id):
        """Get detailed session information"""
        try:
            access_token = self.sf_auth.get_access_token()
            if not access_token:
                return None
            
            headers = {
                'Authorization': f'Bearer {access_token}',
                'Content-Type': 'application/json'
            }
            
            query = f"""
            SELECT Id, Name, Status__c, OwnerId, Owner.Name, 
                   Assigned_Agent__c, Assigned_Agent__r.Name,
                   Created_Date__c, Last_Message_Time__c,
                   Support_Conversation__c
            FROM Chat_Session__c 
            WHERE Id = '{session_id}'
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
            logger.error(f"❌ Error getting session details: {e}")
            return None

# Initialize bot manager
bot_manager = TelegramBotManager()

# Utility functions
def is_phone_number(text):
    if not text:
        return False
    phone_pattern = r'^(\+?251|0)?[97]\d{8}$'
    return re.match(phone_pattern, text.strip()) is not None

def is_menu_command(text):
    """Check if text is a menu command"""
    menu_commands = ['/start', 'hi', 'hello', 'hey', 'menu', 'help']
    return text.strip().lower() in menu_commands

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

def handle_contact_support(chat_id, channel_user_id, conversation_id, user_data):
    """Handle Contact Customer Support option - UPDATED for seamless flow"""
    try:
        # Show typing indicator
        bot_manager.send_typing_action(chat_id)
        
        if not conversation_id:
            error_text = """
❌ *Sorry, we couldn't find your conversation.*

Please try again or contact support through other channels.
            """
            bot_manager.send_message(chat_id, error_text, parse_mode='Markdown')
            return False, None
        
        # Check for existing active sessions
        active_sessions = bot_manager.get_active_sessions(conversation_id)
        
        if active_sessions:
            # Active session exists
            session = active_sessions[0]
            session_id = session.get('Id')
            session_status = session.get('Status__c', 'Unknown')
            
            # Update user session state
            user_session_state[str(chat_id)] = {
                'in_session': True,
                'conversation_id': conversation_id,
                'session_id': session_id,
                'session_status': session_status
            }
            
            if session_status == 'Active':
                response_text = """
✅ *You have an active support session!*

You're currently connected with an agent. Please continue your conversation.
                """
            else:
                # Session is waiting in queue
                queue_position = bot_manager.get_queue_position(conversation_id)
                if queue_position:
                    response_text = f"""
⏳ *You're #{queue_position} in the queue.*

Please wait for an agent to join. You can describe your issue now.
                    """
                else:
                    response_text = """
⏳ *Your support request is in the queue.*

Please wait for an agent to join. You can describe your issue now.
                    """
            
            bot_manager.send_message(chat_id, response_text, parse_mode='Markdown')
            return True, session_id
        
        else:
            # No active session - create new one
            success = bot_manager.create_new_session(
                conversation_id, 
                chat_id, 
                user_data.get('first_name'),
                user_data.get('last_name')
            )
            
            if not success:
                error_text = """
❌ *Sorry, we couldn't create a support session.*

Please try again in a few moments.
                """
                bot_manager.send_message(chat_id, error_text, parse_mode='Markdown')
                return False, None
            
            # Wait a moment for session to be created
            time.sleep(2)
            
            # Get the newly created session
            active_sessions = bot_manager.get_active_sessions(conversation_id)
            if not active_sessions:
                error_text = """
❌ *Session was created but we couldn't retrieve it.*

Please wait a moment and send your message again.
                """
                bot_manager.send_message(chat_id, error_text, parse_mode='Markdown')
                return False, None
            
            session = active_sessions[0]
            session_id = session.get('Id')
            
            # Update user session state
            user_session_state[str(chat_id)] = {
                'in_session': True,
                'conversation_id': conversation_id,
                'session_id': session_id,
                'session_status': 'Waiting'
            }
            
            # Get queue position
            queue_position = bot_manager.get_queue_position(conversation_id)
            
            if queue_position:
                response_text = f"""
✅ *Support session created!*

You are now *#{queue_position} in the queue*. An agent will be with you shortly.

Please describe your issue or question when you're ready.
                """
            else:
                response_text = """
✅ *Support session created!*

You are now in the queue. An agent will be with you shortly.

Please describe your issue or question when you're ready.
                """
            
            bot_manager.send_message(chat_id, response_text, parse_mode='Markdown')
            return True, session_id
        
    except Exception as e:
        logger.error(f"❌ Error handling contact support: {e}")
        error_text = "❌ *Sorry, there was an error connecting to support. Please try again.*"
        bot_manager.send_message(chat_id, error_text, parse_mode='Markdown')
        return False, None

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
        session_id = session.get('Id')
        session_status = session.get('Status__c', 'Unknown')
        
        # Update user session state
        user_session_state[str(chat_id)] = {
            'in_session': True,
            'conversation_id': conversation_id,
            'session_id': session_id,
            'session_status': session_status
        }
        
        if session_status == 'Active':
            response_text = """
✅ *Returning to your support session...*

You're connected with an agent. Please continue your conversation.
            """
        else:
            queue_position = bot_manager.get_queue_position(conversation_id)
            if queue_position:
                response_text = f"""
⏳ *Returning to your support request...*

You're *#{queue_position} in the queue*. Please wait for an agent.
                """
            else:
                response_text = """
⏳ *Returning to your support request...*

You're still in the queue. An agent will join shortly.
                """
    
    return bot_manager.send_message(chat_id, response_text, parse_mode='Markdown')

def send_message_confirmation(chat_id, success, is_session_start=False, queue_position=None):
    """Send appropriate confirmation message"""
    if is_session_start:
        if success:
            if queue_position:
                return bot_manager.send_message(
                    chat_id,
                    f"✅ *Message delivered. You are #{queue_position} in queue.*",
                    parse_mode='Markdown'
                )
            else:
                return bot_manager.send_message(
                    chat_id,
                    "✅ *Message delivered. You are in the queue.*",
                    parse_mode='Markdown'
                )
        else:
            return bot_manager.send_message(
                chat_id,
                "❌ *Failed to send message. Please try again.*",
                parse_mode='Markdown'
            )
    else:
        if success:
            return bot_manager.send_message(
                chat_id,
                "✅ *Message delivered.*",
                parse_mode='Markdown'
            )
        else:
            return bot_manager.send_message(
                chat_id,
                "❌ *Failed to send message. Please try again.*",
                parse_mode='Markdown'
            )

def process_incoming_message(chat_id, message_text, user_data):
    """Process incoming Telegram message with improved session handling"""
    try:
        # Show typing indicator
        bot_manager.send_typing_action(chat_id)
        
        chat_id_str = str(chat_id)
        message_lower = message_text.strip().lower()
        
        logger.info(f"📥 Processing message from {chat_id}: {message_text}")
        
        # STEP 1: Check if Channel_User__c exists
        channel_user = bot_manager.check_existing_channel_user(chat_id_str)
        
        if not channel_user:
            # Handle registration flow
            return handle_new_user_registration(chat_id, message_text, user_data)
        
        # ✅ Channel User EXISTS
        logger.info(f"✅ Existing Channel User found: {channel_user['Id']}")
        
        # Get conversation for this user
        conversation = bot_manager.get_active_support_conversation(channel_user['Id'])
        if not conversation:
            logger.error(f"❌ No active conversation found for channel user {channel_user['Id']}")
            error_text = "❌ Sorry, we couldn't find your conversation. Please start a new session with option 1."
            return bot_manager.send_message(chat_id, error_text, parse_mode='Markdown')
        
        conversation_id = conversation['Id']
        
        # Check user's current session state
        user_state = user_session_state.get(chat_id_str, {})
        is_in_session = user_state.get('in_session', False)
        current_session_status = user_state.get('session_status')
        
        # Check for actual active sessions in Salesforce
        active_sessions = bot_manager.get_active_sessions(conversation_id)
        has_active_salesforce_session = len(active_sessions) > 0
        
        # If user is marked as in session but Salesforce has no active session, reset state
        if is_in_session and not has_active_salesforce_session:
            user_session_state[chat_id_str] = {}
            is_in_session = False
        
        # Handle menu commands (always show menu for these)
        if is_menu_command(message_text):
            user_name = channel_user.get('Contact__r', {}).get('FirstName') or user_data.get('first_name')
            return show_main_menu(chat_id, user_name, has_active_salesforce_session)
        
        # Handle numeric menu selections
        if message_lower in ['1', 'contact', 'support', 'contact support', 'customer support']:
            if is_in_session and current_session_status == 'Active':
                # Already in active session - continue
                return handle_continue_session(chat_id, conversation_id)
            else:
                # Start/continue support session
                success, session_id = handle_contact_support(
                    chat_id, 
                    channel_user['Id'],
                    conversation_id,
                    user_data
                )
                if success and session_id:
                    user_session_state[chat_id_str] = {
                        'in_session': True,
                        'conversation_id': conversation_id,
                        'session_id': session_id,
                        'session_status': 'Waiting'
                    }
                return success
        
        elif message_lower in ['2', 'track', 'track case', 'case', 'my case']:
            return handle_track_case(chat_id)
        
        elif message_lower in ['3', 'new', 'new support', 'new session']:
            # Option to start fresh session even if one exists
            if has_active_salesforce_session:
                confirm_text = """
⚠️ *You already have an active support session.*

Do you want to end the current session and start a new one?
Reply 'YES' to confirm or 'NO' to continue with current session.
                """
                user_session_state[chat_id_str] = {
                    'awaiting_confirmation': 'new_session',
                    'conversation_id': conversation_id
                }
                return bot_manager.send_message(chat_id, confirm_text, parse_mode='Markdown')
            else:
                return handle_contact_support(chat_id, channel_user['Id'], conversation_id, user_data)
        
        elif message_lower in ['4', 'main menu', 'menu']:
            user_name = channel_user.get('Contact__r', {}).get('FirstName') or user_data.get('first_name')
            return show_main_menu(chat_id, user_name, has_active_salesforce_session)
        
        # Handle confirmation responses
        if user_state.get('awaiting_confirmation') == 'new_session':
            if message_lower == 'yes':
                # Logic to close current session and start new one would go here
                # For now, just start new session
                user_session_state[chat_id_str] = {}
                return handle_contact_support(chat_id, channel_user['Id'], conversation_id, user_data)
            elif message_lower == 'no':
                user_session_state[chat_id_str] = {}
                return handle_continue_session(chat_id, conversation_id)
        
        # REGULAR MESSAGE HANDLING
        # If user is in a session (or starting one), forward message immediately
        if is_in_session or has_active_salesforce_session:
            # Get current session details
            current_sessions = bot_manager.get_active_sessions(conversation_id)
            if not current_sessions:
                # No active session despite state - reset and show menu
                user_session_state[chat_id_str] = {}
                user_name = channel_user.get('Contact__r', {}).get('FirstName') or user_data.get('first_name')
                return show_main_menu(chat_id, user_name, False)
            
            current_session = current_sessions[0]
            session_id = current_session.get('Id')
            session_status = current_session.get('Status__c', 'Waiting')
            
            # Update user state with current session info
            user_session_state[chat_id_str] = {
                'in_session': True,
                'conversation_id': conversation_id,
                'session_id': session_id,
                'session_status': session_status
            }
            
            logger.info(f"📤 Forwarding message to session {session_id} (status: {session_status})")
            
            payload = {
                'channelType': 'Telegram',
                'chatId': chat_id_str,
                'message': message_text,
                'messageId': f"TG_{int(time.time())}",
                'firstName': user_data.get('first_name', ''),
                'lastName': user_data.get('last_name', ''),
                'username': user_data.get('username', ''),
                'languageCode': user_data.get('language_code', 'en'),
                'conversationId': conversation_id,
                'sessionId': session_id,
                'isSessionStart': False
            }
            
            success = bot_manager.forward_to_salesforce(payload)
            
            # Send appropriate confirmation
            if success:
                if session_status == 'Waiting':
                    # For waiting sessions, check queue position
                    queue_position = bot_manager.get_queue_position(conversation_id)
                    send_message_confirmation(chat_id, success, is_session_start=False, queue_position=queue_position)
                else:
                    send_message_confirmation(chat_id, success, is_session_start=False)
            else:
                send_message_confirmation(chat_id, False)
            
            return success
        
        else:
            # NO ACTIVE SESSION - Show menu but don't interrupt if this is clearly not a menu command
            # Allow natural conversation starters
            if len(message_text) > 20 or '?' in message_text or 'help' in message_lower or 'issue' in message_lower or 'problem' in message_lower:
                # This looks like a support request, auto-initiate session
                logger.info(f"🤖 Auto-initiating session for support-like message from {chat_id}")
                success, session_id = handle_contact_support(
                    chat_id, 
                    channel_user['Id'],
                    conversation_id,
                    user_data
                )
                
                if success and session_id:
                    # Now forward the original message
                    user_session_state[chat_id_str] = {
                        'in_session': True,
                        'conversation_id': conversation_id,
                        'session_id': session_id,
                        'session_status': 'Waiting'
                    }
                    
                    payload = {
                        'channelType': 'Telegram',
                        'chatId': chat_id_str,
                        'message': message_text,
                        'messageId': f"TG_{int(time.time())}",
                        'firstName': user_data.get('first_name', ''),
                        'lastName': user_data.get('last_name', ''),
                        'username': user_data.get('username', ''),
                        'languageCode': user_data.get('language_code', 'en'),
                        'conversationId': conversation_id,
                        'sessionId': session_id,
                        'isSessionStart': False
                    }
                    
                    forward_success = bot_manager.forward_to_salesforce(payload)
                    
                    if forward_success:
                        queue_position = bot_manager.get_queue_position(conversation_id)
                        send_message_confirmation(chat_id, True, is_session_start=True, queue_position=queue_position)
                    else:
                        send_message_confirmation(chat_id, False, is_session_start=True)
                    
                    return forward_success
                else:
                    return success
            else:
                # Show menu for short/ambiguous messages
                logger.info(f"ℹ️ No active session for user {chat_id}, showing menu")
                user_name = channel_user.get('Contact__r', {}).get('FirstName') or user_data.get('first_name')
                
                response_text = f"""
ℹ️ *Hello {user_name if user_name else 'there'}!*

You don't have an active support session. 

*Please choose an option:*

1️⃣ *Contact Customer Support* - Start a new support session
2️⃣ *Track your Case* - Check status of existing cases

Type *1* or *2* to select an option.
                """
                
                return bot_manager.send_message(chat_id, response_text, parse_mode='Markdown')
                    
    except Exception as e:
        logger.error(f"❌ Error processing message: {e}")
        bot_manager.send_message(
            chat_id,
            "❌ *Sorry, an error occurred. Please try again.*",
            parse_mode='Markdown'
        )
        return False

def handle_new_user_registration(chat_id, message_text, user_data):
    """Handle new user registration (separated for clarity)"""
    chat_id_str = str(chat_id)
    message_lower = message_text.strip().lower()
    
    if message_lower == '/start':
        welcome_text = """
👋 *Welcome to Bank of Abyssinia Support!*

To get started with our support services, we need to register you in our system.

Please share your *phone number* to begin:

Example: *0912121212*
        """
        bot_manager.send_message(chat_id, welcome_text, parse_mode='Markdown')
        return True
    
    elif is_phone_number(message_text):
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
        
        # Show main menu (no active session for new users)
        return show_main_menu(chat_id, contact.get('FirstName') if contact else None, has_active_session=False)
    
    else:
        # User didn't provide phone number
        error_text = """
📱 *Please enter a valid phone number:*

Example: *0912121212*

Or type */start* to see the welcome message.
        """
        return bot_manager.send_message(chat_id, error_text, parse_mode='Markdown')

# Flask routes (remain the same as original)
@app.route('/api/send-to-user', methods=['POST'])
def send_to_user():
    """Endpoint for Salesforce to send messages to Telegram"""
    try:
        data = request.get_json()
        
        if not data or 'chat_id' not in data or 'message' not in data:
            return jsonify({'error': 'Missing chat_id or message'}), 400
        
        chat_id = data['chat_id']
        message = data['message']
        
        # Check if this message changes session status
        session_status = data.get('session_status')
        if session_status:
            user_state = user_session_state.get(str(chat_id), {})
            if user_state:
                user_state['session_status'] = session_status
                user_session_state[str(chat_id)] = user_state
        
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

@app.route('/clear-session-state/<chat_id>', methods=['GET'])
def clear_session_state(chat_id):
    """Clear session state for a user (for testing)"""
    if chat_id in user_session_state:
        del user_session_state[chat_id]
        return jsonify({'status': 'success', 'message': f'Cleared session state for {chat_id}'})
    return jsonify({'status': 'error', 'message': 'No session state found'}), 404

@app.route('/session-state/<chat_id>', methods=['GET'])
def get_session_state(chat_id):
    """Get session state for a user (for debugging)"""
    state = user_session_state.get(chat_id, {})
    return jsonify({'status': 'success', 'state': state})

# Test endpoints (remain the same as original)
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
        'version': '4.1',  # Updated version
        'architecture': 'Channel User → Support Conversation → Chat Sessions',
        'session_management': 'Improved seamless flow',
        'queue_position': 'Fixed logic',
        'endpoints': {
            'webhook': 'POST /webhook',
            'send_to_user': 'POST /api/send-to-user',
            'set_webhook': 'GET /set-webhook',
            'clear_session_state': 'GET /clear-session-state/<chat_id>',
            'session_state': 'GET /session-state/<chat_id>',
            'health': 'GET /health'
        }
    })

@app.route('/health', methods=['GET'])
def health_check():
    try:
        access_token = bot_manager.sf_auth.get_access_token()
        
        return jsonify({
            'status': 'healthy' if BOT_TOKEN and access_token else 'unhealthy',
            'service': 'telegram-salesforce-bot',
            'version': '4.1',
            'session_state_count': len(user_session_state),
            'telegram_bot': '✅ Set' if BOT_TOKEN else '❌ Missing',
            'salesforce_connection': '✅ Connected' if access_token else '❌ Failed',
            'workflow': 'Seamless Session Management',
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
        'version': '4.1',
        'session_management': 'Seamless flow with queue positioning',
        'status': 'Running'
    })

if __name__ == '__main__':
    logger.info("=" * 60)
    logger.info("🚀 Starting Telegram Bot v4.1 (Improved Session Management)")
    logger.info("=" * 60)
    
    if missing_vars:
        logger.error(f"❌ Missing environment variables: {', '.join(missing_vars)}")
    else:
        logger.info("✅ All environment variables are set")
    
    logger.info("📱 Channel Type: Telegram")
    logger.info("👤 Architecture: Channel User → Support Conversation → Chat Sessions")
    logger.info("🔄 Session Flow: Seamless with automatic queue positioning")
    logger.info("📍 Queue Position: Fixed logic with proper session tracking")
    logger.info(f"🌐 Starting server on port {PORT}")
    app.run(host='0.0.0.0', port=PORT, debug=False)