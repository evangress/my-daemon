# SPDX-License-Identifier: Apache-2.0
"""Vault ingestion layer — read Obsidian markdown into structured Notes and Chunks."""

from my_daemon.vault.chunker import chunk_note
from my_daemon.vault.parser import parse_note
from my_daemon.vault.reader import VaultReader

__all__ = ["VaultReader", "parse_note", "chunk_note"]
