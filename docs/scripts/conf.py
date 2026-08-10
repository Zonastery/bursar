import os
import sys

from docutils import nodes
from sphinx_markdown_builder.translator import MarkdownTranslator

from sphinx import addnodes

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "python", "src"))

extensions = [
    "sphinx.ext.autodoc",
    "sphinx.ext.autosummary",
    "sphinx.ext.napoleon",
    "sphinx_markdown_builder",
]
master_doc = "index"
exclude_patterns = ["_build"]
autodoc_member_order = "bysource"
autodoc_typehints = "description"
autosummary_generate = True
autosummary_ignore_module_all = False
autosummary_imported_members = True
napoleon_google_docstring = True
napoleon_numpy_docstring = False
templates_path = ["_templates"]

# Bursar imports these packages only when their optional adapters are used.
# Mock them so the public API can be rendered from a base SDK installation.
autodoc_mock_imports = ["psycopg2", "httpx", "opentelemetry"]


class DocusaurusMarkdownTranslator(MarkdownTranslator):
    """Emit Markdown constructs that Docusaurus can validate and route."""

    def visit_autosummary_table(self, node):
        """Hide Autosummary's generation table; the module page links symbols."""
        raise nodes.SkipNode

    def visit_autosummary_toc(self, node):
        """Keep Sphinx's hidden toctree metadata out of Markdown output."""
        raise nodes.SkipNode

    def visit_abbreviation(self, node):
        """Render Sphinx's keyword-only ``*`` marker in signatures."""
        self.add(node.astext())
        raise nodes.SkipNode

    def visit_reference(self, node):
        """Keep type names in signatures out of Docusaurus heading anchors."""
        parent = node.parent
        while parent is not None:
            if isinstance(parent, addnodes.desc_signature):
                self.add(node.astext())
                raise nodes.SkipNode
            parent = parent.parent
        super().visit_reference(node)

    def depart_reference(self, node):
        """Close the Markdown link context opened by ``visit_reference``."""
        self._pop_context()

    def depart_desc_signature(self, node):
        """Attach each Sphinx object ID to its generated Markdown heading."""
        for anchor in node.get("ids", []):
            self.add(f" {{#{anchor}}}")
        self._pop_context()


def setup(app):
    app.set_translator("markdown", DocusaurusMarkdownTranslator, override=True)
