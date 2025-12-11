import os
import logging
import requests
import time
import re
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
            # Check if token is still valid (with 5-minute buffer)
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
        
    def send_message(self, chat_id, text):
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
            elif response.status_code == 403:
                logger.error(f"❌ Salesforce 403 Forbidden: {response.text}")
                logger.error("❌ Check Apex class permissions and OAuth scopes")
                return False
            else:
                logger.error(f"❌ Salesforce error {response.status_code}: {response.text}")
                return False
                
        except Exception as e:
            logger.error(f"❌ Error forwarding to Salesforce: {e}")
            return False
    
    def test_salesforce_connection(self):
        """Test Salesforce connection and permissions"""
        try:
            access_token = self.sf_auth.get_access_token()
            if not access_token:
                return False, "Failed to get access token"
            
            # Test a simple query to verify permissions
            headers = {
                'Authorization': f'Bearer {access_token}',
                'Content-Type': 'application/json'
            }
            
            test_url = f"{SF_INSTANCE_URL}/services/data/v58.0/query?q=SELECT+Id+FROM+Contact+LIMIT+1"
            response = requests.get(test_url, headers=headers, timeout=30)
            
            if response.status_code == 200:
                return True, "Salesforce connection successful"
            else:
                return False, f"Salesforce test failed: {response.status_code} - {response.text}"
                
        except Exception as e:
            return False, f"Test connection error: {e}"

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

# ... [Previous imports and setup remain the same] ...

# Update the handle-unregistered endpoint to properly communicate with Salesforce
@app.route('/api/handle-unregistered', methods=['POST'])
def handle_unregistered():
    """Handle unregistered users - registration flow"""
    try:
        data = request.get_json()
        chat_id = data.get('chatId')
        message = data.get('message', '')
        
        if not chat_id:
            return jsonify({'error': 'Missing chatId'}), 400
        
        # Check if this is a phone number
        if is_phone_number(message):
            bot_manager.send_message(chat_id,
                '📞 Checking your phone number...\n\n'
                'If we find your account, you will be connected immediately.\n'
                'If not, we will help you create a new account.'
            )
            
            # Create proper payload for Salesforce
            payload = {
                'chatId': str(chat_id),
                'userId': str(chat_id),  # Using chat_id as userId for now
                'message': message,
                'messageId': str(int(time.time())),
                'timestamp': str(int(time.time())),
                'firstName': data.get('firstName', ''),
                'lastName': data.get('lastName', '')
            }
            
            # Forward to Salesforce for processing
            success = bot_manager.forward_to_salesforce(payload)
            
        # Check if this is an email
        elif is_email(message):
            bot_manager.send_message(chat_id,
                '📧 Checking your email address...'
            )
            
            # Create proper payload for Salesforce
            payload = {
                'chatId': str(chat_id),
                'userId': str(chat_id),
                'message': message,
                'messageId': str(int(time.time())),
                'timestamp': str(int(time.time())),
                'firstName': data.get('firstName', ''),
                'lastName': data.get('lastName', '')
            }
            
            bot_manager.forward_to_salesforce(payload)
            
        else:
            # Ask for phone/email
            bot_manager.send_message(chat_id,
                '👋 Welcome! To get started, please share:\n\n'
                '• Your phone number (0912121212)\n'
                '• Or your email address\n\n'
                'We\'ll use this to find your account or create a new one.'
            )
        
        return jsonify({'status': 'success'})
            
    except Exception as e:
        logger.error(f"❌ Handle unregistered error: {e}")
        return jsonify({'error': str(e)}), 500

# Update the webhook to handle unregistered users properly
@app.route('/webhook', methods=['POST'])
def telegram_webhook():
    """Receive Telegram webhook"""
    try:
        if request.is_json:
            update_data = request.get_json()
            logger.info(f"📥 Received webhook update")
            
            if 'message' in update_data:
                message = update_data['message']
                chat_id = message['chat']['id']
                message_text = message.get('text', '')
                user_data = message.get('from', {})
                message_id = message['message_id']
                
                logger.info(f"📥 Message from {chat_id}: {message_text}")
                
                # Handle /start command
                if message_text == '/start':
                    bot_manager.send_message(chat_id,
                        '👋 Welcome to Bank of Abyssinia!\n\n'
                        'Please share your phone number or email address to get started.'
                    )
                    return jsonify({'status': 'ok'})
                
                # Check if this is a phone number or email for unregistered user
                if is_phone_number(message_text) or is_email(message_text):
                    # Forward to unregistered handler endpoint
                    unregistered_payload = {
                        'chatId': str(chat_id),
                        'message': message_text,
                        'firstName': user_data.get('first_name', ''),
                        'lastName': user_data.get('last_name', '')
                    }
                    
                    # Call handle-unregistered endpoint
                    response = requests.post(
                        f'http://localhost:{PORT}/api/handle-unregistered',
                        json=unregistered_payload,
                        timeout=30
                    )
                    
                    logger.info(f"Unregistered handler response: {response.status_code}")
                    
                else:
                    # Prepare payload for Salesforce inbound handler
                    payload = {
                        'chatId': str(chat_id),
                        'userId': str(user_data.get('id', chat_id)),
                        'message': message_text,
                        'messageId': str(message_id),
                        'timestamp': str(int(time.time())),
                        'firstName': user_data.get('first_name', ''),
                        'lastName': user_data.get('last_name', '')
                    }
                    
                    # Forward to Salesforce
                    success = bot_manager.forward_to_salesforce(payload)
                    
                    if not success:
                        # Send error message to user
                        bot_manager.send_message(chat_id,
                            '⚠️ We are experiencing technical difficulties. Please try again in a few moments.'
                        )
            
            return jsonify({'status': 'ok'})
        else:
            logger.error("❌ Non-JSON webhook received")
            return jsonify({'error': 'Invalid data format'}), 400
            
    except Exception as e:
        logger.error(f"❌ Webhook error: {e}")
        return jsonify({'error': str(e)}), 500

# ... [Rest of the code remains the same] ...

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
    success, message = bot_manager.test_salesforce_connection()
    
    if success:
        return jsonify({
            'status': 'success',
            'message': message,
            'instance_url': SF_INSTANCE_URL,
            'client_id_set': bool(SF_CLIENT_ID),
            'client_secret_set': bool(SF_CLIENT_SECRET)
        })
    else:
        return jsonify({
            'status': 'error',
            'message': message,
            'instance_url': SF_INSTANCE_URL,
            'client_id_set': bool(SF_CLIENT_ID),
            'client_secret_set': bool(SF_CLIENT_SECRET)
        }), 500

# Check configuration endpoint
@app.route('/config', methods=['GET'])
def config_check():
    """Check current configuration"""
    config = {
        'bot_token_set': bool(BOT_TOKEN),
        'salesforce_webhook_url': SALESFORCE_WEBHOOK_URL,
        'sf_instance_url': SF_INSTANCE_URL,
        'sf_client_id_set': bool(SF_CLIENT_ID),
        'sf_client_secret_set': bool(SF_CLIENT_SECRET),
        'port': PORT,
        'host': request.host
    }
    return jsonify(config)

# Health check
@app.route('/health', methods=['GET'])
def health_check():
    # Test Salesforce connection
    sf_connected, sf_message = bot_manager.test_salesforce_connection()
    
    return jsonify({
        'status': 'healthy' if BOT_TOKEN and sf_connected else 'unhealthy',
        'service': 'telegram-salesforce-bot',
        'telegram_bot': '✅ Set' if BOT_TOKEN else '❌ Missing',
        'salesforce_connection': '✅ Connected' if sf_connected else f'❌ {sf_message}',
        'timestamp': time.time()
    })

@app.route('/')
def home():
    return jsonify({
        'message': 'Telegram Bot for Salesforce Integration',
        'service': 'Running',
        'endpoints': {
            'webhook': 'POST /webhook',
            'send_to_user': 'POST /api/send-to-user',
            'handle_unregistered': 'POST /api/handle-unregistered',
            'set_webhook': 'GET /set-webhook',
            'test_salesforce': 'GET /test-salesforce',
            'config': 'GET /config',
            'health': 'GET /health'
        },
        'instructions': '1. Check config: GET /config\n2. Test Salesforce: GET /test-salesforce\n3. Set webhook: GET /set-webhook'
    })

if __name__ == '__main__':
    logger.info("=" * 50)
    logger.info("🚀 Starting Telegram Bot")
    logger.info("=" * 50)
    
    if missing_vars:
        logger.error(f"❌ Missing environment variables: {', '.join(missing_vars)}")
    else:
        logger.info("✅ All environment variables are set")
        
        # Test Salesforce connection on startup
        logger.info("🔗 Testing Salesforce connection...")
        sf_connected, sf_message = bot_manager.test_salesforce_connection()
        if sf_connected:
            logger.info("✅ Salesforce connection successful")
        else:
            logger.error(f"❌ Salesforce connection failed: {sf_message}")
    
    logger.info(f"🌐 Starting server on port {PORT}")
    app.run(host='0.0.0.0', port=PORT, debug=False)