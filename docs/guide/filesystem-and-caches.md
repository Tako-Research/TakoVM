---
description: "How the Tako VM container filesystem is laid out, why library caches are redirected to writable scratch space, and how to load ML models under the read-only root filesystem."
---

# Filesystem & Caches

Every job runs in a container whose **root filesystem is mounted read-only**
(`--read-only`). This is a core security property: untrusted code cannot modify
any binary, library, or config baked into the image. Understanding which paths
*are* writable explains how caching libraries behave and how to load large
models without hitting "read-only filesystem" errors.

## What is writable

| Path | Type | Writable? | Persists between runs? |
|------|------|-----------|------------------------|
| `/` (root, incl. `/home/sandbox`) | image layers | **No** (read-only) | n/a |
| `/tmp` | tmpfs (RAM-backed) | Yes | **No** wiped on exit |
| `/output` | bind mount | Yes | Collected as artifacts |
| `/input`, `/code` | bind mount | No (read-only) | n/a |

Two consequences matter for library behavior:

- The sandbox user's home, `/home/sandbox`, lives on the **read-only** root. Anything
  that tries to write under `$HOME` fails.
- `/tmp` is **RAM-backed and size-capped** (100 MB default, 300 MB while installing
  dependencies) and is **wiped when the container exits**. It is scratch space, not
  storage.

> Today each container is single-use, so nothing written at runtime survives. Durable,
> per-agent workspaces are on the roadmap, see the project direction in the README.

## Library caches (`$HOME/.cache`)

Many libraries cache reusable data under `$HOME/.cache` by the
[XDG convention](https://specifications.freedesktop.org/basedir-spec/latest/),
matplotlib caches a font list, fontconfig caches font metadata, ezdxf caches parsed
resources. On a read-only `$HOME` those writes fail, and you would see warnings like:

```
Cannot create cache home directory: '/home/sandbox/.cache/ezdxf', cache files will not be saved.
```

Tako VM avoids this by pointing the cache/config directories at the writable `/tmp`
tmpfs before running your code (in `docker/entrypoint.sh`):

```sh
export XDG_CACHE_HOME=/tmp/.cache       # ezdxf, fontconfig, most XDG-aware libs
export MPLCONFIGDIR=/tmp/.cache/matplotlib   # matplotlib uses its own variable
```

This is handled automatically, no action needed for these libraries. The cache lives
in `/tmp`, so it is recomputed on the next run (it is an optimization, not stored data).

## Loading ML models (Hugging Face, PyTorch, NLTK, ...)

Caching libraries *tolerate* an unwritable cache, they warn and continue. **Download
and cache** libraries do not: when you call `AutoModel.from_pretrained(...)`, the
library downloads weights and writes them under `$HOME/.cache` (or `HF_HOME`), then
loads them from that path. There is no in-memory fallback, so an unwritable cache is a
hard failure, not a warning.

Redirecting the cache to `/tmp` does **not** reliably fix this, `/tmp` is too small for
a multi-hundred-MB model, is RAM-backed (so it competes with your job's memory limit),
and is wiped every run (so the model re-downloads each time). Network is also disabled
by default (`--network=none`), so a runtime download often cannot happen at all.

The correct approach is to **pre-stage the model so it is present and read-only at
runtime.** Hugging Face and friends are happy to *load* from a read-only cache, they
only fail when they must *write* one. Set offline mode so the library reads the staged
files instead of checking the network:

```sh
export HF_HOME=/opt/hf-cache
export HF_HUB_OFFLINE=1        # also: TRANSFORMERS_OFFLINE=1
```

### Option A, bake the model into the executor image (build time)

Download the model while building the image, where the filesystem is writable, and ship
it inside the image. This is the same build-time pattern as
[Custom Libraries](custom-libraries.md).

```dockerfile
ENV HF_HOME=/opt/hf-cache
# Pin a revision so every run loads byte-identical weights.
RUN python -c "from huggingface_hub import snapshot_download; \
    snapshot_download('org/model', revision='<commit-sha>')"
```

At runtime `/opt/hf-cache` is read-only, which is fine, the weights are already there
and the library only reads them.

- **Pros:** fully self-contained, reproducible, no runtime network.
- **Cons:** large images (weights can be GBs); one image per model set.

### Option B, mount a read-only model volume (runtime)

Populate a volume once and mount it read-only, the same shape Tako VM already uses for
its read-only `/code`/`/input` mounts and the `UV_CACHE_VOLUME` dependency cache.

```
--mount=type=volume,source=hf-models,target=/opt/hf-cache,readonly
```

- **Pros:** images stay small; swap models by pointing `HF_HOME` elsewhere; share across jobs.
- **Cons:** the volume must be populated and managed out of band.

### Determinism

Pin the model `revision` to a commit SHA. A baked, revision-pinned model loads identical
weights on every run, which is stronger than a runtime download that could silently pull
an updated or moved model.

## Summary

| If you use... | Do this |
|---------------|---------|
| matplotlib, ezdxf, fontconfig (cache-for-speed libs) | Nothing, caches are redirected to `/tmp` automatically |
| Hugging Face / torch / NLTK (download-and-cache libs) | Pre-stage the model (bake into image or read-only volume) and set `HF_HOME` + offline mode |
| Anything that must write at runtime | Write under `/tmp` (scratch, ephemeral) or `/output` (collected); never expect `$HOME` to be writable |
