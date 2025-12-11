import os
import logging
import requests
import time
from flask import Flask, request, jsonify
from telegram import Update, Bot
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
import asyncio
from threading import Thread

# Configuration
BOT_TOKEN = os.getenv('BOT_TOKEN')
SALESFORCE_WEBHOOK_URL = os.getenv('SALESFORCE_WEBHOOK_URL')
SF_INSTANCE_URL = os.getenv('SF_INSTANCE_URL')
SF_CLIENT_ID = os.getenv('SF_CLIENT_ID')
SF_CLIENT_SECRET = os.getenv('SF_CLIENT_SECRET')

app = Flask(__name__)

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

class TelegramBotManager:
    def __init__(self):
        self.bot_token = BOT_TOKEN
        self.bot = Bot(token=self.bot_token)
        self.sf_webhook = SALESFORCE_WEBHOOK_URL
        
    async def send_message(self, chat_id, text):
        """Send message to Telegram"""
        try:
            await self.bot.send_message(chat_id=chat_id, text=text)
            logger.info(f"✅ Message sent to {chat_id}")
            return True
        except Exception as e:
            logger.error(f"❌ Failed to send to {chat_id}: {e}")
            return False
    
    def forward_to_salesforce(self, payload):
        """Forward message to Salesforce"""
        try:
            headers = {'Content-Type': 'application/json'}
            response = requests.post(
                self.sf_webhook, 
                json=payload, 
                headers=headers, 
                timeout=30
            )
            
            if response.status_code == 200:
                logger.info(f"✅ Forwarded to Salesforce: {payload.get('chatId')}")
                return True
            else:
                logger.error(f"❌ Salesforce error: {response.status_code} - {response.text}")
                return False
                
        except Exception as e:
            logger.error(f"❌ Error forwarding to Salesforce: {e}")
            return False

# Initialize bot manager
bot_manager = TelegramBotManager()

# Flask routes for Salesforce callbacks
@app.route('/api/send-to-user', methods=['POST'])
def send_to_user():
    """Endpoint for Salesforce to send messages to Telegram"""
    try:
        data = request.get_json()
        
        if not data or 'chat_id' not in data or 'message' not in data:
            return jsonify({'error': 'Missing chat_id or message'}), 400
        
        chat_id = data['chat_id']
        message = data['message']
        attachment_url = data.get('attachment_url')
        
        # Run async function
        asyncio.run(bot_manager.send_message(chat_id, message))
        
        return jsonify({
            'status': 'success', 
            'message': 'Message sent to Telegram',
            'chat_id': chat_id
        })
            
    except Exception as e:
        logger.error(f"❌ Send error: {e}")
        return jsonify({'error': str(e)}), 500

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
            asyncio.run(bot_manager.send_message(chat_id,
                '📞 Checking your phone number...\n\n'
                'If we find your account, you will be connected immediately.\n'
                'If not, we will help you create a new account.'
            ))
            
            # Forward to Salesforce for processing
            bot_manager.forward_to_salesforce(data)
            
        # Check if this is an email
        elif is_email(message):
            asyncio.run(bot_manager.send_message(chat_id,
                '📧 Checking your email address...'
            ))
            bot_manager.forward_to_salesforce(data)
            
        else:
            # Ask for phone/email
            asyncio.run(bot_manager.send_message(chat_id,
                '👋 Welcome! To get started, please share:\n\n'
                '• Your phone number (0912121212)\n'
                '• Or your email address'
            ))
        
        return jsonify({'status': 'success'})
            
    except Exception as e:
        logger.error(f"❌ Handle unregistered error: {e}")
        return jsonify({'error': str(e)}), 500

# Webhook for Telegram
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
                message_id = message['message_id']
                
                logger.info(f"📥 Received: {chat_id} - {message_text}")
                
                # Prepare payload for Salesforce
                payload = {
                    'chatId': str(chat_id),
                    'userId': str(user_data.get('id', '')),
                    'message': message_text,
                    'messageId': str(message_id),
                    'timestamp': str(int(time.time())),
                    'firstName': user_data.get('first_name', ''),
                    'lastName': user_data.get('last_name', '')
                }
                
                # Forward to Salesforce
                success = bot_manager.forward_to_salesforce(payload)
                
                if success:
                    return jsonify({'status': 'ok'})
                else:
                    return jsonify({'error': 'Failed to forward to Salesforce'}), 500
            
            return jsonify({'status': 'ok'})
        else:
            logger.error("❌ Non-JSON webhook received")
            return jsonify({'error': 'Invalid data format'}), 400
            
    except Exception as e:
        logger.error(f"❌ Webhook error: {e}")
        return jsonify({'error': str(e)}), 500

# Utility functions
def is_phone_number(text):
    import re
    phone_pattern = r'^(\+?251|0)?[97]\d{8}$'
    return re.match(phone_pattern, text.strip()) is not None

def is_email(text):
    import re
    email_pattern = r'^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$'
    return re.match(email_pattern, text.strip()) is not None

# Health check
@app.route('/health', methods=['GET'])
def health_check():
    return jsonify({
        'status': 'healthy',
        'service': 'telegram-salesforce-bot',
        'bot_token_set': bool(BOT_TOKEN),
        'salesforce_webhook_set': bool(SALESFORCE_WEBHOOK_URL)
    })

@app.route('/')
def home():
    return jsonify({
        'message': 'Telegram Bot for Salesforce Integration',
        'endpoints': {
            'webhook': 'POST /webhook (Telegram webhook)',
            'send_to_user': 'POST /api/send-to-user (Salesforce to Telegram)',
            'handle_unregistered': 'POST /api/handle-unregistered',
            'health': 'GET /health'
        }
    })

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 10000))
    app.run(host='0.0.0.0', port=port, debug=False)