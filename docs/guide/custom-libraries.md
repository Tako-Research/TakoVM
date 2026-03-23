# Custom Libraries

Tako VM supports pre-installing custom Python libraries into executor images. This is useful for:

- Internal/proprietary packages not on PyPI
- Modified versions of public packages
- Packages that require complex build dependencies

## How It Works

Custom libraries are installed at **image build time**, not runtime. This means:

1. Libraries are baked into the Docker image
2. No network access needed at execution time
3. Zero installation overhead per job

## Adding Custom Libraries

### Step 1: Build a Wheel

Build your library as a wheel (`.whl`) file:

```bash
cd your-library/
python -m build --wheel
# or
python setup.py bdist_wheel
```

### Step 2: Add to custom_libs

Copy the wheel to `tako_vm/custom_libs/`:

```bash
cp dist/your_library-1.0.0-py3-none-any.whl /path/to/tako-vm/tako_vm/custom_libs/
```

### Step 3: Rebuild the Executor Image

For the base executor image:

```bash
docker build -t code-executor:latest -f docker/Dockerfile.executor .
```

For job-type-specific images, use the REST API:

```bash
curl -X POST http://localhost:8000/job-types/data-processing/build
```

## Directory Structure

```
tako_vm/
└── custom_libs/
    ├── README.md
    ├── your_library-1.0.0-py3-none-any.whl
    └── another_lib-2.0.0-py3-none-any.whl
```

## Using Custom Libraries

Once installed, import them like any other package:

```python
from your_library import MyClass

result = MyClass().process(data)
```

## Notes

- All `.whl` files in `custom_libs/` are installed automatically
- Dependencies of your library must either be included as wheels or available via PyPI
- To update a library, replace the wheel and rebuild the image
- Libraries are available to all code executions using that image
