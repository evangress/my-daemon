## 2026-05-16 01:56: PM
UserWarning: Qdrant client version 1.18.0 is incompatible with server version 1.11.0. Major versions should match and minor version difference must not exceed 1. Set check_compatibility=False to skip version check.
  show_warning(
╭───────────────────────────────────────── Traceback (most recent call last) ──────────────────────────────────────────╮
│ C:\Users\gress\AppData\Local\my-daemon\src\my_daemon\cli.py:149 in query                                             │
│                                                                                                                      │
│   146 │   llm = LLMClient(s.llm, api_key=s.anthropic_api_key)                                                        │
│   147 │                                                                                                              │
│   148 │   engine = QueryEngine(s, embedder, vector_store, graph_store, feedback_store, llm)                          │
│ ❱ 149 │   response = engine.ask(text, synthesize=not no_synthesize)                                                  │
│   150 │                                                                                                              │
│   151 │   if response.answer:                                                                                        │
│   152 │   │   console.print(Panel(response.answer, title="Daemon", border_style="cyan"))                             │
│                                                                                                                      │
│ C:\Users\gress\AppData\Local\my-daemon\src\my_daemon\pipeline\query.py:43 in ask                                     │
│                                                                                                                      │
│   40 │   def ask(self, query: str, synthesize: bool = True) -> QueryResponse:                                        │
│   41 │   │   t0 = time.perf_counter()                                                                                │
│   42 │   │   result = self.orchestrator.retrieve(query)                                                              │
│ ❱ 43 │   │   answer = self.llm.synthesize(query, result.ranked) if synthesize else ""                                │
│   44 │   │   latency_ms = int((time.perf_counter() - t0) * 1000)                                                     │
│   45 │   │                                                                                                           │
│   46 │   │   summary = {                                                                                             │
│                                                                                                                      │
│ C:\Users\gress\AppData\Local\my-daemon\src\my_daemon\llm\client.py:33 in synthesize                                  │
│                                                                                                                      │
│   30 │                                                                                                               │
│   31 │   def synthesize(self, query: str, chunks: list[RetrievedChunk]) -> str:                                      │
│   32 │   │   client = self._client_()                                                                                │
│ ❱ 33 │   │   message = client.messages.create(                                                                       │
│   34 │   │   │   model=self.config.model,                                                                            │
│   35 │   │   │   max_tokens=self.config.max_tokens,                                                                  │
│   36 │   │   │   temperature=self.config.temperature,                                                                │
│                                                                                                                      │
│ C:\Users\gress\AppData\Local\my-daemon\.venv\Lib\site-packages\anthropic\_utils\_utils.py:294 in wrapper             │
│                                                                                                                      │
│   291 │   │   │   │   │   else:                                                                                      │
│   292 │   │   │   │   │   │   msg = f"Missing required argument: {quote(missing[0])}"                                │
│   293 │   │   │   │   raise TypeError(msg)                                                                           │
│ ❱ 294 │   │   │   return func(*args, **kwargs)                                                                       │
│   295 │   │                                                                                                          │
│   296 │   │   return wrapper  # type: ignore                                                                         │
│   297                                                                                                                │
│                                                                                                                      │
│ C:\Users\gress\AppData\Local\my-daemon\.venv\Lib\site-packages\anthropic\resources\messages\messages.py:1003 in      │
│ create                                                                                                               │
│                                                                                                                      │
│   1000 │   │   │   │   stacklevel=3,                                                                                 │
│   1001 │   │   │   )                                                                                                 │
│   1002 │   │                                                                                                         │
│ ❱ 1003 │   │   return self._post(                                                                                    │
│   1004 │   │   │   "/v1/messages",                                                                                   │
│   1005 │   │   │   body=maybe_transform(                                                                             │
│   1006 │   │   │   │   {                                                                                             │
│                                                                                                                      │
│ C:\Users\gress\AppData\Local\my-daemon\.venv\Lib\site-packages\anthropic\_base_client.py:1374 in post                │
│                                                                                                                      │
│   1371 │   │   opts = FinalRequestOptions.construct(                                                                 │
│   1372 │   │   │   method="post", url=path, json_data=body, content=content, files=to_httpx_files(files), **options  │
│   1373 │   │   )                                                                                                     │
│ ❱ 1374 │   │   return cast(ResponseT, self.request(cast_to, opts, stream=stream, stream_cls=stream_cls))             │
│   1375 │                                                                                                             │
│   1376 │   def patch(                                                                                                │
│   1377 │   │   self,                                                                                                 │
│                                                                                                                      │
│ C:\Users\gress\AppData\Local\my-daemon\.venv\Lib\site-packages\anthropic\_base_client.py:1147 in request             │
│                                                                                                                      │
│   1144 │   │   │   │   │   err.response.read()                                                                       │
│   1145 │   │   │   │                                                                                                 │
│   1146 │   │   │   │   log.debug("Re-raising status error")                                                          │
│ ❱ 1147 │   │   │   │   raise self._make_status_error_from_response(err.response) from None                           │
│   1148 │   │   │                                                                                                     │
│   1149 │   │   │   break                                                                                             │
│   1150                                                                                                               │
╰──────────────────────────────────────────────────────────────────────────────────────────────────────────────────────╯
BadRequestError: Error code: 400 - {'type': 'error', 'error': {'type': 'invalid_request_error', 'message':
'`temperature` is deprecated for this model.'}, 'request_id': 'req_011Cb6gJosaPvUMNNTewVzDF'}