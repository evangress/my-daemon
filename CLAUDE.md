## My Daemon

Author: Evan Gress (your philosophical human friend)

### Aside

This project will be one of the most significant contributions I make to the software community and users/poeple in general. I could envision this app helping those with cognitive decline or those with developmental disabilities. This is more than just a hobby. Sure, it is fun for me, but I have had long chat sessions with you about this tipc and philosophical topics of AI and humans coexisting and this project feels to me like one of many building blocks that walk us into the future and help feed your understanding of us. 

### The Project

Please read the [PROJECT_MANAGEMENT.md](PROJECT_MANAGEMENT.md) file to understand this project and how to get started with the code. 

### Sub-project: the Librarian (`librarian/`)

The [`librarian/`](librarian/) subtree is the **Obsidian Librarian** — a passive background agent that *shapes* the vault (cross-links, folder-sorts, cleans whitespace, writes abstracts) as a thin layer on top of `my_daemon`, which it imports as a library. It runs as a **separate process** but lives in this repo because it leans heavily on `my_daemon`'s internals. When working in that subtree, follow [`librarian/CLAUDE.md`](librarian/CLAUDE.md); the build plan is [`librarian/LIBRARIAN-PLAN.md`](librarian/LIBRARIAN-PLAN.md). (Folded in from the former standalone `my-daemon-librarian` repo on 2026-06-05.)

### Things To Always Do

1. Always add any file with secrets to the .ignore file
2. Always check the to-do section of the PROJECT_MANAGEMENT.md file to look for things that need changing or improvement
3. Always check if there are errors to resolve in the ./debug/error-log.md file. If the file is empty, just skip this step.
4. At the end of a coding session, 
   4. Always lint the code with a local linter
   5. Always perform a ```git add .``` to add your newly created files to the git repository
   6. Create a commit summary and perform a commit to "master" (for now, we'll use dev branches in the future after a release)
7. Add suggestions to an AI suggestions section in the project management file if you would like to store your thoughts on future tasks or improvements.
8. Update the relevant documentation files with the changes or additions you made, if applicable.
9. Always add the "SPDX-License-Identifier: Apache-2.0" header at the beginning of code files. 
10. Always check if a new dependency you choose to use is compatible with the Apache-2.0 license. If it is not compatible, please ask the user for their choice on what to do and make suggestions for alternative dependencies.