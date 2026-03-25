"""Model registry: budget table, HF download, MLC-LLM compile pipeline."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from dataclasses import dataclass

MODELS_DIR = os.path.join(os.path.dirname(__file__), "chat")
EMBED_DIR = os.path.join(os.path.dirname(__file__), "embedding")


@dataclass
class ModelSpec:
    name: str  # display name
    hf_repo: str  # huggingface repo id
    quantization: str  # e.g. q4f16_1, q0f16
    conv_template: str  # e.g. chatml
    context_window: int
    prefill_chunk: int
    estimated_gb: float  # estimated VRAM usage (model + KV cache)
    kind: str = "chat"  # chat | embedding


# ── Default embedding (always the same, ~1.2 GB) ──

DEFAULT_EMBEDDING = ModelSpec(
    name="Qwen3-Embedding-0.6B",
    hf_repo="Qwen/Qwen3-Embedding-0.6B",
    quantization="q0f16",
    conv_template="chatml",
    context_window=512,
    prefill_chunk=512,
    estimated_gb=1.2,
    kind="embedding",
)

# ── Chat model budget table ──
# Sorted ascending by estimated_gb.
# pick_chat_model() selects the largest model that fits in the budget.

CHAT_MODELS: list[ModelSpec] = [
    ModelSpec(
        name="Qwen3-1.7B",
        hf_repo="Qwen/Qwen3-1.7B",
        quantization="q4f16_1",
        conv_template="chatml",
        context_window=4096,
        prefill_chunk=2048,
        estimated_gb=1.8,
    ),
    ModelSpec(
        name="Qwen3-4B",
        hf_repo="Qwen/Qwen3-4B",
        quantization="q4f16_1",
        conv_template="chatml",
        context_window=4096,
        prefill_chunk=2048,
        estimated_gb=3.5,
    ),
    ModelSpec(
        name="Qwen3-8B",
        hf_repo="Qwen/Qwen3-8B",
        quantization="q4f16_1",
        conv_template="chatml",
        context_window=4096,
        prefill_chunk=2048,
        estimated_gb=6.5,
    ),
    ModelSpec(
        name="Qwen3-14B",
        hf_repo="Qwen/Qwen3-14B",
        quantization="q4f16_1",
        conv_template="chatml",
        context_window=4096,
        prefill_chunk=2048,
        estimated_gb=11.0,
    ),
    ModelSpec(
        name="Qwen3-32B",
        hf_repo="Qwen/Qwen3-32B",
        quantization="q4f16_1",
        conv_template="chatml",
        context_window=4096,
        prefill_chunk=2048,
        estimated_gb=22.0,
    ),
    ModelSpec(
        name="Qwen3-30B-A3B",
        hf_repo="Qwen/Qwen3-30B-A3B",
        quantization="q4f16_1",
        conv_template="chatml",
        context_window=4096,
        prefill_chunk=2048,
        estimated_gb=20.0,
    ),
    ModelSpec(
        name="Qwen3-235B-A22B",
        hf_repo="Qwen/Qwen3-235B-A22B",
        quantization="q4f16_1",
        conv_template="chatml",
        context_window=4096,
        prefill_chunk=2048,
        estimated_gb=140.0,
    ),
]


def pick_chat_model(gpu_gb: float) -> ModelSpec | None:
    """Select the largest chat model that fits within the GPU budget.

    Reserves space for the embedding model (~1.2 GB) + 1 GB headroom.
    """
    available = gpu_gb - DEFAULT_EMBEDDING.estimated_gb - 1.0
    chosen = None
    for m in CHAT_MODELS:
        if m.estimated_gb <= available:
            chosen = m
    return chosen


def compiled_dir(spec: ModelSpec) -> str:
    """Return the expected compiled model directory path."""
    base = MODELS_DIR if spec.kind == "chat" else EMBED_DIR
    dir_name = f"{spec.name}-{spec.quantization}-MLC"
    return os.path.join(base, dir_name)


def dylib_path(spec: ModelSpec) -> str:
    """Return the expected .dylib path for a compiled model."""
    d = compiled_dir(spec)
    return os.path.join(d, f"{spec.name}-{spec.quantization}-MLC.dylib")


def is_compiled(spec: ModelSpec) -> bool:
    """Check if the model is already compiled."""
    return os.path.isfile(dylib_path(spec))


def _check_precompiled_source() -> str | None:
    """Check if a pre-compiled embedding model exists in the project's models/ dir."""
    project_root = os.path.dirname(
        os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
    )
    candidates = [
        os.path.join(project_root, "models", "embedding", "qwen3", "compiled"),
    ]
    for base in candidates:
        if os.path.isdir(base):
            return base
    return None


def compile_model(spec: ModelSpec) -> str:
    """Download from HuggingFace and compile with MLC-LLM.

    Returns the compiled model directory path.
    Deletes HF source weights after successful compilation.
    """
    output = compiled_dir(spec)
    if is_compiled(spec):
        return output

    # Embedding models: try to copy from existing pre-compiled source first.
    # Qwen3-Embedding has a different architecture than decoder models and
    # requires special handling in MLC-LLM's convert_weight.
    if spec.kind == "embedding":
        precompiled = _check_precompiled_source()
        if precompiled:
            dir_name = f"{spec.name}-{spec.quantization}-MLC"
            src = os.path.join(precompiled, dir_name)
            if os.path.isdir(src) and os.path.isfile(
                os.path.join(src, f"{dir_name}.dylib")
            ):
                print(f"  Copying pre-compiled {spec.name} ...")
                if os.path.exists(output):
                    shutil.rmtree(output)
                shutil.copytree(src, output)
                print(f"  Done: {output}")
                return output

    # Step 1: download from HuggingFace
    hf_dir = os.path.join(os.path.dirname(output), f"{spec.name}-hf")
    if not os.path.isdir(hf_dir):
        print(f"  Downloading {spec.hf_repo} ...")
        subprocess.run(
            [
                sys.executable,
                "-c",
                f"from huggingface_hub import snapshot_download; "
                f"snapshot_download('{spec.hf_repo}', local_dir='{hf_dir}')",
            ],
            check=True,
        )

    os.makedirs(output, exist_ok=True)

    # Ensure subprocesses can find mlc_llm (vendored or installed).
    from localmelo.support.serving._mlc_path import subprocess_env

    env = subprocess_env()

    # Step 2: gen_config
    print(f"  Generating config ({spec.quantization}) ...")
    subprocess.run(
        [
            sys.executable,
            "-m",
            "mlc_llm",
            "gen_config",
            hf_dir,
            "--quantization",
            spec.quantization,
            "--conv-template",
            spec.conv_template,
            "--context-window-size",
            str(spec.context_window),
            "--prefill-chunk-size",
            str(spec.prefill_chunk),
            "--output",
            output,
        ],
        check=True,
        env=env,
    )

    # Step 3: convert_weight
    print("  Converting weights ...")
    subprocess.run(
        [
            sys.executable,
            "-m",
            "mlc_llm",
            "convert_weight",
            hf_dir,
            "--quantization",
            spec.quantization,
            "--output",
            output,
        ],
        check=True,
        env=env,
    )

    # Step 4: compile
    device = "metal" if sys.platform == "darwin" else "cuda"
    lib_path = dylib_path(spec)
    print(f"  Compiling for {device} ...")
    subprocess.run(
        [
            sys.executable,
            "-m",
            "mlc_llm",
            "compile",
            output,
            "--device",
            device,
            "--output",
            lib_path,
        ],
        check=True,
        env=env,
    )

    # Step 5: clean up HF source weights
    if os.path.isdir(hf_dir):
        hf_size = sum(
            os.path.getsize(os.path.join(d, f))
            for d, _, files in os.walk(hf_dir)
            for f in files
        )
        shutil.rmtree(hf_dir)
        print(f"  Cleaned up source weights ({hf_size / 1e9:.1f} GB freed)")

    print(f"  Done: {output}")
    return output
