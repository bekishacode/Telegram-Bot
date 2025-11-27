# gunicorn.conf.py
import multiprocessing

# Bind to the correct port
bind = "0.0.0.0:{}".format(int(os.environ.get("PORT", 5000)))

# Worker configuration
workers = 1  # Use only 1 worker to avoid event loop issues
worker_class = "sync"  # Use sync workers instead of async
worker_connections = 1000
timeout = 120
keepalive = 2

# Logging
accesslog = "-"
errorlog = "-"