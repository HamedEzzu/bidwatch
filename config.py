"""Central configuration for BidWatch.

Every knob that a user might want to change lives here so the model,
region, job source and cost limits can each be swapped in one line.
"""

import os

# --- Model -----------------------------------------------------------------
# Bedrock model used for scoring and drafting. Swap this single line to
# change models (Anthropic Claude models are not available on this account,
# see README "Design decisions").
MODEL_ID = os.getenv("BIDWATCH_MODEL_ID", "zai.glm-5")
REGION = os.getenv("AWS_REGION", "eu-north-1")

# --- Job source ------------------------------------------------------------
JOB_SOURCE_URL = os.getenv("JOB_SOURCE_URL", "https://remoteok.com/api")
TAG = os.getenv("BIDWATCH_TAG", "backend")
USER_AGENT = "BidWatch/1.0 (freelance job scout; +https://github.com/HamedEzzu/bidwatch)"

# --- Behaviour -------------------------------------------------------------
SCORE_THRESHOLD = int(os.getenv("SCORE_THRESHOLD", "65"))
MAX_POSTINGS_PER_RUN = int(os.getenv("MAX_POSTINGS_PER_RUN", "15"))
RUN_INTERVAL_MINUTES = int(os.getenv("RUN_INTERVAL_MINUTES", "30"))

# --- Applying -------------------------------------------------------------
# No application is ever submitted without an explicit confirmation; this is a
# ceiling on how many confirmed submissions may go out in a rolling hour.
MAX_SUBMISSIONS_PER_HOUR = int(os.getenv("MAX_SUBMISSIONS_PER_HOUR", "5"))
COVER_LETTER_MAX_WORDS = int(os.getenv("COVER_LETTER_MAX_WORDS", "220"))

# Assisted form filling opens a REAL, VISIBLE browser window, so it needs a
# desktop session. Set HEADED_BROWSER=false only for automated testing — a
# headless fill is pointless, since the whole feature hands you the window.
HEADED_BROWSER = os.getenv("HEADED_BROWSER", "true").strip().lower() in ("1", "true", "yes", "on")

# --- Paths -----------------------------------------------------------------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PROFILE_PATH = os.path.join(BASE_DIR, "profile.md")
APPLICANT_PATH = os.path.join(BASE_DIR, "applicant.md")
DB_PATH = os.path.join(BASE_DIR, "bidwatch.db")
FIXTURE_PATH = os.path.join(BASE_DIR, "fixtures", "sample_postings.json")

# Descriptions are truncated before reaching the model to control token cost.
MAX_DESCRIPTION_CHARS = 2000
