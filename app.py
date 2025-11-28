import os
import logging
import re
import json
from dotenv import load_dotenv
from flask import Flask, request, jsonify
import requests
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

class TelegramBot:
    def __init__(self):
        self.bot_token = BOT_TOKEN
        self.base_url = f"https://api.telegram.org/bot{self.bot_token}"
        logger.info("🚀 Telegram Bot Started")
    
    def send_message(self, chat_id, message, attachment_url=None):
        """Send message to Telegram user using direct API calls"""
        try:
            if attachment_url:
                # Send photo with caption
                url = f"{self.base_url}/sendPhoto"
                data = {
                    'chat_id': chat_id,
                    'photo': attachment_url,
                    'caption': message,
                    'parse_mode': 'HTML'
                }
            else:
                # Send text message
                url = f"{self.base_url}/sendMessage"
                data = {
                    'chat_id': chat_id,
                    'text': message,
                    'parse_mode': 'HTML'
                }
            
            response = requests.post(url, data=data, timeout=30)
            result = response.json()
            
            if result.get('ok'):
                logger.info(f"✅ Message sent to user {chat_id}")
                return True
            else:
                logger.error(f"❌ Failed to send to user {chat_id}: {result.get('description')}")
                return False
                
        except Exception as e:
            logger.error(f"❌ Failed to send to user {chat_id}: {e}")
            return False
    
    def send_to_group(self, group_id, message, attachment_url=None):
        """Send message to Telegram group"""
        return self.send_message(group_id, message, attachment_url)

class SalesforceManager:
    def __init__(self):
        self.access_token = None
        self.token_expiry = 0
    
    def get_salesforce_token(self):
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
    
    def salesforce_query(self, query, access_token):
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
    
    def salesforce_create(self, object_name, create_data, access_token):
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
    
    def salesforce_update(self, object_name, record_id, update_data, access_token):
        """Update Salesforce record"""
        try:
            headers = {
                'Authorization': f'Bearer {access_token}',
                'Content-Type': 'application/json'
            }
            
            url = f"{SF_INSTANCE_URL}/services/data/v58.0/sobjects/{object_name}/{record_id}"
            
            response = requests.patch(url, headers=headers, json=update_data)
            
            if response.status_code == 204:
                logger.info(f"✅ Successfully updated {object_name} record {record_id}")
                return True
            else:
                logger.error(f"❌ Failed to update {object_name}: {response.status_code} - {response.text}")
                return False
                
        except Exception as e:
            logger.error(f"Salesforce update exception: {e}")
            return False
    
    def find_contact_by_chat_id(self, chat_id):
        """Find Contact by existing Chat ID"""
        try:
            access_token = self.get_salesforce_token()
            if not access_token:
                return None
            
            query = f"SELECT Id, Name, FirstName, LastName, Phone, MobilePhone, Email FROM Contact WHERE {SF_CHAT_ID_FIELD} = '{chat_id}' LIMIT 1"
            return self.salesforce_query(query, access_token)
            
        except Exception as e:
            logger.error(f"Error finding contact by chat ID: {e}")
            return None
    
    def find_contact_by_phone(self, last_9_digits):
        """Find Contact by phone number - match by last 9 digits"""
        try:
            access_token = self.get_salesforce_token()
            if not access_token:
                return None
            
            query = f"""
            SELECT Id, Name, FirstName, LastName, Phone, MobilePhone, Email 
            FROM Contact 
            WHERE Phone LIKE '%{last_9_digits}'
               OR MobilePhone LIKE '%{last_9_digits}'
            LIMIT 1
            """
            
            return self.salesforce_query(query, access_token)
            
        except Exception as e:
            logger.error(f"Error finding contact by phone: {e}")
            return None
    
    def find_contact_by_email(self, email):
        """Find Contact by email address"""
        try:
            access_token = self.get_salesforce_token()
            if not access_token:
                return None
            
            clean_email = email.strip().lower()
            query = f"SELECT Id, Name, FirstName, LastName, Phone, Email FROM Contact WHERE Email = '{clean_email}' LIMIT 1"
            
            return self.salesforce_query(query, access_token)
            
        except Exception as e:
            logger.error(f"Error finding contact by email: {e}")
            return None
    
    def update_contact_chat_id(self, contact_id, chat_id, user_data):
        """Update Contact with Telegram Chat ID"""
        try:
            access_token = self.get_salesforce_token()
            if not access_token:
                return False
            
            endpoint = f"{SF_INSTANCE_URL}/services/data/v65.0/sobjects/Contact/{contact_id}"
            
            update_data = {
                SF_CHAT_ID_FIELD: str(chat_id)
            }
            
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
    
    def create_new_contact(self, first_name, last_name, phone_number, chat_id, user_data):
        """Create a new Contact record when no existing match is found"""
        try:
            access_token = self.get_salesforce_token()
            if not access_token:
                return False
            
            contact_data = {
                "Salutation": "Mr." if user_data.get('gender') == 'male' else "Ms.",
                "FirstName": first_name,
                "LastName": last_name,
                "MobilePhone": phone_number,
                "Phone": phone_number,
                SF_CHAT_ID_FIELD: str(chat_id)
            }
            
            logger.info(f"🆕 Creating new Contact: {first_name} {last_name}, Phone: {phone_number}")
            
            success = self.salesforce_create(SF_OBJECT_NAME, contact_data, access_token)
            
            if success:
                logger.info(f"✅ Created new Contact for {first_name} {last_name} with phone {phone_number}")
            else:
                logger.error(f"❌ Failed to create new Contact for {first_name} {last_name}")
            
            return success
            
        except Exception as e:
            logger.error(f"❌ Error creating new contact: {e}")
            return False
    
    def store_incoming_message(self, chat_id, message_text, telegram_message_id, user_data):
    """Store incoming Telegram message in Salesforce"""
    try:
        access_token = self.get_salesforce_token()
        if not access_token:
            return False
        
        # Find contact OR lead by chat ID
        contact = self.find_contact_by_chat_id(chat_id)
        lead = self.find_lead_by_chat_id(chat_id)
        
        record_id = None
        object_name = None
        
        if contact:
            record_id = contact['Id']
            object_name = "Contact"
        elif lead:
            record_id = lead['Id'] 
            object_name = "Lead"
        else:
            logger.error(f"❌ No contact or lead found for chat ID: {chat_id}")
            return False
        
        # Create a Task to represent the incoming message
        task_data = {
            "Subject": "Incoming Telegram Message",
            "Status": "Completed",
            "Priority": "Normal",
            "Description": f"From: {user_data.get('first_name', 'Unknown')} {user_data.get('last_name', '')}\n"
                          f"Username: @{user_data.get('username', 'N/A')}\n"
                          f"Message: {message_text}\n"
                          f"Telegram Message ID: {telegram_message_id}",
            "ActivityDate": time.strftime('%Y-%m-%d')
        }
        
        # Link to correct object
        if object_name == "Contact":
            task_data["WhoId"] = record_id
        else:  # Lead
            task_data["WhatId"] = record_id
        
        success = self.salesforce_create("Task", task_data, access_token)
        
        if success:
            logger.info(f"✅ Stored incoming message from {chat_id} in Salesforce for {object_name} {record_id}")
        else:
            logger.error(f"❌ Failed to store incoming message from {chat_id}")
        
        return success
        
    except Exception as e:
        logger.error(f"❌ Error storing incoming message: {e}")
        return False

def find_lead_by_chat_id(self, chat_id):
    """Find Lead by existing Chat ID"""
    try:
        access_token = self.get_salesforce_token()
        if not access_token:
            return None
        
        query = f"SELECT Id, Name, FirstName, LastName, Company, Phone, Email FROM Lead WHERE {SF_CHAT_ID_FIELD} = '{chat_id}' LIMIT 1"
        return self.salesforce_query(query, access_token)
        
    except Exception as e:
        logger.error(f"Error finding lead by chat ID: {e}")
        return None
    
    def create_conversation_thread(self, contact_id, chat_id):
        """Create a conversation thread for tracking messages"""
        try:
            access_token = self.get_salesforce_token()
            if not access_token:
                return None
            
            # Check if thread already exists
            query = f"SELECT Id FROM Conversation_Thread__c WHERE Contact__c = '{contact_id}' LIMIT 1"
            existing_thread = self.salesforce_query(query, access_token)
            
            if existing_thread:
                return existing_thread['Id']
            
            # Create new thread
            thread_data = {
                "Name": f"Telegram Chat - {chat_id}",
                "Contact__c": contact_id,
                "Telegram_Chat_ID__c": str(chat_id),
                "Status__c": "Active",
                "Last_Message_Date__c": time.strftime('%Y-%m-%dT%H:%M:%SZ')
            }
            
            # Note: You'll need to create the Conversation_Thread__c custom object in Salesforce
            # For now, we'll use Tasks. Uncomment when custom object is available:
            # success = self.salesforce_create("Conversation_Thread__c", thread_data, access_token)
            # if success:
            #     return self.get_record_id_from_response(response)
            
            return None
            
        except Exception as e:
            logger.error(f"❌ Error creating conversation thread: {e}")
            return None

class BotManager:
    def __init__(self):
        self.bot = TelegramBot()
        self.sf = SalesforceManager()
    
    def handle_start_command(self, chat_id, user_data):
        """Handle /start command"""
        try:
            logger.info(f"👤 Handling start for user: {user_data.get('first_name', 'Unknown')} (ID: {chat_id})")
            
            # Check if this Chat ID is already registered
            existing_contact = self.sf.find_contact_by_chat_id(chat_id)
            
            if existing_contact:
                contact_name = existing_contact.get('Name', 'Valued Customer')
                self.bot.send_message(
                    chat_id=chat_id,
                    message=f"👋 Welcome back, {contact_name}!\n\n"
                           "You're already registered for promotions and can now chat with our support team directly through Telegram!"
                )
                return
            
            # New user - ask for phone number to find their Contact
            self.bot.send_message(
                chat_id=chat_id,
                message=f"👋 Welcome to Bank of Abyssinia, {user_data.get('first_name', 'there')}!\n\n"
                       "To connect with your existing account and receive personalized promotions, "
                       "please share your phone number or email address."
            )
            
        except Exception as e:
            logger.error(f"❌ Error handling start command: {e}")
    
    def handle_text_message(self, chat_id, message_text, user_data, telegram_message_id):
        """Handle text messages - both registration and conversation"""
        try:
            # Check if this is a registration-related message
            if self.is_registration_message(message_text):
                self.handle_registration_flow(chat_id, message_text, user_data)
                return
            
            # Check if user is registered (has a contact record)
            existing_contact = self.sf.find_contact_by_chat_id(chat_id)
            
            if not existing_contact:
                # User not registered - prompt for registration
                self.bot.send_message(
                    chat_id=chat_id,
                    message="📝 Please register first using /start command to chat with our support team."
                )
                return
            
            # User is registered - store message as incoming conversation
            success = self.sf.store_incoming_message(chat_id, message_text, telegram_message_id, user_data)
            
            if success:
                # Send confirmation to user
                self.bot.send_message(
                    chat_id=chat_id,
                    message="✅ Your message has been sent to our support team. We'll get back to you soon!"
                )
                logger.info(f"💬 Stored incoming message from {chat_id}")
            else:
                self.bot.send_message(
                    chat_id=chat_id,
                    message="❌ Sorry, there was an error processing your message. Please try again."
                )
                    
        except Exception as e:
            logger.error(f"❌ Error handling text message: {e}")
    
    def handle_registration_flow(self, chat_id, message_text, user_data):
        """Handle registration flow (phone/email input)"""
        if message_text == "❌ I don't have an account":
            self.bot.send_message(
                chat_id=chat_id,
                message="Please contact our customer support to create an account first."
            )
            return
        
        if message_text in ["📱 Share Phone Number", "📧 Enter Email Address"]:
            if message_text == "📱 Share Phone Number":
                self.bot.send_message(
                    chat_id=chat_id,
                    message="Please enter your phone number:\n\nExamples: 0912121212, 0712121212, 912121212, +251912121212"
                )
            else:
                self.bot.send_message(
                    chat_id=chat_id,
                    message="Please enter your email address:"
                )
            return
        
        # Check if message is a phone number
        if self.is_phone_number(message_text):
            logger.info(f"📞 Received phone: {message_text} from {chat_id}")
            
            # Extract last 9 digits for matching
            last_9_digits = self.extract_last_9_digits(message_text)
            
            contact_record = self.sf.find_contact_by_phone(last_9_digits)
            
            if contact_record:
                success = self.sf.update_contact_chat_id(contact_record['Id'], chat_id, user_data)
                if success:
                    contact_name = contact_record.get('Name', 'Valued Customer')
                    self.bot.send_message(
                        chat_id=chat_id,
                        message=f"✅ Successfully connected, {contact_name}!\n\n"
                               "You can now chat with our support team directly through Telegram. "
                               "Just send a message and we'll get back to you!"
                    )
                else:
                    self.bot.send_message(
                        chat_id=chat_id,
                        message="❌ Connection failed. Please try again."
                    )
            else:
                # No Contact found - create new one
                success = self.sf.create_new_contact(
                    first_name=user_data.get('first_name', 'Telegram'),
                    last_name=user_data.get('last_name', 'User'),
                    phone_number=message_text,
                    chat_id=chat_id,
                    user_data=user_data
                )
                
                if success:
                    self.bot.send_message(
                        chat_id=chat_id,
                        message="✅ Welcome! We've created a new account for you.\n\n"
                               "You will now receive promotions and can chat with our support team directly through Telegram!"
                    )
                else:
                    self.bot.send_message(
                        chat_id=chat_id,
                        message="❌ Failed to create account. Please try again or contact support."
                    )
        
        # Check if message is an email
        elif self.is_email(message_text):
            logger.info(f"📧 Received email: {message_text} from {chat_id}")
            
            contact_record = self.sf.find_contact_by_email(message_text)
            
            if contact_record:
                success = self.sf.update_contact_chat_id(contact_record['Id'], chat_id, user_data)
                if success:
                    contact_name = contact_record.get('Name', 'Valued Customer')
                    self.bot.send_message(
                        chat_id=chat_id,
                        message=f"✅ Successfully connected, {contact_name}!\n\n"
                               "You can now chat with our support team directly through Telegram!"
                    )
                else:
                    self.bot.send_message(
                        chat_id=chat_id,
                        message="❌ Connection failed. Please try again."
                    )
            else:
                self.bot.send_message(
                    chat_id=chat_id,
                    message="❌ No account found with this email.\n\n"
                           "Please share your phone number to create a new account:\n\nExamples: 0912121212, 0712121212, 912121212"
                )
    
    def is_registration_message(self, message_text):
        """Check if message is part of registration flow"""
        registration_keywords = [
            "❌ I don't have an account",
            "📱 Share Phone Number", 
            "📧 Enter Email Address"
        ]
        return message_text in registration_keywords or self.is_phone_number(message_text) or self.is_email(message_text)
    
    def is_phone_number(self, text):
        phone_pattern = r'^(\+?251|0)?[97]\d{8}$'
        return re.match(phone_pattern, text.strip()) is not None
    
    def is_email(self, text):
        email_pattern = r'^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$'
        return re.match(email_pattern, text.strip()) is not None
    
    def extract_last_9_digits(self, phone):
        digits = re.sub(r'[^\d]', '', phone)
        return digits[-9:] if len(digits) >= 9 else digits

# Initialize bot manager
bot_manager = BotManager()

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
        
        success = bot_manager.bot.send_message(chat_id, message, attachment_url)
        
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
        
        success = bot_manager.bot.send_to_group(group_id, message, attachment_url)
        
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
        
        access_token = bot_manager.sf.get_salesforce_token()
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
                success = bot_manager.bot.send_message(chat_id, message, attachment_url)
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

# Webhook Route - Enhanced for two-way conversations
@app.route('/webhook', methods=['POST'])
def webhook():
    """Handle incoming Telegram updates via webhook"""
    try:
        if request.is_json:
            update_data = request.get_json()
            logger.info(f"📥 Received webhook update: {update_data}")
            
            # Handle different types of updates
            if 'message' in update_data:
                message = update_data.get('message', {})
                chat_id = message.get('chat', {}).get('id')
                message_text = message.get('text', '')
                user_data = message.get('from', {})
                telegram_message_id = message.get('message_id')
                
                if not chat_id:
                    logger.error("❌ No chat ID in message")
                    return jsonify({'status': 'error', 'message': 'No chat ID'}), 400
                
                # Handle commands
                if message_text and message_text.startswith('/'):
                    if message_text == '/start':
                        bot_manager.handle_start_command(chat_id, user_data)
                    elif message_text == '/help':
                        help_text = """
🤖 **Bank of Abyssinia Telegram Bot**

**Available Commands:**
/start - Start bot and register for promotions
/help - Show this help message  
/register - Connect your Salesforce account

**Chat with Support:**
Just send a message and our team will help you!
"""
                        bot_manager.bot.send_message(chat_id, message=help_text)
                    elif message_text == '/register':
                        bot_manager.bot.send_message(chat_id, message="Please share your phone number or email to connect with your account:")
                elif message_text:
                    # Handle regular text messages
                    bot_manager.handle_text_message(chat_id, message_text, user_data, telegram_message_id)
            
            elif 'edited_message' in update_data:
                # Handle edited messages if needed
                logger.info("📝 Received edited message")
                
            elif 'channel_post' in update_data:
                # Handle channel posts if needed
                logger.info("📢 Received channel post")
                
            else:
                logger.warning(f"⚠️ Unhandled update type: {update_data.keys()}")
                return jsonify({'status': 'ignored', 'message': 'Unhandled update type'}), 200
            
            logger.info("✅ Webhook update processed successfully")
            return jsonify({'status': 'ok'})
        else:
            logger.error("❌ Webhook received non-JSON data")
            return jsonify({'error': 'Invalid data'}), 400
    except Exception as e:
        logger.error(f"❌ Webhook error: {e}")
        return jsonify({'error': str(e)}), 500

# New endpoint for Salesforce to check incoming messages
@app.route('/api/get-incoming-messages', methods=['GET'])
def get_incoming_messages():
    """Endpoint for Salesforce to check for new incoming messages"""
    # This would typically query a database for new messages
    # For now, we'll return a placeholder response
    return jsonify({
        'status': 'success',
        'messages': [],
        'message': 'Incoming messages are stored as Tasks in Salesforce'
    })

# Debug endpoints
@app.route('/debug', methods=['GET'])
def debug_info():
    """Debug endpoint to check bot status"""
    bot_status = "Initialized" if bot_manager.bot.bot_token else "Not Initialized"
    sf_token_status = "Available" if bot_manager.sf.access_token else "Not Available"
    
    return jsonify({
        'bot_status': bot_status,
        'salesforce_token_status': sf_token_status,
        'environment_variables_set': {
            'BOT_TOKEN': bool(BOT_TOKEN),
            'SF_INSTANCE_URL': bool(SF_INSTANCE_URL),
            'SF_CLIENT_ID': bool(SF_CLIENT_ID),
            'SF_CLIENT_SECRET': bool(SF_CLIENT_SECRET)
        },
        'features': {
            'two_way_messaging': True,
            'conversation_storage': True,
            'salesforce_integration': True
        }
    })

@app.route('/health', methods=['GET'])
def health_check():
    return jsonify({
        'status': 'healthy', 
        'service': 'telegram-salesforce-middleware',
        'available_groups': TELEGRAM_GROUPS,
        'salesforce_object': SF_OBJECT_NAME,
        'chat_id_field': SF_CHAT_ID_FIELD,
        'features': 'Two-way messaging enabled'
    })

@app.route('/')
def home():
    bot_status = "✅ Running" if bot_manager.bot.bot_token else "❌ Not Running"
    return jsonify({
        'message': 'Telegram-Salesforce Bot is running!',
        'bot_status': bot_status,
        'features': 'Two-way conversations enabled',
        'endpoints': {
            'debug': 'GET /debug',
            'health': 'GET /health',
            'send_to_all_contacts': 'POST /api/send-to-all-contacts',
            'send_to_group': 'POST /api/send-to-group',
            'send_to_user': 'POST /api/send-to-user',
            'get_incoming_messages': 'GET /api/get-incoming-messages',
            'webhook': 'POST /webhook'
        }
    })

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)