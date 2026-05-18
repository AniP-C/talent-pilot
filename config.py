import logging
import os

# 1. STATUS SSOT (Single Source of Truth)
VALID_STATUSES = [
    "APPLIED",
    "ASSESSMENT",
    "INTERVIEW",
    "OFFER",
    "REJECTED"
]

# 2. LOGGING SETUP
# Create logs directory if it doesn't exist
os.makedirs("logs", exist_ok=True)

logging.basicConfig(
    filename="logs/app.log",
    level=logging.INFO, # We only want INFO and ERROR logs
    format="%(asctime)s - %(levelname)s - %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)

# Create a logger object to import elsewhere
logger = logging.getLogger("JobTracker")