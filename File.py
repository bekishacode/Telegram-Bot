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

# In-memory storage for user states
user_states = {}

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
        
    def send_message(self, chat_id, text, reply_markup=None):
        """Send message to Telegram using direct API"""
        try:
            if not self.base_url:
                logger.error("❌ BOT_TOKEN not configured")
                return False
                
            url = f"{self.base_url}/sendMessage"
            data = {
                'chat_id': chat_id,
                'text': text,
                'parse_mode': 'HTML'
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
            
            response = requests.post(
                self.sf_webhook, 
                json=payload, 
                headers=headers, 
                timeout=30
            )
            
            logger.info(f"📤 Salesforce response: {response.status_code}")
            
            if response.status_code == 200:
                logger.info(f"✅ Forwarded to Salesforce: {payload.get('chatId')}")
                return True
            else:
                logger.error(f"❌ Salesforce error {response.status_code}: {response.text}")
                return False
                
        except Exception as e:
            logger.error(f"❌ Error forwarding to Salesforce: {e}")
            return False
    
    def check_existing_contact(self, chat_id):
        """Check if contact exists in Salesforce by Telegram Chat ID"""
        try:
            access_token = self.sf_auth.get_access_token()
            if not access_token:
                return None
            
            headers = {
                'Authorization': f'Bearer {access_token}',
                'Content-Type': 'application/json'
            }
            
            query = f"SELECT Id, FirstName, LastName, Salutation FROM Contact WHERE Telegram_Chat_ID__c = '{chat_id}' LIMIT 1"
            encoded_query = requests.utils.quote(query)
            url = f"{SF_INSTANCE_URL}/services/data/v58.0/query?q={encoded_query}"
            
            response = requests.get(url, headers=headers, timeout=30)
            
            if response.status_code == 200:
                data = response.json()
                if data['totalSize'] > 0:
                    return data['records'][0]
            return None
            
        except Exception as e:
            logger.error(f"❌ Error checking contact: {e}")
            return None
    
    def get_thread_status(self, chat_id):
        """Get conversation thread status from Salesforce"""
        try:
            access_token = self.sf_auth.get_access_token()
            if not access_token:
                return None
            
            headers = {
                'Authorization': f'Bearer {access_token}',
                'Content-Type': 'application/json'
            }
            
            query = f"""
            SELECT Id, Status__c, Assigned__c 
            FROM Conversation_Thread__c 
            WHERE Telegram_Chat_ID__c = '{chat_id}' 
            AND Channel_Type__c = 'Telegram'
            ORDER BY CreatedDate DESC 
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
            logger.error(f"❌ Error getting thread status: {e}")
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
            
            query = f"SELECT Id, FirstName, LastName, Salutation, Phone, Email FROM Contact WHERE Email = '{email}' LIMIT 1"
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
    
    def update_contact_chat_id(self, contact_id, chat_id):
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
                'Telegram_Chat_ID__c': str(chat_id)
            }
            
            response = requests.patch(url, headers=headers, json=data, timeout=30)
            
            if response.status_code == 204:
                logger.info(f"✅ Updated contact {contact_id} with chat ID {chat_id}")
                return True
            else:
                logger.error(f"❌ Failed to update contact: {response.status_code} - {response.text}")
                return False
                
        except Exception as e:
            logger.error(f"❌ Error updating contact: {e}")
            return False
    
    def create_new_contact(self, first_name, last_name, phone, gender, chat_id):
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
                'Telegram_Chat_ID__c': str(chat_id)
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

def handle_registered_user(chat_id, message_text, user_data):
    """Handle messages from registered users"""
    chat_id_str = str(chat_id)
    message_lower = message_text.strip().lower()
    
    logger.info(f"👤 Registered user {chat_id} sent: {message_text}")
    
    # Check if user is in registration flow
    if chat_id_str in user_states and user_states[chat_id_str].get('type') == 'registration':
        handle_registration_flow(chat_id, message_text, user_data)
        return
    
    # Check if user is submitting support request description
    if chat_id_str in user_states and user_states[chat_id_str].get('type') == 'support_description':
        # This is their support request description
        send_to_salesforce(chat_id, message_text, user_data)
        user_states.pop(chat_id_str, None)
        
        bot_manager.send_message(chat_id,
            '✅ Your request has been received!\n\n'
            'An agent will contact you shortly. '
            'Please wait for their response.'
        )
        return
    
    # Check thread status
    thread = bot_manager.get_thread_status(chat_id)
    
    if thread:
        thread_status = thread.get('Status__c')
        is_assigned = thread.get('Assigned__c', False)
        
        logger.info(f"🧵 Thread status: {thread_status}, Assigned: {is_assigned}")
        
        if thread_status == 'Active' and is_assigned:
            # Active conversation with agent
            send_to_salesforce(chat_id, message_text, user_data)
            bot_manager.send_message(chat_id, '✅ Message sent to agent.')
            return
        elif thread_status == 'Active' and not is_assigned:
            # Active but not assigned
            send_to_salesforce(chat_id, message_text, user_data)
            return
        elif thread_status == 'Waiting for Agent':
            # Already submitted request, waiting
            bot_manager.send_message(chat_id,
                '⏳ Your request is still waiting for an agent.\n\n'
                'Please wait for an agent to accept your request.'
            )
            return
    
    # Handle menu selection for closed/new threads
    if message_lower == '1' or 'track' in message_lower:
        bot_manager.send_message(chat_id,
            '📋 Case tracking feature is coming soon!\n\n'
            'Please choose an option:\n'
            '1️⃣ Track your Case\n'
            '2️⃣ Contact Customer Support'
        )
    elif message_lower == '2' or 'support' in message_lower or 'contact' in message_lower:
        # Start support request
        user_states[chat_id_str] = {
            'type': 'support_description',
            'user_data': user_data
        }
        
        # Send to Salesforce to update thread status
        send_to_salesforce(chat_id, "Customer selected: Contact Customer Support", user_data)
        
        bot_manager.send_message(chat_id,
            '💬 Please describe your request or question:\n'
            '(Our support team will assist you shortly)'
        )
    else:
        # Show main menu
        show_main_menu(chat_id, user_data)

def show_main_menu(chat_id, user_data):
    """Show main menu to registered users"""
    contact = bot_manager.check_existing_contact(chat_id)
    if contact:
        salutation = contact.get('Salutation', '')
        first_name = contact.get('FirstName', 'there')
        
        bot_manager.send_message(chat_id,
            f'👋 Welcome back, {salutation} {first_name}!\n\n'
            'Please choose an option:\n'
            '1️⃣ Track your Case\n'
            '2️⃣ Contact Customer Support'
        )
    else:
        bot_manager.send_message(chat_id,
            '👋 Welcome to Bank of Abyssinia!\n\n'
            'Please share your phone number or email address to get started.'
        )

def handle_registration_flow(chat_id, message_text, user_data):
    """Handle user registration flow"""
    chat_id_str = str(chat_id)
    state = user_states[chat_id_str]
    
    if state['step'] == 'gender':
        if message_text.lower() in ['male', 'female']:
            state['gender'] = message_text.lower()
            state['step'] = 'name'
            user_states[chat_id_str] = state
            
            bot_manager.send_message(chat_id,
                'Please enter your First Name and Last Name (separated by space):\n'
                'Example: John Smith'
            )
        else:
            bot_manager.send_message(chat_id,
                'Please select your gender:\n'
                '• Male\n'
                '• Female'
            )
    
    elif state['step'] == 'name':
        name_parts = message_text.split(' ', 1)
        if len(name_parts) >= 2:
            first_name = name_parts[0]
            last_name = name_parts[1]
            
            # Create new contact
            contact_id = bot_manager.create_new_contact(
                first_name=first_name,
                last_name=last_name,
                phone=state['phone'],
                gender=state['gender'],
                chat_id=chat_id
            )
            
            if contact_id:
                # Clear user state
                user_states.pop(chat_id_str, None)
                
                # Show main menu
                show_main_menu(chat_id, user_data)
            else:
                bot_manager.send_message(chat_id,
                    '❌ Sorry, we encountered an error creating your account. Please try again.'
                )
        else:
            bot_manager.send_message(chat_id,
                'Please enter both First Name and Last Name (separated by space):\n'
                'Example: John Smith'
            )

def send_to_salesforce(chat_id, message, user_data):
    """Send message to Salesforce for processing"""
    try:
        payload = {
            'chatId': str(chat_id),
            'userId': str(user_data.get('id', '')),
            'message': message,
            'messageId': str(int(time.time())),
            'timestamp': str(int(time.time())),
            'firstName': user_data.get('first_name', ''),
            'lastName': user_data.get('last_name', '')
        }
        
        success = bot_manager.forward_to_salesforce(payload)
        return success
    except Exception as e:
        logger.error(f"❌ Error sending to Salesforce: {e}")
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
        
        success = bot_manager.send_message(chat_id, message)
        
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
                
                logger.info(f"📥 Message from {chat_id}: {message_text}")
                
                # Handle /start command
                if message_text == '/start':
                    bot_manager.send_message(chat_id,
                        '👋 Welcome to Bank of Abyssinia!\n\n'
                        'Please share your phone number or email address to get started.'
                    )
                    return jsonify({'status': 'ok'})
                
                # Check if user is already registered
                existing_contact = bot_manager.check_existing_contact(chat_id)
                
                if existing_contact:
                    # Registered user
                    handle_registered_user(chat_id, message_text, user_data)
                    return jsonify({'status': 'ok'})
                
                # Unregistered user - handle registration
                chat_id_str = str(chat_id)
                
                # Check if we have registration state for this user
                if chat_id_str not in user_states:
                    # Start new registration
                    if is_phone_number(message_text):
                        clean_phone = clean_phone_number(message_text)
                        logger.info(f"📞 Checking phone: {clean_phone}")
                        
                        bot_manager.send_message(chat_id,
                            '📞 Checking your phone number...'
                        )
                        
                        # Check if contact exists
                        contact = bot_manager.find_contact_by_phone(clean_phone)
                        
                        if contact:
                            # Update existing contact
                            success = bot_manager.update_contact_chat_id(contact['Id'], chat_id)
                            if success:
                                show_main_menu(chat_id, user_data)
                            else:
                                bot_manager.send_message(chat_id,
                                    '❌ Failed to connect your account. Please try again.'
                                )
                        else:
                            # Start new registration
                            user_states[chat_id_str] = {
                                'type': 'registration',
                                'phone': clean_phone,
                                'step': 'gender',
                                'user_data': user_data
                            }
                            bot_manager.send_message(chat_id,
                                '📝 New registration detected.\n\n'
                                'Please select your gender:\n'
                                '• Male\n'
                                '• Female'
                            )
                    
                    elif is_email(message_text):
                        bot_manager.send_message(chat_id,
                            '📧 Checking your email address...'
                        )
                        
                        contact = bot_manager.find_contact_by_email(message_text)
                        
                        if contact:
                            success = bot_manager.update_contact_chat_id(contact['Id'], chat_id)
                            if success:
                                show_main_menu(chat_id, user_data)
                            else:
                                bot_manager.send_message(chat_id,
                                    '❌ Failed to connect your account. Please try again.'
                                )
                        else:
                            bot_manager.send_message(chat_id,
                                '❌ No account found with this email.\n\n'
                                'Please share your phone number to create a new account.'
                            )
                    
                    else:
                        bot_manager.send_message(chat_id,
                            '👋 Welcome! To get started, please share:\n\n'
                            '• Your phone number (0912121212)\n'
                            '• Or your email address'
                        )
                else:
                    # Continue registration based on current step
                    handle_registration_flow(chat_id, message_text, user_data)
            
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

# Test Salesforce connection
@app.route('/test-salesforce', methods=['GET'])
def test_salesforce():
    """Test Salesforce connection"""
    try:
        access_token = bot_manager.sf_auth.get_access_token()
        if not access_token:
            return jsonify({
                'status': 'error',
                'message': 'Failed to get access token'
            }), 500
        
        return jsonify({
            'status': 'success',
            'message': 'Salesforce connection successful',
            'instance_url': SF_INSTANCE_URL
        })
    except Exception as e:
        return jsonify({
            'status': 'error',
            'message': f'Test connection error: {e}'
        }), 500

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
        'service': 'Running',
        'endpoints': {
            'webhook': 'POST /webhook',
            'send_to_user': 'POST /api/send-to-user',
            'set_webhook': 'GET /set-webhook',
            'test_salesforce': 'GET /test-salesforce',
            'health': 'GET /health'
        }
    })

if __name__ == '__main__':
    logger.info("=" * 50)
    logger.info("🚀 Starting Telegram Bot")
    logger.info("=" * 50)
    
    if missing_vars:
        logger.error(f"❌ Missing environment variables: {', '.join(missing_vars)}")
    else:
        logger.info("✅ All environment variables are set")
    
    logger.info(f"🌐 Starting server on port {PORT}")
    app.run(host='0.0.0.0', port=PORT, debug=False)