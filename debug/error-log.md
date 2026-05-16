## 2026-05-16 12:49 PM
Warning: You are sending unauthenticated requests to the HF Hub. Please set a HF_TOKEN to enable higher rate limits and faster downloads.
Loading weights: 100%|█████████████████████████████████████████████████████████████| 199/199 [00:00<00:00, 6595.81it/s]
C:\Users\gress\AppData\Local\my-daemon\src\my_daemon\embeddings\embedder.py:28: FutureWarning: The `get_sentence_embedding_dimension` method has been renamed to `get_embedding_dimension`.
  self._dim = int(self._model.get_sentence_embedding_dimension())
╭───────────────────────────────────────── Traceback (most recent call last) ──────────────────────────────────────────╮
│ C:\Users\gress\AppData\Local\my-daemon\src\my_daemon\cli.py:142 in query                                             │
│                                                                                                                      │
│   139 │   llm = LLMClient(s.llm, api_key=s.anthropic_api_key)                                                        │
│   140 │                                                                                                              │
│   141 │   engine = QueryEngine(s, embedder, vector_store, graph_store, feedback_store, llm)                          │
│ ❱ 142 │   response = engine.ask(text, synthesize=not no_synthesize)                                                  │
│   143 │                                                                                                              │
│   144 │   if response.answer:                                                                                        │
│   145 │   │   console.print(Panel(response.answer, title="Daemon", border_style="cyan"))                             │
│                                                                                                                      │
│ C:\Users\gress\AppData\Local\my-daemon\src\my_daemon\pipeline\query.py:42 in ask                                     │
│                                                                                                                      │
│   39 │                                                                                                               │
│   40 │   def ask(self, query: str, synthesize: bool = True) -> QueryResponse:                                        │
│   41 │   │   t0 = time.perf_counter()                                                                                │
│ ❱ 42 │   │   result = self.orchestrator.retrieve(query)                                                              │
│   43 │   │   answer = self.llm.synthesize(query, result.ranked) if synthesize else ""                                │
│   44 │   │   latency_ms = int((time.perf_counter() - t0) * 1000)                                                     │
│   45                                                                                                                 │
│                                                                                                                      │
│ C:\Users\gress\AppData\Local\my-daemon\src\my_daemon\retrieval\orchestrator.py:31 in retrieve                        │
│                                                                                                                      │
│   28 │   │   self.graph_store = graph_store                                                                          │
│   29 │                                                                                                               │
│   30 │   def retrieve(self, query: str) -> RetrievalResult:                                                          │
│ ❱ 31 │   │   seeds = seed_search(                                                                                    │
│   32 │   │   │   query,                                                                                              │
│   33 │   │   │   self.embedder,                                                                                      │
│   34 │   │   │   self.vector_store,                                                                                  │
│                                                                                                                      │
│ C:\Users\gress\AppData\Local\my-daemon\src\my_daemon\retrieval\seed.py:19 in seed_search                             │
│                                                                                                                      │
│   16 │   """Embed the query and return the top-K Qdrant hits as RetrievedChunks."""                                  │
│   17 │                                                                                                               │
│   18 │   vec = embedder.encode_one(query)                                                                            │
│ ❱ 19 │   hits = vector_store.search(vec, top_k=top_k)                                                                │
│   20 │   seeds: list[RetrievedChunk] = []                                                                            │
│   21 │   for h in hits:                                                                                              │
│   22 │   │   chunk = Chunk(                                                                                          │
│                                                                                                                      │
│ C:\Users\gress\AppData\Local\my-daemon\src\my_daemon\stores\vector.py:67 in search                                   │
│                                                                                                                      │
│   64 │                                                                                                               │
│   65 │   def search(self, vector: list[float], top_k: int = 8) -> list[dict]:                                        │
│   66 │   │   client = self._client_()                                                                                │
│ ❱ 67 │   │   hits = client.search(                                                                                   │
│   68 │   │   │   collection_name=self.collection,                                                                    │
│   69 │   │   │   query_vector=vector,                                                                                │
│   70 │   │   │   limit=top_k,                                                                                        │
╰──────────────────────────────────────────────────────────────────────────────────────────────────────────────────────╯
AttributeError: 'QdrantClient' object has no attribute 'search'