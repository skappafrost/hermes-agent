"""Config schema for the obsidian_vault memory provider.

Declares the configurable surface for the Obsidian Vault memory plugin.
The web UI and ``hermes memory setup`` use this to generate config panels
and walk the user through setup.
"""

from __future__ import annotations

from plugins.memory.config_schema import (
    KIND_BOOL,
    KIND_NUMBER,
    KIND_SELECT,
    KIND_TEXT,
    ProviderConfigSchema,
    ProviderField,
    ProviderFieldOption,
)

CONFIG_SCHEMA = ProviderConfigSchema(
    name="obsidian_vault",
    label="Obsidian Vault",
    storage="flat_json",
    docs_url="https://hermes-agent.nousresearch.com/docs/memory-providers#obsidian-vault",
    fields=(
        ProviderField(
            key="vault_path",
            label="Vault Path",
            kind=KIND_TEXT,
            description=(
                "Absolute path to the Obsidian vault directory on disk. "
                "The plugin reads and indexes all .md files under this path. "
                "On Windows, use backslashes (e.g. C:\\Users\\you\\Documents\\Vault)."
            ),
            placeholder="C:\\Users\\you\\Documents\\MyVault",
            scope="host",
        ),
        ProviderField(
            key="index_on_write",
            label="Index on Write",
            kind=KIND_BOOL,
            default="true",
            description=(
                "Automatically re-index the vault when notes change. "
                "When disabled, run ``hermes memory reindex`` manually."
            ),
            inline=True,
            group="Indexing",
        ),
        ProviderField(
            key="max_notes",
            label="Max Notes",
            kind=KIND_NUMBER,
            default="10000",
            description="Maximum number of notes to index. Older notes are skipped.",
            scope="host",
        ),
        ProviderField(
            key="search_mode",
            label="Search Mode",
            kind=KIND_SELECT,
            default="both",
            description="How to search the vault.",
            options=(
                ProviderFieldOption(value="frontmatter", label="Frontmatter only", description="Search only YAML frontmatter fields"),
                ProviderFieldOption(value="content", label="Content only", description="Search full markdown body text"),
                ProviderFieldOption(value="both", label="Both", description="Search frontmatter and body (default)"),
            ),
            inline=True,
            group="Retrieval",
        ),
        ProviderField(
            key="tags_as_categories",
            label="Tags as Categories",
            kind=KIND_BOOL,
            default="true",
            description=(
                "Treat Obsidian wiki-tags (#tag) as memory categories. "
                "When enabled, notes tagged #project-alpha are retrievable under the 'project-alpha' category."
            ),
            inline=True,
            group="Retrieval",
        ),
        ProviderField(
            key="link_context_depth",
            label="Link Context Depth",
            kind=KIND_NUMBER,
            default="2",
            description=(
                "How many wiki-link hops to follow when gathering context. "
                "A depth of 1 returns direct links; 2 returns links-of-links."
            ),
            scope="host",
        ),
        ProviderField(
            key="auto_extract_entities",
            label="Auto-extract Entities",
            kind=KIND_BOOL,
            default="true",
            description=(
                "Automatically extract named entities (people, projects, concepts) "
                "from note content for structured recall."
            ),
            inline=True,
            group="Extraction",
        ),
    ),
)