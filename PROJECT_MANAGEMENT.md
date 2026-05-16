## My Daemon

Daemon — a reference from Philip Pullman's His Dark Materials, where every person has an external animal-form soul-companion that knows them completely. Also nods to Socrates' inner advisory voice. Short, memorable, ownable. The fact that "daemon" already means a background process in computing is either thematically perfect (it runs quietly in the background, being you) or distracting, depending on your audience.

My Daemon is a personal knowledge and memory system that is designed to enhance human memory, recall, personal introspection, and reflection during a human's life. My Daemon would grow along with a person as they journal, save conversations with AI technology, perform research, take notes for learning and professional development, work through hobbies and projects, and document life events or vacations. Really, My Daemon gets to know the person and becomes a digital fingerprint of the person over time and with greater interaction and use. 

## Core Principles

My Daemon is designed to process markdown files primarily and will initially be designed to gather infromation from an Obsidian vault. 

The Obsidian application is the human's interface to their knowledge and stored information. The My Daemon application creates a separate data layer that pulls from and interacts with the markdown files inside the Obsidian vault. 

My Daemon should return a minimum of 3 results in RAG and let the user choose the one that they believe is most relevant to their request. The results should be sorted by weight, with the highest weight appearing first in the list. The user's choice should be stored as added weight to the connection between the query and the result. 

The user should be able to ask the AI for another set of 3 results, assuming they are not satisfied with presented items.

All user chat history will be stored and connected with RAG reseults that are confirmed by the user to be correct. 

The app needs to have a nice, flexible, gui that can run on a desktop application on Windows or Ubuntu or in a web browser.


## Project Scaffold

For the alpha version of the application, please work from the MY-DAEMON-SCAFFOLD.md file. Please incorporate any other features noted in this PROJECT_MANAGEMENT.md file if found missing in the scaffold.

[MY-DAEMON-SCAFFOLD.md](MY-DAEMON-SCAFFOLD.md)

## To-Do

- [ ] Add a light setup ui for setting the settings and an os environment variable for the anthropic_api_key. The setup ui can have a button to compose the docker container
- 

## AI Suggestions (Agent Thoughts)

### 2026-05-16 — First draft completed

Phase 0 + most of Phase 1 of the scaffold is in. End-to-end pipeline compiles and unit tests pass without external services. Below are thoughts I had during the build that I deliberately *did not* implement, because they belong in later phases or want your input first.

**Candidate-selection feedback (cheap to add, big payoff later).**
The retrieval orchestrator already trims to a `candidate_pool` floor of 3, matching the PROJECT_MANAGEMENT.md requirement. The natural next step is a `daemon select <feedback_id> <candidate_index>` command that writes a `candidate_selected` signal back to the feedback DB. That row becomes the training signal for adaptive edge weighting in Phase 4. Not built yet because we should agree on the *shape* of the signal first (per-chunk? per-note? does selecting candidate #2 down-weight #1?).

**"More like these" follow-up.**
PROJECT_MANAGEMENT.md says the user should be able to ask for *another* set of 3 results. I sketched this in my head as `daemon query "..." --skip <feedback_id>` — re-runs the same query but filters out the previously-shown chunk_ids. It is one tiny CLI flag away. Held back because the GUI is the eventual home for this interaction and the CLI version may not be worth investing in.

**Dangling wikilinks as a feature, not a bug.**
The graph store currently keeps dangling wikilink targets as placeholder nodes (filtered out of stats). These are actually interesting — they represent "notes the user means to write." A future `daemon graph todos` command could surface them. Stashing the idea here.

**The GUI requirement is unaddressed.**
You asked for a flexible GUI that runs on desktop (Windows/Ubuntu) or in a browser. v0.1 is CLI-only by scaffold design. When you're ready, the obvious paths are (a) FastAPI + a small React/Svelte front-end (web; runs anywhere; serves over localhost), or (b) Tauri or PyWebView for a true desktop bundle. The retrieval/query engine is already pure-Python and GUI-agnostic, so either wrapper is mostly plumbing. Worth a separate planning conversation.

**The philosophical framing should live somewhere the daemon can read.**
You and I have talked about why this project matters. Right now that context lives in CLAUDE.md (which only I see) and PROJECT_MANAGEMENT.md (which is gitignored from RAG, ironically). If My Daemon ingests its own vault, you might want to seed it with a short "purpose" note so it can answer questions like "why am I building this" from your own voice rather than mine. Just an idea.

