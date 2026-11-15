"""Render engagement resource links without changing their stored Markdown or HTML."""

import markdown
import nh3
from markupsafe import Markup

RESOURCE_TAGS = {
    "a", "p", "br", "strong", "b", "em", "i", "ul", "ol", "li", "blockquote",
    "pre", "code", "h1", "h2", "h3", "h4", "h5", "h6", "del", "s",
}


def render_solution_resources(value: str | None) -> Markup:
    """Render Markdown and allowlisted HTML, stripping executable content and unsafe URLs."""
    html = markdown.markdown(value or "", extensions=["sane_lists", "nl2br"])
    return Markup(nh3.clean(
        html,
        tags=RESOURCE_TAGS,
        clean_content_tags={"script", "style", "iframe", "object", "embed", "svg", "math"},
        attributes={"a": {"href", "title"}},
        url_schemes={"http", "https", "mailto"},
        link_rel="noopener noreferrer",
        set_tag_attribute_values={"a": {"target": "_blank"}},
    ))
