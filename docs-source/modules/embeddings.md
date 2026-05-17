# `embeddings/` — Dense and sparse encoders

Source: `src/my_daemon/embeddings/`.

Two thin wrappers around the heavy-weight ML libraries the daemon depends on.
Both prefer offline loads from `embeddings.cache_folder` so queries don't
contact the Hugging Face Hub at runtime.

## `Embedder` — dense (sentence-transformers)

`my_daemon.embeddings.Embedder` wraps a `SentenceTransformer` with:

- **Lazy load.** The model isn't loaded until the first `encode()` or
  `dimension` access. This is what makes `daemon status` cheap and the first
  query slow.
- **Cache pinning.** `cache_folder` (default `./data/models/`) is passed to
  sentence-transformers. The loader tries `local_files_only=True` first;
  only on cache miss does it fall back to a one-time download.
- **Normalized output.** All vectors are L2-normalized at encode time so the
  Qdrant `Distance.COSINE` distance is a plain dot product.
- **Batched.** `batch_size` (default 32) is applied to multi-text encode calls.

### Public surface

```python
Embedder(model_name, batch_size=32, device="auto", cache_folder=None)
  .dimension                # property; loads model if needed, returns int
  .encode(texts: list[str]) -> list[list[float]]
  .encode_one(text: str)    -> list[float]
  .download()               -> Path   # force-pull into cache
```

### Default model

`BAAI/bge-small-en-v1.5` — 384-dim, English, strong quality, fast on CPU.

## `SparseEmbedder` — BM25-style (fastembed)

`my_daemon.embeddings.SparseEmbedder` wraps `fastembed.SparseTextEmbedding`.
Default model: `Qdrant/bm42-all-minilm-l6-v2-attentions` — BM42, an
attention-weighted variant of BM25 that ships with Qdrant's fastembed
library.

Same offline-first cache pattern as the dense embedder (toggles
`HF_HUB_OFFLINE` for the duration of construction so a cache miss can't
accidentally pull at runtime).

### Public surface

```python
SparseEmbedder(model_name, cache_folder=None)
  .encode(texts) -> list[tuple[list[int], list[float]]]   # (indices, values)
  .encode_one(text) -> tuple[list[int], list[float]]
  .download() -> Path
```

The `(indices, values)` tuple gets wrapped in a Qdrant `SparseVector` at
upsert / search time inside `stores/vector.py`.

## When `SparseEmbedder` is built

The CLI builds the sparse embedder only when `embeddings.hybrid: true`. The
ingest pipeline, query pipeline, and chat UI all propagate the optional
sparse embedder explicitly — passing `None` falls back to dense-only behavior
at every layer.

## Why offline-first

The vault is the user's life. The daemon should still work on a plane. After
the first run that populates `./data/models/`, the embedder never opens a
socket.

## `daemon models download`

Forces both models into the cache without doing a full ingest. Useful before
the first flight, or to verify that the cache directory has the right
permissions.
