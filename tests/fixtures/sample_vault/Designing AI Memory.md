---
tags: [ai, memory, design]
created: 2026-03-12
---

# Designing AI Memory

Notes on how a personal AI memory system should behave. Inspired by [[Pullman Daemons]] and [[Socratic Daemon]].

## RAG vs Fine-tuning

RAG keeps the user's data outside the model's weights, which is essential for privacy and editability. Fine-tuning bakes facts in and they're hard to remove. For a personal system we want **edit-ability** above all.

## Why Graphs Matter

Pure vector search misses *connection*. A graph layer over [[Obsidian Vaults]] lets us walk from a topic to its neighbors instead of relying purely on semantic proximity.

#rag #graph
