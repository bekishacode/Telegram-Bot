import os

# Server socket
bind = "0.0.0.0:{}".format(int(os.environ.get("PORT", 5000)))

# Worker processes
workers = 4
worker_class = "sync"
worker_connections = 1000
timeout = 120
keepalive = 2

# Logging
accesslog = "-"
errorlog = "-"
loglevel = "info"

# Process naming
proc_name = "telegram-salesforce-bot"