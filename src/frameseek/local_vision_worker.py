"""Private stdin/stdout MLX worker; executed only by the opt-in local runtime.

No HTTP listener or provider credentials. Weights must already exist locally.
This module does not import optional MLX dependencies in the API environment.
"""

import json
import os
import sys
import time
from pathlib import Path


def main():
    os.environ.update(HF_HUB_OFFLINE="1", HF_HUB_DISABLE_IMPLICIT_TOKEN="1")
    import mlx.core as mx
    from mlx_vlm import generate, load
    from mlx_vlm.prompt_utils import apply_chat_template
    from mlx_vlm.structured import build_json_schema_logits_processor
    from mlx_vlm.utils import load_config

    path = Path(sys.argv[1]).resolve(strict=True)
    model, processor = load(str(path), trust_remote_code=False)
    config = load_config(str(path))
    print("FSVISION " + json.dumps({"ready": True}), flush=True)
    for line in sys.stdin:
        try:
            request = json.loads(line)
            images = request.get("images", [])
            if any(not Path(p).is_absolute() or not Path(p).is_file() for p in images):
                raise ValueError("local_image_missing")
            prompt = apply_chat_template(
                processor, config, request["prompt"], num_images=len(images)
            )
            start = time.monotonic()
            processors = (
                [build_json_schema_logits_processor(processor.tokenizer, request["schema"])]
                if request.get("schema")
                else None
            )
            result = generate(
                model,
                processor,
                prompt,
                images or None,
                max_tokens=request.get("max_tokens", 512),
                temperature=0.0,
                logits_processors=processors,
                enable_thinking=False,
                verbose=False,
            )
            output = {"text": result.text, "elapsed": time.monotonic() - start}
        except Exception as error:
            output = {"error": type(error).__name__, "message": str(error)[:500]}
        print("FSVISION " + json.dumps(output, ensure_ascii=False), flush=True)
        mx.clear_cache()


if __name__ == "__main__":
    main()
