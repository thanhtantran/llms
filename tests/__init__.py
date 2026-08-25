# Tests for llms-py package
import os
from os.path import abspath, dirname, join

# Calculate project root path
project_root = dirname(dirname(abspath(__file__)))

# Load environment variables from .env file if python-dotenv is available
dotenv_path = join(project_root, ".env")
if os.path.exists(dotenv_path):
    try:
        from dotenv import load_dotenv

        load_dotenv(dotenv_path)
    except ImportError:
        pass
