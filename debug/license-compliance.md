# License Compliance Report

- **Generated:** 2026-07-29T00:37:44+00:00
- **Project license:** `Apache-2.0`
- **Counts:** 148 compatible · 0 incompatible · 0 unknown · 17 excepted · 165 total
- **Exit code:** `0`

## Excepted (17)

These dependencies **are** incompatible with `Apache-2.0` and are accepted anyway, each for the reason given below. Listed rather than hidden: an exception nobody can see is one nobody can challenge, and a report that said "no action required" over them would be a clean bill of health that is not true. Entries live in `_ACCEPTED_EXCEPTIONS` in `scripts/license_check.py`, so adding one is visible in review.

| Package | Version | Declared License | Reason |
|---|---|---|---|
| [`cuda-bindings`](https://pypi.org/project/cuda-bindings/) | 13.2.0 | LicenseRef-NVIDIA-SOFTWARE-LICENSE | NVIDIA proprietary CUDA runtime; transitive via torch, not redistributed with this project's source |
| [`cuda-toolkit`](https://pypi.org/project/cuda-toolkit/) | 13.0.2 | UNKNOWN | NVIDIA proprietary CUDA runtime; transitive via torch, not redistributed with this project's source |
| [`nvidia-cublas`](https://pypi.org/project/nvidia-cublas/) | 13.1.1.3 | LicenseRef-NVIDIA-Proprietary | NVIDIA proprietary CUDA runtime; transitive via torch, not redistributed with this project's source |
| [`nvidia-cuda-cupti`](https://pypi.org/project/nvidia-cuda-cupti/) | 13.0.85 | Other/Proprietary License | NVIDIA proprietary CUDA runtime; transitive via torch, not redistributed with this project's source |
| [`nvidia-cuda-nvrtc`](https://pypi.org/project/nvidia-cuda-nvrtc/) | 13.0.88 | Other/Proprietary License | NVIDIA proprietary CUDA runtime; transitive via torch, not redistributed with this project's source |
| [`nvidia-cuda-runtime`](https://pypi.org/project/nvidia-cuda-runtime/) | 13.0.96 | LicenseRef-NVIDIA-Proprietary | NVIDIA proprietary CUDA runtime; transitive via torch, not redistributed with this project's source |
| [`nvidia-cudnn-cu13`](https://pypi.org/project/nvidia-cudnn-cu13/) | 9.20.0.48 | LicenseRef-NVIDIA-Proprietary | NVIDIA proprietary CUDA runtime; transitive via torch, not redistributed with this project's source |
| [`nvidia-cufft`](https://pypi.org/project/nvidia-cufft/) | 12.0.0.61 | Other/Proprietary License | NVIDIA proprietary CUDA runtime; transitive via torch, not redistributed with this project's source |
| [`nvidia-cufile`](https://pypi.org/project/nvidia-cufile/) | 1.15.1.6 | Other/Proprietary License | NVIDIA proprietary CUDA runtime; transitive via torch, not redistributed with this project's source |
| [`nvidia-curand`](https://pypi.org/project/nvidia-curand/) | 10.4.0.35 | Other/Proprietary License | NVIDIA proprietary CUDA runtime; transitive via torch, not redistributed with this project's source |
| [`nvidia-cusolver`](https://pypi.org/project/nvidia-cusolver/) | 12.0.4.66 | Other/Proprietary License | NVIDIA proprietary CUDA runtime; transitive via torch, not redistributed with this project's source |
| [`nvidia-cusparse`](https://pypi.org/project/nvidia-cusparse/) | 12.6.3.3 | Other/Proprietary License | NVIDIA proprietary CUDA runtime; transitive via torch, not redistributed with this project's source |
| [`nvidia-cusparselt-cu13`](https://pypi.org/project/nvidia-cusparselt-cu13/) | 0.8.1 | NVIDIA Proprietary Software | NVIDIA proprietary CUDA runtime; transitive via torch, not redistributed with this project's source |
| [`nvidia-nccl-cu13`](https://pypi.org/project/nvidia-nccl-cu13/) | 2.29.7 | LicenseRef-NVIDIA-Proprietary | NVIDIA proprietary CUDA runtime; transitive via torch, not redistributed with this project's source |
| [`nvidia-nvjitlink`](https://pypi.org/project/nvidia-nvjitlink/) | 13.0.88 | Other/Proprietary License | NVIDIA proprietary CUDA runtime; transitive via torch, not redistributed with this project's source |
| [`nvidia-nvshmem-cu13`](https://pypi.org/project/nvidia-nvshmem-cu13/) | 3.4.5 | LicenseRef-NVIDIA-Proprietary | NVIDIA proprietary CUDA runtime; transitive via torch, not redistributed with this project's source |
| [`nvidia-nvtx`](https://pypi.org/project/nvidia-nvtx/) | 13.0.85 | Other/Proprietary License | NVIDIA proprietary CUDA runtime; transitive via torch, not redistributed with this project's source |

## Model weights

`scripts/license_check.py` inspects Python package distributions on PyPI; it has
no notion of a model artifact and cannot see Hugging Face Hub card metadata.
Every model weight this project downloads is therefore verified **by hand**,
not by the automated CI gate. As of this entry the project ships three model
artifacts, none of which the gate covers:

| Model | Role | Licence | Source |
|---|---|---|---|
| `BAAI/bge-small-en-v1.5` | dense embedder | MIT | HF Hub card metadata, checked 2026-07-30 |
| `Qdrant/bm42-all-minilm-l6-v2-attentions` (fastembed's alias for HF repo `Qdrant/all_miniLM_L6_v2_with_attentions`) | BM42 sparse embedder | Apache-2.0 | HF Hub card metadata, checked 2026-07-30 |
| `cross-encoder/ms-marco-MiniLM-L-6-v2` | cross-encoder reranker (default, IV.18 Task 14) | Apache-2.0 (`license:apache-2.0` tag) | HF Hub card metadata, checked 2026-07-30 |
| `BAAI/bge-reranker-base` | cross-encoder reranker (documented fallback) | MIT | HF Hub card metadata, checked 2026-07-30 |

Both reranker candidates were checked against `huggingface_hub.model_info(...).card_data`
on 2026-07-30: `ms-marco-MiniLM-L-6-v2` reports `license: apache-2.0`
(confirmed live via `model_info`, tag `license:apache-2.0` present), and
`bge-reranker-base` reports `license: mit`. Both are Apache-2.0 compatible;
`ms-marco-MiniLM-L-6-v2` is used as the spec's default, with `bge-reranker-base`
kept as the documented fallback. Extending `scripts/license_check.py` to cover
model artifacts is a deliberate future item, not part of this task.
