import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "python", "src"))

extensions = ["sphinx.ext.autodoc", "sphinx_markdown_builder"]
master_doc = "index"
exclude_patterns = ["_build"]

# bursar has optional deps (psycopg2 for postgres, httpx for supabase)
# Mock them so autodoc can still document the rest of the API
autodoc_mock_imports = ["psycopg2", "httpx"]
