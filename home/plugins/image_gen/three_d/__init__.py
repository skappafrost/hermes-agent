#!/usr/bin/env python3
"""3D model generation from 2D images using Hugging Face Spaces.

This tool converts 2D images into 3D models (.glb, .obj) using free
public Hugging Face Spaces. No API keys or payment required.
"""

import json
import os
import shutil
import tempfile
import time
from typing import Any
from gradio_client import Client


def _check_space_available() -> bool:
    """Check if a 3D generation space is reachable."""
    try:
        Client("stabilityai/stable-fast-3d", verbose=False)
        return True
    except Exception:
        return False


def _get_client():
    """Get or create a cached Gradio client."""
    global _client  # type: ignore[name-defined]
    if _client is None:  # type: ignore[name-defined]
        try:
            from gradio_client import Client
        except ImportError as exc:
            raise ImportError(
                "gradio_client is required for 3D generation. "
                "Install it: pip install gradio_client"
            ) from exc
        _client = Client("stabilityai/stable-fast-3d", verbose=False)  # type: ignore[name-defined]
    return _client  # type: ignore[name-defined]


def generate_3d_model_handler(args: dict, **kwargs: Any) -> str:
    """Generate a 3D model from an input image via Stable Fast 3D.

    Args (from args dict):
        image_path: Required. Path to a JPG/PNG image file.
        output_format: "glb", "obj", or "stl". Default "glb".
        foreground_ratio: 0.5-1.0. Default 0.85.
        remesh_option: "None", "Triangle", "Quad". Default "None".
        vertex_count: -1 for auto, 0-20000. Default -1.
        texture_size: 512-2048. Default 1024.
        max_retries: Number of retry attempts. Default 3.

    Returns:
        JSON string with success, output_path, error, and metadata.
    """
    start_time = time.time()

    image_path = args.get("image_path", "")
    output_format = args.get("output_format", "glb")
    max_retries = int(args.get("max_retries", 3))
    foreground_ratio = float(args.get("foreground_ratio", 0.85))
    remesh_option = args.get("remesh_option", "None")
    vertex_count = int(args.get("vertex_count", -1))
    texture_size = int(args.get("texture_size", 1024))

    # Validate inputs
    errors = []
    if not image_path:
        errors.append("image_path is required")
    elif not os.path.isfile(image_path):
        errors.append(f"Input image not found: {image_path}")
    if output_format not in ("glb", "obj", "stl"):
        errors.append(f"Unsupported output_format: {output_format}. Use glb, obj, or stl.")
    if not 0.5 <= foreground_ratio <= 1.0:
        errors.append(f"foreground_ratio must be 0.5-1.0, got {foreground_ratio}")
    if remesh_option not in ("None", "Triangle", "Quad"):
        errors.append(f"remesh_option must be None/Triangle/Quad, got {remesh_option}")
    if vertex_count != -1 and not 0 <= vertex_count <= 20000:
        errors.append(f"vertex_count must be -1 or 0-20000, got {vertex_count}")
    if not 512 <= texture_size <= 2048:
        errors.append(f"texture_size must be 512-2048, got {texture_size}")
    if max_retries < 1:
        errors.append(f"max_retries must be >=1, got {max_retries}")

    if errors:
        return json.dumps({
            "success": False, "error": "; ".join(errors), "output_path": None,
            "metadata": {"timestamp": start_time, "errors": errors},
        })

    last_error = None
    for attempt in range(max_retries):
        try:
            client = _get_client()
            result = client.predict(
                image_path, foreground_ratio, remesh_option, vertex_count, texture_size,
                api_name="/run_button",
            )

            if isinstance(result, (list, tuple)) and len(result) >= 2:
                model_path = result[1]
            else:
                model_path = result

            if not model_path or not os.path.exists(str(model_path)):
                raise ValueError(f"Invalid model path returned: {model_path}")

            timestamp = int(time.time())
            output_filename = f"hermes_3d_model_{timestamp}.{output_format}"
            output_path = os.path.join(tempfile.gettempdir(), output_filename)
            shutil.copy2(str(model_path), output_path)

            if not os.path.exists(output_path):
                raise ValueError("Failed to copy generated model")

            return json.dumps({
                "success": True, "output_path": output_path, "error": None,
                "metadata": {
                    "timestamp": start_time,
                    "space_used": "stabilityai/stable-fast-3d",
                    "space_name": "Stable Fast 3D",
                    "generation_time_seconds": round(time.time() - start_time, 2),
                    "output_format": output_format,
                    "parameters": {
                        "image_path": image_path, "foreground_ratio": foreground_ratio,
                        "remesh_option": remesh_option, "vertex_count": vertex_count,
                        "texture_size": texture_size,
                    },
                    "attempt": attempt + 1,
                },
            })

        except Exception as e:
            last_error = str(e)
            if attempt < max_retries - 1:
                time.sleep(2 ** attempt)
                continue
            break

    return json.dumps({
        "success": False,
        "error": f"Generation failed after {max_retries} attempts: {last_error}",
        "output_path": None,
        "metadata": {
            "timestamp": start_time, "space_used": "stabilityai/stable-fast-3d",
            "generation_time_seconds": round(time.time() - start_time, 2),
            "attempts": max_retries, "last_error": last_error,
        },
    })


def register(ctx) -> None:
    """Register the 3D generation tool with Hermes."""
    ctx.register_tool(
        name="generate_3d_model",
        toolset="image_gen",
        schema={
            "name": "generate_3d_model",
            "description": (
                "Generate a 3D model (.glb) from an input image using "
                "Stability AI's Stable Fast 3D on Hugging Face Spaces. "
                "Free, no API key required."
            ),
            "parameters": {
                "type": "dict",
                "properties": {
                    "image_path": {
                        "type": "string",
                        "description": "Path to input image file (required). JPG, PNG, etc.",
                    },
                    "output_format": {
                        "type": "string", "enum": ["glb", "obj", "stl"], "default": "glb",
                        "description": "Output 3D file format. Default: glb.",
                    },
                    "foreground_ratio": {
                        "type": "number", "minimum": 0.5, "maximum": 1.0, "default": 0.85,
                        "description": "Object framing ratio (0.5-1.0). Default: 0.85.",
                    },
                    "remesh_option": {
                        "type": "string", "enum": ["None", "Triangle", "Quad"], "default": "None",
                        "description": "Mesh topology option. Default: None.",
                    },
                    "vertex_count": {
                        "type": "integer", "minimum": -1, "maximum": 20000, "default": -1,
                        "description": "Target vertices (-1=auto). Default: -1.",
                    },
                    "texture_size": {
                        "type": "integer", "minimum": 512, "maximum": 2048, "default": 1024,
                        "description": "Texture resolution. Default: 1024.",
                    },
                    "max_retries": {
                        "type": "integer", "minimum": 1, "default": 3,
                        "description": "Retry attempts on failure. Default: 3.",
                    },
                },
                "required": ["image_path"],
            },
        },
        handler=generate_3d_model_handler,
        check_fn=_check_space_available,
        requires_env=[],
    )
