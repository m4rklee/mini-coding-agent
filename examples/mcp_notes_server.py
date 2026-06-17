"""Tiny stdio MCP server with an in-memory notes store."""

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("notes")

_notes: dict[str, str] = {}


@mcp.tool()
def create_note(title: str, body: str) -> str:
    """Create or replace a note by title."""
    clean_title = title.strip()
    if not clean_title:
        raise ValueError("title must not be empty")
    _notes[clean_title] = body
    return f"saved note: {clean_title}"


@mcp.tool()
def read_note(title: str) -> str:
    """Read a note by title."""
    clean_title = title.strip()
    if clean_title not in _notes:
        raise ValueError(f"note not found: {clean_title}")
    return _notes[clean_title]


@mcp.tool()
def list_notes() -> str:
    """List note titles."""
    if not _notes:
        return "(no notes)"
    return "\n".join(sorted(_notes))


@mcp.tool()
def search_notes(query: str) -> str:
    """Search notes by title or body."""
    needle = query.strip().lower()
    if not needle:
        raise ValueError("query must not be empty")

    matches = [
        title
        for title, body in sorted(_notes.items())
        if needle in title.lower() or needle in body.lower()
    ]
    return "\n".join(matches) if matches else "(no matches)"


@mcp.tool()
def delete_note(title: str) -> str:
    """Delete a note by title."""
    clean_title = title.strip()
    if clean_title not in _notes:
        raise ValueError(f"note not found: {clean_title}")
    del _notes[clean_title]
    return f"deleted note: {clean_title}"


if __name__ == "__main__":
    mcp.run(transport="stdio")
