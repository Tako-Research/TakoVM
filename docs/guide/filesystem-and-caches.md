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
| `/` (root, incl. most of `/home/sandbox`) | image layers | **No** (read-only) | n/a |
| `/home/sandbox/.cache` | tmpfs (RAM-backed) | Yes | **No** wiped on exit |
| `/tmp` | tmpfs (RAM-backed) | Yes | **No** wiped on exit |
| `/output` | bind mount | Yes | Collected as artifacts |
| `/input`, `/code` | bind mount | No (read-only) | n/a |

Two consequences matter for library behavior:

- The sandbox user's home, `/home/sandbox`, is on the **read-only** root, **except**
  `~/.cache`, which is a writable tmpfs mount (see below). Writes elsewhere under
  `$HOME` (e.g. `~/.config`, `~/.local`) fail.
- The writable tmpfs areas (`~/.cache`, `/tmp`) are **RAM-backed, size-capped, and
  wiped when the container exits**. `/tmp` defaults to 100 MB (300 MB while installing
  dependencies); `~/.cache` defaults to 256 MB (`container_limits.cache_tmpfs_size`).
  They are scratch space, not storage.

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

Tako VM avoids this by mounting a writable tmpfs **over `~/.cache` itself**, so the
conventional path works without any redirection:

```
--tmpfs=/home/sandbox/.cache:rw,nosuid,mode=1777,size=<cache_tmpfs_size>
```

`mode=1777` lets the unprivileged sandbox user write to it (the container drops
`CAP_CHOWN`, so a `chown` would not work). Because the cache is at its standard
location, *any* library that writes under `~/.cache` works, including ones that ignore
`XDG_CACHE_HOME`. The only knob you may want is the size:

```yaml
container_limits:
  cache_tmpfs_size: "256m"   # default; RAM-backed, counts against memory_limit
```

This is handled automatically, no action needed for these libraries. The cache lives in
the tmpfs, so it is recomputed on the next run (it is an optimization, not stored data).
matplotlib also keeps a *config* dir outside `~/.cache`; the entrypoint points
`MPLCONFIGDIR` into the writable cache so matplotlib does not warn.

## Loading ML models (Hugging Face, PyTorch, NLTK, ...)

Caching libraries *tolerate* an unwritable cache, they warn and continue. **Download
and cache** libraries do not: when you call `AutoModel.from_pretrained(...)`, the
library downloads weights and writes them under `$HOME/.cache` (or `HF_HOME`), then
loads them from that path. There is no in-memory fallback, so an unwritable cache is a
hard failure, not a warning.

The writable `~/.cache` tmpfs does **not** reliably fix this: even sized up, it is
RAM-backed (so a multi-hundred-MB model competes with your job's memory limit) and is
wiped every run (so the model re-downloads each time). Network is also disabled by
default (`--network=none`), so a runtime download often cannot happen at all.

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
| matplotlib, ezdxf, fontconfig (cache-for-speed libs) | Nothing, `~/.cache` is a writable tmpfs automatically |
| Hugging Face / torch / NLTK (download-and-cache libs) | Pre-stage the model (bake into image or read-only volume) and set `HF_HOME` + offline mode |
| Anything that must write at runtime | Write under `/tmp` (scratch, ephemeral) or `/output` (collected); never expect `$HOME` to be writable |
