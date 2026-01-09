import os
import logging
import requests
import time
import re
import json
import uuid
from datetime import datetime
from collections import OrderedDict
from flask import Flask, request, jsonify, g

# ============================================
# ENHANCED CONFIGURATION WITH SECURITY SETTINGS
# ============================================
BOT_TOKEN = os.getenv('BOT_TOKEN')
SALESFORCE_WEBHOOK_URL = os.getenv('SALESFORCE_WEBHOOK_URL')
SF_INSTANCE_URL = os.getenv('SF_INSTANCE_URL')
SF_CLIENT_ID = os.getenv('SF_CLIENT_ID')
SF_CLIENT_SECRET = os.getenv('SF_CLIENT_SECRET')

# Security configurations
RATE_LIMIT_PER_MINUTE = int(os.getenv('RATE_LIMIT_PER_MINUTE', '30'))
MAX_MESSAGE_LENGTH = int(os.getenv('MAX_MESSAGE_LENGTH', '4000'))
REQUEST_TIMEOUT = int(os.getenv('REQUEST_TIMEOUT', '30'))
ENABLE_RATE_LIMITING = os.getenv('ENABLE_RATE_LIMITING', 'true').lower() == 'true'
ENABLE_INPUT_SANITIZATION = os.getenv('ENABLE_INPUT_SANITIZATION', 'true').lower() == 'true'
PORT = int(os.getenv('PORT', '10000'))

# Validate required environment variables
missing_vars = []
for var_name, var_value in [
    ('BOT_TOKEN', BOT_TOKEN),
    ('SALESFORCE_WEBHOOK_URL', SALESFORCE_WEBHOOK_URL),
    ('SF_INSTANCE_URL', SF_INSTANCE_URL),
    ('SF_CLIENT_ID', SF_CLIENT_ID),
    ('SF_CLIENT_SECRET', SF_CLIENT_SECRET)
]:
    if not var_value:
        missing_vars.append(var_name)

app = Flask(__name__)

# ============================================
# ENHANCED LOGGING WITH SECURITY CONTEXT
# ============================================
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - [%(request_id)s] - [IP:%(client_ip)s] - %(message)s'
)
logger = logging.getLogger(__name__)

# Add filter to inject request context
class SecurityContextFilter(logging.Filter):
    def filter(self, record):
        record.request_id = getattr(g, 'request_id', 'no-id')
        record.client_ip = getattr(g, 'client_ip', 'no-ip')
        return True

logger.addFilter(SecurityContextFilter())

# ============================================
# RATE LIMITING IMPLEMENTATION
# ============================================
class RateLimiter:
    """Simple in-memory rate limiter"""
    
    def __init__(self, requests_per_minute=30):
        self.requests_per_minute = requests_per_minute
        self.requests = OrderedDict()  # {ip: [timestamps]}
        self.cleanup_interval = 60  # Cleanup every 60 seconds
        self.last_cleanup = time.time()
    
    def _cleanup_old_requests(self):
        """Remove requests older than 1 minute"""
        current_time = time.time()
        cutoff = current_time - 60
        
        for ip in list(self.requests.keys()):
            # Filter timestamps
            self.requests[ip] = [t for t in self.requests[ip] if t > cutoff]
            # Remove empty lists
            if not self.requests[ip]:
                del self.requests[ip]
        
        self.last_cleanup = current_time
    
    def is_rate_limited(self, ip):
        """Check if IP is rate limited"""
        if not ENABLE_RATE_LIMITING:
            return False
        
        current_time = time.time()
        
        # Cleanup old requests if needed
        if current_time - self.last_cleanup > self.cleanup_interval:
            self._cleanup_old_requests()
        
        # Get or create IP entry
        if ip not in self.requests:
            self.requests[ip] = []
        
        # Remove timestamps older than 1 minute
        self.requests[ip] = [t for t in self.requests[ip] if t > current_time - 60]
        
        # Check if rate limited
        if len(self.requests[ip]) >= self.requests_per_minute:
            return True
        
        # Add current request
        self.requests[ip].append(current_time)
        return False

# Initialize rate limiter
rate_limiter = RateLimiter(requests_per_minute=RATE_LIMIT_PER_MINUTE)

# ============================================
# SECURITY MIDDLEWARE
# ============================================
@app.before_request
def security_middleware():
    """Security middleware for all requests"""
    # Generate request ID for tracing
    g.request_id = str(uuid.uuid4())[:12]
    
    # Get client IP (handling proxies)
    g.client_ip = request.headers.get('X-Forwarded-For', request.remote_addr)
    if ',' in g.client_ip:
        g.client_ip = g.client_ip.split(',')[0].strip()
    
    # Skip rate limiting for health/status endpoints
    if request.path in ['/health', '/', '/test', '/metrics']:
        return
    
    # Apply rate limiting
    if rate_limiter.is_rate_limited(g.client_ip):
        logger.warning(f"Rate limit exceeded for IP: {g.client_ip}")
        return jsonify({
            'error': 'Rate limit exceeded',
            'message': 'Too many requests. Please try again later.'
        }), 429

# ============================================
# SECURITY UTILITY FUNCTIONS
# ============================================
def sanitize_input(text, max_length=MAX_MESSAGE_LENGTH):
    """Sanitize user input to prevent injection attacks"""
    if not text or not ENABLE_INPUT_SANITIZATION:
        return text[:max_length] if text else text
    
    # Remove null bytes
    text = text.replace('\x00', '')
    
    # Truncate to max length
    if len(text) > max_length:
        text = text[:max_length]
        logger.warning(f"Message truncated to {max_length} characters")
    
    return text

def sanitize_salesforce_id(sf_id):
    """Validate and sanitize Salesforce ID"""
    if not sf_id:
        return None
    
    # Salesforce IDs are 15 or 18 characters, alphanumeric
    if not re.match(r'^[a-zA-Z0-9]{15,18}$', sf_id):
        logger.warning(f"Invalid Salesforce ID format: {sf_id}")
        return None
    
    return sf_id

def sanitize_phone_number(phone):
    """Enhanced phone number sanitization with validation"""
    if not phone:
        return ""
    
    # Remove all non-digits
    cleaned = re.sub(r'[^\d]', '', phone)
    
    # Validate Ethiopian phone format
    if len(cleaned) < 9 or len(cleaned) > 12:
        return ""
    
    # Handle Ethiopian country codes
    if cleaned.startswith('251'):
        cleaned = cleaned[3:]
    elif cleaned.startswith('+251'):
        cleaned = cleaned[4:]
    
    # Ensure it starts with 0
    if not cleaned.startswith('0'):
        cleaned = '0' + cleaned
    
    # Final validation: Ethiopian mobile numbers start with 09 or 07
    if not re.match(r'^0[79]\d{8}$', cleaned):
        return ""
    
    return cleaned

def validate_telegram_payload(payload):
    """Validate Telegram webhook payload structure"""
    required_fields = ['update_id']
    
    if not isinstance(payload, dict):
        return False, "Payload must be a dictionary"
    
    for field in required_fields:
        if field not in payload:
            return False, f"Missing required field: {field}"
    
    # Validate message structure if present
    if 'message' in payload:
        message = payload['message']
        if 'chat' not in message or 'id' not in message['chat']:
            return False, "Invalid message structure"
        
        # Validate chat ID is numeric
        if not isinstance(message['chat']['id'], (int, str)):
            return False, "Invalid chat ID type"
        
        try:
            chat_id = int(message['chat']['id'])
            if chat_id <= 0:
                return False, "Invalid chat ID value"
        except (ValueError, TypeError):
            return False, "Chat ID must be numeric"
    
    return True, "Valid payload"

# ============================================
# ENHANCED SALESFORCE AUTH WITH SECURITY
# ============================================
class SalesforceAuth:
    """Handles Salesforce OAuth 2.0 authentication with security enhancements"""
    
    def __init__(self):
        self.instance_url = SF_INSTANCE_URL.rstrip('/')
        self.client_id = SF_CLIENT_ID
        self.client_secret = SF_CLIENT_SECRET
        self.access_token = None
        self.token_expiry = 0
        self.token_lock = False
    
    def get_access_token(self):
        """Get Salesforce access token with enhanced security"""
        try:
            # Check cached token with safety margin
            if self.access_token and time.time() < (self.token_expiry - 300):
                logger.debug("Using cached Salesforce access token")
                return self.access_token
            
            # Prevent multiple concurrent token requests
            if self.token_lock:
                wait_time = 0
                while self.token_lock and wait_time < 5:
                    time.sleep(0.1)
                    wait_time += 0.1
                if self.access_token and time.time() < (self.token_expiry - 300):
                    return self.access_token
            
            self.token_lock = True
            
            token_url = f"{self.instance_url}/services/oauth2/token"
            payload = {
                'grant_type': 'client_credentials',
                'client_id': self.client_id,
                'client_secret': self.client_secret
            }
            
            headers = {
                'Content-Type': 'application/x-www-form-urlencoded',
                'User-Agent': 'Telegram-Support-Bot/1.0'
            }
            
            logger.info("Requesting Salesforce access token...")
            response = requests.post(
                token_url, 
                data=payload, 
                headers=headers, 
                timeout=REQUEST_TIMEOUT,
                verify=True
            )
            
            if response.status_code == 200:
                token_data = response.json()
                self.access_token = token_data.get('access_token')
                expires_in = token_data.get('expires_in', 3600)
                self.token_expiry = time.time() + expires_in
                
                # Log token acquisition (without exposing token)
                token_prefix = self.access_token[:10] + '...' if self.access_token else 'None'
                logger.info(f"Salesforce access token acquired: {token_prefix}")
                return self.access_token
            else:
                logger.error(f"Token request failed: {response.status_code}")
                return None
                
        except requests.exceptions.SSLError as e:
            logger.error(f"SSL error during token request: {e}")
            return None
        except requests.exceptions.Timeout:
            logger.error("Token request timeout")
            return None
        except Exception as e:
            logger.error(f"Token exception: {str(e)[:100]}")
            return None
        finally:
            self.token_lock = False

# ============================================
# ENHANCED TELEGRAM BOT MANAGER WITH SECURITY
# ============================================
class TelegramBotManager:


    #/////////
    def answer_callback_query(self, callback_query_id, text=None, show_alert=False):
        """Answer a callback query"""
        try:
            url = f"{self.base_url}/answerCallbackQuery"
            data = {
                'callback_query_id': callback_query_id,
                'show_alert': show_alert
            }
            if text:
                data['text'] = text[:200]  # Telegram has 200 char limit for this
                
            response = self._execute_safe_request(url, data=data)
            return response.json().get('ok', False)
        except Exception as e:
            logger.error(f"Error answering callback query: {e}")
            return False

    def edit_message_reply_markup(self, chat_id, message_id, reply_markup=None):
        """Edit message reply markup (remove buttons)"""
        try:
            url = f"{self.base_url}/editMessageReplyMarkup"
            data = {
                'chat_id': chat_id,
                'message_id': message_id
            }
            if reply_markup:
                data['reply_markup'] = json.dumps(reply_markup)
                
            response = self._execute_safe_request(url, data=data)
            return response.json().get('ok', False)
        except Exception as e:
            logger.error(f"Error editing message markup: {e}")
            return False
    #/////////

    def __init__(self):
        self.bot_token = BOT_TOKEN
        if self.bot_token:
            self.base_url = f"https://api.telegram.org/bot{self.bot_token}"
        else:
            self.base_url = None
            
        self.sf_webhook = SALESFORCE_WEBHOOK_URL
        self.sf_auth = SalesforceAuth()
        
    def _execute_safe_request(self, url, method='POST', **kwargs):
        """Execute HTTP request with enhanced security"""
        try:
            # Set default timeout
            kwargs.setdefault('timeout', REQUEST_TIMEOUT)
            
            # Add security headers
            headers = kwargs.get('headers', {})
            headers['User-Agent'] = 'Telegram-Support-Bot/1.0'
            kwargs['headers'] = headers
            
            # Execute request
            if method.upper() == 'POST':
                response = requests.post(url, **kwargs)
            elif method.upper() == 'GET':
                response = requests.get(url, **kwargs)
            elif method.upper() == 'PATCH':
                response = requests.patch(url, **kwargs)
            else:
                raise ValueError(f"Unsupported HTTP method: {method}")
            
            return response
            
        except requests.exceptions.Timeout:
            logger.error(f"Request timeout to {url}")
            raise
        except requests.exceptions.SSLError as e:
            logger.error(f"SSL error to {url}: {e}")
            raise
        except requests.exceptions.ConnectionError as e:
            logger.error(f"Connection error to {url}: {e}")
            raise
        except Exception as e:
            logger.error(f"Request error to {url}: {str(e)[:100]}")
            raise
    
    def send_message(self, chat_id, text, reply_markup=None, parse_mode='HTML'):
        """Send message to Telegram with security enhancements"""
        try:
            if not self.base_url:
                logger.error("BOT_TOKEN not configured")
                return False
            
            # Sanitize message text
            safe_text = sanitize_input(text)
            
            url = f"{self.base_url}/sendMessage"
            data = {
                'chat_id': chat_id,
                'text': safe_text,
                'parse_mode': parse_mode
            }
            
            if reply_markup:
                data['reply_markup'] = json.dumps(reply_markup)
            
            # Retry logic with exponential backoff
            max_retries = 3
            for attempt in range(max_retries):
                try:
                    response = self._execute_safe_request(url, data=data)
                    result = response.json()
                    
                    if result.get('ok'):
                        logger.info(f"Message sent to {chat_id}")
                        return True
                    else:
                        error_desc = result.get('description', 'Unknown error')
                        
                        # Handle rate limits from Telegram
                        if "retry after" in error_desc.lower() and attempt < max_retries - 1:
                            match = re.search(r'retry after (\d+)', error_desc.lower())
                            if match:
                                wait_time = int(match.group(1))
                                logger.warning(f"Telegram rate limit, waiting {wait_time}s")
                                time.sleep(wait_time)
                                continue
                        
                        logger.error(f"Failed to send to {chat_id}: {error_desc}")
                        return False
                        
                except Exception as e:
                    if attempt < max_retries - 1:
                        wait_time = 2 ** attempt  # Exponential backoff
                        logger.warning(f"Send failed, retry {attempt + 1}/{max_retries} in {wait_time}s")
                        time.sleep(wait_time)
                        continue
                    raise
                
        except Exception as e:
            logger.error(f"Failed to send to {chat_id}: {str(e)[:100]}")
            return False
    
    def forward_to_salesforce(self, payload):
        """Forward message to Salesforce with enhanced security"""
        try:
            if not self.sf_webhook:
                logger.error("SALESFORCE_WEBHOOK_URL not configured")
                return False
            
            # Get Salesforce access token
            access_token = self.sf_auth.get_access_token()
            if not access_token:
                logger.error("Failed to get Salesforce access token")
                return False
            
            # Sanitize payload
            safe_payload = self._sanitize_payload(payload)
            
            headers = {
                'Authorization': f'Bearer {access_token}',
                'Content-Type': 'application/json',
                'User-Agent': 'Telegram-Support-Bot/1.0'
            }
            
            logger.info(f"Forwarding to Salesforce webhook")
            
            # Retry logic for Salesforce
            max_retries = 3
            for attempt in range(max_retries):
                try:
                    response = self._execute_safe_request(
                        self.sf_webhook,
                        json=safe_payload,
                        headers=headers
                    )
                    
                    if response.status_code == 200:
                        logger.info(f"Forwarded to Salesforce: {safe_payload.get('chatId')}")
                        return True
                    elif response.status_code == 401 and attempt < max_retries - 1:
                        # Token expired, refresh and retry
                        logger.warning(f"Auth failed, refreshing token and retrying")
                        self.sf_auth.access_token = None
                        self.sf_auth.token_expiry = 0
                        access_token = self.sf_auth.get_access_token()
                        if access_token:
                            headers['Authorization'] = f'Bearer {access_token}'
                        time.sleep(1)
                        continue
                    else:
                        logger.error(f"Salesforce error {response.status_code}")
                        return False
                        
                except Exception as e:
                    if attempt < max_retries - 1:
                        logger.warning(f"Salesforce request failed, retry {attempt + 1}/{max_retries}")
                        time.sleep(2)
                        continue
                    raise
                
        except Exception as e:
            logger.error(f"Error forwarding to Salesforce: {str(e)[:100]}")
            return False
    
    def _sanitize_payload(self, payload):
        """Sanitize payload before sending to Salesforce"""
        safe_payload = payload.copy()
        
        # Sanitize text fields
        text_fields = ['message', 'firstName', 'lastName', 'username']
        for field in text_fields:
            if field in safe_payload:
                safe_payload[field] = sanitize_input(safe_payload[field])
        
        # Sanitize IDs
        id_fields = ['conversationId', 'sessionId', 'chatId']
        for field in id_fields:
            if field in safe_payload:
                # Remove any non-alphanumeric characters from IDs
                if safe_payload[field]:
                    safe_payload[field] = re.sub(r'[^a-zA-Z0-9]', '', str(safe_payload[field]))
        
        return safe_payload
    
    def _sanitize_sql_param(self, param):
        """Sanitize parameters for SOQL queries"""
        if param is None:
            return "''"
        
        param_str = str(param)
        
        # Remove potentially dangerous characters
        # Allow alphanumeric, spaces, underscores, dashes, dots
        sanitized = re.sub(r'[^\w\s\-\.]', '', param_str)
        
        # Escape single quotes for SOQL
        sanitized = sanitized.replace("'", "\\'")
        
        # Limit length
        if len(sanitized) > 255:
            sanitized = sanitized[:255]
            logger.warning(f"SQL parameter truncated to 255 chars")
        
        return f"'{sanitized}'" if sanitized else "''"
    
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
            logger.error(f"Error sending typing action: {e}")
            return False
    
    def clean_phone_number(self, phone):
        """Clean phone number for Salesforce"""
        return sanitize_phone_number(phone)
    
    def check_existing_channel_user(self, telegram_id):
        """Check if Channel_User__c exists by Telegram Chat ID with SQL injection protection"""
        try:
            access_token = self.sf_auth.get_access_token()
            if not access_token:
                return None
            
            headers = {
                'Authorization': f'Bearer {access_token}',
                'Content-Type': 'application/json'
            }
            
            # Sanitize input
            sanitized_id = self._sanitize_sql_param(telegram_id)
            
            query = f"""
            SELECT Id, Name, Channel_Type__c, Channel_ID__c,
                   Telegram_Chat_ID__c, Contact__c, Contact__r.Name,
                   Contact__r.FirstName, Contact__r.LastName,
                   Created_Date__c, Last_Activity_Date__c
            FROM Channel_User__c 
            WHERE Channel_Type__c = 'Telegram' 
            AND Telegram_Chat_ID__c = {sanitized_id}
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
            logger.error(f"Error checking channel user: {e}")
            return None
    
    def find_contact_by_phone(self, phone_number):
        """Find contact by phone number in Salesforce with SQL injection protection"""
        try:
            access_token = self.sf_auth.get_access_token()
            if not access_token:
                return None
            
            headers = {
                'Authorization': f'Bearer {access_token}',
                'Content-Type': 'application/json'
            }
            
            clean_phone = self.clean_phone_number(phone_number)
            if not clean_phone:
                return None
            
            # Use LIKE with sanitized input
            sanitized_phone = self._sanitize_sql_param(f"%{clean_phone}")
            
            query = f"""
            SELECT Id, FirstName, LastName, Salutation, Phone, MobilePhone, Email 
            FROM Contact 
            WHERE Phone LIKE {sanitized_phone}
               OR MobilePhone LIKE {sanitized_phone}
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
            logger.error(f"Error finding contact by phone: {e}")
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
            
            # Sanitize input
            sanitized_id = sanitize_salesforce_id(channel_user_id)
            if not sanitized_id:
                return None
            
            query = f"""
            SELECT Id, Name, Status__c, Channel_User_Name__c,
                   Created_Date__c, Last_Activity_Date__c,
                   Last_Message_Date__c
            FROM Support_Conversation__c 
            WHERE Channel_User_Name__c = '{sanitized_id}'
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
            logger.error(f"Error getting active conversation: {e}")
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
            
            # Sanitize input
            sanitized_id = sanitize_salesforce_id(conversation_id)
            if not sanitized_id:
                return []
            
            query = f"""
            SELECT Id, Name, Status__c, OwnerId, Owner.Name, 
                   Assigned_Agent__c, Assigned_Agent__r.Name,
                   Created_Date__c, Last_Message_Time__c
            FROM Chat_Session__c 
            WHERE Support_Conversation__c = '{sanitized_id}'
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
            logger.error(f"Error getting active sessions: {e}")
            return []
    
    def create_channel_user_with_conversation(self, telegram_id, phone=None, contact_id=None, first_name=None, last_name=None):
        """Create Channel_User__c AND Support_Conversation__c together with sanitized data"""
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
            
            # Sanitize inputs for name
            safe_first_name = re.sub(r'[^\w\s\-]', '', first_name or '')[:40]
            safe_last_name = re.sub(r'[^\w\s\-]', '', last_name or '')[:40]
            
            # Generate a name for the channel user
            if safe_first_name and safe_last_name:
                name = f'Telegram: {safe_first_name} {safe_last_name}'
            elif phone:
                name = f'Telegram: {phone}'
            else:
                name = f'Telegram: {telegram_id}'
            
            # Truncate name if too long
            name = name[:80]
            
            channel_user_data = {
                'Channel_Type__c': 'Telegram',
                'Channel_ID__c': f'telegram_{telegram_id}'[:80],
                'Telegram_Chat_ID__c': str(telegram_id),
                'Name': name,
                'Created_Date__c': time.strftime('%Y-%m-%dT%H:%M:%S.000Z', time.gmtime()),
                'Last_Activity_Date__c': time.strftime('%Y-%m-%dT%H:%M:%S.000Z', time.gmtime())
            }
            
            # Add contact relationship if available and valid
            if contact_id and re.match(r'^[a-zA-Z0-9]{15,18}$', contact_id):
                channel_user_data['Contact__c'] = contact_id
            
            logger.info(f"Creating Channel User for {telegram_id}")
            response = requests.post(channel_user_url, headers=headers, json=channel_user_data, timeout=30)
            
            if response.status_code != 201:
                logger.error(f"Failed to create Channel_User__c: {response.status_code}")
                return None
            
            channel_user_result = response.json()
            channel_user_id = channel_user_result['id']
            logger.info(f"Created Channel_User__c: {channel_user_id}")
            
            # 2. CREATE SUPPORT CONVERSATION (Active state)
            conversation_url = f"{SF_INSTANCE_URL}/services/data/v58.0/sobjects/Support_Conversation__c/"
            
            conversation_data = {
                'Channel_User_Name__c': channel_user_id,
                'Status__c': 'Active',
                'Created_Date__c': time.strftime('%Y-%m-%dT%H:%M:%S.000Z', time.gmtime()),
                'Last_Activity_Date__c': time.strftime('%Y-%m-%dT%H:%M:%S.000Z', time.gmtime()),
                'Last_Message_Date__c': time.strftime('%Y-%m-%dT%H:%M:%S.000Z', time.gmtime())
            }
            
            logger.info(f"Creating Support Conversation for Channel User {channel_user_id}")
            response = requests.post(conversation_url, headers=headers, json=conversation_data, timeout=30)
            
            if response.status_code != 201:
                logger.error(f"Failed to create Support_Conversation__c: {response.status_code}")
                # Channel user was created, but conversation failed - return channel user ID only
                return {'channelUserId': channel_user_id, 'conversationId': None}
            
            conversation_result = response.json()
            conversation_id = conversation_result['id']
            logger.info(f"Created Support_Conversation__c: {conversation_id}")
            
            # 3. UPDATE CONTACT WITH TELEGRAM ID (if contact exists and valid)
            if contact_id and re.match(r'^[a-zA-Z0-9]{15,18}$', contact_id):
                self.update_contact_telegram_id(contact_id, telegram_id)
            
            return {
                'channelUserId': channel_user_id,
                'conversationId': conversation_id
            }
                
        except Exception as e:
            logger.error(f"Error creating channel user with conversation: {e}")
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
                logger.info(f"Linked Channel_User__c {channel_user_id} to Contact {contact_id}")
                return True
            else:
                logger.error(f"Failed to link Channel_User__c: {response.status_code}")
                return False
                
        except Exception as e:
            logger.error(f"Error linking channel user: {e}")
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
                logger.info(f"Updated contact {contact_id} with Telegram ID {telegram_id}")
                return True
            else:
                logger.error(f"Failed to update contact: {response.status_code}")
                return False
                
        except Exception as e:
            logger.error(f"Error updating contact: {e}")
            return False
    
    def create_new_session(self, conversation_id, chat_id, first_name=None, last_name=None):
        """Trigger creation of new chat session via webhook"""
        try:
            payload = {
                'channelType': 'Telegram',
                'chatId': str(chat_id),
                'messageId': f"TG_SESSION_{int(time.time())}",
                'firstName': first_name,
                'lastName': last_name,
                'isSessionStart': True,
                'conversationId': conversation_id
            }
            
            success = self.forward_to_salesforce(payload)
            
            if success:
                logger.info(f"Triggered session creation for conversation {conversation_id}")
                return True
            else:
                logger.error(f"Failed to trigger session creation")
                return False
                
        except Exception as e:
            logger.error(f"Error creating new session: {e}")
            return False
    
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
            logger.error(f"Error getting queue position: {e}")
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
            logger.error(f"Error getting session details: {e}")
            return None

# Initialize bot manager
bot_manager = TelegramBotManager()

# ============================================
# UTILITY FUNCTIONS WITH SECURITY
# ============================================
def is_phone_number(text):
    if not text:
        return False
    
    # Use the enhanced sanitize_phone_number function
    cleaned = sanitize_phone_number(text)
    return bool(cleaned)

def is_menu_command(text):
    """Check if text is a menu command"""
    if not text:
        return False
    
    menu_commands = ['/start', 'hi', 'hello', 'hey', 'menu', 'help']
    return text.strip().lower() in menu_commands

def show_main_menu(chat_id, user_name=None, has_active_session=False):
    """Show main menu with inline keyboard buttons"""
    
    # Create appropriate keyboard based on session state
    if has_active_session:
        keyboard = [
            [{"text": "🔄 Continue Support Session", "callback_data": "continue_session"}],
            [{"text": "📋 Track your Case", "callback_data": "track_case"}],
            [{"text": "➕ New Support Request", "callback_data": "new_session"}],
            [{"text": "🏠 Main Menu", "callback_data": "main_menu"}]
        ]
        
        menu_text = f"""
🔔 *You have an active support session*

*Choose an option:*

🔄 *Continue Support Session* - Go back to your active support conversation
📋 *Track your Case* - Check status of existing cases  
➕ *New Support Request* - Start a new support session
🏠 *Main Menu* - See all options
        """
    else:
        welcome_text = "👋 *Welcome to Bank of Abyssinia Support!*"
        if user_name:
            safe_name = re.sub(r'[^\w\s\-]', '', user_name)[:30]
            welcome_text = f"👋 *Welcome back, {safe_name}!*"
        
        keyboard = [
            [{"text": "👥 Contact Customer Support", "callback_data": "contact_support"}],
            [{"text": "📋 Track your Case", "callback_data": "track_case"}],
            [{"text": "🏠 Main Menu", "callback_data": "main_menu"}]
        ]
        
        menu_text = f"""
{welcome_text}

*Choose an option:*

👥 *Contact Customer Support* - Connect with our support team
📋 *Track your Case* - Check status of existing cases
        """
    
    reply_markup = {
        "inline_keyboard": keyboard,
        "resize_keyboard": True,
        "one_time_keyboard": False
    }
    
    return bot_manager.send_message(chat_id, menu_text, reply_markup=reply_markup, parse_mode='Markdown')

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
        logger.error(f"Error handling contact support: {e}")
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

# In-memory storage for user session state
user_session_state = {}

#///////////////////////////////
def handle_callback_query(callback_query):
    """Handle inline keyboard button presses"""
    try:
        chat_id = callback_query['message']['chat']['id']
        callback_data = callback_query['data']
        message_id = callback_query['message']['message_id']
        
        # Acknowledge callback query (removes loading state)
        bot_manager.answer_callback_query(callback_query['id'])
        
        # Get user data from callback query
        user_data = callback_query.get('from', {})
        
        logger.info(f"Callback query from {chat_id}: {callback_data}")
        
        # Check if Channel_User__c exists
        channel_user = bot_manager.check_existing_channel_user(str(chat_id))
        
        if not channel_user:
            # Handle registration for new users
            return handle_new_user_registration_callback(chat_id, callback_data, user_data)
        
        # Get conversation
        conversation = bot_manager.get_active_support_conversation(channel_user['Id'])
        if not conversation:
            error_text = "❌ Sorry, we couldn't find your conversation."
            bot_manager.send_message(chat_id, error_text, parse_mode='Markdown')
            return False
        
        conversation_id = conversation['Id']
        
        # Handle different callback actions
        if callback_data == 'contact_support':
            success, session_id = handle_contact_support(
                chat_id, 
                channel_user['Id'],
                conversation_id,
                user_data
            )
            if success and session_id:
                user_session_state[str(chat_id)] = {
                    'in_session': True,
                    'conversation_id': conversation_id,
                    'session_id': session_id,
                    'session_status': 'Waiting'
                }
            return success
            
        elif callback_data == 'track_case':
            return handle_track_case(chat_id)
            
        elif callback_data == 'new_session':
            # Option to start fresh session even if one exists
            active_sessions = bot_manager.get_active_sessions(conversation_id)
            if active_sessions:
                confirm_keyboard = {
                    "inline_keyboard": [
                        [{"text": "✅ Yes", "callback_data": "confirm_new_session"}],
                        [{"text": "❌ No", "callback_data": "cancel_new_session"}]
                    ]
                }
                confirm_text = """
⚠️ *You already have an active support session.*

Do you want to end the current session and start a new one?
                """
                bot_manager.send_message(chat_id, confirm_text, 
                                       reply_markup=confirm_keyboard, 
                                       parse_mode='Markdown')
                return True
            else:
                return handle_contact_support(chat_id, channel_user['Id'], conversation_id, user_data)
                
        elif callback_data == 'continue_session':
            return handle_continue_session(chat_id, conversation_id)
            
        elif callback_data == 'main_menu':
            user_name = channel_user.get('Contact__r', {}).get('FirstName') or user_data.get('first_name')
            active_sessions = bot_manager.get_active_sessions(conversation_id)
            has_active_session = len(active_sessions) > 0
            return show_main_menu(chat_id, user_name, has_active_session)
            
        elif callback_data == 'confirm_new_session':
            # Logic to close current session would go here
            user_session_state[str(chat_id)] = {}
            return handle_contact_support(chat_id, channel_user['Id'], conversation_id, user_data)
            
        elif callback_data == 'cancel_new_session':
            return handle_continue_session(chat_id, conversation_id)
        
        # Edit the original message to remove buttons after selection
        bot_manager.edit_message_reply_markup(chat_id, message_id, reply_markup=None)
        
        return True
        
    except Exception as e:
        logger.error(f"Callback query error: {e}")
        return False


def handle_new_user_registration_callback(chat_id, callback_data, user_data):
    """Handle callback queries for new users"""
    if callback_data == 'register_phone':
        welcome_text = """
👋 *Welcome to Bank of Abyssinia Support!*

To get started with our support services, we need to register you in our system.

Please share your *phone number* to begin:

Example: *0912121212*
        """
        bot_manager.send_message(chat_id, welcome_text, parse_mode='Markdown')
    return True
#///////////////////////////////

def process_incoming_message(chat_id, message_text, user_data):
    """Process incoming Telegram message with improved session handling and security"""
    try:
        # Sanitize inputs
        safe_message = sanitize_input(message_text)
        safe_chat_id = str(chat_id)
        
        # Validate chat ID
        if not safe_chat_id.isdigit():
            logger.warning(f"Invalid chat ID format: {chat_id}")
            return False
        
        # Show typing indicator
        bot_manager.send_typing_action(chat_id)
        
        chat_id_str = safe_chat_id
        message_lower = safe_message.strip().lower()
        
        logger.info(f"Processing message from {chat_id}: {safe_message[:50]}...")
        
        # STEP 1: Check if Channel_User__c exists
        channel_user = bot_manager.check_existing_channel_user(chat_id_str)
        
        if not channel_user:
            # Handle registration flow
            return handle_new_user_registration(chat_id, safe_message, user_data)
        
        # ✅ Channel User EXISTS
        logger.info(f"Existing Channel User found: {channel_user['Id']}")
        
        # Get conversation for this user
        conversation = bot_manager.get_active_support_conversation(channel_user['Id'])
        if not conversation:
            logger.error(f"No active conversation found for channel user {channel_user['Id']}")
            error_text = "❌ Sorry, we couldn't find your conversation. Please start a new session."
            
            # Show button menu instead of text instruction
            user_name = channel_user.get('Contact__r', {}).get('FirstName') or user_data.get('first_name')
            return show_main_menu(chat_id, user_name, has_active_session=False)
        
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
        if is_menu_command(safe_message):
            user_name = channel_user.get('Contact__r', {}).get('FirstName') or user_data.get('first_name')
            return show_main_menu(chat_id, user_name, has_active_salesforce_session)
        
        # REMOVE THE NUMERIC MENU HANDLING SECTION AND REPLACE WITH:
        # ========================================================
        # Handle text commands for backward compatibility
        # ========================================================
        
        # Only handle text commands if they're clearly menu-related
        # Otherwise, treat them as regular messages
        if message_lower in ['contact', 'support', 'contact support', 'customer support']:
            # For text commands, show the menu with buttons
            user_name = channel_user.get('Contact__r', {}).get('FirstName') or user_data.get('first_name')
            return show_main_menu(chat_id, user_name, has_active_salesforce_session)
        
        elif message_lower in ['track', 'track case', 'case', 'my case']:
            # Handle track case via text command
            return handle_track_case(chat_id)
        
        elif message_lower in ['new', 'new support', 'new session']:
            # Option to start fresh session even if one exists
            if has_active_salesforce_session:
                confirm_keyboard = {
                    "inline_keyboard": [
                        [{"text": "✅ Yes", "callback_data": "confirm_new_session"}],
                        [{"text": "❌ No", "callback_data": "cancel_new_session"}]
                    ]
                }
                confirm_text = """
⚠️ *You already have an active support session.*

Do you want to end the current session and start a new one?
                """
                # Store conversation ID for callback handling
                user_session_state[chat_id_str] = {
                    'awaiting_confirmation': 'new_session',
                    'conversation_id': conversation_id
                }
                return bot_manager.send_message(chat_id, confirm_text, 
                                              reply_markup=confirm_keyboard, 
                                              parse_mode='Markdown')
            else:
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
        
        elif message_lower in ['main menu', 'menu']:
            user_name = channel_user.get('Contact__r', {}).get('FirstName') or user_data.get('first_name')
            return show_main_menu(chat_id, user_name, has_active_salesforce_session)
        
        # Handle confirmation responses (text-based fallback)
        if user_state.get('awaiting_confirmation') == 'new_session':
            if message_lower == 'yes':
                # Logic to close current session and start new one
                user_session_state[chat_id_str] = {}
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
            elif message_lower == 'no':
                user_session_state[chat_id_str] = {}
                return handle_continue_session(chat_id, conversation_id)
        
        # ========================================================
        # REGULAR MESSAGE HANDLING
        # ========================================================
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
            
            logger.info(f"Forwarding message to session {session_id} (status: {session_status})")
            
            payload = {
                'channelType': 'Telegram',
                'chatId': chat_id_str,
                'message': safe_message,
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
            if len(safe_message) > 20 or '?' in safe_message or 'help' in message_lower or 'issue' in message_lower or 'problem' in message_lower:
                # This looks like a support request, auto-initiate session
                logger.info(f"Auto-initiating session for support-like message from {chat_id}")
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
                        'message': safe_message,
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
                # Show menu with buttons for short/ambiguous messages
                logger.info(f"No active session for user {chat_id}, showing menu with buttons")
                user_name = channel_user.get('Contact__r', {}).get('FirstName') or user_data.get('first_name')
                
                # Use the button-based menu instead of text menu
                return show_main_menu(chat_id, user_name, has_active_session=False)
                    
    except Exception as e:
        logger.error(f"Error processing message: {e}")
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
        # Use buttons for new users
        keyboard = {
            "inline_keyboard": [
                [{"text": "📱 Register with Phone Number", "callback_data": "register_phone"}]
            ]
        }
        
        welcome_text = """
👋 *Welcome to Bank of Abyssinia Support!*

To get started with our support services, we need to register you in our system.
        """
        bot_manager.send_message(chat_id, welcome_text, 
                               reply_markup=keyboard, 
                               parse_mode='Markdown')
        return True
    
    elif is_phone_number(message_text):
        clean_phone = bot_manager.clean_phone_number(message_text)
        
        if not clean_phone:
            error_text = """
📱 *Please enter a valid Ethiopian phone number:*

Example: *0912121212* or *+251912121212*
            """
            return bot_manager.send_message(chat_id, error_text, parse_mode='Markdown')
        
        logger.info(f"Creating Channel User and Conversation for {chat_id} with phone: {clean_phone}")
        
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
📱 *Please enter a valid Ethiopian phone number:*

Example: *0912121212* or *+251912121212*

Or type */start* to see the welcome message.
        """
        return bot_manager.send_message(chat_id, error_text, parse_mode='Markdown')

# ============================================
# SECURE FLASK ROUTES
# ============================================

@app.route('/api/send-to-user', methods=['POST'])
def send_to_user():
    """Secure endpoint for Salesforce to send messages to Telegram"""
    try:
        if not request.is_json:
            logger.warning("Non-JSON request to /api/send-to-user")
            return jsonify({'error': 'Content-Type must be application/json'}), 400
        
        data = request.get_json()
        
        # Validate required fields
        if not data or 'chat_id' not in data or 'message' not in data:
            return jsonify({'error': 'Missing chat_id or message'}), 400
        
        chat_id = data['chat_id']
        message = data['message']
        
        # Validate chat_id
        if not isinstance(chat_id, (int, str)):
            return jsonify({'error': 'Invalid chat_id format'}), 400
        
        # Sanitize inputs
        safe_chat_id = str(chat_id)
        safe_message = sanitize_input(message)
        
        # Check if this message changes session status
        session_status = data.get('session_status')
        if session_status in ['Active', 'Waiting', 'Closed']:
            user_state = user_session_state.get(safe_chat_id, {})
            if user_state:
                user_state['session_status'] = session_status
                user_session_state[safe_chat_id] = user_state
        
        # Validate parse_mode
        parse_mode = data.get('parse_mode', 'HTML')
        if parse_mode not in ['HTML', 'Markdown', 'MarkdownV2']:
            parse_mode = 'HTML'
        
        success = bot_manager.send_message(chat_id, safe_message, parse_mode=parse_mode)
        
        if success:
            return jsonify({
                'status': 'success', 
                'message': 'Message sent to Telegram',
                'chat_id': chat_id
            })
        else:
            return jsonify({'error': 'Failed to send message to Telegram'}), 500
            
    except Exception as e:
        logger.error(f"Send error: {str(e)[:100]}")
        return jsonify({'error': 'Internal server error'}), 500
    
#//HELPER FUNCTIONS FOR BULK PROMOTIONS
def is_valid_url(url):
    """Validate URL format - updated version"""
    if not url:
        return False
    
    try:
        result = urlparse(url)
        # Check for basic URL structure
        return bool(result.scheme and result.netloc)
    except:
        return False

def send_promotion_photo(chat_id, photo_url, caption=None, buttons=None):
    """Send photo promotion with caption and buttons"""
    try:
        if not BOT_TOKEN:
            logger.error("BOT_TOKEN not configured")
            return False
        
        url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendPhoto"
        data = {
            'chat_id': chat_id,
            'photo': photo_url,
            'parse_mode': 'HTML'
        }
        
        if caption:
            safe_caption = sanitize_input(caption, max_length=1024)  # Telegram photo caption limit
            data['caption'] = safe_caption
        
        if buttons:
            keyboard = build_inline_keyboard(buttons)
            if keyboard:
                data['reply_markup'] = json.dumps({'inline_keyboard': keyboard})
        
        response = requests.post(url, data=data, timeout=30)
        result = response.json()
        
        if result.get('ok'):
            logger.debug(f"Photo promotion sent to {chat_id}")
            return True
        else:
            logger.error(f"Failed to send photo to {chat_id}: {result.get('description')}")
            return False
            
    except Exception as e:
        logger.error(f"Error sending photo promotion: {e}")
        return False

def send_promotion_text(chat_id, text, buttons=None):
    """Send text promotion with optional buttons"""
    try:
        if not BOT_TOKEN:
            logger.error("BOT_TOKEN not configured")
            return False
        
        url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
        data = {
            'chat_id': chat_id,
            'text': text,
            'parse_mode': 'HTML',
            'disable_web_page_preview': False  # Allow link previews for promotions
        }
        
        if buttons:
            keyboard = build_inline_keyboard(buttons)
            if keyboard:
                data['reply_markup'] = json.dumps({'inline_keyboard': keyboard})
        
        response = requests.post(url, data=data, timeout=30)
        result = response.json()
        
        if result.get('ok'):
            logger.debug(f"Text promotion sent to {chat_id}")
            return True
        else:
            logger.error(f"Failed to send text to {chat_id}: {result.get('description')}")
            return False
            
    except Exception as e:
        logger.error(f"Error sending text promotion: {e}")
        return False

def build_inline_keyboard(buttons):
    """Build inline keyboard from button configuration"""
    if not buttons or not isinstance(buttons, list):
        return []
    
    keyboard = []
    for button_row in buttons:
        if not isinstance(button_row, list):
            continue
        
        row = []
        for button in button_row:
            if isinstance(button, dict):
                # Format 1: {"text": "Button", "url": "https://..."}
                if 'text' in button and 'url' in button:
                    row.append({
                        'text': str(button['text']),
                        'url': str(button['url'])
                    })
                # Format 2: {"text": "Button", "callback_data": "data"}
                elif 'text' in button and 'callback_data' in button:
                    row.append({
                        'text': str(button['text']),
                        'callback_data': str(button['callback_data'])
                    })
            elif isinstance(button, list) and len(button) >= 2:
                # Format 3: ["Button Text", "https://url.com"]
                row.append({
                    'text': str(button[0]),
                    'url': str(button[1])
                })
        
        if row:
            keyboard.append(row)
    
    return keyboard
#////////////////////////////////////////////////////////
# VALIDATE AND SEND BULK PROMOTIONS TO MULTIPLE USERS
def validate_attachment_url(url):
    """Production-ready URL validation"""
    if not url:
        return False, "No URL provided"
    
    # Basic URL structure
    if not url.startswith(('http://', 'https://')):
        return False, "URL must start with http:// or https://"
    
    try:
        parsed = urlparse(url)
        
        # Must have scheme and netloc
        if not parsed.scheme or not parsed.netloc:
            return False, "Invalid URL format"
        
        # Block dangerous URLs
        blocked_patterns = [
            'localhost', '127.0.0.1', '169.254.', '10.', '192.168.',
            '::1', '0.0.0.0', '[::]', 'internal.', 'private.',
            'metadata.google.internal', '169.254.169.254'
        ]
        
        for pattern in blocked_patterns:
            if pattern in parsed.netloc:
                return False, f"Blocked URL pattern: {pattern}"
        
        # Domain whitelist (if configured)
        if ALLOWED_ATTACHMENT_DOMAINS:
            allowed = False
            for allowed_domain in ALLOWED_ATTACHMENT_DOMAINS:
                if allowed_domain and parsed.netloc.endswith(allowed_domain.strip()):
                    allowed = True
                    break
            
            if not allowed:
                # For Marketing Cloud, we need to handle subdomains properly
                # Check if any allowed domain is a suffix of the hostname
                hostname = parsed.netloc
                for allowed_domain in ALLOWED_ATTACHMENT_DOMAINS:
                    if allowed_domain and hostname.endswith('.' + allowed_domain.strip()):
                        allowed = True
                        break
                
                if not allowed:
                    return False, f"Domain not allowed: {parsed.netloc}"
        
        # File type validation
        valid_extensions = ['.jpg', '.jpeg', '.png', '.gif', '.webp']
        if not any(parsed.path.lower().endswith(ext) for ext in valid_extensions):
            return False, "Only image files allowed (jpg, png, gif, webp)"
        
        # Size check (by content-length header, not by URL)
        # We'll check this separately when downloading
        
        return True, "Valid"
        
    except Exception as e:
        logger.error(f"URL validation error: {e}")
        return False, "URL validation failed"
    
def check_attachment_size(url):
    """Check attachment size before sending"""
    try:
        head_response = requests.head(url, timeout=5, allow_redirects=True)
        content_length = head_response.headers.get('content-length')
        
        if content_length:
            size_mb = int(content_length) / (1024 * 1024)
            if size_mb > MAX_ATTACHMENT_SIZE_MB:
                return False, f"File too large: {size_mb:.1f}MB (max: {MAX_ATTACHMENT_SIZE_MB}MB)"
        
        return True, "Size OK"
    except:
        # If HEAD fails, we'll try anyway but log it
        logger.warning(f"Could not check size for: {url}")
        return True, "Size check skipped"

#///SEND TO MASS USERS AT ONCE ENDPOINT
@app.route('/api/send-bulk-promotion', methods=['POST'])
def send_bulk_promotion():
    """Dedicated endpoint for sending bulk promotions with attachments"""
    try:
        if not request.is_json:
            logger.warning("Non-JSON request to /api/send-bulk-promotion")
            return jsonify({'error': 'Content-Type must be application/json'}), 400
        
        data = request.get_json()
        
        # Validate required fields
        required_fields = ['chat_ids', 'message']
        for field in required_fields:
            if field not in data:
                return jsonify({'error': f'Missing required field: {field}'}), 400
        
        chat_ids = data['chat_ids']
        message = data['message']
        campaign_id = data.get('campaign_id', '')
        message_type = data.get('message_type', 'promotion')
        attachment_url = data.get('attachment_url')  # Optional image URL
        buttons = data.get('buttons')  # Optional buttons
        
        # Validate inputs
        if not isinstance(chat_ids, list):
            return jsonify({'error': 'chat_ids must be a list'}), 400
        
        if len(chat_ids) == 0:
            return jsonify({'error': 'No chat_ids provided'}), 400
        
        # Security limits
        MAX_BULK_RECIPIENTS = int(os.getenv('MAX_BULK_RECIPIENTS', '500'))
        if len(chat_ids) > MAX_BULK_RECIPIENTS:
            chat_ids = chat_ids[:MAX_BULK_RECIPIENTS]
            logger.warning(f"Bulk promotion truncated to {MAX_BULK_RECIPIENTS} recipients")
        
        # Validate attachment URL if provided
        if attachment_url:
            is_valid, error_msg = validate_attachment_url(attachment_url)
            if not is_valid:
                logger.warning(f"Invalid attachment URL: {error_msg}")
                # Return error instead of silently removing
                return jsonify({
                    'status': 'error',
                    'error': f'Invalid attachment URL: {error_msg}'
                }), 400
            
            # Check size
            size_ok, size_msg = check_attachment_size(attachment_url)
            if not size_ok:
                return jsonify({
                    'status': 'error',
                    'error': size_msg
                }), 400
        
        # Sanitize message
        safe_message = sanitize_input(message, max_length=1024)  # Shorter limit for promotions
        
        # Track results
        results = {
            'total': len(chat_ids),
            'successful': 0,
            'failed': 0,
            'failed_details': [],
            'campaign_id': campaign_id,
            'message_type': message_type,
            'has_attachment': bool(attachment_url),
            'has_buttons': bool(buttons)
        }
        
        logger.info(f"Starting bulk promotion to {len(chat_ids)} users, campaign: {campaign_id}")
        
        # Send messages with smart rate limiting
        for i, chat_id in enumerate(chat_ids):
            try:
                # Smart rate limiting for bulk sends
                if i > 0:
                    # Telegram has limits: 30 messages/second to different chats
                    if i % 25 == 0:  # Conservative: 25 messages then pause
                        time.sleep(1)
                    elif i % 5 == 0:  # Small pause every 5 messages
                        time.sleep(0.1)
                
                # Validate chat_id
                chat_id_str = str(chat_id)
                if not chat_id_str.isdigit():
                    logger.warning(f"Invalid chat ID format: {chat_id}")
                    results['failed'] += 1
                    results['failed_details'].append({
                        'chat_id': chat_id,
                        'error': 'Invalid chat ID format'
                    })
                    continue
                
                # Send promotion (with or without attachment)
                if attachment_url:
                    # Send photo with caption and buttons
                    success = send_promotion_photo(
                        chat_id=chat_id_str,
                        photo_url=attachment_url,
                        caption=safe_message,
                        buttons=buttons
                    )
                else:
                    # Send text message with optional buttons
                    success = send_promotion_text(
                        chat_id=chat_id_str,
                        text=safe_message,
                        buttons=buttons
                    )
                
                if success:
                    results['successful'] += 1
                else:
                    results['failed'] += 1
                    results['failed_details'].append({
                        'chat_id': chat_id,
                        'error': 'Failed to send promotion'
                    })
                
                # Log progress for large batches
                if len(chat_ids) > 50 and i % 50 == 0:
                    logger.info(f"Bulk promotion progress: {i}/{len(chat_ids)} sent")
                
            except Exception as e:
                logger.error(f"Error sending to {chat_id}: {e}")
                results['failed'] += 1
                results['failed_details'].append({
                    'chat_id': chat_id,
                    'error': str(e)[:100]
                })
        
        logger.info(f"Bulk promotion completed: {results['successful']}/{results['total']} successful")
        
        return jsonify({
            'status': 'success',
            'action': 'bulk_promotion',
            'results': results,
            'summary': {
                'sent': results['successful'],
                'failed': results['failed'],
                'total': results['total'],
                'success_rate': round((results['successful'] / results['total']) * 100, 2) if results['total'] > 0 else 0
            }
        })
            
    except Exception as e:
        logger.error(f"Bulk promotion error: {str(e)[:100]}")
        return jsonify({'error': 'Internal server error'}), 500
#//////////////////////////////////////

@app.route('/webhook', methods=['POST'])
def telegram_webhook():
    """Secure Telegram webhook endpoint"""
    try:
        if not request.is_json:
            logger.warning("Non-JSON webhook received")
            return jsonify({'error': 'Invalid data format'}), 400
        
        update_data = request.get_json()
        
        # Validate Telegram payload
        is_valid, error_msg = validate_telegram_payload(update_data)
        if not is_valid:
            logger.warning(f"Invalid Telegram payload: {error_msg}")
            return jsonify({'error': error_msg}), 400
        
        # Handle callback queries (button presses)
        if 'callback_query' in update_data:
            handle_callback_query(update_data['callback_query'])
            return jsonify({'status': 'ok'})
        
        # Handle regular messages
        if 'message' in update_data:
            message = update_data['message']
            chat_id = message['chat']['id']
            message_text = message.get('text', '')
            user_data = message.get('from', {})
            
            # Log incoming message
            msg_preview = message_text[:50] + '...' if len(message_text) > 50 else message_text
            logger.info(f"Telegram message from {chat_id}: {msg_preview}")
            
            # Process the message
            process_incoming_message(chat_id, message_text, user_data)
        
        return jsonify({'status': 'ok'})
            
    except Exception as e:
        logger.error(f"Webhook error: {str(e)[:100]}")
        return jsonify({'error': 'Internal server error'}), 500

@app.route('/set-webhook', methods=['GET'])
def set_webhook():
    """Set Telegram webhook programmatically"""
    try:
        if not BOT_TOKEN:
            return jsonify({'error': 'BOT_TOKEN not configured'}), 500
            
        webhook_url = f"https://{request.host}/webhook"
        set_url = f"https://api.telegram.org/bot{BOT_TOKEN}/setWebhook?url={webhook_url}"
        
        logger.info(f"Setting webhook to: {webhook_url}")
        
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
        logger.error(f"Set webhook error: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/clear-session-state/<chat_id>', methods=['GET'])
def clear_session_state(chat_id):
    """Clear session state for a user (for testing)"""
    if not chat_id.isdigit():
        return jsonify({'error': 'Invalid chat ID format'}), 400
    
    if chat_id in user_session_state:
        del user_session_state[chat_id]
        return jsonify({'status': 'success', 'message': f'Cleared session state for {chat_id}'})
    
    return jsonify({'status': 'error', 'message': 'No session state found'}), 404

@app.route('/session-state/<chat_id>', methods=['GET'])
def get_session_state(chat_id):
    """Get session state for a user (for debugging)"""
    if not chat_id.isdigit():
        return jsonify({'error': 'Invalid chat ID format'}), 400
    
    state = user_session_state.get(chat_id, {})
    return jsonify({'status': 'success', 'state': state})

# ============================================
# SECURITY MONITORING ENDPOINTS
# ============================================

@app.route('/metrics', methods=['GET'])
def security_metrics():
    """Security and performance metrics endpoint"""
    try:
        current_time = time.time()
        
        # Get rate limiting stats
        rate_stats = {
            'enabled': ENABLE_RATE_LIMITING,
            'limit_per_minute': RATE_LIMIT_PER_MINUTE,
            'active_ips': len(rate_limiter.requests),
            'total_requests': sum(len(timestamps) for timestamps in rate_limiter.requests.values())
        }
        
        # Get session stats
        session_stats = {
            'total_sessions': len(user_session_state),
            'active_sessions': sum(1 for s in user_session_state.values() if s.get('in_session')),
            'waiting_sessions': sum(1 for s in user_session_state.values() if s.get('session_status') == 'Waiting')
        }
        
        # System health
        access_token = bot_manager.sf_auth.get_access_token()
        
        return jsonify({
            'status': 'healthy',
            'timestamp': datetime.now().isoformat(),
            'security': {
                'rate_limiting': rate_stats,
                'input_sanitization': ENABLE_INPUT_SANITIZATION,
                'max_message_length': MAX_MESSAGE_LENGTH
            },
            'sessions': session_stats,
            'services': {
                'telegram': 'connected' if BOT_TOKEN else 'disconnected',
                'salesforce': 'connected' if access_token else 'disconnected'
            },
            'uptime': current_time - app_start_time
        })
    except Exception as e:
        logger.error(f"Metrics error: {str(e)[:100]}")
        return jsonify({'error': 'Metrics unavailable'}), 500

@app.route('/security/logs', methods=['GET'])
def security_logs():
    """Get recent security-related logs (limited for security)"""
    return jsonify({
        'warning': 'Security logs endpoint requires authentication in production',
        'rate_limited_ips': list(rate_limiter.requests.keys())[:10]
    })

# ============================================
# TEST ENDPOINTS
# ============================================

@app.route('/test-registration/<phone>', methods=['GET'])
def test_registration(phone):
    """Test registration endpoint"""
    try:
        # Simulate a user registration
        chat_id = "123456789"
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
        logger.error(f"Test registration error: {e}")
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
        logger.error(f"Test conversation error: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/test', methods=['GET'])
def test():
    return jsonify({
        'status': 'online',
        'service': 'Telegram Bot Integration',
        'version': '5.0-security',
        'architecture': 'Channel User → Support Conversation → Chat Sessions',
        'security_features': {
            'rate_limiting': 'enabled' if ENABLE_RATE_LIMITING else 'disabled',
            'input_sanitization': 'enabled' if ENABLE_INPUT_SANITIZATION else 'disabled',
            'sql_injection_protection': 'enabled'
        },
        'endpoints': {
            'webhook': 'POST /webhook',
            'send_to_user': 'POST /api/send-to-user',
            'set_webhook': 'GET /set-webhook',
            'clear_session_state': 'GET /clear-session-state/<chat_id>',
            'session_state': 'GET /session-state/<chat_id>',
            'metrics': 'GET /metrics',
            'health': 'GET /health'
        }
    })

# ============================================
# ENHANCED HEALTH CHECK
# ============================================

@app.route('/health', methods=['GET'])
def health_check():
    """Enhanced health check with security status"""
    try:
        access_token = bot_manager.sf_auth.get_access_token()
        
        health_status = {
            'status': 'healthy' if BOT_TOKEN and access_token else 'degraded',
            'service': 'telegram-salesforce-bot',
            'version': '5.0-security',
            'timestamp': datetime.now().isoformat(),
            'security': {
                'rate_limiting': 'enabled' if ENABLE_RATE_LIMITING else 'disabled',
                'input_sanitization': 'enabled' if ENABLE_INPUT_SANITIZATION else 'disabled',
                'request_timeout': REQUEST_TIMEOUT
            },
            'telegram_bot': 'configured' if BOT_TOKEN else 'missing',
            'salesforce_connection': 'connected' if access_token else 'disconnected',
            'session_state_count': len(user_session_state),
            'rate_limiting_active_ips': len(rate_limiter.requests)
        }
        
        status_code = 200 if health_status['status'] == 'healthy' else 503
        return jsonify(health_status), status_code
        
    except Exception as e:
        return jsonify({
            'status': 'unhealthy',
            'error': str(e)[:100]
        }), 500

@app.route('/')
def home():
    return jsonify({
        'message': 'Telegram Bot for Salesforce Integration',
        'architecture': 'Channel User → Support Conversation → Chat Sessions',
        'version': '5.0-security',
        'security': 'Enhanced with rate limiting and input sanitization',
        'status': 'Running'
    })

# ============================================
# MAIN EXECUTION
# ============================================

if __name__ == '__main__':
    app_start_time = time.time()
    
    logger.info("=" * 70)
    logger.info("🚀 Starting Telegram Bot v5.0 (Security Enhanced)")
    logger.info("=" * 70)
    
    if missing_vars:
        logger.error(f"Missing environment variables: {', '.join(missing_vars)}")
    else:
        logger.info("✅ All environment variables are set")
    
    logger.info(f"📱 Channel Type: Telegram")
    logger.info(f"🔒 Security Features:")
    logger.info(f"   • Rate Limiting: {'ENABLED' if ENABLE_RATE_LIMITING else 'DISABLED'}")
    logger.info(f"   • Input Sanitization: {'ENABLED' if ENABLE_INPUT_SANITIZATION else 'DISABLED'}")
    logger.info(f"   • Max Message Length: {MAX_MESSAGE_LENGTH}")
    logger.info(f"   • Request Timeout: {REQUEST_TIMEOUT}s")
    logger.info(f"🌐 Starting server on port {PORT}")
    
    app.run(host='0.0.0.0', port=PORT, debug=False)